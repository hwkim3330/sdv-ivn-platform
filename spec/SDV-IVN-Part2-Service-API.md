# SDV-IVN Part 2 — SDV Vehicle 및 Network Service API

버전 0.1.0 · 2026-09 · 상태: 초안
기계 판본: `api/openapi/sdv-ivn.yaml` (OpenAPI 3.1)

## 1. 세 면

REST 와 DDS 를 경쟁 관계로 두면 설계가 망가진다. API 는 면(plane)으로 나뉜다.

| 면 | 수단 | 이 문서가 정의하는 것 |
|---|---|---|
| Management | REST / YANG / CORECONF | 스트림 등록, TSN 설정, 통계 조회 |
| Data | DDS / RTPS | Abstract DDS API (§5) |
| Network | Ethernet / TSN / T1S | (Part 4) |

실시간 데이터는 REST 로 흐르지 않는다. 설정과 조회만 흐른다.

## 2. 다섯 개의 API

| API | 사용자 | 역할 |
|---|---|---|
| Vehicle API | SDV 애플리케이션 | 차량 데이터·기능 접근 |
| Network Service API | 애플리케이션 / 미들웨어 | QoS·경로·이중화 요청 |
| Device API | 미들웨어 / 관리 SW | IVN 반도체·스위치 설정 |
| Fault API | 전부 | 결함 보고, 주입, 구독 |
| Diagnostics API | 관리 SW | 헬스, 통계 |

## 3. Network Service API (이 과제의 핵심)

### 3.1 애플리케이션이 내는 것

```json
{
  "name": "/external/selected/control_cmd",
  "qos_class": "REDUNDANT_SAFETY",
  "payload_bytes": 80,
  "period_ms": 10,
  "talker": "hpc.autoware",
  "listeners": ["zone.front"]
}
```

**여기 없는 것이 요점이다.** PCP 도, VLAN 도, 셰이퍼도, 스트림 ID 도,
idleSlope 도 애플리케이션이 정하지 않는다.

### 3.2 플랫폼이 돌려주는 것

```json
{
  "name": "/external/selected/control_cmd",
  "rate_mbps": 0.165,
  "frames_per_sample": 1,
  "wire_bytes_per_sample": 206,
  "vlan": 10, "pcp": 7,
  "dds": { "reliability": "RELIABLE", "deadline_ms": 5.0,
           "latency_budget_ms": 5.0, "transport_priority": 7, "dscp": 46 },
  "tsn": { "shaper": "tas", "redundancy": "frer_dual", "reserved_mbps": 0.165 }
}
```

### 3.3 거부는 오류가 아니라 답이다

`409` 는 재시도로 뚫을 수 있는 것이 아니다. 정책 판정이다.

```json
{
  "stream": "/control/command/actuation_cmd",
  "device": "t1s_10m",
  "requirement": "frer_dual",
  "capability": "frer",
  "reason": "class REDUNDANT_SAFETY requires frer_dual but t1s_10m
             (10BASE-T1S multidrop) has no 802.1CB support"
}
```

클라이언트가 해야 할 일은 재시도가 아니라 **등급을 다시 고르거나 링크를 바꾸는
것**이다. 그 판단은 사람이 한다.

### 3.4 엔드포인트

| 메서드 | 경로 | 내용 |
|---|---|---|
| GET | `/v1/network/classes` | 다섯 등급과 파생 값 |
| GET | `/v1/network/streams` | 등록된 스트림 |
| POST | `/v1/network/streams` | 등록 (승인 판정 포함) |
| GET/DELETE | `/v1/network/streams/{name}` | 조회 / 해제 |
| POST | `/v1/network/admission` | 배치 없이 판정만 (상태 불변) |
| POST | `/v1/network/plan` | 특정 장비 배치 계획 (거부·예외 포함) |
| GET | `/v1/network/measurements` | 실측 지연·지터·유실 |
| GET | `/v1/network/topology` | 링크와 장비 |

## 4. Vehicle API — VSS 위에 Network 가지를 얹는다

차량 신호명을 새로 만들지 않는다. COVESA VSS 를 쓴다. COVESA 와 JASPAR 가
2026-06 에 VSS/VISS 기반 SDV API 표준화 협력을 공식화했으므로, 독자 규격을
만드는 것은 호환성만 잃는 선택이다.

```
Vehicle.Speed
Vehicle.Powertrain.*
Vehicle.Chassis.SteeringWheel.Angle
Vehicle.ADAS.*
```

**KETI 확장은 Network 가지다.** 여기가 이 과제의 자리다.

```
Network.Link.Front.Status
Network.Link.Rear.Status

Network.Stream.<name>.LatencyUs
Network.Stream.<name>.JitterUs
Network.Stream.<name>.DeadlineMiss
Network.Stream.<name>.WithinBudget

Network.Redundancy.FRER.Status
Network.Redundancy.FRER.DiscardedDuplicates

Network.Device.<zone>.Port<n>.Status
Network.Device.<zone>.Port<n>.QueueDrops
```

차량 신호와 네트워크 신호가 **같은 이름공간**에 있는 것이 요점이다.
애플리케이션은 속도를 읽는 것과 같은 방식으로 자기 스트림의 지연을 읽는다.

## 5. Abstract DDS API

"SDV 용 DDS 개발"을 DDS 를 새로 만드는 것으로 가져가면 범위가 감당이 안 된다.
대신 **어댑터 경계**를 만든다.

```
SDV Application
      ↓
Abstract DDS API      ← 여기가 고정된다
      ↓
Fast DDS │ Cyclone DDS │ 개발칩 DDS Adapter
```

최소 표면:

```python
create_service()      create_stream()
publish()             subscribe()
set_qos()             get_qos()
get_health()          get_statistics()
request_redundancy()
```

사용:

```python
stream = sdv.create_stream("/external/selected/control_cmd",
                           qos="REDUNDANT_SAFETY")
stream.publish(control_command)
```

애플리케이션은 Fast DDS 인지 TSN 브리지 (FRER 없음) 인지 알지 못한다. 2027년에 마지막 어댑터를
국산 반도체로 바꿀 때 이 줄이 그대로 남는 것이 이 경계의 목적이다.

## 6. Fault API

결함 흐름은 하나여야 한다.

```
Fault Monitor → Fault API → Policy/Reconfiguration Manager
                                    ├── Network Adaptation
                                    └── Application Adaptation
```

주입 가능한 결함은 센서를 넘어 네트워크까지 간다.

```
link_down · packet_loss · packet_delay · packet_jitter
dds_deadline_miss · dds_liveliness_lost
switch_failure · path_failure · frer_path_loss
t1s_node_failure · sensor_stale
```

## 7. 측정값의 표기 규칙 (필수)

**아직 재지 않은 값은 `null` 이고 `0` 이 아니다.** 유실 0건과 유실을 재지
않은 것은 다르다. API 가 둘을 같은 값으로 돌려주면 보고서가 그 차이를 잃는다.

## 8. 인증

이 판본의 범위 밖이다. 차량 내부 관리망에서만 노출한다고 가정한다. 외부 노출
시의 요구사항은 TTAK.KO-12.0420(SDV 보안 프레임워크)을 따른다.
