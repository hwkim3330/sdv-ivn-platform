"""Per-stream measurement core: pure arithmetic, no DDS, no ROS.

Split out from the binding on purpose. The statistics are what the conformance
report stands on, so they have to be testable without a bench, a simulator or a
running middleware. `collector.py` feeds this from real subscriptions.

WHAT LATENCY MEANS HERE, AND WHEN IT DOES NOT MEAN IT
-----------------------------------------------------
End-to-end latency is (receive time on the listener) - (source timestamp set by
the talker). That subtraction is only meaningful if the two clocks agree. On one
host they do. Across the network they agree only as well as gPTP has
synchronised them, so a latency measured across two hosts carries the time
synchronisation error inside it.

This module therefore refuses to report latency unless the caller states the
clock relationship (`clock_sync`). `SAME_HOST` and `PTP_LOCKED` produce a
number; `UNSYNCHRONISED` produces None and a reason. A latency figure whose
clock basis is unknown is not a conservative estimate, it is an unknown
quantity with a plausible-looking value.

Inter-arrival and its jitter need no common clock: both timestamps come from
the listener. They are always reported.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum


class ClockSync(str, Enum):
    SAME_HOST = "same_host"          # one machine, one clock
    PTP_LOCKED = "ptp_locked"        # gPTP synchronised, offset bounded
    UNSYNCHRONISED = "unsynchronised"


@dataclass
class Distribution:
    min: float
    p50: float
    p99: float
    max: float
    mean: float
    stdev: float
    n: int

    def as_dict(self) -> dict:
        return {"min": round(self.min, 3), "p50": round(self.p50, 3),
                "p99": round(self.p99, 3), "max": round(self.max, 3),
                "mean": round(self.mean, 3), "stdev": round(self.stdev, 3), "n": self.n}


def distribution(values: list[float]) -> Distribution | None:
    """Percentiles by nearest-rank on the sorted sample.

    p99 of fewer than 100 samples is the maximum, which is honest but weak; the
    sample count travels with the distribution so a reader can see that.
    """
    if not values:
        return None
    v = sorted(values)
    n = len(v)
    mean = sum(v) / n
    var = sum((x - mean) ** 2 for x in v) / n if n > 1 else 0.0

    def rank(q: float) -> float:
        return v[min(n - 1, max(0, math.ceil(q * n) - 1))]

    return Distribution(min=v[0], p50=rank(0.5), p99=rank(0.99), max=v[-1],
                        mean=mean, stdev=math.sqrt(var), n=n)


@dataclass
class StreamStats:
    """Accumulator for one stream.

    Times are seconds as floats. Reported values are microseconds, because the
    budgets this is judged against are milliseconds and a microsecond figure
    keeps three significant digits without scientific notation.
    """
    name: str
    qos_class: str = ""
    deadline_ms: float | None = None
    latency_budget_ms: float | None = None
    jitter_budget_ms: float | None = None
    clock_sync: ClockSync = ClockSync.SAME_HOST

    latencies_us: list[float] = field(default_factory=list)
    inter_arrival_us: list[float] = field(default_factory=list)
    payload_bytes: list[int] = field(default_factory=list)

    deadline_miss: int = 0
    liveliness_lost: int = 0
    sample_lost: int = 0
    duplicates_discarded: int = 0      # 802.1CB elimination, when observable

    _last_rx: float | None = None
    _first_rx: float | None = None
    _last_seq: int | None = None

    def on_sample(self, rx_time: float, source_time: float | None = None,
                  payload_bytes: int | None = None, seq: int | None = None) -> None:
        if self._first_rx is None:
            self._first_rx = rx_time
        if self._last_rx is not None:
            gap_us = (rx_time - self._last_rx) * 1e6
            self.inter_arrival_us.append(gap_us)
            # A deadline is missed when the gap exceeds it, which is what the
            # DDS DEADLINE listener would report; counted here too so the
            # measurement stands on its own if the listener is not wired.
            if self.deadline_ms is not None and gap_us > self.deadline_ms * 1000.0:
                self.deadline_miss += 1
        self._last_rx = rx_time

        if source_time is not None and self.clock_sync is not ClockSync.UNSYNCHRONISED:
            self.latencies_us.append((rx_time - source_time) * 1e6)
        if payload_bytes is not None:
            self.payload_bytes.append(payload_bytes)
        if seq is not None:
            if self._last_seq is not None and seq > self._last_seq + 1:
                self.sample_lost += seq - self._last_seq - 1
            self._last_seq = seq

    # -- derived ------------------------------------------------------------
    @property
    def duration_s(self) -> float:
        if self._first_rx is None or self._last_rx is None:
            return 0.0
        return self._last_rx - self._first_rx

    def jitter_us(self) -> Distribution | None:
        """Jitter as the deviation of end-to-end latency, which is what the
        2.5 ms budget is about.

        Not RFC 3550 interarrival jitter: that is a smoothed estimate designed
        for media playout, and reporting it against a hard bound understates
        the tail. Here jitter is |latency - median latency| per sample, so its
        maximum is the worst deviation actually seen.
        """
        if len(self.latencies_us) < 2:
            return None
        d = distribution(self.latencies_us)
        return distribution([abs(x - d.p50) for x in self.latencies_us])

    def arrival_jitter_us(self) -> Distribution | None:
        """Deviation of inter-arrival from its median. Needs no common clock,
        so this is the figure that survives an unsynchronised setup."""
        if len(self.inter_arrival_us) < 2:
            return None
        d = distribution(self.inter_arrival_us)
        return distribution([abs(x - d.p50) for x in self.inter_arrival_us])

    def throughput_mbps(self) -> float | None:
        if not self.payload_bytes or self.duration_s <= 0:
            return None
        return sum(self.payload_bytes) * 8 / self.duration_s / 1e6

    def within_budget(self) -> bool | None:
        """None when the question cannot be answered, not False.

        A stream whose latency was never measured has not passed and has not
        failed. Reporting False would make an unmeasured system look broken;
        reporting True would make it look verified. Both are lies.
        """
        lat = distribution(self.latencies_us)
        jit = self.jitter_us()
        if self.latency_budget_ms is None and self.jitter_budget_ms is None:
            return None
        checks = []
        if self.latency_budget_ms is not None:
            if lat is None:
                return None
            checks.append(lat.max <= self.latency_budget_ms * 1000.0)
        if self.jitter_budget_ms is not None:
            if jit is None:
                return None
            checks.append(jit.max <= self.jitter_budget_ms * 1000.0)
        return all(checks)

    def report(self) -> dict:
        lat = distribution(self.latencies_us)
        note = None
        if self.clock_sync is ClockSync.UNSYNCHRONISED:
            note = ("talker and listener clocks are not synchronised; end-to-end "
                    "latency is not reported. Inter-arrival figures are unaffected.")
        elif self.clock_sync is ClockSync.PTP_LOCKED:
            note = "latency includes the residual gPTP offset between the two hosts"
        return {
            "stream": self.name,
            "qos_class": self.qos_class,
            "duration_s": round(self.duration_s, 3),
            "samples": len(self.inter_arrival_us) + 1 if self._first_rx is not None else 0,
            "clock_sync": self.clock_sync.value,
            "note": note,
            "latency_us": lat.as_dict() if lat else None,
            "jitter_us": (self.jitter_us().as_dict() if self.jitter_us() else None),
            "inter_arrival_us": (distribution(self.inter_arrival_us).as_dict()
                                 if self.inter_arrival_us else None),
            "arrival_jitter_us": (self.arrival_jitter_us().as_dict()
                                  if self.arrival_jitter_us() else None),
            "deadline_miss": self.deadline_miss,
            "liveliness_lost": self.liveliness_lost,
            "sample_lost": self.sample_lost,
            "duplicates_discarded": self.duplicates_discarded,
            "throughput_mbps": (round(self.throughput_mbps(), 4)
                                if self.throughput_mbps() is not None else None),
            "budget_latency_ms": self.latency_budget_ms,
            "budget_jitter_ms": self.jitter_budget_ms,
            "within_budget": self.within_budget(),
        }


class Monitor:
    """A set of stream accumulators, built from a resolved profile."""

    def __init__(self, clock_sync: ClockSync = ClockSync.SAME_HOST):
        self.clock_sync = clock_sync
        self.streams: dict[str, StreamStats] = {}

    @classmethod
    def from_profile(cls, resolved, clock_sync: ClockSync = ClockSync.SAME_HOST) -> "Monitor":
        m = cls(clock_sync)
        for s in resolved:
            m.streams[s.name] = StreamStats(
                name=s.name, qos_class=s.request.qos_class,
                deadline_ms=s.dds.get("deadline_ms"),
                latency_budget_ms=s.cls.get("latency_budget_ms"),
                jitter_budget_ms=s.cls.get("jitter_budget_ms"),
                clock_sync=clock_sync)
        return m

    def on_sample(self, stream: str, rx_time: float, **kw) -> None:
        st = self.streams.get(stream)
        if st is None:
            st = self.streams[stream] = StreamStats(name=stream, clock_sync=self.clock_sync)
        st.on_sample(rx_time, **kw)

    def report(self) -> list[dict]:
        return [s.report() for s in self.streams.values()]

    def table(self, only_budgeted: bool = False) -> str:
        rows = [s for s in self.streams.values()
                if not only_budgeted or s.latency_budget_ms is not None]
        out = [f"{'스트림':46s} {'등급':17s} {'p99 지연':>10s} {'최대 지터':>10s} "
               f"{'마감위반':>8s} {'판정':>6s}"]
        out.append("-" * 104)
        for s in sorted(rows, key=lambda x: x.name):
            lat = distribution(s.latencies_us)
            jit = s.jitter_us()
            verdict = {True: "통과", False: "실패", None: "—"}[s.within_budget()]
            out.append(f"{s.name[:46]:46s} {s.qos_class:17s} "
                       f"{(f'{lat.p99:.0f} us' if lat else '—'):>10s} "
                       f"{(f'{jit.max:.0f} us' if jit else '—'):>10s} "
                       f"{s.deadline_miss:8d} {verdict:>6s}")
        return "\n".join(out)
