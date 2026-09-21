"""802.1CB configuration, split into the two halves the hardware splits it into.

Clause 6 (stream identification) and Clause 7 (sequence generation, replication,
recovery, elimination) are separate YANG models and, on the bench hardware, a
device may implement one and not the other. The Microchip parts here carry
ieee802-dot1cb-stream-identification but not ieee802-dot1cb-frer, so they can
recognise a redundant stream and do nothing about it.

Emitting both halves and letting the write fail on the second is not acceptable
for a safety class: the resulting network identifies the stream, forwards one
copy, and reports success. The planner in middleware/core/deploy.py refuses the
stream instead. This module only produces the data; refusal is that module's job.
"""
from __future__ import annotations

from typing import Any

SI_PATH = "/ieee802-dot1cb-stream-identification:stream-identity[index={index}]"
FRER_SEQ_GEN = "/ieee802-dot1cb-frer:frame-replication-and-elimination/sequence-generation[index={index}]"
FRER_SEQ_REC = "/ieee802-dot1cb-frer:frame-replication-and-elimination/sequence-recovery[index={index}]"


def stream_identity(stream, index: int, in_facing_ports: list[str],
                    out_facing_ports: list[str]) -> dict[str, Any]:
    """Clause 6. This much every 802.1CB-aware device here can take."""
    f = stream.tsn["frer"]
    return {
        SI_PATH.format(index=index): {
            "handle": f["stream_handle"],
            "in-facing": {"input-port-list": in_facing_ports,
                          "output-port-list": out_facing_ports},
            "parameters": {
                "ieee802-dot1cb-stream-identification-types:ip-stream-identification": {
                    "vlan-tagged": "tagged",
                    "priority": stream.pcp,
                    "vlan": stream.vlan,
                }
            },
        }
    }


def sequence_generation(stream, index: int) -> dict[str, Any]:
    """Clause 7 talker side. Refused on devices without ieee802-dot1cb-frer."""
    f = stream.tsn["frer"]
    return {FRER_SEQ_GEN.format(index=index): {
        "stream": [f["stream_handle"]],
        "direction-out-facing": True,
    }}


def sequence_recovery(stream, index: int, ports: list[str]) -> dict[str, Any]:
    """Clause 7 listener side.

    `history-length` and `reset-timeout` are the two values that decide whether
    redundancy helps or hurts. A history shorter than the burst discards good
    frames as out-of-window; a reset timeout shorter than the path-delay
    difference resets the recovery function between the two copies of the same
    frame, so both are accepted and the duplicate reaches the application.
    """
    f = stream.tsn["frer"]
    rec = f["sequence_recovery"]
    return {FRER_SEQ_REC.format(index=index): {
        "stream": [f["stream_handle"]],
        "port": ports,
        "algorithm": "ieee802-dot1cb-frer:vector-recovery-algorithm",
        "history-length": rec["history_length"],
        "reset-timeout": int(rec["reset_timeout_ms"]),
        "take-no-sequence": rec["take_no_sequence"],
        "individual-recovery": False,
        "latent-error-detection": {"latent-error-detection": True,
                                   "difference": 10, "period": 2000, "reset-period": 30000},
    }}
