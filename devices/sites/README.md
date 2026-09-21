# 현장 파일 (site profiles)

실제 장비를 `devices/device-classes.yaml` 의 등급에 붙이는 곳이다. 플랫폼 본체는
벤더를 알지 않으므로, 벤더 이름은 여기서만 나타난다.

```yaml
# devices/sites/my-bench.yaml
site: 우리 벤치
devices:
  switch_a: { class: tsn_bridge_no_frer, transport: coreconf, address: /dev/ttyACM0 }
  switch_b: { class: tsn_bridge_no_preemption, transport: json_rpc, address: 192.168.100.2 }
  edge_1:   { class: edge_multidrop_10m }
  new_chip: { class: unverified }
```

쓰는 법:

```bash
python profiles/autoware/build_profile.py --site devices/sites/my-bench.yaml --device switch_a
```

이 디렉터리의 파일은 공개 저장소에 올리지 않아도 된다. 등급만 맞으면 플랫폼은
동일하게 동작한다.
