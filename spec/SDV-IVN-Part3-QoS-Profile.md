# SDV-IVN Part 3 — SDV 통신 QoS 프로파일

버전 0.1.0 · 2026-09 · 상태: 초안
기계 판본: `schemas/yaml/qos-classes.yaml` · 집행: `middleware/qos/mapper.py`

## 1. 왜 다섯 개인가

DDS 의 QoS 정책은 22 개이고 대부분 서로 영향을 준다. 이것을 애플리케이션
개발자에게 그대로 노출하면 두 가지가 벌어진다. 같은 요구사항이 팀마다 다른
조합으로 구현되고, 네트워크는 그 차이를 조정할 수단이 없다. 어느 토픽이
정말로 5 ms 를 필요로 하는지 알 수 없으면 TAS 윈도우를 배정할 근거가 없다.

그래서 애플리케이션이 고르는 것은 **등급 이름 하나**다. 등급은 다섯 개이고,
이 수는 임의가 아니라 차량 트래픽이 실제로 갈리는 지점의 수다.

| 갈리는 지점 | 질문 |
|---|---|
| 마감이 있는가 | 없으면 BEST_EFFORT |
| 한 샘플을 버려도 되는가 | 못 버리고 크면 BULK_DATA |
| 놓치면 안전이 깨지는가 | 아니면 REALTIME |
| 경로 하나가 죽어도 되는가 | 아니면 REDUNDANT_SAFETY, 되면 SAFETY_CRITICAL |

## 2. 등급 정의

### SDV_QOS_0 — BEST_EFFORT

남는 대역만 쓴다. 로그, 통계, HMI 상태, OTA 대용량 전송의 배경 트래픽.

| 항목 | 값 |
|---|---|
| reliability | BEST_EFFORT |
| durability | VOLATILE |
| history | KEEP_LAST 1 |
| deadline | 주기 × 10 |
| latency budget | 없음 |
| PCP / TC | 0 / 0 |
| DSCP | 0 |
| 셰이퍼 | best effort |
| 선점 | preemptable |
| 기본 도메인 | diagnostics |

### SDV_QOS_1 — BULK_DATA

크고, 실시간이 아니고, **반드시 도착해야** 하는 것. 지도, OTA 이미지,
캘리브레이션 데이터.

이 등급이 따로 있는 이유는 실측된 것이다. 8 MB 급 lanelet2 지도가 기본 UDP
소켓 버퍼에서 조각 유실이 나 planner 가 지도를 받지 못했다. 전송 설정 하나가
자율주행 기능을 통째로 망가뜨린 사례이고, 그래서 대용량·transient 트래픽은
별도 등급으로 다룬다.

| 항목 | 값 |
|---|---|
| reliability | RELIABLE |
| durability | TRANSIENT_LOCAL |
| history | KEEP_LAST 1 |
| deadline | 주기 × 5 |
| PCP / TC | 2 / 2 |
| DSCP | 10 (AF11) |
| 셰이퍼 | CBS |
| 선점 | preemptable |
| 등급 상한 | 500 Mb/s |
| 기본 도메인 | diagnostics |

TRANSIENT_LOCAL 이 기본인 이유는 지도의 경우 늦게 붙는 구독자가 지도를 받는
유일한 길이기 때문이다. 대용량 전송에서 이 설정은 비싸므로, 도착 후 다시 읽을
필요가 없는 스트림은 `durability` 를 VOLATILE 로 내려 쓸 수 있다.

### SDV_QOS_2 — REALTIME

주기적이고 마감이 있으나 한 샘플을 버려도 되는 것. 라이다, 카메라, IMU, GNSS,
궤적, 차량 상태.

| 항목 | 값 |
|---|---|
| reliability | BEST_EFFORT |
| durability | VOLATILE |
| history | KEEP_LAST 2 |
| deadline | 주기 × 2 |
| latency budget | 10 ms |
| jitter budget | 5 ms |
| PCP / TC | 4 / 4 |
| DSCP | 34 (AF41) |
| 셰이퍼 | CBS |
| 선점 | preemptable |
| 등급 상한 | 2,500 Mb/s |
| 기본 도메인 | adas |

BEST_EFFORT 인 것이 이 등급의 요점이다. 이미 더 새로운 샘플이 있는 센서 값을
재전송하는 것은 링크를 낡은 데이터에 쓰는 일이다. 유실은 deadline 으로
보고되지 재전송으로 메우지 않는다.

