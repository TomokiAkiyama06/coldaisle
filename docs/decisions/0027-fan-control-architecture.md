# 決定記録 0027: Fan 制御アーキテクチャ（Supervisor + Learned MPC + Reactive Guard + Critical Safety）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-12、リポジトリ所有者が承認）
- **Date**: 2026-09-12
- **Supersedes**: なし（決定記録としては無い。`docs/requirements.md` の N-01・D-03 を本記録に合わせて改訂した）
- **関連**: [`0026-three-zone-fan-control.md`](0026-three-zone-fan-control.md) /
  [`0012-rule-engine.md`](0012-rule-engine.md) /
  [`0014-llm-provider.md`](0014-llm-provider.md) / [`0015-llm-tools.md`](0015-llm-tools.md) /
  `docs/requirements.md` S-13 / N-01 / N-02 / D-03 /
  `AGENTS.md`「絶対に守るルール」1〜5・「制御アーキテクチャの固定前提」
- **対象 Issue**: #30 / #43 / PR #96 のレビュー指摘（P1）

---

## 1. Context

要件は v1 からファン制御を外していた。

- N-01：「ファン制御・自動シャットダウン等のアクチュエーション」は v1 のスコープ外。**BIOS Q-Fan を唯一の権威として残す**
- D-03：「将来ファン制御を実装する場合も、制御ロジックは Safety-1 に置き、**AIは関与させない**」

一方で、決定記録 0026（2026-09-11、FINAL）は **v1 で Front / Rear / Top を独立 PWM として制御する**と決めており、要件の N-01 と食い違ったままだった。

さらに PR #96 の AGENTS.md は **Supervisor + Learned MPC + Reactive Guard + Critical Safety** を「固定前提」とした。しかしそれに対応する決定記録が無く、レビューで「承認されていない安全系の設計を、実装者への固定の指示にしている」と指摘された（P1）。

2026-09-12、リポジトリ所有者が**この構成を承認し、要件も合わせて直す**と判断した。本記録はその内容を残す。

---

## 2. Decision

### 2.1 v1 に 3系統 Fan 制御を含める

Front / Rear / Top を独立に制御する。内部の制御単位は PWM ではなく **`demand = 0.0..1.0`** とする。

Zone の構成、Top の `max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)`、AIO Pump を制御対象外とすることは**決定記録 0026 のまま**。本記録はその上に載る制御の構成を決める。

### 2.2 制御の経路は固定する

```text
Telemetry
  ↓
State Estimator / Workload Regime
  ↓
Supervisor
  ↓
Learned Thermal Model + Learned MPC
  ↓
Model Confidence / OOD Gate
  ↓
Requested Demand (Front / Rear / Top)
  ↓
Reactive Guard
  ↓
Critical Safety
  ↓
Effective Demand
  ↓
Fan Hardware Mapping
  ↓
PWM
```

**Reactive Guard と Critical Safety を迂回して PWM・hwmon へ書き込む経路を作らない。**

### 2.3 役割を混ぜない

| 層 | 時間軸 | 責務 | ML |
|---|---|---|---|
| Supervisor | 数分〜長期 | 運転戦略・目的関数の重み・Workload Regime | 可（初期は RulePolicy） |
| Learned MPC | 数十秒〜数分 | 未来の予測と Demand の最適化 | 可 |
| Reactive Guard | 数秒 | 急変への即応の floor / ceiling | **不可（決定論的）** |
| Critical Safety | 即時 | 安全制約と最終裁定 | **不可（決定論的）** |

Critical Safety が持つもの：絶対温度の上限、最低安全 Demand、CPU cooling floor、tach stall、telemetry loss、deadman、emergency Max、Manual safety override。**すべて ML から独立させる。**

### 2.4 ML が出せるのは `requested_demand` まで

Learned MPC / Supervisor の出力は **`requested_demand` まで**で、Effective Demand を決めるのは Reactive Guard と Critical Safety である。

