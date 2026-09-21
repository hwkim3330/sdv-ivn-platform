"""The Autoware reference application must fit the IVN profile.

These are not unit tests of the mapper; they assert that the real traffic of
the reference application can actually be carried, and they fail when someone
adds a topic that cannot. That failure is the point: an application change that
breaks the network budget should break a test, not a demo.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "middleware"))
sys.path.insert(0, str(ROOT / "profiles" / "autoware"))

from qos.mapper import QosRegistry, AdmissionError            # noqa: E402
from core.deploy import CapabilityGate, Waiver                # noqa: E402
import build_profile                                          # noqa: E402


@pytest.fixture(scope="module")
def built():
    catalog = build_profile.load_catalog()
    reg = QosRegistry()
    resolved, refused = build_profile.build(catalog, reg)
    return catalog, reg, resolved, refused


def test_every_catalogued_topic_resolves(built):
    _, _, resolved, refused = built
    assert not refused, "\n".join(f"{t['topic']}: {why}" for t, why in refused)


def test_catalogue_covers_every_bucket(built):
    catalog, _, _, _ = built
    buckets = {t["bucket"] for t in catalog["topics"]}
    required = {"sensing_high_bandwidth", "sensing_low", "localization", "planning",
                "control", "vehicle_status", "fault", "reconfiguration", "map",
                "diagnostics", "ui"}
    assert required <= buckets, f"missing: {required - buckets}"


def test_every_topic_names_a_class_that_exists(built):
    catalog, reg, _, _ = built
    for t in catalog["topics"]:
        assert t["sdv_class"] in reg.classes, f"{t['topic']}: {t['sdv_class']}"


def test_assumed_values_are_labelled(built):
    """A rate or payload not taken from code must say where it came from, so a
    later reader can tell a measurement from a guess."""
    catalog, _, _, _ = built
    for t in catalog["topics"]:
        if "rate_basis" in t or "payload_basis" in t:
            basis = t.get("rate_basis", "") + t.get("payload_basis", "")
            assert len(basis) > 15, f"{t['topic']}: basis too thin"


def test_control_command_is_the_most_protected_class(built):
    _, _, resolved, _ = built
    cmd = next(s for s in resolved if s.name == "/external/selected/control_cmd")
    assert cmd.request.qos_class == "REDUNDANT_SAFETY"
    assert cmd.tsn["redundancy"] == "frer_dual"
    assert cmd.dds["reliability"] == "RELIABLE"


def test_every_link_is_admissible(built):
    _, reg, resolved, _ = built
    for link, streams in build_profile.by_link(resolved).items():
        v = reg.admit(streams, link)
        assert v["admitted"], f"{link}: {v['reason']}"


def test_edge_bus_carries_only_state_and_commands(built):
    """10 Mb/s is for state and commands. A sensor payload there starves the
    control traffic sharing the segment."""
    _, _, resolved, _ = built
    edge = [s for s in resolved if s.request.link == "t1s_10m"]
    assert edge, "the profile should exercise the edge bus"
    for s in edge:
        assert s.request.qos_class in ("BEST_EFFORT", "SAFETY_CRITICAL", "REDUNDANT_SAFETY")
        assert s.rate_mbps < 1.0, f"{s.name} is {s.rate_mbps:.2f} Mb/s on a 10 Mb/s bus"


def test_edge_bus_has_no_redundant_streams(built):
    """802.1CB on a multidrop segment replicates onto the same wire. FINDINGS 1."""
    _, _, resolved, _ = built
    for s in resolved:
        if s.request.link == "t1s_10m":
            assert s.tsn["redundancy"] == "none"


def test_gate_control_list_is_buildable_per_link(built):
    _, reg, resolved, _ = built
    for link, streams in build_profile.by_link(resolved).items():
        port = reg.links[link]["rate_mbps"]
        reg.build_gate_control_list(streams, port)   # raises if it does not fit


def test_lidar_scan_is_many_frames(built):
    """A bandwidth plan that charges a point cloud as one frame is wrong by
    the framing overhead of about 1,800 frames."""
    _, _, resolved, _ = built
    lidar = next(s for s in resolved if "concatenated" in s.name)
    assert lidar.frames_per_sample > 1000


def test_kpi_streams_carry_the_budgets(built):
    _, _, resolved, _ = built
    kpi = [s for s in resolved
           if s.request.qos_class in ("SAFETY_CRITICAL", "REDUNDANT_SAFETY")]
    assert len(kpi) >= 10
    for s in kpi:
        assert s.cls["latency_budget_ms"] == 5.0
        assert s.cls["jitter_budget_ms"] == 2.5


def test_the_bench_switch_refuses_the_redundant_streams(built):
    """LAN9692 has 802.1CB stream identification but not replication. The
    profile must not pretend otherwise."""
    _, reg, resolved, _ = built
    plan = CapabilityGate().plan(resolved, "lan9692", registry=reg)
    assert not plan.ok
    assert {r.capability for r in plan.refusals} == {"frer"}


def test_the_bench_switch_runs_with_a_recorded_waiver(built):
    _, reg, resolved, _ = built
    catalog = build_profile.load_catalog()
    fresh, _ = build_profile.build(catalog, reg)      # unmutated copies
    w = [Waiver("*", "lan9692", "frer",
                "no 802.1CB replication on this part; this run measures the "
                "DDS-TSN mapping and the latency budget, not path redundancy",
                "bench")]
    plan = CapabilityGate().plan(fresh, "lan9692", registry=reg, waivers=w)
    assert plan.ok and plan.degraded
    assert all(s.tsn["redundancy"] == "none" for s in plan.streams)


def test_profile_yaml_round_trips(built):
    import yaml
    _, _, resolved, _ = built
    doc = yaml.safe_load(build_profile.to_profile_yaml(resolved))
    assert len(doc["streams"]) == len(resolved)
    assert all("dds" in s and "tsn" in s for s in doc["streams"])
