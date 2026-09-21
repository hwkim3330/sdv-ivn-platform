# SDV-IVN Part 4 — DDS / TSN 및 IVN 반도체 사상

버전 0.1.0 · 2026-09 · 상태: 초안
레퍼런스 구현: `middleware/qos/mapper.py`, `network/`, `dds/`
적합성 시험: `tests/unit/test_qos_mapping.py`

## 1. 사상의 전체 모습

```
  SDV QoS 등급 (Part 3)
        │
        ├──→ DDS QoS 정책        ──→ Fast DDS XML / Cyclone 설정+엔티티 표
        │
        ├──→ 802.1Q VLAN + PCP   ──→ ip link egress-qos-map
        │
        ├──→ 802.1Qav CBS        ──→ idleSlope / sendSlope / hiCredit / loCredit
        │
        ├──→ 802.1Qbv TAS        ──→ gate control list + admin-base-time
        │
        ├──→ 802.1Qbu 선점       ──→ frame-preemption-status-table
        │
        └──→ 802.1CB FRER        ──→ stream identity + sequence gen/recovery
```

## 2. 등급 → DDS QoS

| SDV 항목 | DDS 정책 | 규칙 |
|---|---|---|
| reliability | RELIABILITY | reliable → RELIABLE, best_effort → BEST_EFFORT |
| durability | DURABILITY | 스트림이 덮어쓸 수 있다 |
| history | HISTORY | 등급이 정한다 |
| deadline | DEADLINE | **주기 × 등급 tolerance.** event_driven 이면 끈다 |
| latency budget | LATENCY_BUDGET | 등급 값 그대로 |
| liveliness | LIVELINESS | 등급이 automatic 일 때만, lease = max(100 ms, 마감×3) |
| PCP | TRANSPORT_PRIORITY | 등급의 PCP 와 **같은 값** |
| DSCP | (전송 설정) | IP TOS 로 표시 |
| — | RESOURCE_LIMITS | KEEP_LAST 일 때 history 깊이로 제한 |

### 2.1 DEADLINE 이 주기에서만 나오는 이유

LATENCY_BUDGET 은 "얼마나 늦게 도착해도 되는가"이고 DEADLINE 은 "샘플이 얼마나
멀리 떨어져도 되는가"다. 다른 양이다.

초안은 SAFETY_CRITICAL 의 DEADLINE 을 KPI 와 같은 5 ms 로 두었다. 주기 10 ms
제어 스트림을 모니터에 걸자 건강한 표본 500개 중 499개가 위반으로 집계됐다.
100 Hz 발행자가 5 ms 간격을 낼 수는 없다. Part 3 §2.6 참조.

### 2.2 LIVELINESS 를 아무 데나 걸지 않는 이유

말 없는 토커 자체가 결함인 등급에서만 건다. best-effort 센서 등급에서는
DEADLINE 이 이미 그것을 보고하고, LIVELINESS 주장 트래픽은 우리가 보호하려는
바로 그 등급에 얹히는 추가 부하가 된다.

### 2.3 RESOURCE_LIMITS 를 반드시 거는 이유

멈춘 구독자 하나가 writer 의 큐를 안전 태스크의 메모리까지 키울 수 있다.
KEEP_LAST 등급은 history 깊이 이상을 쌓을 이유가 없다.

## 3. 우선순위가 선로에 닿는 경로 (가장 자주 끊기는 곳)

```
DDS TRANSPORT_PRIORITY
        ↓
SO_PRIORITY (소켓 옵션)
        ↓
skb->priority
        ↓
VLAN egress-qos-map          ← DDS 가 설정하지 않는다
        ↓
802.1Q PCP (선로)
```

**egress-qos-map 이 없으면 모든 프레임이 PCP 0 으로 나간다.** 프로파일이
무엇을 적었든 상관없다. 스위치의 등급별 스케줄링은 한 등급만 보게 되고,
설정은 전부 맞아 보이는데 아무 효과가 없다.

그래서 이 플랫폼은 `TRANSPORT_PRIORITY == PCP` 를 불변식으로 두고 단위시험으로
지킨다. 그리고 VLAN 인터페이스 생성 명령을 매핑과 함께 생성한다.

