"""Resolved streams -> IEEE 802.1Q YANG instance data.

The paths and node names follow the models in the local catalog at
keti-9662-0129-new/tsc2cbor/.yang-cache/ (ieee802-dot1q-sched-bridge@2023-03-08,
ieee802-dot1q-bridge@2023-04-17, ieee802-dot1q-preemption@2022-08-18), which is
what the CORECONF write path actually resolves SIDs against. The YAML shape is
the one keti-tsn's `patch` verb consumes, so output from here can be pushed
without a translation step.

Two things this emitter does that a hand-written config usually does not:

  * `admin-base-time` is emitted, not left unset. An unset base time means each
    bridge starts its cycle whenever it happens to be configured, and two
    bridges whose cycles are offset by half a period turn a 1 ms guarantee into
    a 1.5 ms one. Every port in a domain must share the base time.
  * consecutive entries with the same gate mask are merged, because a gate
    control list is a scarce hardware resource (typically 64..256 entries) and
    an unmerged list from a solver wastes most of it on no-ops.
"""
from __future__ import annotations

from typing import Any

SCHED_PATH = ("/ietf-interfaces:interfaces/interface[name='{port}']"
              "/ieee802-dot1q-bridge:bridge-port"
              "/ieee802-dot1q-sched-bridge:gate-parameter-table")
PREEMPT_PATH = ("/ietf-interfaces:interfaces/interface[name='{port}']"
                "/ieee802-dot1q-preemption-bridge:frame-preemption-parameters")
# Credit-based shaping is not in the IEEE model the way scheduling is; on the
# Microchip parts it lives under the vendor augment. Recorded as such rather
# than pretending it is standard.
CBS_PATH_MCHP = ("/ietf-interfaces:interfaces/interface[name='{port}']"
                 "/mchp-velocitysp-port:eth-qos/config"
                 "/traffic-class-shapers[traffic-class={tc}]/credit-based/idle-slope")


def _merge(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entries:
        if e["time_interval_us"] <= 0:
            continue
        if out and out[-1]["gate_states"] == e["gate_states"]:
            out[-1] = dict(out[-1], time_interval_us=out[-1]["time_interval_us"] + e["time_interval_us"])
        else:
            out.append(dict(e))
    return out


def gate_parameter_table(port: str, gcl: dict[str, Any], base_time_s: int = 0,
                         base_time_ns: int = 0) -> dict[str, Any]:
    """One port's 802.1Qbv configuration as YANG instance data."""
    if not gcl.get("enabled"):
        return {SCHED_PATH.format(port=port): {"gate-enabled": False}}
    merged = _merge(gcl["entries"])
    cycle_ns = int(round(gcl["cycle_time_us"] * 1000))
    return {
        SCHED_PATH.format(port=port): {
            "gate-enabled": True,
            "admin-gate-states": 255,
            "admin-control-list-length": len(merged),
            "admin-control-list": [
                {
                    "index": i,
                    "operation-name": "ieee802-dot1q-sched:set-gate-states",
                    "sgs-params": {
                        "gate-states-value": e["gate_states"],
                        "time-interval-value": int(round(e["time_interval_us"] * 1000)),
                    },
                }
                for i, e in enumerate(merged)
            ],
            # numerator/denominator, per ieee802-dot1q-sched
            "admin-cycle-time": {"numerator": cycle_ns, "denominator": 1_000_000_000},
            "admin-base-time": {"seconds": base_time_s, "nanoseconds": base_time_ns},
            "config-change": True,
        }
    }


def cbs_idle_slopes(port: str, streams, vendor: str = "mchp") -> dict[str, Any]:
    """idleSlope per traffic class, summed over the streams sharing that class."""
    if vendor != "mchp":
        raise NotImplementedError(f"no CBS path known for vendor {vendor!r}; "
                                  "add it to hardware/capabilities.yaml first")
    per_tc: dict[int, float] = {}
    for s in streams:
        if s.tsn["shaper"] != "cbs":
            continue
        tc = s.tsn["traffic_class"]
        per_tc[tc] = per_tc.get(tc, 0.0) + s.tsn["cbs"]["idle_slope_kbps"]
    return {CBS_PATH_MCHP.format(port=port, tc=tc): int(round(kbps))
            for tc, kbps in sorted(per_tc.items())}


def preemption(port: str, streams) -> dict[str, Any]:
    """Per-priority express/preemptable assignment, 802.1Qbu.

    A priority is express only if EVERY class mapped to it is express. Mixing
    an express and a preemptable class onto one priority and then marking the
    priority express makes the preemptable traffic un-preemptable, which is the
    opposite of what was asked for.
    """
    status: dict[int, str] = {}
    for s in streams:
        want = "express" if s.cls["preemption"] == "express" else "preemptable"
        prev = status.get(s.pcp)
        status[s.pcp] = "preemptable" if prev == "preemptable" or want == "preemptable" else "express"
    return {
        PREEMPT_PATH.format(port=port): {
            "frame-preemption-status-table": [
                {"priority": p, "frame-preemption-status": v} for p, v in sorted(status.items())
            ]
        }
    }


def to_keti_tsn_yaml(doc: dict[str, Any]) -> str:
    """The YAML the keti-tsn CLI's `patch` verb takes: a list of one-key maps."""
    import yaml as _yaml
    return _yaml.safe_dump([{k: v} for k, v in doc.items()],
                           sort_keys=False, default_flow_style=False, allow_unicode=True)
