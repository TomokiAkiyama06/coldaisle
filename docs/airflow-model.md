# Airflow Model

coldaisle v1ではFront / Rear / Topを独立PWM制御する。

## 対象zone

- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top / CPU Radiator: Cooler Master MasterLiquid Atmos II 360

## 基本原則

ファン径とRPMだけから絶対CFMを計算しない。
メーカー公称値はpriorとして保持するが、ケース前面・ラジエーター・グリル・内部圧力差による抵抗を含まないため、装着状態の実風量とはみなさない。

## モデル

各zoneについて次を持つ。

```text
manufacturer metadata
    ↓
PWM -> RPM measured curve
    ↓
Airflow Index 0..1
    ↓
Estimated Effective Flow（q_front / q_rear / q_top）
    ↓
installed thermal effectiveness
```

### Manufacturer metadata

Front NF-A12x25 G2（1基あたり）:
- max RPM: 1800
- max airflow: 63.15 CFM
- max static pressure: 3.14 mmH2O

Top Atmos II 360:
- fan speed: 690〜2500 RPM
- max airflow: 190 CFM
- max static pressure: 3.61 mmH2O

Rearは実機ラベル / 型番を確認し、公開仕様が無ければ測定値中心で扱う。

## Airflow Index

`airflow_index` は0〜1の相対指標とする。
実測CFMではない。

PWM→RPM sweepからlookup tableを作り、必要に応じてpiecewise linearで補間する。
単純な `rpm / max_rpm` は初期proxyとしてのみ使い、最終モデルに固定しない。

`airflow_index` は**その zone 自身の能力比**で、zone をまたいで比べない（Front の 0.5 と Rear の 0.5 は同じ風量ではない）。
zone 間の比較には次の Estimated Effective Flow を使う。

## Estimated Effective Flow

zone をまたいで比べるための共通の尺度として、zone ごとの推定実効風量を持つ（GitHub #75）。

```text
q_front
q_rear
q_top
```

- 物理 CFM である必要はない。内部の単位（EFU: Effective Flow Unit など）でよい
- PWM → RPM の実測、Airflow Index、固定熱負荷での熱応答から作る
- 実測 CFM と誤解されない名前・表示にする

## Thermal effectiveness

固定熱負荷でzoneを1つずつ変化させ、次への効果を測定する。

- GPU Intake
- GPU Exhaust
- Rear Exhaust
- Top Exhaust
- `d.case_delta`
- GPU core / hotspot
- CPU temperature

概念的なresponse matrix:

```text
                  GPU Intake   case ΔT   GPU temp   CPU temp
Front +Δ             ...         ...       ...        ...
Rear  +Δ             ...         ...       ...        ...
Top   +Δ             ...         ...       ...        ...
```

このresponse matrixを使い、同じ追加騒音・PWM変化に対して最も効果の高いzoneを優先する。

## Top control

Topはcoldaisleが管理し、ケース排気だけではなくCPU冷却も担当する。

```text
top_demand = max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)
```

CPU telemetryが失われた場合、Topは安全側の高回転へ移行する。

## Air balance

吸気CFMと排気CFMの厳密一致を目標にしない。
圧力センサーや常設風量計を使わないため、実際の熱応答とコンポーネント温度を優先する。

必要なら一時的な風速計測を検証補助として利用できるが、v1の必須条件にはしない。

```text
estimated_intake  = q_front
estimated_exhaust = q_rear + q_top

balance_ratio = estimated_exhaust / estimated_intake
```

- `q_front` / `q_rear` / `q_top` のどれかが欠測・stale のとき、または `q_front` が 0 以下のときは `balance_ratio` を**計算しない**（0 で割らない）。状態は `UNKNOWN` とし、Front の stall や入力の欠測そのものは Critical Safety（決定記録 0028 §2.7）と入力の分類（決定記録 0029、提案中）で扱う
- **`balance_ratio = 1.0` を固定の正解にしない**
- `d.case_delta`（`air.rear_exhaust - air.front_intake`）は温度差だけで判断せず、推定吸排気量とセットで評価する
- 状態の候補（GitHub #81）: `BALANCED` / `INTAKE_HEAVY` / `EXHAUST_HEAVY` / `THERMALLY_LIMITED` / `UNKNOWN`（比を計算できない）

## Top → Front make-up air

Top の排気が強いと、CPU だけの負荷でも強い負圧になりうる。Front の **requested** は概念上次のとおりとし、具体的な関係は実測（GitHub #50 / #75）で決める。

```text
front_requested_demand = max(gpu_or_case_front_demand, top_makeup_air_demand)
```

これは制御器（Fallback / Learned MPC）が出す requested であり、**別の最終値ではない**。effective は通常どおり Reactive Guard と Critical Safety を通って決まる（決定記録 0028 §2.3 / §2.4）。

Top の CPU 冷却の下限は Critical Safety が持つ（決定記録 0028 §2.4）。ケース換気の都合で CPU 冷却を下げない。
