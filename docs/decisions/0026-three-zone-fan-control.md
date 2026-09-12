# 0026: Front / Rear / Topを独立Fan zoneとして制御する

- Status: FINAL
- Date: 2026-09-11
- Renumbered: 0021 → 0026（番号の重複を解消。0021 は `0021-public-repo-hygiene` が先に使用していた）

## Decision

coldaisle v1ではFront / Rear / Topを最初から別PWM系統として扱う。

- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top / CPU Radiator: Cooler Master MasterLiquid Atmos II 360

RearはFrontのFan Hubから分離し、別のPWM headerへ接続する。

TopもcoldaisleがPWMを管理する。TopはCPU AIOラジエーターファンでもあるため、最終要求は以下の考え方とする。

```text
top_demand = max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)
```

通常のケース換気はFront + Rearで行い、Topの`case_aux_exhaust_demand`はFront + Rearで不足する場合のみ上げる。

AIO Pumpはcoldaisle制御外とし、BIOS / 固定安全設定を維持する。

## Airflow

風量はファン径×RPMから絶対CFMを計算しない。
メーカー公称値をpriorとして保持し、実機PWM→RPM characterization、Airflow Index、固定熱負荷でのthermal effectivenessを使って制御設計する。

詳細は `docs/airflow-model.md` と logical #44 を参照する。

## Safety

CPU telemetry loss、Top tach stall、Top PWM write failure、fan daemon state未確定時はTopを安全側の高回転へ移行する。
AI / LLMはPWM制御ループへ入れない。
