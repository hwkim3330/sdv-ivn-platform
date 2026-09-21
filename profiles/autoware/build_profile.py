#!/usr/bin/env python3
"""Autoware 토픽 카탈로그 -> IVN 트래픽 프로파일.

카탈로그(관측된 것)에서 대역, 승인 판정, TSN 설정(계산된 것)을 만든다.
숫자를 손으로 적지 않는 이유는 단순하다: 손으로 적은 대역표는 토픽이 하나
바뀌는 순간 조용히 틀려지고, 틀린 것을 알아볼 방법이 없다.

    python profiles/autoware/build_profile.py            # 요약
    python profiles/autoware/build_profile.py --yaml     # 프로파일 YAML
    python profiles/autoware/build_profile.py --device lan9692   # 배치 계획
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "middleware"))

from qos.mapper import QosRegistry, StreamRequest, AdmissionError   # noqa: E402
from core.deploy import CapabilityGate, Waiver                       # noqa: E402

CATALOG = Path(__file__).parent / "topic_catalog.yaml"


def load_catalog(path: Path = CATALOG) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build(catalog: dict, registry: QosRegistry):
    """Resolve every catalogue entry. Entries the profile refuses come back as
    refusals rather than disappearing -- a topic the IVN profile will not carry
    is a finding about the application, not a gap in the table."""
    resolved, refused = [], []
    for t in catalog["topics"]:
        req = StreamRequest(
            name=t["topic"], qos_class=t["sdv_class"],
            payload_bytes=t["payload_bytes"], period_ms=1000.0 / t["rate_hz"],
            domain=t.get("domain"), link=t.get("link", "multigige_1g"),
            durability=t.get("durability"), ros_topic=t["topic"],
        )
        try:
            resolved.append(registry.resolve(req))
        except AdmissionError as e:
            refused.append((t, str(e)))
    return resolved, refused


def by_link(streams):
    out: dict[str, list] = {}
    for s in streams:
        out.setdefault(s.request.link, []).append(s)
    return out


def summarise(catalog, registry, resolved, refused) -> str:
    L = []
    L.append(f"Autoware 트래픽 프로파일 — 카탈로그 {len(catalog['topics'])} 토픽, "
             f"해석 {len(resolved)}, 프로파일 거부 {len(refused)}\n")

    L.append(f"{'토픽':52s} {'등급':17s} {'Mb/s':>9s} {'프레임':>7s} {'PCP':>4s} {'VLAN':>5s}  셰이퍼")
    L.append("-" * 112)
    for s in sorted(resolved, key=lambda x: -x.rate_mbps):
        L.append(f"{s.name[:52]:52s} {s.request.qos_class:17s} {s.rate_mbps:9.3f} "
                 f"{s.frames_per_sample:7d} {s.pcp:4d} {s.vlan:5d}  {s.tsn['shaper']}")

    if refused:
        L.append("\n프로파일이 거부한 토픽 (설계 결함이지 표의 누락이 아니다)")
        for t, why in refused:
            L.append(f"  {t['topic']}")
            L.append(f"    {why}")

    L.append("\n링크별 승인")
    for link, ss in sorted(by_link(resolved).items()):
        v = registry.admit(ss, link)
        mark = "통과" if v["admitted"] else "거부"
        L.append(f"  {link:15s} {v['offered_mbps']:10.3f} / {v['port_mbps']:>6} Mb/s"
                 f"  = {v['utilisation']*100:5.1f}%  [{mark}]  ({len(ss)} 스트림)")
        if not v["admitted"]:
            L.append(f"      {v['reason']}")
        for cls, mbps in sorted(v["per_class_mbps"].items(), key=lambda kv: -kv[1]):
            L.append(f"      {cls:18s} {mbps:9.3f} Mb/s")

    L.append("\nTAS 게이트 목록 (링크별)")
    for link, ss in sorted(by_link(resolved).items()):
        port = registry.links[link]["rate_mbps"]
        try:
            gcl = registry.build_gate_control_list(ss, port)
        except AdmissionError as e:
            L.append(f"  {link:15s} 실패 — {e}")
            continue
        if not gcl["enabled"]:
            L.append(f"  {link:15s} TAS 스트림 없음")
            continue
        L.append(f"  {link:15s} 사이클 {gcl['cycle_time_us']:.1f} us, "
                 f"보호 구간 {gcl['utilisation_protected']*100:.3f}%, "
                 f"가드밴드 {gcl['guard_band_us']:.3f} us, 엔트리 {len(gcl['entries'])}")

    L.append("\nKPI 대상 스트림 (5 ms / 2.5 ms)")
    for s in resolved:
        if s.request.qos_class in ("SAFETY_CRITICAL", "REDUNDANT_SAFETY"):
            lat = s.cls["latency_budget_ms"]; jit = s.cls["jitter_budget_ms"]
            L.append(f"  {s.name[:48]:48s} 예산 {lat} ms / {jit} ms   실측 —")
    L.append("  '실측 —' 은 아직 재지 않았다는 뜻이다. DDS 모니터가 채운다.")
    return "\n".join(L)


def to_profile_yaml(resolved) -> str:
    doc = {"version": "0.1.0", "generated_from": "topic_catalog.yaml", "streams": []}
    for s in resolved:
        doc["streams"].append({
            "name": s.name,
            "sdv_class": s.request.qos_class,
            "link": s.request.link,
            "period_ms": round(s.request.period_ms, 4),
            "payload_bytes": s.request.payload_bytes,
            "wire": {"frames_per_sample": s.frames_per_sample,
                     "bytes_per_sample": s.wire_bytes_per_sample,
                     "rate_mbps": round(s.rate_mbps, 4)},
            "dds": s.dds,
            "tsn": {k: v for k, v in s.tsn.items() if k != "frer"},
            "frer": s.tsn.get("frer"),
        })
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", action="store_true", help="프로파일 YAML 출력")
    ap.add_argument("--device", help="이 장비에 대한 배치 계획")
    ap.add_argument("--waive", action="append", default=[],
                    help="device:capability:rationale 형식의 예외")
    args = ap.parse_args()

    catalog = load_catalog()
    registry = QosRegistry()
    resolved, refused = build(catalog, registry)

    if args.yaml:
        print(to_profile_yaml(resolved))
        return 0

    if args.device:
        waivers = []
        for w in args.waive:
            dev, cap, why = w.split(":", 2)
            waivers.append(Waiver("*", dev, cap, why, "cli"))
        plan = CapabilityGate().plan(resolved, args.device, registry=registry, waivers=waivers)
        print(plan.report())
        return 0 if plan.ok else 1

    print(summarise(catalog, registry, resolved, refused))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
