# SDV-IVN Part 1 — 참조 아키텍처 및 요구사항

버전 0.1.0 · 2026-09 · 상태: 초안

## 1. 범위

이 문서는 SDV(Software Defined Vehicle)의 차량 내부 네트워크에서, 서로 다른
안전 수준의 데이터가 각각 요구되는 전송 품질을 보장받기 위한 소프트웨어 계층
구조와 용어, 데이터 흐름, 그리고 각 계층 사이의 책임 경계를 정의한다.

Part 2 는 그 경계의 API 를, Part 3 은 품질 등급을, Part 4 는 등급이 DDS 와
IEEE 802.1 설정으로 내려가는 사상(寫像)을, Part 5 는 적합성 시험을 정의한다.

## 2. 이 문서에서의 SDV

일반적인 SDV 정의는 "하드웨어를 크게 바꾸지 않고 소프트웨어로 기능을 추가·개선
하며 OTA 로 지속 발전시킬 수 있는 차량"이다. 이 문서는 더 좁게, **네트워크
관점**으로 좁힌다.

> **SDV 차량 통신 아키텍처**란, 다양한 안전 수준을 가진 SDV 용 데이터가
> In-Vehicle Network 통신반도체를 통해 각각의 전송 품질을 보장받을 수 있게
> 하는 전장 네트워크를 말한다.

이 정의가 이 저장소의 모든 선택을 설명한다. 왜 Autoware 를 쓰는가 — 안전
수준이 서로 다른 실제 트래픽을 만들어내는 애플리케이션이 필요하기 때문이다.
왜 DDS 인가 — 그 트래픽이 이미 DDS 로 흐르고, QoS 라는 개념이 표준으로 있기
때문이다. 왜 API 를 만드는가 — 애플리케이션이 반도체를 몰라도 되게 하기
위해서다.

## 3. 계층

```
┌──────────────────────────────────────────────────────────┐
│ L5  SDV Applications                                     │
│     Autoware · HMI · Diagnostics · Vehicle Service · Test│
└───────────────────────────┬──────────────────────────────┘
                     SDV Application API          (Part 2)
┌───────────────────────────▼──────────────────────────────┐
│ L4  SDV Service Layer                                    │
│     Vehicle API · Network API · Fault API                │
│     Service API · Diagnostics API · Reconfiguration API  │
└───────────────────────────┬──────────────────────────────┘
                   Abstract Communication API      (Part 2)
┌───────────────────────────▼──────────────────────────────┐
│ L3  DDS Layer                                            │
│     Topic Registry · QoS Manager · Discovery · Monitor   │
│     Fast DDS · Cyclone DDS · 개발칩 DDS Adapter          │
└───────────────────────────┬──────────────────────────────┘
                      QoS Policy Mapper            (Part 4)
┌───────────────────────────▼──────────────────────────────┐
│ L2  SDV Network Control Layer                            │
│     VLAN · PCP · CBS · TAS · FRER · PTP · MACsec         │
└───────────────────────────┬──────────────────────────────┘
                     Device Abstraction API        (Part 2)
┌───────────────────────────▼──────────────────────────────┐
│ L1  IVN 반도체                                            │
│     Multi-GigE (10G)          │  10BASE-T1S (10M)        │
│     상용 TSN 브리지     │  상용 T1S PHY            │
│     → 개발 Multi-GigE 칩      │  → 개발 10M 칩           │
└──────────────────────────────────────────────────────────┘
```

### 3.1 각 계층의 책임

| 계층 | 하는 일 | 하지 않는 일 |
|---|---|---|
| L5 애플리케이션 | 무엇이 필요한지 이름으로 말한다 ("이건 REDUNDANT_SAFETY") | 우선순위, VLAN, 셰이퍼, 반도체를 알지 못한다 |
| L4 서비스 | 차량 데이터와 네트워크 자원을 서비스로 노출 | 전송 구현을 고르지 않는다 |
| L3 DDS | 발행·구독, 디스커버리, QoS 집행, 측정 | 스위치를 설정하지 않는다 |
| L2 네트워크 제어 | 등급을 802.1 설정으로 번역, 승인 판정 | 어느 벤더인지 알지 못한다 |
| L1 반도체 | 프레임을 실제로 스케줄링·복제·제거 | — |

### 3.2 관리면·데이터면·네트워크면

REST 와 DDS 를 경쟁 관계로 두면 설계가 망가진다. 세 면으로 나눈다.

| 면 | 수단 | 예 |
|---|---|---|
| Management Plane | REST / YANG / CORECONF | 스트림 등록, TSN 설정, 통계 조회 |
| Data Plane | DDS / RTPS | 자세, 궤적, 제어 명령, 포인트클라우드 |
| Network Plane | Ethernet / TSN / 10BASE-T1S | 프레임 |

## 4. QoS 등급 (요약, 정본은 Part 3)

애플리케이션이 고르는 것은 다섯 개다.

| 등급 | 이름 | 성격 |
|---|---|---|
| SDV_QOS_0 | BEST_EFFORT | 남는 대역 |
| SDV_QOS_1 | BULK_DATA | 크고 느리지만 반드시 도착 |
| SDV_QOS_2 | REALTIME | 주기적, 마감 있음, 한 샘플 유실 허용 |
| SDV_QOS_3 | SAFETY_CRITICAL | 놓치면 안전이 깨짐 |
| SDV_QOS_4 | REDUNDANT_SAFETY | 위 + 경로 이중화 |

