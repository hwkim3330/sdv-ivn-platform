# 로드맵

## 2026년 남은 기간

### 2026.09 — 현행 baseline 고정  (진행 중)

| 산출물 | 상태 |
|---|---|
| SDV-IVN Architecture v0.1 (Part 1) | 완료 |
| SDV QoS Profile v0.1 (Part 3) | 완료 |
| DDS/TSN Mapping v0.1 (Part 4) | 완료 |
| Service API v0.1 (Part 2 + OpenAPI) | 완료 |
| Conformance v0.1 (Part 5) | 완료 |
| Autoware Traffic Profile v0.1 | 완료, 27 토픽 |
| QoS 매퍼 레퍼런스 구현 | 완료, 시험 65건 |
| 현재 Fast DDS 성능 측정 | **미착수** |

### 2026.10 — API v0.1 과 자동 생성

| 할 일 |
|---|
| Network Service API 서버 구현 (FastAPI, OpenAPI 자동 노출) |
| Fast DDS 프로파일 자동 생성기를 Autoware 실행 경로에 연결 |
| Vehicle API 의 `Network.*` VSS 가지 정의 |
| Abstract DDS API 최소 표면 구현 (Fast DDS 어댑터 먼저) |
| `ros_ws_gateway.py` 를 5개 서비스로 분해 시작 |

### 2026.11 — DDS 모니터와 TSN 브리지 (FRER 없음) 결선

| 할 일 |
|---|
| DDS 모니터를 rclpy 에 결선 (`dds/monitor/collector.py`) |
| gPTP 동기 확인 후 두 호스트 간 지연 측정 개시 |
| TSN 브리지 (FRER 없음) 에 생성된 YANG 을 실제로 밀어 넣고 확인 |
| 대조군 측정: baseline / pcp-only / full |
| FRER on/off 비교 (TSN 브리지 (선점 없음), 9692 는 FRER 없음) |
| PCP 7 항목: gPTP 오프셋 분포 비교 |

### 2026.12 — 2차년도 데모

```
CARLA / Autoware
      ↓  SDV API
     DDS
      ↓  QoS Policy Mapper
TSN 브리지 (FRER 없음) 망  (+ D10 으로 FRER)
      ↓
Remote node

+ 결함 주입 (링크 단절, 패킷 유실, FRER 경로 상실)
+ 지연 · 지터 대시보드
```

2차년도 목표인 "고속 데이터/고신뢰 QoS 아키텍처와 상용 통신반도체 기반
네트워크 SW 설계"를 직접 보여주는 형태다.

## 2027년 — 개발 반도체로 교체

```
2026   Autoware → SDV API → DDS → Network API → [상용 TSN 브리지]
2027   Autoware → SDV API → DDS → Network API → [국산 IVN 반도체]
```

**애플리케이션과 DDS 와 QoS 정책이 한 줄도 바뀌지 않는 것**이 목표다.
`devices/` 의 어댑터 하나만 갈아끼운다.

`devices/capabilities.yaml` 의 국산 칩 항목은 현재 전부 `null` — 미측정이다.
실물이 오면 `true`/`false` 로 채우고, 그 순간 플랫폼이 어떤 등급을 배치할 수
있는지 자동으로 답한다.

## 표준화

```
2026   KETI Open Specification  (이 저장소)
2027   시험 구현 + 기업 상호운용성 → TTA 표준안 → TTAK 단체표준
2028+  KS / 국제표준 연계 검토
```

OMG DDS-TSN 1.0 이 2026-09 에 Formal 로 나온 시점이 유리하다. 우리 자리는
DDS 를 새로 표준화하는 것이 아니라 **차량용 IVN 반도체에 적용하는 한국형
프로파일**이고, 그것을 선점하는 이야기가 과제 일정과 맞는다.

## 아직 안 한 것 (숨기지 않기)

| 항목 | 상태 |
|---|---|
| 생성된 YANG 을 실제 보드에 밀어 넣기 | **한 번도 안 해봄** |
| 실측 지연·지터 | 없음. 프로파일은 예산만 적어 두었다 |
| 두 호스트 간 시계 동기 | 미확인. 모니터가 이것 없이는 지연을 보고하지 않는다 |
| PointCloud2 의 실제 point_step | 16 B 로 가정. 40 B 면 라이다 대역이 2.5배 |
| 상호운용성 (Fast DDS ↔ Cyclone) | 미착수 |
| 10BASE-T1S 실물 | 없음 |
