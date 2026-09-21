# SDV-IVN Platform

**한국형 SDV 차량 내부 네트워크 소프트웨어 플랫폼** — 표준안, API, DDS/TSN 프로파일,
레퍼런스 구현, 시험 체계를 한 저장소에 둔다.

IVN 통신반도체 과제의 3세부 소프트웨어 결과물이다. 과제가 요구하는 것은
Multi-GigE 와 10 Mbps 통신반도체를 **실증할 수 있는** SDV 네트워크 아키텍처와
그 위의 운영 소프트웨어이고, 2027년 3차년도에는 "개발 통신반도체 연동을 위한
SDV용 DDS 개발"이 명시돼 있다. 이 저장소가 그 축이다.

## 이 저장소가 답하려는 질문

> 서로 다른 안전 수준을 가진 SDV 데이터가, In-Vehicle Network 통신반도체를 통해
> 각각의 전송 품질을 보장받으려면 소프트웨어가 무엇을 해야 하는가.

답의 형태는 계층 하나가 아니라 사슬 하나다.

```
          SDV Applications
   Autoware │ HMI │ Diagnostics │ Test App
                    │
             SDV Service API
   Vehicle API │ Network API │ Fault API │ Diagnostics API
                    │
            Abstract DDS API
   Topic Registry │ QoS Manager │ Discovery │ Monitor
   Fast DDS │ Cyclone DDS │ (개발칩 DDS Adapter)
                    │
           QoS Policy Mapper          <- 이 저장소의 핵심
                    │
        SDV Network Control Layer
   VLAN │ PCP │ CBS │ TAS │ FRER │ PTP │ MACsec
                    │
             Device Abstraction
                    │
        ┌───────────┴───────────┐
   Multi-GigE                10BASE-T1S
   상용 TSN 브리지 → 국산 10G 칩    상용 → 국산 10M 칩
```

**Autoware 는 이 플랫폼이 아니라 이 플랫폼을 검증하는 첫 번째 SDV 애플리케이션이다.**
구현은 `hwkim3330/autoware` 에 그대로 두고, 여기서는 그 트래픽을 IVN 관점으로
분류해 프로파일로 쓴다 (`profiles/autoware/`).

## 왜 계층을 나누는가

마지막 어댑터만 갈아끼우면 애플리케이션이 그대로 가기 때문이다.

```
2026   Autoware → SDV API → DDS → Network API → [상용 TSN 브리지]
2027   Autoware → SDV API → DDS → Network API → [국산 IVN 반도체]
```

애플리케이션과 DDS 와 QoS 정책이 한 줄도 바뀌지 않는 것 — 그것이
Software Defined 라고 말할 수 있는 근거다.

## 핵심: QoS Policy Mapper

애플리케이션에 DDS 옵션을 그대로 노출하면 같은 요구사항이 팀마다 다른 QoS 로
구현되고, 네트워크는 그 차이를 조정할 수단이 없다. 그래서 애플리케이션이 고르는
것은 **다섯 개의 등급**뿐이다.

| 등급 | 이름 | PCP | 셰이퍼 | 신뢰성 | 마감 | 용도 |
|---|---|---|---|---|---|---|
| SDV_QOS_0 | BEST_EFFORT | 0 | best effort | best effort | — | 로그, 통계, HMI |
| SDV_QOS_1 | BULK_DATA | 2 | CBS | reliable | 1 s | 지도, OTA, 캘리브레이션 |
| SDV_QOS_2 | REALTIME | 4 | CBS | best effort | 20 ms | 라이다, 카메라, IMU, 궤적 |
| SDV_QOS_3 | SAFETY_CRITICAL | 6 | TAS | reliable | 5 ms | 제어 명령, 측위, 결함 |
| SDV_QOS_4 | REDUNDANT_SAFETY | 7 | TAS | reliable | 5 ms | 위 + 802.1CB 경로 이중화 |

나머지는 전부 여기서 파생된다.

