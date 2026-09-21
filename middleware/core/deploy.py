"""Turn resolved streams into a per-device configuration plan, or refuse.

The platform's one hard rule lives here: a QoS class promises something, and a
device that cannot keep the promise is a refusal at planning time rather than a
downgrade at runtime. A SAFETY_CRITICAL stream on a bridge without 802.1CB does
not become a SAFETY_CRITICAL stream with one path; it does not get deployed.

`null` in hardware/capabilities.yaml means "not yet measured" and is treated as
a refusal too, with a different message. The IVN semiconductors under
development are all null today, and a plan that quietly assumed they would
support everything would be the most expensive kind of wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CAPS_PATH = Path(__file__).resolve().parents[2] / "devices" / "device-classes.yaml"


@dataclass
class Refusal:
    stream: str
    device: str
    requirement: str
    capability: str
    reason: str

    def __str__(self) -> str:
        return f"{self.stream} on {self.device}: {self.reason}"


@dataclass
class Waiver:
    """A knowingly accepted deviation, with the evidence that it is acceptable.

    Refusal by default is right, but a bench where nothing can be demonstrated
    is not useful either. A waiver lets an engineer say "yes, this TSN 브리지 (FRER 없음) has
    no FRER, run the CONTROL stream single-path anyway, here is why that is
    safe for this test" -- and leaves that sentence attached to the plan, so it
    shows up in the report rather than living in someone's memory.

    A waiver degrades the stream visibly: the resolved class is rewritten to
    what the hardware can really do, so downstream emitters and the conformance
    tests both see the honest configuration.
    """
    stream: str
    device: str
    capability: str
    rationale: str
    approved_by: str = ""

    def matches(self, refusal: "Refusal") -> bool:
        return ((self.stream in ("*", refusal.stream))
                and (self.device in ("*", refusal.device))
                and self.capability == refusal.capability)


@dataclass
class DeploymentPlan:
    device: str
    streams: list[Any] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    waived: list[tuple[Refusal, "Waiver"]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def degraded(self) -> bool:
        """Deployable, but not delivering what the QoS class promises."""
        return bool(self.waived)

    def report(self) -> str:
        lines = [f"device {self.device}: {len(self.streams)} deployed, "
                 f"{len(self.refusals)} refused, {len(self.waived)} waived"]
        for r in self.refusals:
            lines.append(f"  REFUSED  {r}")
        for r, w in self.waived:
            lines.append(f"  WAIVED   {r.stream}: no {r.capability} on {r.device} "
                         f"-- {w.rationale}" + (f" [{w.approved_by}]" if w.approved_by else ""))
        return "\n".join(lines)


class CapabilityGate:
    def __init__(self, path: str | Path | None = None):
        with open(path or CAPS_PATH) as f:
            self.doc = yaml.safe_load(f)
        self.devices = self.doc["classes"]          # capability classes, not vendors
        self.requirement_map = self.doc["requirement_map"]
        self.site: dict[str, str] = {}              # device name -> class name

    def load_site(self, path: str | Path) -> "CapabilityGate":
        """Attach real hardware to capability classes.

        Vendor names live here and nowhere else in the platform. A site file is
        optional and need not be published with the repository.
        """
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
        for name, spec in (doc.get("devices") or {}).items():
            cls = spec.get("class") if isinstance(spec, dict) else spec
            if cls not in self.devices:
                raise KeyError(f"{name}: unknown capability class {cls!r}; "
                               f"known: {', '.join(sorted(self.devices))}")
            self.site[name] = cls
        return self

    def _class_of(self, device: str) -> str:
        """A name is either a capability class or a site device mapped to one."""
        if device in self.devices:
            return device
        if device in self.site:
            return self.site[device]
        known = sorted(set(self.devices) | set(self.site))
        raise KeyError(f"unknown device or class {device!r}; known: {', '.join(known)}")

    def requirements_of(self, stream) -> list[str]:
        """What this stream needs from a bridge, in capability terms."""
        need = []
        if stream.tsn["redundancy"] != "none":
            need.append(stream.tsn["redundancy"])
        if stream.tsn["shaper"] in ("tas", "cbs"):
            need.append(stream.tsn["shaper"])
        if stream.tsn["preemption"] == "express":
            need.append("express")
        return need

    @staticmethod
    def _degrade(stream, capability: str) -> None:
        """Rewrite the resolved stream to what the hardware will really do, so
        every emitter downstream and every test sees the same truth."""
        if capability == "frer":
            stream.tsn["redundancy"] = "none"
            stream.tsn.pop("frer", None)
        elif capability == "preemption":
            stream.tsn["preemption"] = "not_supported"
        elif capability in ("tas", "cbs"):
            stream.tsn["shaper"] = "strict_priority"
            stream.tsn.pop("cbs", None)
            stream.tsn.pop("tas_bytes_per_period", None)
        stream.tsn.setdefault("degraded", []).append(capability)

    def check(self, stream, device: str) -> list[Refusal]:
        cls_name = self._class_of(device)
        caps = self.devices[cls_name]["capabilities"]
        out = []
        for req in self.requirements_of(stream):
            cap = self.requirement_map.get(req, req)
            have = caps.get(cap)
            if have is True:
                continue
            if have is None:
                out.append(Refusal(stream.name, device, req, cap,
                                   f"{device} 의 {cap} 지원 여부가 아직 미확정이다. "
                                   f"실측 전까지 {stream.request.qos_class} 를 여기 배치할 수 없다"))
            else:
                ev = (self.devices[cls_name].get("evidence") or {}).get(cap, "")
                out.append(Refusal(stream.name, device, req, cap,
                                   f"{stream.request.qos_class} 는 {req} 를 요구하는데 "
                                   f"{device} 에 {cap} 가 없다"
                                   + (f" — {ev.strip().splitlines()[0]}" if ev else "")))
        return out

    def plan(self, streams: list[Any], device: str, port: str = "1",
             port_mbps: float | None = None, registry=None,
             waivers: list[Waiver] | None = None) -> DeploymentPlan:
        """A full per-device plan: refusals first, then the config for what remains.

        A stream whose every refusal is covered by a waiver is deployed, with
        the unsupported feature actually removed from its configuration -- not
        left in place to be ignored by the hardware.
        """
        waivers = waivers or []
        plan = DeploymentPlan(device=device)
        for s in streams:
            refusals = self.check(s, device)
            if not refusals:
                plan.streams.append(s)
                continue
            covered = [(r, next((w for w in waivers if w.matches(r)), None)) for r in refusals]
            if any(w is None for _, w in covered):
                plan.refusals.extend(r for r, w in covered if w is None)
                continue
            for r, w in covered:
                plan.waived.append((r, w))
                self._degrade(s, r.capability)
            plan.streams.append(s)
        if not plan.streams:
            return plan

        # Imports are local so this module stays usable without the emitters
        # (a caller that only wants the refusal verdict should not need them).
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from network.tsn import emit_yang
        from network.frer import emit_frer

        dev = self.devices[self._class_of(device)]
        if port_mbps is None:
            port_mbps = 1000.0
        if registry is not None:
            gcl = registry.build_gate_control_list(plan.streams, port_mbps)
            plan.config.update(emit_yang.gate_parameter_table(port, gcl))
            plan.config["_gcl"] = gcl
        plan.config.update(emit_yang.cbsa(port, plan.streams))
        plan.config.update(emit_yang.queue_max_sdu(port, plan.streams))
        if dev["capabilities"].get("preemption") is True:
            plan.config.update(emit_yang.preemption(port, plan.streams))
        idx = 0
        for s in plan.streams:
            if s.tsn["redundancy"] == "none":
                continue
            plan.config.update(emit_frer.stream_identity(s, idx, [port], [port]))
            plan.config.update(emit_frer.sequence_generation(s, idx))
            plan.config.update(emit_frer.sequence_recovery(s, idx, [port]))
            plan.config.update(emit_frer.stream_split(s, idx, [port]))
            plan.config.update(emit_frer.sequence_identification(s, idx, [port]))
            idx += 1
        return plan
