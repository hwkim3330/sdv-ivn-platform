"""ResolvedStream -> Eclipse Cyclone DDS configuration.

Cyclone splits the two halves differently from Fast DDS: transport, discovery
and scheduling are configuration (this XML), while per-topic reliability,
deadline and history are set on the entity in code. Emitting a Cyclone "QoS
profile XML" the way Fast DDS has one would be inventing a feature, so this
emitter produces the half Cyclone really reads, plus a companion table the
application applies to its entities.

The DiffServ field is how a Cyclone participant marks packets; the PCP then
comes from the VLAN egress-qos-map exactly as it does under Fast DDS.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET
from xml.dom import minidom


def emit(streams, domain_id: int = 0, interface: str = "auto") -> str:
    root = ET.Element("CycloneDDS", {"xmlns": "https://cdds.io/config"})
    dom = ET.SubElement(root, "Domain", {"Id": str(domain_id)})
    general = ET.SubElement(dom, "General")
    if interface != "auto":
        ifaces = ET.SubElement(general, "Interfaces")
        ET.SubElement(ifaces, "NetworkInterface", {"name": interface})
    # The highest DSCP in use on this participant; Cyclone marks per-socket,
    # so a participant carrying mixed classes marks at its most urgent.
    dscp = max((s.dds["dscp"] for s in streams), default=0)
    ET.SubElement(general, "DontRoute").text = "true"
    tp = ET.SubElement(dom, "Internal")
    ET.SubElement(tp, "MinimumSocketReceiveBufferSize").text = "1MB"
    ts = ET.SubElement(dom, "Tracing")
    ET.SubElement(ts, "Category").text = "config"
    net = ET.SubElement(dom, "General")
    ET.SubElement(net, "Transport").text = "udp"
    # DiffServField is the documented Cyclone knob for the IP TOS byte.
    ET.SubElement(net, "DiffServField").text = str(dscp << 2)
    raw = ET.tostring(root, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ")


def entity_qos_table(streams) -> list[dict]:
    """What the application must set on each entity, since Cyclone does not
    take it from configuration. Consumed by api/network/ and the Autoware
    bridge; also the table a conformance test compares a live system against."""
    out = []
    for s in streams:
        q = s.dds
        out.append({
            "topic": s.name,
            "reliability": q["reliability"],
            "durability": q["durability"],
            "history_kind": q["history"]["kind"],
            "history_depth": q["history"]["depth"],
            "deadline_ms": q["deadline_ms"],
            "latency_budget_ms": q.get("latency_budget_ms"),
            "liveliness_lease_ms": (q.get("liveliness") or {}).get("lease_duration_ms"),
            "transport_priority": q["transport_priority"],
            "dscp": q["dscp"],
        })
    return out
