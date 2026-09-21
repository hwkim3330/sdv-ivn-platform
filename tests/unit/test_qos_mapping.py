"""Unit tests for the QoS mapper — the normative behaviour of Part 3 and Part 4.

These are the tests a second implementation has to pass to claim conformance,
so they assert on the profile's promises rather than on this implementation's
internals.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "middleware"))

from qos.mapper import QosRegistry, StreamRequest, AdmissionError, _frames_for, WIRE_OVERHEAD
from core.deploy import CapabilityGate, Waiver


@pytest.fixture(scope="module")
def reg():
    return QosRegistry()


# --- the profile itself ----------------------------------------------------

def test_five_classes_with_stable_ids(reg):
    ids = {c["id"] for c in reg.classes.values()}
    assert ids == {"SDV_QOS_0", "SDV_QOS_1", "SDV_QOS_2", "SDV_QOS_3", "SDV_QOS_4"}


def test_every_class_has_a_distinct_priority(reg):
    pcps = [c["pcp"] for c in reg.classes.values()]
    assert len(pcps) == len(set(pcps))


def test_priority_rises_with_urgency(reg):
    order = ["BEST_EFFORT", "BULK_DATA", "REALTIME", "SAFETY_CRITICAL", "REDUNDANT_SAFETY"]
    pcps = [reg.classes[n]["pcp"] for n in order]
    assert pcps == sorted(pcps), f"priorities out of order: {pcps}"


def test_kpi_classes_carry_the_project_targets(reg):
    """5 ms end to end and 2.5 ms jitter are the project's measurable targets;
    if they drift out of the profile the conformance tests stop testing them."""
    for name in ("SAFETY_CRITICAL", "REDUNDANT_SAFETY"):
        c = reg.classes[name]
        assert c["latency_budget_ms"] == 5.0
        assert c["jitter_budget_ms"] == 2.5


def test_only_the_redundant_class_asks_for_frer(reg):
    redundant = {n for n, c in reg.classes.items() if c["redundancy"] != "none"}
    assert redundant == {"REDUNDANT_SAFETY"}


# --- framing ---------------------------------------------------------------

def test_wire_cost_exceeds_payload(reg):
    """A bandwidth plan that charges payload only is optimistic by the overhead
    of every frame, which for small control messages is most of the traffic."""
    s = reg.resolve(StreamRequest("t", "SAFETY_CRITICAL", 64, 10.0))
    assert s.wire_bytes_per_sample > 64 + WIRE_OVERHEAD


def test_large_sample_is_fragmented_and_each_fragment_pays_overhead():
    frames, wire = _frames_for(1_900_000)
    assert frames > 1300, "a 1.9 MB LiDAR scan is not one frame"
    assert wire > 1_900_000 + frames * WIRE_OVERHEAD * 0.9


def test_one_small_sample_is_one_frame():
    frames, _ = _frames_for(100)
    assert frames == 1


def test_minimum_ethernet_payload_is_respected():
    """A 1-byte sample still costs a 64-byte minimum frame on the wire."""
    _, wire = _frames_for(1)
    assert wire >= 46 + WIRE_OVERHEAD


# --- DDS derivation --------------------------------------------------------

def test_deadline_follows_the_stream_period(reg):
    """DEADLINE is how far apart two samples may be, so it scales with the
    period. A 100 Hz stream gets a deadline near 10 ms, not the class's cap."""
    fast = reg.resolve(StreamRequest("f", "SAFETY_CRITICAL", 128, 10.0))
    slow = reg.resolve(StreamRequest("s", "SAFETY_CRITICAL", 128, 50.0))
    assert fast.dds["deadline_ms"] < slow.dds["deadline_ms"]


def test_deadline_is_never_shorter_than_the_period(reg):
    """The bug this rule exists for: a 5 ms deadline on a 10 ms stream makes
    every correctly delivered sample a violation. The monitor reported 499
    misses in 500 healthy samples before DEADLINE was separated from the
    latency budget."""
    for name in reg.classes:
        for period in (1.0, 10.0, 100.0, 1000.0):
            s = reg.resolve(StreamRequest("t", name, 128, period))
            assert s.dds["deadline_ms"] >= period, f"{name} at {period} ms"


def test_latency_budget_is_not_the_deadline(reg):
    """The two are different quantities and the profile must keep them apart."""
    s = reg.resolve(StreamRequest("t", "SAFETY_CRITICAL", 128, 10.0))
    assert s.dds["latency_budget_ms"] == 5.0
    assert s.dds["deadline_ms"] != s.dds["latency_budget_ms"]


