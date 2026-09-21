"""Network Service API — the logic, with no web framework in it.

Kept free of FastAPI (or any server) on purpose: the admission rules are the
part that has to be testable, reviewable and portable to another language for
the standard's second implementation. api/server.py is a thin adapter that
turns these methods into HTTP.

The one behaviour worth stating up front: a refusal is a verdict, not an error.
A client that gets one should change the class or the link, not retry. Retrying
a policy decision is how a system ends up with a stream nobody admitted.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "middleware")):
    if p not in sys.path:
        sys.path.insert(0, p)

from qos.mapper import QosRegistry, StreamRequest, AdmissionError      # noqa: E402
from core.deploy import CapabilityGate, Waiver                          # noqa: E402


class Refused(Exception):
    """Carries the structured refusal, so the HTTP layer can render 409."""

    def __init__(self, detail: dict[str, Any]):
        super().__init__(detail.get("reason", "refused"))
        self.detail = detail


def stream_json(s) -> dict[str, Any]:
    return {
        "name": s.name,
        "qos_class": s.request.qos_class,
        "link": s.request.link,
        "period_ms": round(s.request.period_ms, 4),
        "payload_bytes": s.request.payload_bytes,
        "rate_mbps": round(s.rate_mbps, 4),
        "frames_per_sample": s.frames_per_sample,
        "wire_bytes_per_sample": s.wire_bytes_per_sample,
        "vlan": s.vlan,
        "pcp": s.pcp,
        "dds": s.dds,
        "tsn": s.tsn,
    }


class NetworkService:
    def __init__(self, registry: QosRegistry | None = None,
                 gate: CapabilityGate | None = None,
                 site: str | Path | None = None):
        self.registry = registry or QosRegistry()
        self.gate = gate or CapabilityGate()
        if site:
            self.gate.load_site(site)
        self.streams: dict[str, Any] = {}
        self._lock = threading.Lock()          # registration mutates shared budget

    # -- catalogue -----------------------------------------------------------
    def classes(self) -> dict[str, Any]:
        return {"version": self.registry.doc["version"],
                "profile": self.registry.doc["profile"],
                "policy": self.registry.policy,
                "domains": self.registry.domains,
                "links": self.registry.links,
                "classes": self.registry.classes}

    def devices(self) -> list[dict[str, Any]]:
        out = []
        for name, spec in self.gate.devices.items():
            out.append({"id": name, "kind": "class",
                        "description": spec.get("description", ""),
                        "capabilities": spec["capabilities"],
                        "evidence": spec.get("evidence", {})})
        for name, cls in self.gate.site.items():
            out.append({"id": name, "kind": "device", "class": cls,
                        "capabilities": self.gate.devices[cls]["capabilities"]})
        return out

    # -- streams -------------------------------------------------------------
    def _build(self, body: dict[str, Any]):
        try:
            req = StreamRequest(
                name=body["name"], qos_class=body["qos_class"],
                payload_bytes=int(body["payload_bytes"]),
                period_ms=float(body["period_ms"]),
                talker=body.get("talker", ""), listeners=body.get("listeners", []),
                domain=body.get("domain"), link=body.get("link", "multigige_1g"),
                durability=body.get("durability"),
                event_driven=bool(body.get("event_driven", False)),
                ros_topic=body.get("ros_topic", ""))
        except KeyError as e:
            raise Refused({"reason": f"missing field {e.args[0]!r}"}) from None
        try:
            return self.registry.resolve(req)
        except (AdmissionError, KeyError, ValueError) as e:
            raise Refused({"stream": body.get("name", "?"), "reason": str(e)}) from None

    def list_streams(self, domain: str | None = None,
                     qos_class: str | None = None) -> list[dict[str, Any]]:
        out = []
        for s in self.streams.values():
            if domain and s.tsn["domain"] != domain:
                continue
            if qos_class and s.request.qos_class != qos_class:
                continue
            out.append(stream_json(s))
        return out

    def get_stream(self, name: str) -> dict[str, Any] | None:
        s = self.streams.get(name)
        return stream_json(s) if s else None

    def register(self, body: dict[str, Any]) -> dict[str, Any]:
        """Resolve, then check the stream against what is already reserved.

        Admission is re-evaluated over the whole link, not just this stream: a
        stream that fits on an empty link may not fit beside the eight already
        there, and checking it alone is how a link ends up oversubscribed one
        admission at a time.
        """
        s = self._build(body)
        with self._lock:
            if s.name in self.streams:
                raise Refused({"stream": s.name, "reason": "already registered"})
            same_link = [x for x in self.streams.values()
                         if x.request.link == s.request.link] + [s]
            verdict = self.registry.admit(same_link, s.request.link)
            if not verdict["admitted"]:
                raise Refused({"stream": s.name, "reason": verdict["reason"],
                               "verdict": verdict})
            self.streams[s.name] = s
        return stream_json(s)

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self.streams.pop(name, None) is not None

    # -- planning ------------------------------------------------------------
    def admission(self, bodies: list[dict[str, Any]], link: str) -> dict[str, Any]:
        """Judge a set without registering it. Changes nothing."""
        resolved, refusals = [], []
        for b in bodies:
            try:
                resolved.append(self._build(b))
            except Refused as e:
                refusals.append(e.detail)
        verdict = self.registry.admit(resolved, link)
        verdict["refused"] = refusals
        verdict["streams"] = [stream_json(s) for s in resolved]
        return verdict

    def plan(self, bodies: list[dict[str, Any]], device: str, port: str = "1",
             waivers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        resolved, refusals = [], []
        for b in bodies:
            try:
                resolved.append(self._build(b))
            except Refused as e:
                refusals.append(e.detail)
        ws = [Waiver(w.get("stream", "*"), w["device"], w["capability"],
                     w["rationale"], w.get("approved_by", ""))
              for w in (waivers or [])]
        p = self.gate.plan(resolved, device, port=port, registry=self.registry, waivers=ws)
        return {
            "device": device,
            "deployed": [stream_json(s) for s in p.streams],
            "refusals": refusals + [asdict(r) for r in p.refusals],
            "waived": [{"refusal": asdict(r), "waiver": asdict(w)} for r, w in p.waived],
            "degraded": p.degraded,
            "ok": p.ok and not refusals,
            "config": {k: v for k, v in p.config.items() if not k.startswith("_")},
        }

    # -- topology ------------------------------------------------------------
    def topology(self) -> dict[str, Any]:
        used = {}
        for s in self.streams.values():
            used.setdefault(s.request.link, {"streams": 0, "mbps": 0.0})
            used[s.request.link]["streams"] += 1
            used[s.request.link]["mbps"] += s.rate_mbps
        links = []
        for name, spec in self.registry.links.items():
            u = used.get(name, {"streams": 0, "mbps": 0.0})
            links.append({"link": name, "medium": spec["medium"],
                          "rate_mbps": spec["rate_mbps"],
                          "reserved_mbps": round(u["mbps"], 4),
                          "utilisation": round(u["mbps"] / spec["rate_mbps"], 4),
                          "streams": u["streams"]})
        return {"devices": self.devices(), "links": links}