```bash
ip link add link enp11s0 name enp11s0.10 type vlan id 10 \
    egress-qos-map 0:0 2:2 4:4 6:6 7:7
```

## 4. 등급 → 802.1Qav (CBS)

IEEE 802.1Q-2022 8.6.8.2.

```
idleSlope = 예약 대역
sendSlope = idleSlope − 포트속도                  (항상 음수)
hiCredit  = idleSlope × (간섭바이트 × 8) / 포트속도
loCredit  = sendSlope × (최대프레임 × 8) / 포트속도
```

간섭 바이트는 이 등급이 기다려야 하는 최악의 프레임이다.

| 포트 | 간섭 |
|---|---|
| 선점 가능 (802.1Qbu) | 143 바이트 (선점 잔여분) |
| 선점 불가 | 1,522 바이트 (최대 프레임) |

선점이 되는 포트에서 hiCredit 이 낮아지는 것은 그만큼 덜 기다리기 때문이다.

같은 트래픽 클래스를 공유하는 스트림들의 idleSlope 는 **합산**된다.

## 5. 등급 → 802.1Qbv (TAS)

### 5.1 사이클

사이클은 그 포트의 TAS 스트림 중 **가장 짧은 주기**다. 그래야 모든 TAS
스트림이 자기 주기마다 최소 한 번 창을 받는다.

### 5.2 창 크기

```
창 = (그 클래스가 사이클당 옮겨야 하는 바이트) × 바이트시간 + 가드밴드
가드밴드 = 143 바이트 × 바이트시간          (선점 가능 포트)
```

가드밴드 없이는 창이 닫히는 순간 전송 중이던 프레임이 창을 넘어간다.

### 5.3 들어가지 않으면 실패한다 (필수)

보호 창의 합이 사이클을 넘으면 오류다. 창을 줄여 맞추면 하드웨어는 받아들이고
마감은 놓친다. 이것은 승인 실패이지 반올림할 값이 아니다.

### 5.4 admin-base-time 은 반드시 적는다 (필수)

비워 두면 각 브리지가 설정된 시점에 자기 사이클을 시작한다. 두 브리지의
사이클이 반 주기 어긋나면 1 ms 보장이 1.5 ms 가 된다. **한 도메인의 모든
포트는 같은 base time 을 공유해야 한다.**

### 5.5 같은 마스크는 합친다 (권고)

게이트 제어 목록은 희소한 하드웨어 자원이다(보통 64–256 엔트리). 솔버가 뱉은
목록을 합치지 않으면 대부분을 무의미한 엔트리에 쓴다.

## 6. 등급 → 802.1Qbu (선점)

우선순위는 **그 우선순위에 사상된 모든 등급이 express 일 때만** express 다.
express 등급과 preemptable 등급을 한 우선순위에 섞고 그 우선순위를 express 로
표시하면, preemptable 트래픽이 선점 불가가 된다. 의도의 정반대다.

## 7. 등급 → 802.1CB (FRER)

802.1CB 는 하드웨어가 나누는 대로 두 부분으로 나뉜다.

| 조항 | YANG 모델 | 내용 |
|---|---|---|
| Clause 6 | `ieee802-dot1cb-stream-identification` | 스트림 식별 |
| Clause 7 | `ieee802-dot1cb-frer` | 순번 생성, 복제, 복구, 제거 |

**장비가 앞의 것만 가질 수 있다.** 그 경우 스트림을 알아보고 아무것도 하지
않는다 (§9 참조).

### 7.1 복구 창 (필수)

```
history_length   ≥ 2 ms 안에 오는 프레임 수 × 2, 최소 4
reset_timeout_ms ≥ max(10, 주기 × 4)
```

history 가 버스트보다 짧으면 정상 프레임이 창 밖으로 판정돼 버려진다.
reset timeout 이 두 경로의 지연 차보다 짧으면 같은 프레임의 두 복제 사이에서
복구 함수가 초기화돼 둘 다 통과하고, 중복이 애플리케이션까지 올라간다.
두 경우 모두 이중화가 아무 일도 못 한다.

### 7.2 대역 (필수)

복제분은 실제로 망을 지나간다. 승인 계산에서 **두 배로 센다.**

## 8. 등급 → 10BASE-T1S