**学習不足・未知状態を通常状態として扱わない。** Model Confidence / OOD を判定し、次の場合は Baseline / Fallback Controller へ退避して安全運転を続ける。

- Confidence が低い、または OOD
- ML の停止、optimizer の timeout、モデルの読み込み失敗

**決定記録 0026 のカーブ制御は、この Baseline / Fallback に当たる。**

### 2.5 制御権は段階的に解放する

**Shadow → authority 制限 → full authority** の順とする。学習が不十分な期間は、同じ Interface で RulePolicy を active にし、RLPolicy は shadow で動かせること。

### 2.6 LLM と制御 ML を混同しない

- **LLM は読み取り専用・提案のみ。** Fan Demand や PWM を直接変更しない（N-02 / AGENTS.md ルール1）
- 制御 ML（Supervisor / Learned MPC）は制御専用で、時系列 Window を直接扱ってよい。**生の時系列を LLM のプロンプトに入れない規約（FR-504）は LLM にだけ適用する**

### 2.7 その他の前提

- Workload Regime（`IDLE` / `TRANSIENT_*` / `SUSTAINED_*` / `COOLDOWN` / `UNKNOWN`）は Telemetry から推定する。外部の expected duration は hint / prior としてのみ扱う
- Noise は Thermal Model と分ける。初期は Zone 別の近似 Penalty でよいが、**RPM→dBA の単純比例を真値として扱わない**
- **GPU AI Service が Compute Mode で止まっても制御は継続する。** 制御系を GPU AI Service に依存させない
- Safety floor・Ramp・目的関数の重みなどの定数はコードに埋めず `config/*.yaml` へ（AGENTS.md ルール9）

---

## 3. Consequences

### 良くなること

- ファン制御が、**安全の境界を明示したうえで** v1 に入る（要件・0026・AGENTS.md の食い違いが解ける）
- ML が壊れても、Baseline / Fallback と Critical Safety で安全運転が続く（2.4）
- **LLM がファンに触れる経路が無い**（2.6）
- 未知状態を通常状態として扱わない（2.4）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 安全系の複雑さが増す | Critical Safety を決定論的・ML非依存にし、単独で試験できるようにする |
| 学習に実機のログが要る | Shadow から始める。試験は Mock / Replay / simulated backend で回す（AGENTS.md ルール7） |
| 閾値・Floor の値が実機待ち | #19（ベースライン）・#44（キャラクタライズ）まで暫定。確定したら記録に残す |
| BIOS Q-Fan が唯一の権威ではなくなる | **Safety-0（BIOS / GPU 自身のサーマルスロットリング）は最終防衛線として残す** |
| 制御デーモン自体が止まったときの Fan の挙動 | 実機で確認する（#43） |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| ファン制御を v1 から外したままにする（N-01 のまま） | 決定記録 0026（FINAL）と実機の構成が既にファン制御を前提にしている |
| カーブ制御だけを最終形にする | Baseline / Fallback として残す（2.4）。目標の構成にはしない |
| LLM に Fan Demand を決めさせる | N-02・AGENTS.md ルール1。高額ハードの制御権をローカルモデルに渡さない |
| Critical Safety に ML を入れる | 安全の最終裁定は決定論的でなければ検証できない（2.3） |
| 最初から full authority にする | 学習不足・未知状態を通常状態として扱うことになる（2.5） |
| ファン径 × RPM から絶対 CFM / dBA を計算して真値にする | 決定記録 0026 のとおりキャラクタライズで扱う |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | Safety floor・Ramp・各閾値の値 | #19 / #44（実機） |
| 2 | 目的関数の重み、予測の horizon | M9（`thermal-model` / `learned-mpc`） |
| 3 | Acoustic Model（実測 SPL・周波数特性） | M10 |
| 4 | **自動シャットダウンは本記録の範囲外**（N-01 で v1 スコープ外のまま） | 必要になった時点で別の決定記録 |
| 5 | 「coldaisle にファン制御の手段は無い」と書いている既存のコード・文書、AGENTS.md の旧ルール番号の引用 | 追従の PR |
