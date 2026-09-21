"""Resolved streams -> IEEE 802.1Q instance data, standard models only.

Every path this module produces resolves against the published IEEE YANG
modules shipped in schemas/yang/ieee, and tests/conformance/test_yang_paths.py
checks that on every run. No vendor module, no vendor augment, no vendor
deviation appears here. A device that needs something else is the adapter's
problem, in devices/, not the platform's.

That rule was earned. An earlier version of this file wrote CBS idle slope to a
vendor path in kilobits per second and packed the gate states into a vendor
container called `sgs-params`. Both are real -- a particular switch family does
accept them -- and both are wrong as a platform interface: the standard leaf is
`admin-idle-slope` in BITS per second, and the gate state is `gate-states-value`
sitting directly on `gate-control-entry`.

STANDARD PATHS USED
  802.1Qbv  ieee802-dot1q-sched-bridge:gate-parameter-table
  802.1Qav  ieee802-dot1q-cbsa-bridge:cbsa/cbsa-parameter-table
  802.1Qbu  ieee802-dot1q-preemption-bridge:frame-preemption-parameters
  802.1Q    ieee802-dot1q-bridge:bridge-port (the augment target for all three)
"""
from __future__ import annotations

from typing import Any

PORT = "/ietf-interfaces:interfaces/interface[name='{port}']/ieee802-dot1q-bridge:bridge-port"
SCHED = PORT + "/ieee802-dot1q-sched-bridge:gate-parameter-table"
CBSA = PORT + "/ieee802-dot1q-cbsa-bridge:cbsa"
PREEMPT = PORT + "/ieee802-dot1q-preemption-bridge:frame-preemption-parameters"

# ieee802-dot1q-types:type-of-operation identities usable in a schedule.
OP_SET_GATE_STATES = "ieee802-dot1q-types:set-gate-states"


def _merge(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold consecutive entries with the same gate mask.

    A gate control list is a scarce hardware resource -- commonly 64 to 256
    entries -- and a list straight out of a scheduler spends most of it on
    no-ops. Merging is also why `admin-control-list-length` is computed here
    rather than taken from the caller.
    """
    out: list[dict[str, Any]] = []
    for e in entries:
        if e["time_interval_us"] <= 0:
            continue
        if out and out[-1]["gate_states"] == e["gate_states"]:
            out[-1] = dict(out[-1],
                           time_interval_us=out[-1]["time_interval_us"] + e["time_interval_us"])
        else:
            out.append(dict(e))
    return out


def gate_parameter_table(port: str, gcl: dict[str, Any], base_time_s: int = 0,
                         base_time_ns: int = 0) -> dict[str, Any]:
    """One port's 802.1Qbv configuration.

    `admin-base-time` is always written. Leaving it unset lets each bridge start
    its cycle whenever it happened to be configured, and two bridges whose
    cycles sit half a period apart turn a 1 ms guarantee into a 1.5 ms one.
    Every port in a domain must be given the same base time.
    """
    if not gcl.get("enabled"):
        return {SCHED.format(port=port): {"gate-enabled": False}}
    merged = _merge(gcl["entries"])
    cycle_ns = int(round(gcl["cycle_time_us"] * 1000))
    return {
        SCHED.format(port=port): {
            "gate-enabled": True,
            "admin-gate-states": 255,
            "admin-control-list": {
                "gate-control-entry": [
                    {
                        "index": i,
                        "operation-name": OP_SET_GATE_STATES,
                        "gate-states-value": e["gate_states"],
                        "time-interval-value": int(round(e["time_interval_us"] * 1000)),
                    }
                    for i, e in enumerate(merged)
                ]
            },
            "admin-cycle-time": {"numerator": cycle_ns, "denominator": 1_000_000_000},
            "admin-base-time": {"seconds": base_time_s, "nanoseconds": base_time_ns},
            "config-change": True,
        }
    }


def cbsa(port: str, streams) -> dict[str, Any]:
    """Credit-based shaper reservation per traffic class, 802.1Qav.

    `admin-idle-slope` is in BITS per second. Streams sharing a traffic class
    have their reservations summed, because the shaper serves the queue, not
    the stream.
    """
    per_tc: dict[int, float] = {}
    for s in streams:
        if s.tsn["shaper"] != "cbs":
            continue
        tc = s.tsn["traffic_class"]
        per_tc[tc] = per_tc.get(tc, 0.0) + s.tsn["cbs"]["idle_slope_bps"]
    if not per_tc:
        return {}
    return {
        CBSA.format(port=port): {
            "cbsa-parameter-table": [
                {"traffic-class": tc, "admin-idle-slope": int(round(bps))}
                for tc, bps in sorted(per_tc.items())
            ]
        }
    }


def preemption(port: str, streams) -> dict[str, Any]:
    """Per-priority express/preemptable assignment, 802.1Qbu.

    The standard node is a container of eight leaves, `priority0` to
    `priority7`, not a keyed list.

    A priority is express only if EVERY class mapped to it is express. Mixing
    an express class and a preemptable one onto one priority and then marking
    that priority express makes the preemptable traffic un-preemptable, which
    is the opposite of what was asked for.
    """
    status: dict[int, str] = {}
    for s in streams:
        want = "express" if s.cls["preemption"] == "express" else "preemptable"
        prev = status.get(s.pcp)
        status[s.pcp] = "preemptable" if prev == "preemptable" or want == "preemptable" else "express"
    if not status:
        return {}
    table = {f"priority{p}": v for p, v in sorted(status.items())}
    return {PREEMPT.format(port=port): {"frame-preemption-status-table": table}}


def queue_max_sdu(port: str, streams) -> dict[str, Any]:
    """Per-traffic-class maximum SDU, which bounds how long a frame of that
    class can hold a gate open past its window. Only emitted where a class has
    a protected window, since it costs nothing elsewhere and constrains the
    guard band calculation where it matters."""
    rows = []
    for tc in sorted({s.tsn["traffic_class"] for s in streams if s.tsn["shaper"] == "tas"}):
        biggest = max(s.tsn["max_frame_bytes"] for s in streams
                      if s.tsn["traffic_class"] == tc)
        rows.append({"traffic-class": tc, "queue-max-sdu": int(biggest)})
    if not rows:
        return {}
    return {SCHED.format(port=port) + "/queue-max-sdu-table": rows}


def to_instance_yaml(doc: dict[str, Any]) -> str:
    """Instance data as a list of single-key maps, the shape a CORECONF or
    RESTCONF writer takes after prefixing. Device adapters translate from here."""
    import yaml as _yaml
    return _yaml.safe_dump([{k: v} for k, v in doc.items()],
                           sort_keys=False, default_flow_style=False, allow_unicode=True)