10BASE-T1S 멀티드롭에는 게이트도 크레딧도 선점도 없다. 중재는 PLCA(802.3cg)가
한다.

| 요구 | 이 매체에서 |
|---|---|
| TAS | strict priority 로 내려감 |
| CBS | strict priority 로 내려감 |
| 선점 | 없음 |
| FRER | 없음 → REDUNDANT_SAFETY 거부 |
| REALTIME / BULK_DATA | 거부 |

내려간 사실은 결과에 남는다. 하드웨어가 무시할 게이트 제어 목록을 만들어
보내는 것보다 낫다.

## 9. 장비 능력과 거부 (필수)

등급은 약속이다. 약속을 지킬 수 없는 장비에 배치하는 것은 조용한 강등이 아니라
**거부**여야 한다.

`devices/capabilities.yaml` 이 장비별 능력과 그 근거를 담는다. 세 가지 값이 있다.

| 값 | 뜻 | 거부 메시지 |
|---|---|---|
| `true` | 있다 | — |
| `false` | 없다 (근거 기록) | "…에 …가 없다 — 근거" |
| `null` | 아직 측정 안 됐다 | "…의 … 지원 여부가 아직 미확정이다" |

개발 중인 국산 반도체의 능력은 전부 `null` 이다. 실물 없이 `true` 로 가정한
계획은 가장 비싸게 틀린다.

### 9.1 기록된 예외

벤치에서 아무것도 못 돌리는 플랫폼은 쓸모가 없다. 예외를 허용하되, 예외는
기능을 **설정에서 실제로 제거**하고 그 사실을 배치 계획에 남긴다. 강등된
스트림은 그 뒤의 모든 방출기와 모든 시험이 동일하게 본다.

## 10. 알려진 하드웨어 제약 (측정됨)

| 장비 | 제약 | 근거 |
|---|---|---|
| FRER 없는 TSN 브리지 | 802.1CB **복제·제거 없음**. 식별만. | YANG 카탈로그에 `ieee802-dot1cb-frer` 부재, `sequence-generation`/`sequence-recovery` 노드 없음. 벤더 매뉴얼 재확인. |
| TSN 브리지 (선점 없음) | 프레임 선점 없음 | 카탈로그 |
| TSN 브리지 (선점 없음) | FRER 동작 확인됨 | 실기에서 B 스위치 Gi1/6 물리 단절 시 수신 유지 |
| TSN 브리지 (선점 없음) | "2.5G" 광 포트 상한은 2.5G | 10G-SR 모듈 링크 안 올라옴. 스위치 간은 구리 Gi. |
| 개발 10G/10M 칩 | 전 항목 미확정 | 실물 없음 |

## 11. YANG 경로

| 설정 | 경로 |
|---|---|
| TAS | `/ietf-interfaces:interfaces/interface[name='{port}']/ieee802-dot1q-bridge:bridge-port/ieee802-dot1q-sched-bridge:gate-parameter-table` |
| 선점 | `…/ieee802-dot1q-preemption-bridge:frame-preemption-parameters` |
| CBS | `…/ieee802-dot1q-cbsa-bridge:cbsa/cbsa-parameter-table[traffic-class={tc}]/admin-idle-slope` |
| 스트림 식별 | `/ieee802-dot1cb-stream-identification:stream-identity[index={i}]` |
| FRER 순번 생성 | `/ieee802-dot1cb-frer:frame-replication-and-elimination/sequence-generation[index={i}]` |
| FRER 순번 복구 | `…/sequence-recovery[index={i}]` |

CBS 가 벤더 augment 아래 있는 것은 사실 그대로 적은 것이다. 스케줄링과 달리
크레딧 기반 셰이핑은 IEEE 모델에 같은 방식으로 들어 있지 않다.

## 12. 적합성 (Part 5 가 검사하는 항목)

- 대역 계산에 이더넷 프레이밍이 포함되는가
- MTU 초과 샘플이 조각당 오버헤드를 내는가
- DEADLINE 이 주기로 조여지는가
- TRANSPORT_PRIORITY 와 PCP 가 같은가
- FRER 대역을 두 번 세는가
- 게이트 목록이 사이클에 맞는가, 안 맞으면 실패하는가
- 능력 없는 장비를 거부하는가, 예외가 기능을 실제로 제거하는가
