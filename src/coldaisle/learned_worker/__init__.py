"""合成の起点: Learned MPC worker（`coldaisle-learnd --role mpc`。#86 / 決定記録 0077 段階 3）。

`coldaisle-fand` が毎 tick 送る frame（`LearnedFrame` v3）の列だけを入力にし、検証した
反実仮想 Thermal Model artifact で `LearnedMpcRuntime.propose()` を `mpc.period_ms` ごとに呼んで、
結果（`MpcProposal`。提案か失敗）を fand の役割ごとのソケットへ返す。

- **出せるのは `requested_demand` までの提案だけ。** Gate → Reactive Guard → Critical Safety は
  fand の中で常に掛かり、この package からそれらを迂回する経路は無い（AGENTS.md ルール2）
- Telemetry・SQLite・API・較正ファイルを読まない（0077 §2.3 / 0101 §2.1）。較正は frame が運ぶ
- hwmon / PWM・シリアル・LLM 層に触れない（`coldaisle.ai` / `coldaisle.api` / `coldaisle.store` /
  `coldaisle.control.hardware` / `serial` を import しない。試験で止める）
- 観測 window・anchor の action・提案を作らない周期の規則は決定記録 0107
"""
