# Autoware — SDV-IVN 레퍼런스 애플리케이션

Autoware 는 이 플랫폼이 아니다. **이 플랫폼을 검증하는 첫 번째 SDV
애플리케이션**이다.

구현은 `hwkim3330/autoware` 에 그대로 둔다. 거기에는 이미
`LiDAR/IMU/GNSS → NDT → Routing → Planning → MPC → Vehicle Gate` 폐루프가
돌고 있고, 백엔드를 CARLA / AWSIM / rosbag / 실지도 / Niro 로 바꿀 수 있다.
그것을 다시 만들 이유가 없다.

여기가 하는 일은 하나다: **그 트래픽을 IVN 관점으로 분류해 프로파일로 쓴다.**

```
hwkim3330/autoware              sdv-ivn-platform
  실제 ROS 2 토픽        →      profiles/autoware/topic_catalog.yaml
                                        ↓  build_profile.py
                                트래픽 행렬 · 승인 판정 · TSN 설정
                                        ↓
                                LAN9692 / D10 / 개발 반도체
```

## 왜 이 애플리케이션인가

과제의 성능 항목은 숫자만으로는 증명되지 않는다. 링크 수, 단말 수, 지연 5 ms,
지터 2.5 ms, 802.1CB 다중화, 가상 도메인 — 이것을 합성 트래픽으로 재면
"패킷 생성기로 잰 수치"가 되고, 실제 SDV 워크로드로 재면 **실증**이 된다.

Autoware 는 마침 안전 수준이 뚜렷하게 갈리는 트래픽을 동시에 만들어낸다.

| 성격 | 예 | 등급 |
|---|---|---|
| 고대역·주기 | 라이다 220 Mb/s, 카메라 414 Mb/s | REALTIME |
| 소형·실시간 | 측위 자세 19 Hz, 융합 자세 50 Hz | SAFETY_CRITICAL |
| 소형·안전 | 제어 명령 100 Hz | REDUNDANT_SAFETY |
| 이벤트·고신뢰 | 결함 보고, MRM 상태 | REDUNDANT_SAFETY |
| 대용량·래치 | 지도 | BULK_DATA |
| 관리 | 진단, HMI | BEST_EFFORT |

한 애플리케이션 안에 다섯 등급이 전부 들어 있다. 그래서 레퍼런스다.

## 쓰는 법

```bash
python profiles/autoware/build_profile.py                 # 트래픽 행렬 + 승인
python profiles/autoware/build_profile.py --yaml          # 프로파일 YAML
python profiles/autoware/build_profile.py --device lan9692  # 배치 계획 (거부 포함)

# 벤치 스위치에 FRER 이 없는 것을 알고 돌릴 때
python profiles/autoware/build_profile.py --device lan9692 \
  --waive 'lan9692:frer:이번 시험은 경로 이중화가 아니라 DDS-TSN 매핑과 지연·지터를 본다'
```

## 옮길 것과 남길 것

공통 기능과 Autoware 종속 기능을 가르는 것이 요점이다.

| Autoware 레포에 남는다 | 이 플랫폼으로 온다 |
|---|---|
| CARLA / AWSIM / rosbag 백엔드 | QoS / TSN / FRER 설정 |
| HMI, 태블릿 앱 | 표준안, API, 스키마 |
| 센서 어댑터, 모드 매니저 (adaptation plugin) | 범용 결함 판정 로직 |
| `ros_ws_gateway.py` (SDV API 클라이언트로 축소) | `fastdds_udp.xml` 의 후신 |
| 차량·시뮬레이터 결선 | LAN9692 / D10 제어 |

`ros_ws_gateway.py` 는 87 KB 한 파일에 ROS 구독, AD API, 경로 관리, 텔레옵,
결함 주입, 재구성, 신호등, 카메라, WebSocket, HMI JSON, 차량 제어가 다 들어
있다. 플랫폼화할 때 `vehicle_service` / `driving_service` / `fault_service` /
`network_service` / `diagnostics_service` 로 가른다. WebSocket 은 그 아래에서
**SDV API 클라이언트** 하나로 남는다.

```
지금:  태블릿 → ros_ws_gateway → ROS 토픽
뒤:    태블릿 → WebSocket 어댑터 → SDV API → DDS / ROS 2
```

## 알아 둘 것 (조사에서 확인됨)

- `ros/roii_reconfig_manager.py` 는 **2026-07-07 에 삭제됐다.** 그 토픽
  `/roii/driving_mode` 는 한 번도 발행된 적이 없다. 실제로 도는 재구성 로직은
  `ros/roii_watchdog.py` 가 `/roii/reconfig_status` 로 낸다.
- lanelet2 벡터 지도(~8 MB)는 **DDS 토픽으로 흐르지 않는다.** 게이트웨이가
  파일에서 직접 읽는다. 그러나 `config/fastdds_udp.xml` 의 32 MiB 소켓 버퍼는
  그 지도의 조각 유실 때문에 존재하고, 그것이 BULK_DATA 등급의 근거다.
- RViz 가 구독하는 ~150개 planning 디버그 토픽은 시각화 전용이다. 어떤 노드도
  발행하지 않으므로 프로파일에 넣지 않는다 (RViz 링크를 모델링할 때만 넣는다).
- `webapp/`, `app/` 에는 ROS 토픽 이름이 없다. WebSocket JSON 계약만 쓴다.
