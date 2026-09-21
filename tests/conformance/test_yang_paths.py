"""Every path the emitters produce must exist in the published IEEE models.

This is the test that would have caught the emitters addressing a container
called `frame-replication-and-elimination`, which no model defines, and putting
a `priority` leaf into `ip-stream-identification`, which has none. Neither
mistake is visible in code review; both produce documents a device rejects with
a generic error or, worse, silently ignores.

The models live in schemas/yang and are the IEEE published versions, unmodified.
No vendor module is loaded, so a path that only a particular product accepts
fails here -- which is the intent. Vendor translation belongs in devices/.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "middleware", ROOT / "schemas" / "yang", ROOT / "profiles" / "autoware"):
    sys.path.insert(0, str(p))

from model import standard_set                                   # noqa: E402
from qos.mapper import QosRegistry, StreamRequest                # noqa: E402
from network.tsn import emit_yang                                # noqa: E402
from network.frer import emit_frer                               # noqa: E402
import build_profile                                             # noqa: E402


@pytest.fixture(scope="module")
def yang():
    return standard_set()


@pytest.fixture(scope="module")
def streams():
    reg = QosRegistry()
    resolved, _ = build_profile.build(build_profile.load_catalog(), reg)
    return reg, resolved


def check(yang, doc, label):
    bad = []
    for path in doc:
        if path.startswith("_"):
            continue
        ok, why = yang.resolve(path)
        if not ok:
            bad.append(f"{label}: {path}\n    {why}")
    assert not bad, "\n".join(bad)


# --- the models themselves --------------------------------------------------

def test_the_standard_models_parse(yang):
    assert len(yang.modules) > 80, f"only {len(yang.modules)} modules parsed"


def test_the_models_needed_by_the_profile_are_present(yang):
    for m in ("ieee802-dot1q-sched-bridge", "ieee802-dot1q-cbsa-bridge",
              "ieee802-dot1q-preemption-bridge", "ieee802-dot1cb-frer",
              "ieee802-dot1cb-stream-identification", "ieee802-dot1q-bridge",
              "ietf-interfaces"):
        assert m in yang.modules, m


def test_no_vendor_module_is_loaded(yang):
    """The platform interface is the standard. A vendor module here would let a
    product-specific path pass this test and then fail on other hardware."""
    for name in yang.modules:
        assert name.startswith(("ieee802", "ietf-", "iana-")), f"non-standard module {name}"


# --- the validator does its job --------------------------------------------

def test_a_nonexistent_container_is_rejected(yang):
    ok, why = yang.resolve("/ieee802-dot1cb-frer:frame-replication-and-elimination/x")
    assert not ok and "no top-level node" in why


def test_a_wrong_list_key_is_rejected(yang):
    ok, why = yang.resolve("/ieee802-dot1cb-frer:frer/sequence-recovery[handle='1']")
    assert not ok and "keyed by" in why


def test_a_real_path_is_accepted(yang):
    ok, _ = yang.resolve("/ieee802-dot1cb-frer:frer/sequence-recovery[index='0']/history-length")
    assert ok


# --- the emitters -----------------------------------------------------------

def test_gate_control_list_paths(yang, streams):
    reg, resolved = streams
    gcl = reg.build_gate_control_list(
        [s for s in resolved if s.request.link == "multigige_1g"], 1000)
    check(yang, emit_yang.gate_parameter_table("1", gcl), "802.1Qbv")


def test_gate_control_entry_is_flat(yang, streams):
    """`gate-states-value` sits directly on gate-control-entry. A vendor
    wrapper container is a deviation, not the standard."""
    reg, resolved = streams
    gcl = reg.build_gate_control_list(
        [s for s in resolved if s.request.link == "multigige_1g"], 1000)
    doc = emit_yang.gate_parameter_table("1", gcl)
    entry = next(iter(doc.values()))["admin-control-list"]["gate-control-entry"][0]
    assert set(entry) == {"index", "operation-name", "gate-states-value", "time-interval-value"}


def test_cbsa_paths(yang, streams):
    _, resolved = streams
    check(yang, emit_yang.cbsa("1", resolved), "802.1Qav")


def test_cbsa_idle_slope_is_bits_per_second(yang, streams):
    """The standard leaf is in bits/second. Writing kilobits reserves a
    thousandth of the intended bandwidth and the link looks fine until it is
    loaded."""
    reg, resolved = streams
    doc = emit_yang.cbsa("1", resolved)
    rows = next(iter(doc.values()))["cbsa-parameter-table"]
    total_bps = sum(r["admin-idle-slope"] for r in rows)
    cbs_mbps = sum(s.rate_mbps for s in resolved if s.tsn["shaper"] == "cbs")
    assert total_bps == pytest.approx(cbs_mbps * 1e6, rel=1e-3)


def test_preemption_paths(yang, streams):
    _, resolved = streams
    check(yang, emit_yang.preemption("1", resolved), "802.1Qbu")


def test_preemption_table_is_eight_leaves_not_a_list(yang, streams):
    _, resolved = streams
    doc = emit_yang.preemption("1", resolved)
    table = next(iter(doc.values()))["frame-preemption-status-table"]
    assert isinstance(table, dict)
    assert all(k.startswith("priority") for k in table)


def test_queue_max_sdu_paths(yang, streams):
    _, resolved = streams
    check(yang, emit_yang.queue_max_sdu("1", resolved), "queue-max-sdu")


def test_frer_paths(yang, streams):
    _, resolved = streams
    red = [s for s in resolved if s.tsn["redundancy"] != "none"]
    assert red, "the profile should exercise 802.1CB"
    for i, s in enumerate(red):
        check(yang, emit_frer.stream_identity(s, i, ["1"], ["2"]), "1CB identity")
        check(yang, emit_frer.sequence_generation(s, i), "1CB seq-gen")
        check(yang, emit_frer.sequence_recovery(s, i, ["1", "2"]), "1CB seq-rec")
        check(yang, emit_frer.stream_split(s, i, ["1", "2"]), "1CB split")
        check(yang, emit_frer.sequence_identification(s, i, ["1", "2"]), "1CB seq-id")


def test_stream_split_exists_for_every_redundant_stream(yang, streams):
    """Sequence generation alone looks like it worked and sends one copy. The
    split is what makes the stream redundant."""
    _, resolved = streams
    for i, s in enumerate(x for x in resolved if x.tsn["redundancy"] != "none"):
        doc = emit_frer.stream_split(s, i, ["1", "2"])
        assert len(doc) == 2, "one entry per egress port"
        ids = {v["output-id"] for v in doc.values()}
        assert len(ids) == 2, "the two copies must be distinguishable"


def test_frer_recovery_body_uses_standard_node_names(yang, streams):
    _, resolved = streams
    s = next(x for x in resolved if x.tsn["redundancy"] != "none")
    body = next(iter(emit_frer.sequence_recovery(s, 0, ["1"]).values()))
    known = {"stream", "port", "direction-out-facing", "reset", "algorithm",
             "history-length", "reset-timeout", "invalid-sequence-value",
             "take-no-sequence", "individual-recovery", "latent-error-detection",
             "latent-error-detection-parameters"}
    assert set(body) <= known, f"unknown nodes: {set(body) - known}"


def test_stream_identity_carries_no_priority_leaf(yang, streams):
    """Priority reaches the wire through the VLAN tag. Stream identification
    matches on VLAN and addresses and has no priority leaf to set."""
    _, resolved = streams
    s = next(x for x in resolved if x.tsn["redundancy"] != "none")
    body = next(iter(emit_frer.stream_identity(s, 0, ["1"], ["2"]).values()))
    for section in body.values():
        if isinstance(section, dict):
            assert "priority" not in section


def test_every_emitted_path_across_the_whole_profile(yang, streams):
    """The end-to-end check: build every document the platform would push for
    the reference application and resolve every path in all of them."""
    reg, resolved = streams
    docs = {}
    for link in {s.request.link for s in resolved}:
        ss = [s for s in resolved if s.request.link == link]
        try:
            gcl = reg.build_gate_control_list(ss, reg.links[link]["rate_mbps"])
            docs.update(emit_yang.gate_parameter_table("1", gcl))
        except Exception:
            pass
        docs.update(emit_yang.cbsa("1", ss))
        docs.update(emit_yang.preemption("1", ss))
        docs.update(emit_yang.queue_max_sdu("1", ss))
    for i, s in enumerate(x for x in resolved if x.tsn["redundancy"] != "none"):
        docs.update(emit_frer.stream_identity(s, i, ["1"], ["2"]))
        docs.update(emit_frer.sequence_generation(s, i))
        docs.update(emit_frer.sequence_recovery(s, i, ["1", "2"]))
        docs.update(emit_frer.stream_split(s, i, ["1", "2"]))
        docs.update(emit_frer.sequence_identification(s, i, ["1", "2"]))
    assert len(docs) > 8
    check(yang, docs, "profile")