셰이퍼가 CBS 인 이유는 이 트래픽이 버스트로 오기 때문이다. 라이다 한 스캔은
1,342 프레임이고, 게이트를 열어 한꺼번에 내보내는 것보다 크레딧으로 펴는 쪽이
다른 등급의 지터를 덜 해친다.

### SDV_QOS_3 — SAFETY_CRITICAL

제어 명령, 측위 자세, 결함 보고. 놓치면 안전이 깨지지만 경로 이중화까지는
요구하지 않는 것. **과제 KPI 가 직접 겨누는 등급이다.**

| 항목 | 값 | 출처 |
|---|---|---|
| reliability | RELIABLE | |
| durability | VOLATILE | |
| history | KEEP_LAST 1 | |
| deadline | 주기 × 1.5 | |
| latency budget | 5 ms | KPI |
| jitter budget | 2.5 ms | KPI |
| liveliness | AUTOMATIC | |
| PCP / TC | 6 / 6 | 802.1DG |
| DSCP | 46 (EF) | |
| 셰이퍼 | TAS (802.1Qbv) | |
| 선점 | express (802.1Qbu) | |
| 등급 상한 | 50 Mb/s | |
| 기본 도메인 | chassis |

### SDV_QOS_4 — REDUNDANT_SAFETY

SAFETY_CRITICAL 에 경로 이중화를 더한 것. 한 링크나 한 스위치가 죽어도 끊기지
않아야 하는 스트림.

SDV_QOS_3 과 같되 다음이 다르다.

| 항목 | 값 |
|---|---|
| PCP / TC | 7 / 7 |
| 이중화 | 802.1CB frer_dual, 경로 2 |

복제분이 네트워크를 지나가므로 **승인 계산에서 대역을 두 배로 센다.**

## 2.6 DEADLINE 은 지연 예산이 아니다 (필수)

두 값은 자주 섞이고, 섞으면 정상 동작하는 스트림이 계속 위반으로 보고된다.

| 값 | 뜻 |
|---|---|
| `latency_budget_ms` | 토커가 보낸 뒤 리스너에 닿기까지. **KPI 가 겨누는 값.** |
| DEADLINE | 샘플과 샘플 사이의 최대 간격. 발행이 멎은 것을 잡는 장치. |

DEADLINE 은 **오로지 주기에서 파생한다.**

```
deadline = 주기 × deadline_tolerance
```

| 등급 | tolerance |
|---|---|
| BEST_EFFORT | 10.0 |
| BULK_DATA | 5.0 |
| REALTIME | 2.0 |
| SAFETY_CRITICAL | 1.5 |
| REDUNDANT_SAFETY | 1.5 |

이 규칙은 실측으로 얻었다. 초안에서는 SAFETY_CRITICAL 의 DEADLINE 을 KPI 와
같은 5 ms 로 두었는데, 주기 10 ms 인 제어 스트림을 모니터에 걸자 건강한 표본
500개 중 **499개가 마감 위반**으로 집계됐다. 100 Hz 로 도는 발행자가 5 ms 마다
샘플을 낼 수는 없다.

등급별 상한(`deadline_max_ms`)을 두는 두 번째 안도 틀렸다. 1 Hz 로 도는 결함
보고에 1.5 s 마감은 옳은 값인데 상한이 그것을 거부했다. 느린 안전 스트림이
문제가 아니었다.

진짜 문제는 **주기가 없는 스트림에 주기를 지어낸 것**이다. 래치
(transient_local) 토픽 — 경로, 지도, MRM 상태 — 은 이벤트로 한 번 발행되고
끝난다. 이런 스트림은 `event_driven` 으로 표시하고 **DEADLINE 을 끈다**
(DURATION_INFINITE). 대역 예산을 위해 대입한 주기는 대역 계산에만 쓴다.

## 3. PCP 7 을 애플리케이션에 주는 것에 대하여

이 프로파일은 REDUNDANT_SAFETY 에 PCP 7 을 배정한다. 대가를 명시해 둔다.

