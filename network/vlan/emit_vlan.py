"""VLAN and priority configuration for a Linux endpoint.

This is the step that most often makes an otherwise correct TSN setup do
nothing. A DDS participant setting TRANSPORT_PRIORITY reaches skb->priority;
the 802.1Q PCP on the wire comes from the VLAN interface's egress-qos-map. If
that map is missing, every frame leaves with PCP 0 and the bridge's careful
per-priority scheduling sees one class.
"""
from __future__ import annotations


def egress_qos_map(streams) -> dict[int, int]:
    """skb priority -> 802.1Q PCP. Identity here by construction, because the
    mapper sets TRANSPORT_PRIORITY to the PCP; emitted explicitly anyway so the
    assumption is visible and testable rather than implied."""
    return {s.dds["transport_priority"]: s.pcp for s in streams}


def ip_link_commands(parent: str, streams, domains: dict[str, dict]) -> list[str]:
    """`ip link` commands creating one VLAN interface per domain in use."""
    used = sorted({(s.tsn["domain"], s.vlan) for s in streams}, key=lambda t: t[1])
    qos = egress_qos_map(streams)
    mapping = " ".join(f"{k}:{v}" for k, v in sorted(qos.items()))
    cmds = []
    for domain, vid in used:
        dev = f"{parent}.{vid}"
        cmds.append(f"ip link add link {parent} name {dev} type vlan id {vid} "
                    f"egress-qos-map {mapping}")
        cmds.append(f"ip link set {dev} up   # domain: {domain}")
    return cmds


def tc_commands(parent: str, streams, port_mbps: float, gcl: dict | None = None) -> list[str]:
    """Endpoint-side shaping: mqprio to split traffic classes onto hardware
    queues, then taprio or cbs per class.

    Offload is requested, not assumed: `taprio ... flags 0x2` is full hardware
    offload and fails loudly on a NIC that cannot do it, which is the behaviour
    we want. A software fallback that silently misses a gate by 200 us would
    pass a functional test and fail the jitter KPI.
    """
    tcs = sorted({s.tsn["traffic_class"] for s in streams}, reverse=True)
    n = len(tcs)
    # priority -> queue index, one queue per traffic class in use
    prio_map = []
    order = {tc: i for i, tc in enumerate(tcs)}
    for prio in range(16):
        match = [s for s in streams if s.pcp == prio]
        prio_map.append(str(order[match[0].tsn["traffic_class"]]) if match else str(n - 1))
    cmds = [
        f"tc qdisc replace dev {parent} parent root handle 100 mqprio "
        f"num_tc {n} map {' '.join(prio_map)} "
        f"queues {' '.join(f'1@{i}' for i in range(n))} hw 0"
    ]
    for s in streams:
        if s.tsn["shaper"] != "cbs":
            continue
        q = order[s.tsn["traffic_class"]] + 1
        c = s.tsn["cbs"]
        cmds.append(
            f"tc qdisc replace dev {parent} parent 100:{q} cbs "
            f"idleslope {int(c['idle_slope_kbps'])} sendslope {int(c['send_slope_kbps'])} "
            f"hicredit {int(c['hi_credit_bits'])} locredit {int(c['lo_credit_bits'])} offload 1"
        )
    if gcl and gcl.get("enabled"):
        sched = " ".join(
            f"sched-entry S {e['gate_states']:02x} {int(e['time_interval_us'] * 1000)}"
            for e in gcl["entries"] if e["time_interval_us"] > 0)
        cmds.append(
            f"tc qdisc replace dev {parent} parent root handle 100 taprio "
            f"num_tc {n} map {' '.join(prio_map)} "
            f"queues {' '.join(f'1@{i}' for i in range(n))} "
            f"base-time 0 {sched} flags 0x2   # 0x2 = full hardware offload, fails loudly"
        )
    return cmds