```python
from qos.mapper import QosRegistry, StreamRequest

reg = QosRegistry()
s = reg.resolve(StreamRequest("/control/command/control_cmd",
                              "REDUNDANT_SAFETY", payload_bytes=128, period_ms=20))

s.dds    # RELIABLE, KEEP_LAST 1, deadline 5 ms, latency budget 5 ms, DSCP 46
s.tsn    # VLAN 10, PCP 7, TAS, FRER dual path, 0.102 Mb/s reserved
```

그리고 벤더 문법으로 내려간다.

```python
from dds.fastdds import emit as fastdds
from network.tsn import emit_yang
from network.vlan import emit_vlan

fastdds.emit([s])                                  # Fast DDS XML 프로파일
emit_yang.gate_parameter_table("1", gcl)           # ieee802-dot1q-sched-bridge
emit_vlan.ip_link_commands("enp11s0", [s], reg.domains)   # egress-qos-map
```

## 표준만 쓴다

플랫폼이 만드는 설정은 **공표된 IEEE 표준 모델의 경로만** 쓴다. 벤더 모듈도,
벤더 augment 도, 벤더 deviation 도 본체에 없다.

| 기능 | 표준 모델 |
|---|---|
| 802.1Qbv 스케줄 | `ieee802-dot1q-sched-bridge` |
| 802.1Qav 크레딧 | `ieee802-dot1q-cbsa-bridge` |
| 802.1Qbu 선점 | `ieee802-dot1q-preemption-bridge` |
| 802.1CB 식별 | `ieee802-dot1cb-stream-identification` |
| 802.1CB 이중화 | `ieee802-dot1cb-frer` |

벤더가 다른 경로를 요구하면 그것은 **어댑터의 일**이다(`devices/`). 번역은
경계 한 곳에서만 일어난다. 그래야 2027년에 반도체를 바꿔도 상위가 그대로 간다.

그리고 이것은 검사된다. `schemas/yang/model.py` 가 표준 모델을 파싱하고,
적합성 시험이 **플랫폼이 만드는 모든 경로**를 매번 대조한다. 이 규칙으로 실제
오류 여섯 개를 잡았다 — 없는 컨테이너 이름, 벤더 래퍼, kbps 대 bps, 리스트인 줄
알았던 컨테이너, 없는 리프, 잘못된 리스트 키. 전부 코드 검토로는 안 보인다.

## 장비는 이름이 아니라 능력으로 가른다

플랫폼은 어떤 회사의 어떤 칩인지 알지 않는다. 능력 등급 하나를 알 뿐이다.

```
tsn_bridge_full           전 기능
tsn_bridge_no_frer        스케줄·크레딧·선점은 되나 802.1CB 복제 없음
tsn_bridge_no_preemption  이중화는 되나 선점 없음
ethernet_bridge_plain     VLAN·우선순위만
edge_multidrop_10m        10BASE-T1S. 게이트도 크레딧도 없고 중재는 PLCA
unverified                아직 측정 안 함 — 어떤 등급도 배치되지 않는다
```

실제 장비를 등급에 붙이는 것은 현장 파일(`devices/sites/`)의 몫이고, 그 파일은
공개 저장소에 올리지 않아도 된다.

## 이 플랫폼이 거절하는 것들

측정된 사실에 근거해 **조용히 강등하는 대신 거부한다.** 약속을 못 지키는
네트워크보다, 못 지킨다고 말하는 네트워크가 낫다.

- **FRER 없는 TSN 브리지 에 FRER 이 없다.** YANG 카탈로그에
  `ieee802-dot1cb-stream-identification` 은 있으나 `ieee802-dot1cb-frer` 이 아예
  없다. 스트림을 식별하고 아무것도 하지 않는다. REDUNDANT_SAFETY 는 이 장비에
  배치되지 않는다.
- **10BASE-T1S 에 포인트클라우드를 얹을 수 없다.** 10 Mb/s 링크에 허용되는
  등급이 프로파일에 못박혀 있다. 엣지 버스는 상태와 명령을 위한 것이다.
- **`unverified` 등급의 능력은 전부 `null` 이다.** "아직 안 재봤다"는 "없다"와
  다른 메시지로 거부된다. 개발 중인 반도체가 여기 들어간다.
- **FRER 대역은 두 번 센다.** 복제분이 네트워크를 지나가기 때문이다.
- **게이트 스케줄이 사이클에 안 들어가면 반올림하지 않고 실패한다.**