## 5. 가상 도메인

분리는 우선순위보다 도메인이 먼저다. 우선순위는 혼잡을 나누고, 도메인은
접근을 나눈다. 침해된 인포테인먼트 노드가 섀시 VLAN 으로 프레임을 낼 수
없어야 한다.

| 도메인 | VLAN | 내용 |
|---|---|---|
| chassis | 10 | 제동/조향/구동 |
| adas | 20 | 인지/측위/계획 |
| body | 30 | 도어/공조/조명 |
| infotainment | 40 | 미디어/HMI |
| diagnostics | 50 | 진단/로그/OTA |
| network_ctrl | 1 | gPTP, TSN 재구성, 반도체 관리 |

## 6. 물리 구조와 두 반도체의 역할

```
              HPC / Autoware
                    │  Multi-GigE
              Zonal Switch
             ╱              ╲
      Multi-GigE          10BASE-T1S
          │                    │
    Camera / LiDAR      Sensor / Actuator
```

역할을 프로파일 차원에서 못박는다.

- **Multi-GigE = 백본.** 센서 페이로드, 지도, 고대역 융합 결과.
- **10BASE-T1S = 엣지 센서/액추에이터 망.** 차량 상태, 센서 상태, 결함 알림,
  제어 명령, 진단, 액추에이터 명령.

10 Mb/s 링크에 포인트클라우드를 얹는 설계는 문서상의 권고가 아니라 매퍼가
거부하는 조건이다 (`schemas/yaml/qos-classes.yaml`의 `link_admissible_classes`).

## 7. 성능 요구사항

과제가 직접 겨누는 측정 항목이다. Part 5 의 적합성 시험이 이 값을 잰다.

| 항목 | 목표 | 적용 등급 |
|---|---|---|
| Edge → HPC 최대 지연 | ≤ 5 ms | SDV_QOS_3, SDV_QOS_4 |
| 최대 지터 | ≤ 2.5 ms | SDV_QOS_3, SDV_QOS_4 |
| 경로 이중화 | IEEE 802.1CB | SDV_QOS_4 |
| 가상 도메인 | VLAN 분리 | 전 등급 |
| 링크 | 10 Gb/s 급 Multi-GigE | — |
| 엣지 버스 | 10 Mb/s 급 10BASE-T1S 다중 단말 | — |

측정은 **부하가 걸린 망**에서 한다. 한가한 링크의 지터는 이 아키텍처가
있든 없든 좋다.

## 8. 결함과 재구성

결함 처리 흐름은 하나여야 한다. 현재 Autoware 레퍼런스 구현에는 결함·재구성
판단이 여러 모듈에 흩어져 있고, 이 플랫폼으로 옮기면서 단일 흐름으로 정리한다.

```
Fault Monitor  →  Fault API  →  Policy / Reconfiguration Manager
                                        ├── Network Adaptation
                                        └── Application Adaptation
```

IVN 플랫폼이 다루는 결함은 센서 고장을 넘어선다.

| 분류 | 결함 |
|---|---|
| 링크 | Link down, 패킷 유실, 지연 증가, 지터 증가 |
| DDS | Deadline miss, Liveliness lost, Sample lost |
| 경로 | 스위치 고장, 경로 단절, FRER 한 경로 상실 |
| 엣지 | 10BASE-T1S 노드 고장, PLCA 비콘 상실 |
| 센서 | (기존) LiDAR 정지, 타임스탬프 어긋남, GNSS 이상 |

## 9. 준수 표기

이 문서에서 **필수**는 적합성 시험이 검사하는 항목을, **권고**는 검사하지 않는
설계 지침을 뜻한다.

- 애플리케이션은 Part 3 의 등급 이름으로만 품질을 요구해야 한다. (필수)
- 등급이 요구하는 능력이 없는 장비에 그 등급의 스트림을 배치해서는 안 된다.
  기록된 예외가 있는 경우, 해당 기능은 설정에서 제거되어야 하며 제거 사실이
  배치 계획에 남아야 한다. (필수)
- 대역 계산은 이더넷 프레이밍 오버헤드를 포함해야 한다. (필수)
- 802.1CB 로 복제되는 스트림의 대역은 승인 계산에서 두 배로 센다. (필수)
- 도메인 분리를 우선순위 분리보다 먼저 적용한다. (권고)

## 10. 참조

- IEEE 802.1Q-2022 (VLAN, CBS 8.6.8.2, TAS)
- IEEE 802.1DG (차량용 TSN 프로파일)
- IEEE 802.1CB (Frame Replication and Elimination for Reliability)
- IEEE 802.1AS (gPTP), IEEE 802.1Qbu / 802.3br (프레임 선점)
- IEEE 802.3cg (10BASE-T1S, PLCA)
- OMG DDS, OMG DDS-TSN 1.0 (2026-09 Formal)
- COVESA VSS / VISS
- TTAK.KO-12.0420 소프트웨어 정의 차량(SDV)을 위한 보안 프레임워크
