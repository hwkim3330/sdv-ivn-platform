"""ResolvedStream -> eProsima Fast DDS XML profile.

Fast DDS is the RMW that ROS 2 (and therefore Autoware) uses by default here,
so this is the emitter the reference application consumes. The XML is written
whole rather than patched into an existing file: a profile assembled from the
registry and a profile hand-edited afterwards are indistinguishable at runtime
and that is exactly the drift Part 4 exists to prevent.

The priority path on Linux is worth stating plainly, because it is the step
that most often silently does nothing:

    DDS TRANSPORT_PRIORITY  ->  SO_PRIORITY on the socket
                            ->  skb->priority
                            ->  VLAN egress-qos-map  ->  802.1Q PCP on the wire

Fast DDS sets the socket option; the egress-qos-map is a property of the VLAN
interface and is NOT set by DDS. network/vlan/emit_vlan.py emits it, and
without it every frame leaves with PCP 0 no matter what the profile says.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET
from xml.dom import minidom


def _ms(v: float) -> tuple[str, str]:
    """Fast DDS durations are (sec, nanosec) pairs."""
    sec = int(v // 1000)
    nsec = int(round((v - sec * 1000) * 1e6))
    return str(sec), str(nsec)


def _duration(parent: ET.Element, tag: str, ms: float) -> None:
    d = ET.SubElement(parent, tag)
    sec, nsec = _ms(ms)
    ET.SubElement(d, "sec").text = sec
    ET.SubElement(d, "nanosec").text = nsec


def writer_profile(stream, profile_name: str | None = None) -> ET.Element:
    q = stream.dds
    name = profile_name or f"{stream.name}::writer"
    w = ET.Element("data_writer", {"profile_name": name})
    topic = ET.SubElement(w, "topic")
    ET.SubElement(topic, "name").text = stream.name
    hist = ET.SubElement(topic, "historyQos")
    ET.SubElement(hist, "kind").text = q["history"]["kind"]
    if q["history"]["kind"] == "KEEP_LAST":
        ET.SubElement(hist, "depth").text = str(q["history"]["depth"])

    qos = ET.SubElement(w, "qos")
    rel = ET.SubElement(qos, "reliability")
    ET.SubElement(rel, "kind").text = q["reliability"]
    dur = ET.SubElement(qos, "durability")
    ET.SubElement(dur, "kind").text = q["durability"]
    dl = ET.SubElement(qos, "deadline")
    _duration(dl, "period", q["deadline_ms"])
    if "latency_budget_ms" in q:
        lb = ET.SubElement(qos, "latencyBudget")
        _duration(lb, "duration", q["latency_budget_ms"])
    if "liveliness" in q:
        lv = ET.SubElement(qos, "liveliness")
        ET.SubElement(lv, "kind").text = q["liveliness"]["kind"]
        _duration(lv, "lease_duration", q["liveliness"]["lease_duration_ms"])
    # TRANSPORT_PRIORITY is the hook that reaches the wire. See module docstring.
    ET.SubElement(qos, "transportPriority").text = str(q["transport_priority"])

    if "resource_limits" in q:
        rl = ET.SubElement(w, "topic")   # resource limits live under topic in Fast DDS
        ET.SubElement(rl, "name").text = stream.name
        res = ET.SubElement(rl, "resourceLimitsQos")
        ET.SubElement(res, "max_samples_per_instance").text = str(
            q["resource_limits"]["max_samples_per_instance"])
        ET.SubElement(res, "max_instances").text = str(q["resource_limits"]["max_instances"])
    return w


def reader_profile(stream, profile_name: str | None = None) -> ET.Element:
    q = stream.dds
    name = profile_name or f"{stream.name}::reader"
    r = ET.Element("data_reader", {"profile_name": name})
    topic = ET.SubElement(r, "topic")
    ET.SubElement(topic, "name").text = stream.name
    hist = ET.SubElement(topic, "historyQos")
    ET.SubElement(hist, "kind").text = q["history"]["kind"]
    if q["history"]["kind"] == "KEEP_LAST":
        ET.SubElement(hist, "depth").text = str(q["history"]["depth"])
    qos = ET.SubElement(r, "qos")
    rel = ET.SubElement(qos, "reliability")
    ET.SubElement(rel, "kind").text = q["reliability"]
    dur = ET.SubElement(qos, "durability")
    ET.SubElement(dur, "kind").text = q["durability"]
    dl = ET.SubElement(qos, "deadline")
    _duration(dl, "period", q["deadline_ms"])
    if "liveliness" in q:
        lv = ET.SubElement(qos, "liveliness")
        ET.SubElement(lv, "kind").text = q["liveliness"]["kind"]
        _duration(lv, "lease_duration", q["liveliness"]["lease_duration_ms"])
    return r


def emit(streams, domain_id: int = 0) -> str:
    """A complete <dds> profiles document for a set of resolved streams."""
    root = ET.Element("dds", {"xmlns": "http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles"})
    profiles = ET.SubElement(root, "profiles")

    part = ET.SubElement(profiles, "participant",
                         {"profile_name": "keti_sdv_ivn", "is_default_profile": "true"})
    rtps = ET.SubElement(part, "rtps")
    ET.SubElement(rtps, "name").text = "keti-sdv-ivn"
    builtin = ET.SubElement(rtps, "builtin")
    ET.SubElement(builtin, "domainId").text = str(domain_id)

    for s in streams:
        profiles.append(writer_profile(s))
        profiles.append(reader_profile(s))

    raw = ET.tostring(root, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ")