def test_a_slow_safety_stream_gets_a_proportionally_slow_deadline(reg):
    """A fault report at 1 Hz deserves a 1.5 s deadline. An earlier version
    capped the deadline per class and refused exactly this stream."""
    s = reg.resolve(StreamRequest("fault", "SAFETY_CRITICAL", 128, 1000.0))
    assert s.dds["deadline_ms"] == pytest.approx(1500.0)


def test_event_driven_streams_have_no_deadline(reg):
    """A latched route or map has no period, so any deadline on it is invented.
    None means DURATION_INFINITE -- the policy is off, not zero."""
    s = reg.resolve(StreamRequest("route", "BULK_DATA", 4096, 50000.0, event_driven=True))
    assert s.dds["deadline_ms"] is None


def test_safety_classes_are_reliable_and_keep_one_sample(reg):
    for name in ("SAFETY_CRITICAL", "REDUNDANT_SAFETY"):
        s = reg.resolve(StreamRequest("t", name, 128, 5.0))
        assert s.dds["reliability"] == "RELIABLE"
        assert s.dds["history"] == {"kind": "KEEP_LAST", "depth": 1}


def test_realtime_is_best_effort(reg):
    """Retransmitting a sensor sample that a newer one has already replaced
    spends the link on stale data."""
    s = reg.resolve(StreamRequest("t", "REALTIME", 4096, 10.0))
    assert s.dds["reliability"] == "BEST_EFFORT"


def test_durability_override_reaches_the_dds_qos(reg):
    s = reg.resolve(StreamRequest("map", "BULK_DATA", 8_000_000, 60000.0,
                                  durability="transient_local"))
    assert s.dds["durability"] == "TRANSIENT_LOCAL"


def test_transport_priority_equals_pcp(reg):
    """The chain TRANSPORT_PRIORITY -> SO_PRIORITY -> egress-qos-map -> PCP only
    works if the first and last agree."""
    for name in reg.classes:
        s = reg.resolve(StreamRequest("t", name, 128, 20.0))
        assert s.dds["transport_priority"] == s.pcp


# --- admission -------------------------------------------------------------

def test_t1s_refuses_sensor_payload(reg):
    with pytest.raises(AdmissionError, match="not admissible"):
        reg.resolve(StreamRequest("lidar", "REALTIME", 1_900_000, 100.0, link="t1s_10m"))


def test_t1s_accepts_control_and_state(reg):
    s = reg.resolve(StreamRequest("cmd", "SAFETY_CRITICAL", 64, 20.0, link="t1s_10m"))
    assert s.rate_mbps < 10


def test_t1s_degrades_tas_to_strict_priority_because_it_has_no_gates(reg):
    s = reg.resolve(StreamRequest("cmd", "SAFETY_CRITICAL", 64, 20.0, link="t1s_10m"))
    assert s.tsn["shaper"] == "strict_priority"


def test_redundant_class_is_refused_on_a_link_without_frer(reg):
    with pytest.raises(AdmissionError, match="802.1CB"):
        reg.resolve(StreamRequest("cmd", "REDUNDANT_SAFETY", 64, 20.0, link="t1s_10m"))


def test_a_stream_too_big_for_its_link_is_refused(reg):
    with pytest.raises(AdmissionError):
        reg.resolve(StreamRequest("huge", "REALTIME", 2_000_000, 1.0, link="multigige_1g"))


def test_frer_is_charged_twice_at_admission(reg):
    """Replication doubles what crosses the network. A plan that counts it once
    passes on paper and saturates in the lab."""
    s = reg.resolve(StreamRequest("cmd", "REDUNDANT_SAFETY", 1000, 1.0))
    single = reg.resolve(StreamRequest("cmd2", "SAFETY_CRITICAL", 1000, 1.0))
    a = reg.admit([s], "multigige_1g")
    b = reg.admit([single], "multigige_1g")
    assert a["offered_mbps"] == pytest.approx(b["offered_mbps"] * 2, rel=1e-6)


def test_admission_ceiling_is_below_the_line_rate(reg):
    streams = [reg.resolve(StreamRequest(f"s{i}", "REALTIME", 1400, 0.1))
               for i in range(8)]
    v = reg.admit(streams, "multigige_1g")
    assert v["utilisation"] > 0.75
    assert not v["admitted"] and "ceiling" in v["reason"]


# --- 802.1Qbv --------------------------------------------------------------

def test_gate_control_list_fits_its_cycle(reg):
    streams = [reg.resolve(StreamRequest("c", "SAFETY_CRITICAL", 128, 5.0)),
               reg.resolve(StreamRequest("r", "REDUNDANT_SAFETY", 128, 5.0))]
    gcl = reg.build_gate_control_list(streams, 1000)
    total = sum(e["time_interval_us"] for e in gcl["entries"])
    assert total == pytest.approx(gcl["cycle_time_us"], rel=1e-9)


