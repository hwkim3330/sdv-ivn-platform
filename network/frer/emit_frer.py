"""802.1CB configuration, standard models only.

The standard splits this the way hardware splits it, and a device may implement
one half and not the other:

    Clause 6   ieee802-dot1cb-stream-identification   which frames are a stream
    Clause 7   ieee802-dot1cb-frer                    replicate, recover, discard

A bridge with only the first recognises a redundant stream and does nothing
about it. Emitting both halves and letting the second write fail is not an
acceptable outcome for a safety class: the network then identifies the stream,
forwards one copy, and reports success. middleware/core/deploy.py refuses the
stream instead. This module only produces data.

Node names here were checked against the published model rather than written
from memory, after an earlier version addressed a container called
`frame-replication-and-elimination` that does not exist -- the real one is
`frer` -- and put a `priority` leaf into `ip-stream-identification`, which has
no such leaf.
"""
from __future__ import annotations

import math
from typing import Any

SI = "/ieee802-dot1cb-stream-identification:stream-identity[index='{index}']"
FRER = "/ieee802-dot1cb-frer:frer"
SEQ_GEN = FRER + "/sequence-generation[index='{index}']"
SEQ_REC = FRER + "/sequence-recovery[index='{index}']"
# Both of these are keyed by (port, direction-out-facing), not by an index.
# The distinction matters: splitting is a property of an egress port, so one
# entry exists per port that a stream fans out to, and addressing them by a
# running index writes the wrong list entirely.
SEQ_ID = FRER + "/sequence-identification[port='{port}'][direction-out-facing='{dir}']"
STREAM_SPLIT = FRER + "/stream-split[port='{port}'][direction-out-facing='{dir}']"


def stream_identity(stream, index: int, in_input: list[str], in_output: list[str],
                    out_input: list[str] | None = None,
                    out_output: list[str] | None = None) -> dict[str, Any]:
    """Clause 6. Identify the stream by its VLAN and destination.

    `ip-stream-identification` carries no priority leaf; the class's PCP reaches
    the wire through the VLAN tag, not through stream identification. Matching
    here is on VLAN and addresses.
    """
    f = stream.tsn["frer"]
    body: dict[str, Any] = {
        "handle": f["stream_handle"],
        "in-facing": {"input-port-list": in_input, "output-port-list": in_output},
        "null-stream-identification": {
            "destination-mac": f.get("destination_mac", "01-00-5e-00-00-00"),
            "tagged": "tagged",
            "vlan": stream.vlan,
        },
    }
    if out_input or out_output:
        body["out-facing"] = {"input-port-list": out_input or [],
                              "output-port-list": out_output or []}
    return {SI.format(index=index): body}


def sequence_generation(stream, index: int, out_facing: bool = True) -> dict[str, Any]:
    """Clause 7, talker side: attach a sequence number so the far end can
    discard the duplicate. Refused on devices without ieee802-dot1cb-frer."""
    f = stream.tsn["frer"]
    return {SEQ_GEN.format(index=index): {
        "stream": [f["stream_handle"]],
        "direction-out-facing": out_facing,
        "reset": False,
    }}


def sequence_recovery(stream, index: int, ports: list[str],
                      out_facing: bool = True) -> dict[str, Any]:
    """Clause 7, listener side.

    Two values decide whether redundancy helps or does nothing.

    `history-length` shorter than the burst discards good frames as out of
    window. `reset-timeout` shorter than the difference in delay between the
    two paths lets the recovery function reset between the two copies of one
    frame, so both are accepted and the duplicate reaches the application.

    Latent error detection is switched on because the failure it catches is the
    silent one: one path dies, the other keeps delivering, and nothing looks
    wrong until the second path dies too.
    """
    f = stream.tsn["frer"]
    rec = f["sequence_recovery"]
    return {SEQ_REC.format(index=index): {
        "stream": [f["stream_handle"]],
        "port": ports,
        "direction-out-facing": out_facing,
        "algorithm": {"vector": {}},          # presence container: vector recovery
        "history-length": rec["history_length"],
        "reset-timeout": int(rec["reset_timeout_ms"]),
        "invalid-sequence-value": 0,
        "take-no-sequence": rec["take_no_sequence"],
        "individual-recovery": False,
        "latent-error-detection": True,
        "latent-error-detection-parameters": {
            "difference": 10,
            "period": 2000,
            "paths": f.get("paths", 2),
            "reset-period": 30000,
        },
        "reset": False,
    }}


def stream_split(stream, index: int, out_ports: list[str],
                 out_facing: bool = True) -> dict[str, Any]:
    """The replication itself: one entry per egress port the stream fans out to.

    Without this the sequence number is generated and the frame still travels a
    single path. This is the node that actually makes a stream redundant, and
    it is the one most often left out, because sequence generation alone looks
    like it did something.

    `input-id` is the handle arriving at the port; `output-id` is the handle the
    copy leaves with, so the two copies are distinguishable downstream.
    """
    f = stream.tsn["frer"]
    out: dict[str, Any] = {}
    for i, port in enumerate(out_ports):
        out[STREAM_SPLIT.format(port=port, dir=str(out_facing).lower())] = {
            "port": port,
            "direction-out-facing": out_facing,
            "input-id": f["stream_handle"],
            "output-id": f["stream_handle"] + i + 1,
        }
    return out


def sequence_identification(stream, index: int, ports: list[str],
                            out_facing: bool = True) -> dict[str, Any]:
    """Where the sequence number is encoded into the frame, and how.

    R-TAG is the 802.1CB encapsulation; the alternatives in the model belong to
    other sequencing schemes this profile does not use.
    """
    f = stream.tsn["frer"]
    return {
        SEQ_ID.format(port=p, dir=str(out_facing).lower()): {
            "port": p,
            "direction-out-facing": out_facing,
            "stream": [f["stream_handle"]],
            "active": True,
            "encapsulation": "ieee802-dot1cb-frer:r-tag",
        }
        for p in ports
    }


def recovery_window(period_ms: float) -> dict[str, Any]:
    """The two window values derived from a stream's period alone, exposed so
    the profile and the tests can agree on them without an emitter."""
    burst = max(2, math.ceil(2.0 / period_ms))
    return {"history_length": max(4, burst * 2),
            "reset_timeout_ms": max(10.0, period_ms * 4)}
