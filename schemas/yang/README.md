# YANG 모델

이 디렉터리의 모델은 **공표된 표준 원본 그대로**다. 수정하지 않았고, 벤더
모듈을 섞지 않았다.

| 경로 | 내용 | 출처 |
|---|---|---|
| `ieee/` | IEEE 802.1 공표 모델 62개 + `ieee802-types` | YangModels/yang, `standard/ieee/published/802.1` |
| `ieee802.3/` | IEEE 802.3 공표 모델 10개 | 같은 곳, `published/802.3` |
| `ietf/` | IETF 기반 모델 (`ietf-interfaces` 등) | IETF |

## 왜 표준만 두는가

플랫폼의 인터페이스가 표준이어야 2027년에 반도체를 바꿔도 상위가 그대로 가기
때문이다. 벤더 모듈을 여기 두면 그 제품에서만 통하는 경로가 적합성 시험을
통과해 버리고, 다른 하드웨어에서 조용히 깨진다.

벤더가 표준과 다른 경로를 요구하면 그것은 **어댑터의 일**이다 (`devices/`).
번역은 경계 한 곳에서만 일어난다.

## 이 규칙으로 잡은 실제 오류

| 잘못 쓴 것 | 표준 |
|---|---|
| `frame-replication-and-elimination` 컨테이너 | `frer` |
| `gate-control-entry/sgs-params/gate-states-value` | `gate-control-entry/gate-states-value` (평평함) |
| CBS idleSlope 를 **kbps** 로, 벤더 경로에 | `ieee802-dot1q-cbsa:admin-idle-slope`, **bits/second** |
| `frame-preemption-status-table` 를 키 있는 리스트로 | 컨테이너 + `priority0`..`priority7` 여덟 리프 |
| `ip-stream-identification` 에 `priority` 리프 | 그런 리프 없음. 우선순위는 VLAN 태그로 간다 |
| `stream-split[index=N]` | `stream-split[port][direction-out-facing]` |

여섯 개 전부 코드 검토로는 보이지 않는다. 장비가 일반 오류로 거절하거나,
더 나쁘게는 조용히 무시한다.

## 검증기

`model.py` 가 이 모델들을 파싱해 경로 존재 여부를 답한다.

```python
from model import standard_set
ok, why = standard_set().resolve(
    "/ieee802-dot1cb-frer:frer/sequence-recovery[index='0']/history-length")
```

`tests/conformance/test_yang_paths.py` 가 플랫폼이 만드는 **모든 경로**를 매번
이것으로 검사한다.

지원 범위와 한계는 `model.py` 의 docstring 에 적혀 있다. YANG 컴파일러가
아니다 — `when`/`must`/`if-feature` 를 평가하지 않고 타입도 보지 않는다.
경로 존재가 적합성 시험이 필요로 하는 전부다.

## 갱신

```bash
curl -fsSL https://api.github.com/repos/YangModels/yang/contents/standard/ieee/published/802.1 \
  | python3 -c "import json,sys;[print(x['download_url']) for x in json.load(sys.stdin) if x['name'].endswith('.yang')]" \
  | xargs -n1 -P8 curl -fsSLO --output-dir schemas/yang/ieee
python -m pytest tests/conformance/test_yang_paths.py -q
```

모델이 개정되면 시험이 먼저 깨지는 것이 목적이다.