def test_gate_control_list_includes_a_guard_band(reg):
    streams = [reg.resolve(StreamRequest("c", "SAFETY_CRITICAL", 128, 5.0))]
    gcl = reg.build_gate_control_list(streams, 1000)
    assert gcl["guard_band_us"] > 0


def test_impossible_schedule_is_an_error_not_a_rounding(reg):
    """A cycle too short for its own guard band must fail loudly. Rounding the
    window down instead produces a schedule the hardware accepts and misses."""
    streams = [reg.resolve(StreamRequest("c", "SAFETY_CRITICAL", 1400, 5.0))]
    with pytest.raises(AdmissionError, match="cycle"):
        reg.build_gate_control_list(streams, 1000, cycle_ms=0.001)


def test_cycle_is_the_shortest_period_so_every_stream_gets_a_window(reg):
    streams = [reg.resolve(StreamRequest("a", "SAFETY_CRITICAL", 128, 20.0)),
               reg.resolve(StreamRequest("b", "REDUNDANT_SAFETY", 128, 4.0))]
    gcl = reg.build_gate_control_list(streams, 1000)
    assert gcl["cycle_time_us"] == pytest.approx(4000.0)


# --- 802.1Qav --------------------------------------------------------------

def test_send_slope_is_negative(reg):
    c = reg.cbs_parameters(100, 1000, 1500)
    assert c["send_slope_bps"] < 0


def test_idle_slope_is_in_bits_per_second(reg):
    """ieee802-dot1q-cbsa:admin-idle-slope is defined in bits/second. Carrying
    kilobits and converting at the emitter is how a shaper ends up reserving a
    thousandth of what was asked for."""
    c = reg.cbs_parameters(165.5, 1000, 1500)
    assert c["idle_slope_bps"] == pytest.approx(165_500_000, rel=1e-6)


def test_preemption_lowers_the_credit_a_class_must_accrue(reg):
    with_pre = reg.cbs_parameters(100, 1000, 1500, preemptable_interference=True)
    without = reg.cbs_parameters(100, 1000, 1500, preemptable_interference=False)
    assert with_pre["hi_credit_bits"] < without["hi_credit_bits"]


# --- capability gate -------------------------------------------------------

def test_missing_frer_refuses_rather_than_downgrades():
    reg = QosRegistry()
    gate = CapabilityGate()
    s = reg.resolve(StreamRequest("cmd", "REDUNDANT_SAFETY", 128, 5.0))
    plan = gate.plan([s], "tsn_bridge_no_frer", registry=reg)
    assert not plan.ok
    assert any(r.capability == "frer" for r in plan.refusals)
    assert s.tsn["redundancy"] == "frer_dual", "refusal must not mutate the stream"


def test_unmeasured_capability_is_refused_differently_from_absent_one():
    reg = QosRegistry()
    gate = CapabilityGate()
    s = reg.resolve(StreamRequest("cmd", "REDUNDANT_SAFETY", 128, 5.0))
    unknown = gate.plan([s], "unverified", registry=reg)
    absent = gate.plan([s], "tsn_bridge_no_frer", registry=reg)
    assert any("미확정" in r.reason for r in unknown.refusals)
    assert not any("미확정" in r.reason for r in absent.refusals)


def test_a_waiver_deploys_but_marks_the_stream_degraded():
    reg = QosRegistry()
    gate = CapabilityGate()
    s = reg.resolve(StreamRequest("cmd", "REDUNDANT_SAFETY", 128, 5.0))
    w = [Waiver("*", "tsn_bridge_no_frer", "frer", "single path for this bench run", "tester")]
    plan = gate.plan([s], "tsn_bridge_no_frer", registry=reg, waivers=w)
    assert plan.ok and plan.degraded
    assert s.tsn["redundancy"] == "none", "a waiver must remove the feature, not hide it"
    assert "frer" in s.tsn["degraded"]


def test_waiver_for_a_different_device_does_not_apply():
    reg = QosRegistry()
    gate = CapabilityGate()
    s = reg.resolve(StreamRequest("cmd", "REDUNDANT_SAFETY", 128, 5.0))
    w = [Waiver("*", "tsn_bridge_no_preemption", "frer", "irrelevant", "tester")]
    assert not gate.plan([s], "tsn_bridge_no_frer", registry=reg, waivers=w).ok


def test_a_class_may_have_redundancy_but_not_preemption():
    reg = QosRegistry()
    gate = CapabilityGate()
    s = reg.resolve(StreamRequest("cmd", "REDUNDANT_SAFETY", 128, 5.0))
    refusals = gate.check(s, "tsn_bridge_no_preemption")
    caps = {r.capability for r in refusals}
    assert "frer" not in caps and "preemption" in caps
