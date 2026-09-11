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