거부를 넘기려면 **기록된 예외**가 필요하다. 예외는 기능을 숨기는 게 아니라
설정에서 실제로 제거하고, 그 사실을 계획서에 남긴다.

```python
Waiver("*", "tsn_bridge_no_frer", "frer",
       "이 브리지에 802.1CB 복제가 없다. 이번 시험은 경로 이중화가 아니라 "
       "DDS-TSN 매핑과 지연·지터를 보는 것이므로 단일 경로로 진행한다.",
       approved_by="…")
```

## 저장소 구조

| 경로 | 내용 |
|---|---|
| `spec/` | 표준안 Part 1–5. 코드와 같이 개발한다. |
| `schemas/` | 기계 판본 — YAML 프로파일, JSON Schema, **IEEE/IETF 표준 YANG 72개**, IDL |
| `api/` | Vehicle / Network / Device / Fault / Diagnostics API |
| `middleware/` | core, qos(매퍼), discovery, reconfiguration |
| `dds/` | Fast DDS / Cyclone DDS 어댑터, 프로파일, 모니터 |
| `network/` | vlan, tsn, frer, ptp, macsec, t1s |
| `devices/` | 능력 등급 표와 장비 어댑터. 벤더 이름은 `devices/sites/` 에만 |
| `profiles/autoware/` | Autoware 토픽 카탈로그와 트래픽 프로파일 |
| `applications/autoware/` | 레퍼런스 애플리케이션 연결부 |
| `tools/` | 트래픽 생성기, QoS 모니터, 결함 주입기 |
| `tests/` | unit, conformance, performance, interoperability, hardware |

## 표준화 경로

DDS 를 새로 표준화하는 것이 아니다. DDS 는 이미 OMG 국제표준이고,
**DDS-TSN 1.0 이 2026년 9월 Formal 로 나왔다.** 우리 자리는 그것을 차량용 IVN
반도체와 SDV 애플리케이션에 적용하는 **한국형 프로파일과 API** 다.

```
2026   KETI Open Specification  (이 저장소)
2027   시험 구현 + 기업 상호운용성
       TTA 표준안 → TTAK 단체표준
2028+  KS / 국제표준 연계 검토
```

문서만으로는 약하다. 그래서 한 세트로 만든다 —
**규격 + Open API + 레퍼런스 구현 + 시험 스위트 + 적합성 시험 + Autoware 실증.**

## 시작하기

```bash
pip install pyyaml pytest
python -m pytest tests -q                     # 83건
python profiles/autoware/build_profile.py     # 레퍼런스 앱 트래픽 행렬
```

라이선스는 Apache-2.0 이다. `schemas/yang/` 의 IEEE·IETF 모델은 원본 그대로이며
각자의 조건을 따른다.

## 상태

| | |
|---|---|
| 표준안 Part 1~5 | 초안 완료 |
| QoS 프로파일 (Part 3) | 5등급 확정 |
| DDS/TSN 매퍼 (Part 4) | 동작 |
| 능력 게이트 + 기록된 예외 | 동작 |
| Fast DDS / Cyclone 방출기 | 동작 |
| 802.1Qbv / Qav / Qbu / 1CB 방출기 | 동작, 표준 경로 검증됨, **실기 미검증** |
| Autoware 트래픽 프로파일 | 27 토픽, 동작 |
| OpenAPI (관리면) | 14 경로, 스키마 15 |
| DDS 모니터 (통계 코어) | 동작, **실제 구독 미결선** |
| 시험 | 83건 통과 (단위 51, 적합성 32) |
| 성능 적합성 (층위 C) | **미착수** — 벤치 결선 필요 |

실기에 써 본 적 없는 것은 위 표에 그렇게 적혀 있다. 방출기가 만드는 YANG 은
TSN 브리지 (FRER 없음) 의 실제 모델 경로를 따르지만 아직 보드에 밀어넣어 확인하지 않았다.
지연·지터 실측은 하나도 없다. 프로파일은 예산을 적어 두었을 뿐이다.

남은 일과 일정은 [docs/ROADMAP.md](docs/ROADMAP.md) 에 있다.
