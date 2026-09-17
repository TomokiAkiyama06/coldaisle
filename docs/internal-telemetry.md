# Internal Telemetry Collector

`coldaisle-telemetry` は NVML と Linux hwmon を読み、外付けセンサーの ingest daemon と
同じ SQLite `readings` テーブルへ Unix ms のホスト受信時刻で保存する。これにより
`air.*`、GPU、CPU、Board、Fan を同じ timeline から参照できる。

```bash
uv run coldaisle-telemetry --once
uv run coldaisle-telemetry
```

NVML は `nvidia-smi` を起動せず `nvidia-ml-py` から直接読む。GPU が無い、driver が
停止した、または optional な hotspot / memory temperature を公開しない場合も、値を
0で補わず `missing` として記録し、他の source の収集を続ける。source health は
`sys.telemetry_source.nvml` / `sys.telemetry_source.hwmon` の状態遷移として残る。

## hwmon の対応付け

`config/internal-telemetry.yaml` の `sensors` は、実機で確認した driver の `name` と
`*_label` を安定識別子として使う。label が公開されない driver に限り、実機で再起動後も
同じ物理入力を指すと確認した `channel`（`tempN` / `powerN` / `fanN`）を label の代わりに
指定できる。`hwmonN` と絶対 target path は設定しない。collector は poll ごとに
`hwmonN` を探索し直す。

測定種別と保存単位は次のとおり。

| measurement | hwmon 入力 | 保存単位 |
|---|---|---|
| `temperature` | `tempN_input` | C |
| `power` | `powerN_input` | W |
| `rpm` | `fanN_input` | rpm |
| `pwm` | 対応する `pwmN` | % |

設定例の driver / label は実機で `name` と `*_label` を確認して置き換える。public
repository には実機の `hwmonN` や個体識別子を追加しない。

```yaml
- metric: cpu.package
  enabled: true
  driver: example_cpu_driver
  label: Package
  channel: null
  measurement: temperature
  required: true
  minimum: null
  maximum: null
- metric: fan.front.rpm
  enabled: true
  driver: example_super_io
  label: Front Intake
  channel: null
  measurement: rpm
  required: false
  minimum: null
  maximum: null
```

同じ selector が複数見つかった場合は推測で選ばず `missing` にする。T_SENSOR の metric
は決定記録0032（Proposed）で、取得端子名ではなく測定位置を表す
`board.connector_12v2x6` を提案している。未設置の間は
`enabled: false` のままなので Critical 入力には含めない。有効化には #50 で確認した
`minimum` / `maximum` が必要で、範囲外は `suspect` になる。collector 自体が止まった
場合は既存 Store の鮮度判定で最後の値が `stale` になる。

## 実機で残る確認

- CPU Package / CPU Power / VRM / chipset / T_SENSOR の driver と label
- Front / Rear / Top、AIO Pump、VRM Fan の label と RPM/PWM の物理対応
- GPU driver が hotspot / memory temperature を NVML で公開するか
- T_SENSOR の断線時の値と #50 の較正に基づく妥当範囲

これらは観測事実なしに repository へ仮置きしない。fake NVML と一時 hwmon fixture で、
欠測・一部故障・番号変更・曖昧な対応・単位変換を実機なしで検証する。
