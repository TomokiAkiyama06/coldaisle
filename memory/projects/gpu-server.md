---
project: gpu-server
source: coldaisle
---

# gpu-server 運用メモリ

**いまの値はここだけを見れば分かります。** 経緯は Decisions に積まれます。

> **閾値はベースライン測定（#19）まで暫定です。** ここに載っているのは
> 「いまこの値で鳴る」という事実であって、「この値が危険」という意味ではありません。
>
> 更新は `uv run coldaisle-memory`（差分を見せるだけ）→ 確認 → `--apply`。

## Current Facts

<!-- coldaisle:current:start -->
<!-- この節は `coldaisle-memory` が作り直します。手で書き足さないでください。 -->
<!-- 経緯は下の Decisions に積まれます。そちらは書き換えません。 -->

| キー | 項目 | 値 | 出所 | 記録日 |
|---|---|---|---|---|
| `rule.airflow_degraded` | airflow_degraded の設定 | warning / clear=18, fire_after_s=300, metric=d.gpu_delta, threshold=20 | config/rules.yaml | 2026-08-26 |
| `rule.humidity_out_of_range` | humidity_out_of_range の設定 | warning / fire_after_s=600, high=70, high_clear=67, low=20, low_clear=23, metric=air.room_humidity | config/rules.yaml | 2026-08-26 |
| `rule.intake_high` | intake_high の設定 | warning / clear=38, fire_after_s=120, metric=air.gpu_intake, threshold=40 | config/rules.yaml | 2026-08-26 |
| `rule.probe_changed` | probe_changed の設定 | info | config/rules.yaml | 2026-08-26 |
| `rule.rapid_rise` | rapid_rise の設定 | critical / clear=3, fire_after_s=0, metric=air.gpu_intake, slope_window_s=120, threshold=5 | config/rules.yaml | 2026-08-26 |
| `rule.recirculation` | recirculation の設定 | warning / clear=4, fire_after_s=300, metric=d.intake_rise, threshold=5 | config/rules.yaml | 2026-08-26 |
| `rule.room_high` | room_high の設定 | warning / clear=28, fire_after_s=600, metric=air.room, threshold=30 | config/rules.yaml | 2026-08-26 |
| `rule.sensor_fault` | sensor_fault の設定 | critical / clear_s=10, fire_after_s=0, silence_s=30 | config/rules.yaml | 2026-08-26 |
| `rule.sensor_missing` | sensor_missing の設定 | warning / clear_consecutive=3, consecutive=5 | config/rules.yaml | 2026-08-26 |
| `calibration.front_intake` | front_intake の較正オフセット | +0.00 C | config/calibration.json | 2026-08-26 |
| `calibration.gpu_exhaust` | gpu_exhaust の較正オフセット | +0.00 C | config/calibration.json | 2026-08-26 |
| `calibration.gpu_intake` | gpu_intake の較正オフセット | +0.00 C | config/calibration.json | 2026-08-26 |
| `calibration.rear_exhaust` | rear_exhaust の較正オフセット | +0.00 C | config/calibration.json | 2026-08-26 |
| `calibration.room_temp` | room_temp の較正オフセット | +0.00 C | config/calibration.json | 2026-08-26 |
| `calibration.top_exhaust` | top_exhaust の較正オフセット | +0.00 C | config/calibration.json | 2026-08-26 |

<!-- coldaisle:current:end -->

## Decisions

### 2026-08-26

Key: `rule.airflow_degraded`
Decision: airflow_degraded の設定 = warning / clear=18, fire_after_s=300, metric=d.gpu_delta, threshold=20
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `rule.humidity_out_of_range`
Decision: humidity_out_of_range の設定 = warning / fire_after_s=600, high=70, high_clear=67, low=20, low_clear=23, metric=air.room_humidity
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `rule.intake_high`
Decision: intake_high の設定 = warning / clear=38, fire_after_s=120, metric=air.gpu_intake, threshold=40
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `rule.probe_changed`
Decision: probe_changed の設定 = info
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `rule.rapid_rise`
Decision: rapid_rise の設定 = critical / clear=3, fire_after_s=0, metric=air.gpu_intake, slope_window_s=120, threshold=5
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `rule.recirculation`
Decision: recirculation の設定 = warning / clear=4, fire_after_s=300, metric=d.intake_rise, threshold=5
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `rule.room_high`
Decision: room_high の設定 = warning / clear=28, fire_after_s=600, metric=air.room, threshold=30
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `rule.sensor_fault`
Decision: sensor_fault の設定 = critical / clear_s=10, fire_after_s=0, silence_s=30
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `rule.sensor_missing`
Decision: sensor_missing の設定 = warning / clear_consecutive=3, consecutive=5
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/rules.yaml

Key: `calibration.front_intake`
Decision: front_intake の較正オフセット = +0.00 C
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/calibration.json

Key: `calibration.gpu_exhaust`
Decision: gpu_exhaust の較正オフセット = +0.00 C
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/calibration.json

Key: `calibration.gpu_intake`
Decision: gpu_intake の較正オフセット = +0.00 C
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/calibration.json

Key: `calibration.rear_exhaust`
Decision: rear_exhaust の較正オフセット = +0.00 C
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/calibration.json

Key: `calibration.room_temp`
Decision: room_temp の較正オフセット = +0.00 C
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/calibration.json

Key: `calibration.top_exhaust`
Decision: top_exhaust の較正オフセット = +0.00 C
Status: Observed（coldaisle が設定の変化を記録。確定させるのは人）
Evidence: config/calibration.json

