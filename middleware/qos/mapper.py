"""SDV QoS class -> DDS QoS + 802.1Q TSN configuration.

The one place where an application's intent ("this is SAFETY_CRITICAL") turns
into numbers a DDS participant and a bridge can be programmed with. Part 4 of
the specification is the prose form of this module; this is the normative
behaviour.

    app  ->  qos_class + payload/period  ->  [ mapper ]  ->  DDS QoS
                                                          ->  VLAN + PCP
                                                          ->  CBS idleSlope
                                                          ->  TAS gate windows
                                                          ->  802.1CB FRER
                                                          ->  admission verdict

Ethernet framing is counted honestly throughout: a stream's wire cost is not
its payload. Every rate below includes preamble, SFD, MAC header with the
802.1Q tag, FCS and the inter-frame gap.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# --- Ethernet framing constants (IEEE 802.3) -------------------------------
PREAMBLE_SFD = 8          # 7 preamble + 1 start-of-frame delimiter
MAC_HEADER = 14           # dst(6) + src(6) + ethertype(2)
VLAN_TAG = 4              # 802.1Q C-TAG
FCS = 4
IFG = 12                  # inter-frame gap, 96 bit times
MIN_PAYLOAD = 46          # 802.3 minimum, padded below this
MAX_PAYLOAD = 1500

# Per-frame overhead on the wire for a VLAN-tagged frame.
WIRE_OVERHEAD = PREAMBLE_SFD + MAC_HEADER + VLAN_TAG + FCS + IFG   # 42 bytes

# --- Upper-layer headers we must not pretend away -------------------------
# RTPS over UDP/IPv4: IP(20) + UDP(8) + RTPS header(20) + DATA submessage with
# inline QoS and a 16-byte writer/reader/sequence prologue. 56 bytes of RTPS
# per DATA submessage is the figure Fast DDS and Cyclone both land near for a
# plain keyed sample; it is a floor, not a guarantee, so it is named here
# rather than buried in an expression.
IP_UDP = 28
RTPS_OVERHEAD = 56

# 802.1Qbu/802.3br: an express frame can preempt a preemptable one, but not
# below this residue, so this is the worst-case interference a high-priority
# frame still waits for on a preemption-capable port.
PREEMPT_RESIDUE_BYTES = 143
# Without preemption the interference is a full maximum-size frame.
MAX_FRAME_BYTES = MAX_PAYLOAD + MAC_HEADER + VLAN_TAG + FCS


class AdmissionError(Exception):
    """A stream that cannot be admitted without breaking a promise already made."""


def _frames_for(payload_bytes: int) -> tuple[int, int]:
    """(frame count, bytes on the wire) for one sample of `payload_bytes`.

    A sample larger than one MTU is fragmented by RTPS, and every fragment pays
    its own Ethernet and IP/UDP overhead. A LiDAR scan is thousands of frames;
    charging it as one is how bandwidth plans come out optimistic.
    """
    if payload_bytes <= 0:
        raise ValueError("payload_bytes must be positive")
    usable = MAX_PAYLOAD - IP_UDP - RTPS_OVERHEAD
    frames = max(1, math.ceil(payload_bytes / usable))
    total = 0
    left = payload_bytes
    for _ in range(frames):
        chunk = min(left, usable)
        left -= chunk
        l3 = IP_UDP + RTPS_OVERHEAD + chunk
        total += max(l3, MIN_PAYLOAD) + WIRE_OVERHEAD
    return frames, total


@dataclass
class StreamRequest:
    """What an application asks for. Deliberately small: no PCP, no VLAN, no
    shaper. Those are the network's business, derived from the class."""
    name: str
    qos_class: str
    payload_bytes: int
    period_ms: float
    talker: str = ""
    listeners: list[str] = field(default_factory=list)
    domain: str | None = None            # override the class default
    link: str = "multigige_1g"
    redundancy: str | None = None        # override; "none" opts out of FRER
    durability: str | None = None        # override; the map case needs transient_local
    event_driven: bool = False           # latched/event topic: no meaningful period
    ros_topic: str = ""                  # provenance, when derived from a catalogue

    @property
    def rate_hz(self) -> float:
        return 1000.0 / self.period_ms