PCP 7 은 통상 gPTP(802.1AS)와 TSN 재구성 트래픽의 자리다. 애플리케이션 등급이
7 을 쓰면 시간 동기 트래픽이 최상위 우선순위를 애플리케이션과 나눠 쓰게 된다.
시간 동기가 밀리면 TAS 게이트 자체가 밀리므로, 이것은 이론적 위험이 아니라
**측정해야 할 항목**이다.

프로파일에 토글이 있다.

```yaml
policy:
  allow_pcp7_for_applications: true    # 기본
```

`false` 로 두면 REDUNDANT_SAFETY 가 6, SAFETY_CRITICAL 이 5 로 내려가고 7 은
network_ctrl 전용이 된다. 두 설정이 한 파일 안에 있는 이유는, 손으로 고친 두
벌의 설정이 되면 비교가 불가능해지기 때문이다.

**시험 항목 (Part 5):** 두 설정에서 부하 중 gPTP 오프셋 분포를 비교한다.

## 4. 대역 계산 규칙 (필수)

대역은 페이로드가 아니라 **선로 위의 바이트**로 센다.

VLAN 태그가 붙은 프레임 하나의 오버헤드:

```
프리앰블+SFD  8
MAC 헤더     14
802.1Q 태그   4
FCS           4
IFG          12
             ──
             42 바이트
```

RTPS over UDP/IPv4 상위 헤더: IP 20 + UDP 8 + RTPS 56 = **84 바이트**.

MTU 를 넘는 샘플은 조각화되고 **모든 조각이 자기 오버헤드를 낸다.**

| 예 | 페이로드 | 프레임 | 선로 바이트 |
|---|---|---|---|
| 제어 명령 | 128 B | 1 | 254 B |
| 측위 자세 | 512 B | 1 | 638 B |
| 라이다 스캔 | 1.9 MB | 1,342 | 2.07 MB |

라이다 한 스캔을 프레임 하나로 계산하면 대역 계획이 **8 %** 낙관적이 되고,
작은 제어 메시지에서는 오버헤드가 트래픽의 대부분이다.

## 5. 링크별 허용 등급 (필수)

| 링크 | 속도 | 허용 등급 |
|---|---|---|
| multigige_10g | 10 Gb/s | 전부 |
| multigige_2g5 | 2.5 Gb/s | 전부 |
| multigige_1g | 1 Gb/s | 전부 |
| t1s_10m | 10 Mb/s | BEST_EFFORT, SAFETY_CRITICAL, REDUNDANT_SAFETY |

10BASE-T1S 에 REALTIME 이나 BULK_DATA 를 배치하는 것은 거부된다. 엣지 버스는
상태와 명령을 위한 것이고, 10 Mb/s 에 센서 페이로드를 얹으면 그 세그먼트의
제어 트래픽이 같이 죽는다.

또한 10BASE-T1S 멀티드롭에는 게이트도 크레딧도 없다. TAS/CBS 를 요구하는
등급은 strict priority 로 내려가고, 그 사실이 배치 결과에 남는다.

## 6. 승인 규칙 (필수)

- 포트 제공 대역이 **선로 속도의 75 %** 를 넘으면 승인하지 않는다.
  802.1Qav 크레딧 거동과 실제 토커의 버스트성은 포화에 가까워질수록 급격히
  나빠지고, 과제의 지터 목표는 부하가 걸린 망에서 재는 값이다.
- 802.1CB 로 복제되는 스트림은 대역을 두 배로 센다.
- 등급 상한(`max_rate_mbps`)을 넘는 스트림은 개별적으로 거부된다.
- 보호 윈도우의 합이 TAS 사이클에 들어가지 않으면 **반올림하지 않고 실패**한다.

## 7. 등급 선택 지침 (권고)

| 데이터 | 등급 |
|---|---|
| 라이다 / 카메라 원본 | REALTIME |
| IMU / GNSS | REALTIME |
| 측위 자세, 운동 상태 | SAFETY_CRITICAL |
| 계획 궤적 | REALTIME |
| 제어 명령, 액추에이터 명령 | REDUNDANT_SAFETY |
| 차량 상태 (속도, 조향각, 기어) | SAFETY_CRITICAL |
| 결함 보고, 재구성 명령 | REDUNDANT_SAFETY |
| lanelet2 지도, 포인트클라우드 지도 | BULK_DATA |
| 진단, 통계, 헬스 | BEST_EFFORT |
| HMI 상태 | BEST_EFFORT |
