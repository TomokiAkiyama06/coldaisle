# Reactive Guard

Reactive Guard は、Learned MPC / Fallback が作った `requested_demand` と Critical Safety の
間で、数秒スケールの急変に先回りする決定論的な層である。raw telemetry を再取得せず、
#102 の同一 control tick の `ControlStateSnapshot` だけを読む。

## v1 の trigger と zone

| trigger | snapshot の値 | 一時 floor を掛ける zone |
|---|---|---|
| CPU 温度上昇 | `cpu.package` の trend（℃/s） | Top |
| GPU 温度上昇 | `gpu.0.core` の trend（℃/s） | Front / Rear |
| CPU power 上昇 | `power.cpu.package` の trend（W/s） | Top |
| GPU power 上昇 | `power.gpu.0` の trend（W/s） | Front / Rear |
| 吸気温度上昇 | `d.intake_rise` | Front / Rear |
| GPU hotspot 高温 | `gpu.0.hotspot` | Front / Rear |

trend は State Estimator が `last_changed_mono_ms` の実時間差で計算した `per_second` を使う。
control tick 数や設定上の想定周期で割らないため、sampling jitter があっても同じ変化率を
同じ値として判定する。新しいサンプルがまだ来ていない tick は欠測とはせず、直前の
hysteresis 状態を維持する。signal が `missing` / `suspect` / `stale` になった場合は値を
補完せず、その trigger を解除候補にして既存の hold だけを満了させる。

`telemetry_health=DEGRADED` の間は、決定記録 0029 §2.3 に従って
`fan-policy.yaml` の `degraded_*` 閾値組を使う。Critical 入力の欠測に対して Max や
`fault_demand` を決めるのは Critical Safety であり、Guard は代替しない。

## Hysteresis / hold / trace

各 trigger は `activate_above` で発火し、発火後は `clear_at_or_below` 以下になるまで残る。
解除条件に入っても、最後に発火していた単調時計時刻から `hold_ms` までは floor を維持する。
壁時計の時刻合わせは hold に影響しない。

`ReactiveGuardDecision` は入力 snapshot の schema version / tick id / 時刻と、zone ごとの
`GuardZoneOutput`、比較した値と閾値、使えなかった入力、開始・解除 event を持つ。inactive な
`GuardZoneOutput` は理由を持てない既存契約なので、解除理由は `events` に残す。

上昇への先回りで demand を下げないため、v1 の trigger は floor だけを出し、ceiling は
出さない。型の `ceiling` は将来、別途安全性を定義した行き過ぎ抑制 rule を追加できる境界
として残す。Critical Safety の floor は Guard の ceiling より常に強く、forced Max は
すべてに勝つ（決定記録 0028 §2.4）。

## 実測前の設定

`floor` / `hold_ms` と各 trigger の通常・Degraded の発火/解除閾値は、すべて
`ConfigValue` として `status: provisional | confirmed` を持つ。実運用用の3つの Control
Config はリポジトリに置いていない。#50 / #75 の実測が終わるまでは値を `confirmed` に
せず、値の確定・緩和には決定記録 0028 §2.9 の所有者承認が必要である。

CPU package power の canonical metric `power.cpu.package` は #65 の収集境界と共有する。
実機の hwmon driver / label 対応が未確定の間も、Replay / Mock で同じ snapshot
契約を使える。