@dataclass
class ResolvedStream:
    """Everything derived. Emitters below turn this into vendor syntax."""
    request: StreamRequest
    cls: dict[str, Any]
    vlan: int
    pcp: int
    frames_per_sample: int
    wire_bytes_per_sample: int
    rate_mbps: float
    dds: dict[str, Any]
    tsn: dict[str, Any]

    @property
    def name(self) -> str:
        return self.request.name


class QosRegistry:
    """The class registry plus the derivations Part 4 defines over it."""

    DEFAULT_PATH = Path(__file__).resolve().parents[2] / "schemas" / "yaml" / "qos-classes.yaml"

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else self.DEFAULT_PATH
        with open(self.path) as f:
            self.doc = yaml.safe_load(f)
        self.classes = self.doc["classes"]
        self.domains = self.doc["domains"]
        self.links = self.doc["links"]
        self.policy = self.doc.get("policy", {})
        self.link_admissible = self.doc.get("link_admissible_classes", {})
        self._apply_pcp_policy()
        self._validate()

    def _apply_pcp_policy(self) -> None:
        """PCP 7 goes to an application class only if the profile says so.

        The default in this profile is to allow it, because the reference
        architecture asks for REDUNDANT_SAFETY at 7. The cost is that gPTP and
        TSN reconfiguration no longer have a priority no application can reach,
        and a slipped time sync moves the TAS gates themselves. Turning the
        policy off moves everything down one and gives 7 back to network
        control; the fallback map is in the profile so the two configurations
        stay comparable instead of being two hand-edited files.
        """
        if self.policy.get("allow_pcp7_for_applications", True):
            return
        for name, override in (self.doc.get("pcp_fallback") or {}).items():
            if name in self.classes:
                self.classes[name].update(override)

    # -- invariants the registry must hold before anything is derived from it
    def _validate(self) -> None:
        seen: dict[int, str] = {}
        for name, c in self.classes.items():
            pcp = c["pcp"]
            if not 0 <= pcp <= 7:
                raise ValueError(f"{name}: pcp {pcp} outside 0..7")
            if pcp == 7 and not self.policy.get("allow_pcp7_for_applications", True):
                raise ValueError(
                    f"{name}: pcp 7 is reserved for network control under this profile's "
                    "policy, but the class still claims it; fix pcp_fallback")
            if pcp in seen:
                raise ValueError(f"{name} and {seen[pcp]} both claim pcp {pcp}")
            seen[pcp] = name
            if c["default_domain"] not in self.domains:
                raise ValueError(f"{name}: unknown domain {c['default_domain']}")
            if c.get("deadline_tolerance", 2.0) < 1.0:
                raise ValueError(
                    f"{name}: deadline tolerance {c['deadline_tolerance']} is below 1.0, "
                    "which asks publishers to be faster than their own period")

    # -- DDS -----------------------------------------------------------------
    def dds_qos(self, cls: dict[str, Any], req: StreamRequest) -> dict[str, Any]:
        """The abstract DDS QoS policy set. Vendor XML is generated from this,
        never the other way round.

        DEADLINE is derived from the stream's own period, not from the class's
        latency target. They are different quantities that are easy to conflate:
        LATENCY_BUDGET is how long a sample may take to arrive, DEADLINE is how
        far apart two samples may be. Setting DEADLINE to a 5 ms latency target
        on a 10 ms stream makes every correctly delivered sample a violation --
        which is exactly what the monitor reported the first time it was run
        against this profile, 499 misses in 500 healthy samples.

        So: deadline = period x the class's tolerance, and nothing else. A first
        attempt also capped it per class, which was wrong a second time: a fault
        report published at 1 Hz deserves a 1.5 s deadline, and the cap refused
        it. The stream that really has no sensible deadline is the latched,
        event-driven one -- a route, a map, an MRM state -- where a period had
        to be invented to budget bandwidth at all. Those declare event_driven
        and get no DEADLINE, rather than a fabricated one.
        """
        if req.event_driven:
            deadline = None
        else:
            deadline = req.period_ms * cls.get("deadline_tolerance", 2.0)
        q: dict[str, Any] = {
            "reliability": "RELIABLE" if cls["reliability"] == "reliable" else "BEST_EFFORT",
            # None means DURATION_INFINITE: the policy is off, not zero.
            "durability": {"volatile": "VOLATILE", "transient_local": "TRANSIENT_LOCAL"}[cls["durability"]],
            "history": {"kind": "KEEP_LAST" if cls["history"]["kind"] == "keep_last" else "KEEP_ALL",
                        "depth": cls["history"]["depth"]},
            "deadline_ms": deadline,
            "transport_priority": cls["pcp"],   # carried to the socket, see below
            "dscp": cls["dscp"],
        }
        if cls["latency_budget_ms"] is not None:
            q["latency_budget_ms"] = cls["latency_budget_ms"]
        # LIVELINESS only where a silent talker is itself the fault. On a
        # best-effort sensor class the deadline already reports it and an extra
        # assertion is just traffic in the class we are trying to protect.
        if cls.get("liveliness") == "automatic":
            lease = max(100.0, (deadline if deadline is not None else req.period_ms) * 3)
            q["liveliness"] = {"kind": "AUTOMATIC", "lease_duration_ms": lease}
        # RESOURCE_LIMITS bound the writer so a stalled listener cannot grow a
        # queue into the memory of a safety task.
        if q["history"]["kind"] == "KEEP_LAST":
            q["resource_limits"] = {"max_samples_per_instance": max(1, q["history"]["depth"]),
                                    "max_instances": 64}
        return q

    # -- 802.1Qav credit-based shaper ---------------------------------------
    @staticmethod
    def cbs_parameters(rate_mbps: float, port_mbps: float, max_frame_bytes: int,
                       preemptable_interference: bool = True) -> dict[str, float]:
        """idleSlope/sendSlope/hiCredit/loCredit per IEEE 802.1Q-2022 clause 8.6.8.2.

        idleSlope is the stream's own reserved rate. The credits follow from it
        and from the worst-case interference the class must wait through.
        """
        idle = rate_mbps
        send = idle - port_mbps                       # negative by construction
        interference = PREEMPT_RESIDUE_BYTES if preemptable_interference else MAX_FRAME_BYTES
        # hiCredit: credit accrued while blocked by the largest interfering frame
        hi = idle * (interference * 8) / port_mbps
        # loCredit: credit spent transmitting this class's own largest frame
        lo = send * (max_frame_bytes * 8) / port_mbps
        # Units are bits per second, because that is what the standard leaf
        # ieee802-dot1q-cbsa:admin-idle-slope is defined in. Linux `tc cbs`
        # wants kilobit per second; that conversion belongs in the emitter that
        # speaks to Linux, not in the value the platform carries.
        return {"idle_slope_bps": round(idle * 1e6, 3),
                "send_slope_bps": round(send * 1e6, 3),
                "hi_credit_bits": round(hi, 3),
                "lo_credit_bits": round(lo, 3)}

    # -- resolution ----------------------------------------------------------
    def resolve(self, req: StreamRequest) -> ResolvedStream:
        if req.qos_class not in self.classes:
            raise KeyError(f"unknown QoS class {req.qos_class!r}; "
                           f"known: {', '.join(sorted(self.classes))}")
        cls = dict(self.classes[req.qos_class])
        if req.durability is not None:
            if req.durability not in ("volatile", "transient_local"):
                raise ValueError(f"{req.name}: unknown durability {req.durability!r}")
            cls["durability"] = req.durability
        if req.link not in self.links:
            raise KeyError(f"unknown link {req.link!r}")
        link = self.links[req.link]
        allowed = self.link_admissible.get(req.link)
        if allowed is not None and req.qos_class not in allowed:
            raise AdmissionError(
                f"{req.name}: {req.qos_class} is not admissible on {req.link} "
                f"({link['medium']}); this link carries only {', '.join(allowed)}. "
                "A 10 Mb/s edge bus is for state and commands, not sensor payload.")
        domain = req.domain or cls["default_domain"]
        if domain not in self.domains:
            raise KeyError(f"unknown domain {domain!r}")

        frames, wire = _frames_for(req.payload_bytes)
        rate_mbps = wire * 8 * req.rate_hz / 1e6

        if cls["max_rate_mbps"] is not None and rate_mbps > cls["max_rate_mbps"]:
            raise AdmissionError(
                f"{req.name}: {rate_mbps:.1f} Mb/s exceeds the {req.qos_class} "
                f"class ceiling of {cls['max_rate_mbps']} Mb/s")
        if rate_mbps > link["rate_mbps"]:
            raise AdmissionError(
                f"{req.name}: {rate_mbps:.1f} Mb/s does not fit a "
                f"{link['rate_mbps']} Mb/s {req.link} link")

        redundancy = req.redundancy or cls["redundancy"]
        if redundancy != "none" and not link["frer"]:
            raise AdmissionError(
                f"{req.name}: class {req.qos_class} requires {redundancy} but "
                f"{req.link} ({link['medium']}) has no 802.1CB support")
        shaper = cls["shaper"]
        if shaper == "tas" and not link["tas"]:
            # A 10BASE-T1S multidrop segment has no gates. Degrade to the
            # highest-priority strict queue and say so, rather than emitting a
            # gate control list the hardware will ignore.
            shaper = "strict_priority"
        if shaper == "cbs" and not link["cbs"]:
            shaper = "strict_priority"

        max_frame = min(MAX_FRAME_BYTES, wire // frames)
        tsn: dict[str, Any] = {
            "vlan": self.domains[domain]["vlan"],
            "domain": domain,
            "pcp": cls["pcp"],
            "traffic_class": cls["traffic_class"],
            "shaper": shaper,
            "redundancy": redundancy,
            "preemption": cls["preemption"] if link["preemption"] else "not_supported",
            "reserved_mbps": round(rate_mbps, 4),
            "frames_per_sample": frames,
            "max_frame_bytes": max_frame,
        }
        if shaper == "cbs":
            tsn["cbs"] = self.cbs_parameters(rate_mbps, link["rate_mbps"], max_frame,
                                             preemptable_interference=link["preemption"])
        if shaper == "tas":
            # The per-stream share of a cycle; the full gate control list is
            # built across all TAS streams by build_gate_control_list().
            tsn["tas_bytes_per_period"] = wire
        if redundancy == "frer_dual":
            tsn["frer"] = self.frer_config(req, self.domains[domain]["vlan"], cls["pcp"])

        return ResolvedStream(request=req, cls=cls, vlan=self.domains[domain]["vlan"],
                              pcp=cls["pcp"], frames_per_sample=frames,
                              wire_bytes_per_sample=wire, rate_mbps=rate_mbps,
                              dds=self.dds_qos(cls, req), tsn=tsn)

    # -- 802.1CB ------------------------------------------------------------
    @staticmethod
    def frer_config(req: StreamRequest, vlan: int, pcp: int) -> dict[str, Any]:
        """Frame replication and elimination for one stream.

        The recovery window has to be at least as long as the difference in
        delay between the two paths, or the eliminator drops the copy that
        arrives 'too late' and the redundancy achieves nothing. Two hops of
        difference at gigabit is well under a millisecond, but the history
        length is expressed in frames, so it must also cover the burst.
        """
        handle = (abs(hash(req.name)) % 0xFFFF) or 1
        burst = max(2, math.ceil(2.0 / req.period_ms))   # frames within 2 ms
        return {
            "stream_handle": handle,
            "identification": {"type": "ip-stream-identification",
                               "vlan": vlan, "pcp": pcp},
            "sequence_generation": {"at": req.talker or "talker", "encapsulation": "r-tag"},
            "sequence_recovery": {
                "at": req.listeners or ["listener"],
                "algorithm": "vector",
                "history_length": max(4, burst * 2),
                "reset_timeout_ms": max(10.0, req.period_ms * 4),
                "take_no_sequence": False,
            },
            "paths": 2,
        }

    # -- 802.1Qbv -----------------------------------------------------------
    def build_gate_control_list(self, streams: list[ResolvedStream], port_mbps: float,
                                cycle_ms: float | None = None) -> dict[str, Any]:
        """One gate control list covering every TAS stream on a port.

        The cycle is the shortest stream period, so every TAS stream gets at
        least one window per period. Windows are sized by the bytes that class
        must move per cycle plus a guard band, and the remainder is one open
        window for everything else. If the protected windows do not fit, that
        is an admission failure, not a value to round down.
        """
        tas = [s for s in streams if s.tsn["shaper"] == "tas"]
        if not tas:
            return {"enabled": False, "reason": "no TAS stream on this port"}
        if cycle_ms is None:
            cycle_ms = min(s.request.period_ms for s in tas)

        byte_time_us = 8.0 / port_mbps                 # microseconds per byte
        guard_us = PREEMPT_RESIDUE_BYTES * byte_time_us

        by_tc: dict[int, float] = {}
        for s in tas:
            reps = cycle_ms / s.request.period_ms
            by_tc.setdefault(s.tsn["traffic_class"], 0.0)
            by_tc[s.tsn["traffic_class"]] += s.wire_bytes_per_sample * reps

        entries = []
        used_us = 0.0
        for tc in sorted(by_tc, reverse=True):         # most urgent first in the cycle
            win = by_tc[tc] * byte_time_us + guard_us
            entries.append({"gate_states": 1 << tc, "time_interval_us": round(win, 3),
                            "traffic_class": tc, "protected": True})
            used_us += win

        cycle_us = cycle_ms * 1000.0
        if used_us > cycle_us:
            raise AdmissionError(
                f"protected windows need {used_us:.1f} us of a {cycle_us:.1f} us cycle; "
                f"reduce rate, widen the cycle, or move a stream to CBS")
        open_mask = 0xFF & ~sum(1 << tc for tc in by_tc)
        entries.append({"gate_states": open_mask, "time_interval_us": round(cycle_us - used_us, 3),
                        "traffic_class": None, "protected": False})
        return {"enabled": True, "cycle_time_us": round(cycle_us, 3),
                "utilisation_protected": round(used_us / cycle_us, 4),
                "guard_band_us": round(guard_us, 3), "entries": entries}

    # -- admission across a whole port --------------------------------------
    def admit(self, streams: list[ResolvedStream], link: str = "multigige_1g",
              max_utilisation: float = 0.75) -> dict[str, Any]:
        """Aggregate check for one egress port.

        The ceiling is 75% rather than 100% on purpose: 802.1Qav credit
        behaviour and the burstiness of real talkers both degrade sharply as a
        link approaches saturation, and the project's jitter KPI is measured on
        the loaded network, not an idle one.
        """
        port = self.links[link]["rate_mbps"]
        # FRER doubles what actually crosses the network.
        total = sum(s.rate_mbps * (2 if s.tsn["redundancy"] == "frer_dual" else 1)
                    for s in streams)
        util = total / port
        per_class: dict[str, float] = {}
        for s in streams:
            per_class.setdefault(s.request.qos_class, 0.0)
            per_class[s.request.qos_class] += s.rate_mbps
        ok = util <= max_utilisation
        return {"link": link, "port_mbps": port, "offered_mbps": round(total, 3),
                "utilisation": round(util, 4), "limit": max_utilisation,
                "admitted": ok, "per_class_mbps": {k: round(v, 3) for k, v in per_class.items()},
                "reason": None if ok else
                          f"{util*100:.1f}% of the {port} Mb/s port exceeds the "
                          f"{max_utilisation*100:.0f}% admission ceiling"}
