"""Tests for the measurement core.

The point of these is that the conformance report cannot overstate what was
measured. Most of them are about the difference between 'zero' and 'unknown'.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dds.monitor.stats import StreamStats, Monitor, ClockSync, distribution  # noqa: E402


def feed(st, n=200, period=0.010, latency=0.001, jitter=0.0):
    import random
    random.seed(11)
    t = 0.0
    for i in range(n):
        t += period * (1 + random.uniform(-jitter, jitter))
        st.on_sample(rx_time=t, source_time=t - latency, payload_bytes=128, seq=i)
    return st


def test_distribution_of_nothing_is_none():
    assert distribution([]) is None


def test_percentile_uses_nearest_rank():
    d = distribution(list(range(1, 101)))
    assert d.p50 == 50 and d.p99 == 99 and d.max == 100


def test_unmeasured_latency_is_none_not_zero():
    st = StreamStats("s", latency_budget_ms=5.0)
    assert st.report()["latency_us"] is None


def test_unmeasured_verdict_is_none_not_false():
    """A stream that was never measured has not passed and has not failed.
    False would make an unmeasured system look broken."""
    st = StreamStats("s", latency_budget_ms=5.0, jitter_budget_ms=2.5)
    assert st.within_budget() is None


def test_unsynchronised_clocks_suppress_latency():
    st = StreamStats("s", clock_sync=ClockSync.UNSYNCHRONISED, latency_budget_ms=5.0)
    feed(st)
    r = st.report()
    assert r["latency_us"] is None
    assert "not synchronised" in r["note"]
    assert r["inter_arrival_us"] is not None, "arrival figures need no common clock"


def test_ptp_locked_latency_is_reported_with_a_caveat():
    st = StreamStats("s", clock_sync=ClockSync.PTP_LOCKED, latency_budget_ms=5.0)
    feed(st)
    r = st.report()
    assert r["latency_us"] is not None
    assert "gPTP" in r["note"]


def test_a_healthy_stream_misses_no_deadline():
    """The regression this file exists for: a 100 Hz stream against a deadline
    derived from its own period must report zero misses."""
    st = StreamStats("s", deadline_ms=15.0)
    feed(st, period=0.010, jitter=0.05)
    assert st.deadline_miss == 0


def test_a_stalled_publisher_is_caught():
    st = StreamStats("s", deadline_ms=15.0)
    feed(st, n=50, period=0.010)
    st.on_sample(rx_time=st._last_rx + 0.5)
    assert st.deadline_miss == 1


def test_sequence_gaps_count_as_lost_samples():
    st = StreamStats("s")
    for i, seq in enumerate([0, 1, 2, 7, 8]):
        st.on_sample(rx_time=i * 0.01, seq=seq)
    assert st.sample_lost == 4


def test_jitter_is_deviation_of_latency_not_smoothed():
    """RFC 3550 interarrival jitter is smoothed for media playout and
    understates the tail. Against a hard 2.5 ms bound the worst deviation
    actually observed is the figure that matters."""
    st = StreamStats("s")
    base = 0.001
    for i in range(100):
        extra = 0.004 if i == 50 else 0.0
        st.on_sample(rx_time=i * 0.01, source_time=i * 0.01 - base - extra)
    j = st.jitter_us()
    assert j.max == pytest.approx(4000, rel=0.02), "the one outlier must survive"


def test_within_budget_fails_on_the_maximum_not_the_median():
    st = StreamStats("s", latency_budget_ms=5.0)
    for i in range(100):
        extra = 0.010 if i == 3 else 0.0
        st.on_sample(rx_time=i * 0.01, source_time=i * 0.01 - 0.001 - extra)
    assert st.within_budget() is False


def test_throughput_needs_a_duration():
    st = StreamStats("s")
    st.on_sample(rx_time=0.0, payload_bytes=1000)
    assert st.throughput_mbps() is None


def test_monitor_built_from_a_profile_carries_the_budgets():
    sys.path.insert(0, str(ROOT / "middleware"))
    sys.path.insert(0, str(ROOT / "profiles" / "autoware"))
    from qos.mapper import QosRegistry
    import build_profile
    reg = QosRegistry()
    resolved, _ = build_profile.build(build_profile.load_catalog(), reg)
    m = Monitor.from_profile(resolved)
    ctrl = m.streams["/external/selected/control_cmd"]
    assert ctrl.latency_budget_ms == 5.0 and ctrl.jitter_budget_ms == 2.5
    assert ctrl.deadline_ms >= 10.0, "deadline must not be shorter than the period"


def test_table_marks_unmeasured_streams_with_a_dash():
    m = Monitor()
    m.streams["a"] = StreamStats("a", qos_class="SAFETY_CRITICAL",
                                 latency_budget_ms=5.0, jitter_budget_ms=2.5)
    assert "—" in m.table()
