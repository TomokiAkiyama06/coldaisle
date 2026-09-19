# 決定記録（Decision Records）

設計・運用上の決定はすべてここに集約する。
**ADR と「プロジェクト決定」を分けない。** 1人で開発する規模では、
探す場所が2箇所になる不利益のほうが大きい。

## 命名

```text
docs/decisions/NNNN-<slug>.md      例: 0002-metric-naming.md
```

- **連番。日付ではない。** 同日に複数出ても衝突せず、「0002 で決めたとおり」と参照できる
- **番号は再利用しない。** 取り下げた決定も欠番にせず `Status: Rejected` で残す

## 追記のみ

**既存の決定記録を書き換えない。**
変更が必要なら新しい記録を作り、`Supersedes` で旧記録を指す。
旧記録側への `Superseded by` の追記だけが例外。

「なぜ当時そう決めたか」が消えると、同じ議論を半年後に繰り返す。

決定の内容ではない付随情報（Status の遷移、改名に伴うリンクの追従）の更新は
書き換えに当たらない。

## Status

| 値 | 意味 |
|---|---|
| `Proposed` | レビュー中。PR のマージをもって `FINAL` になる |
| `FINAL` | 有効な決定 |
| `Superseded` | 後続の記録に置き換えられた。`Superseded by` を付ける |
| `Rejected` | 取り下げ。番号は欠番にせず残す |

## テンプレート

```markdown
# 決定記録 NNNN: <題名>

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: YYYY-MM-DD
- **Supersedes**: なし
- **関連**: <参照する要件・レビュー・他の決定記録>
- **対象 Issue**: #N

## 1. Context

何が問題で、なぜ今決める必要があるのか。

## 2. Decision

決めた内容。実装が参照できる具体性で書く。

## 3. Consequences

良くなること / 悪くなること。悪くなることには緩和策を添える。

## 4. 却下した代替案

案と、却下した理由。**ここが最も後から効く。**

## 5. 未決事項

先送りした論点と、どこで決めるか。
```

## 一覧

| 番号 | 内容 | Status |
|---|---|---|
| [0001](0001-initial-project-decisions.md) | プロジェクト初期の決定（D-01〜D-19、V-01） | FINAL |
| [0002](0002-metric-naming.md) | メトリクス命名規約とDBスキーマ（ロング形式） | FINAL |
| [0003](0003-device-json-schema.md) | デバイス出力 JSON スキーマ v1 | Proposed |
| [0004](0004-storage-read-contract.md) | ストレージ層の読み出し契約 | FINAL |
| [0005](0005-model-selection.md) | ローカルモデルは Qwen3.8-27B 単体構成 | FINAL |
| [0006](0006-gpu-mode-and-mixed-state.md) | GPU Mode を2段階にし `mixed` を異常として扱う | FINAL |
| [0007](0007-ingest-pipeline.md) | 取り込みパイプラインの規約 | Proposed |
| [0008](0008-rollup-and-retention.md) | ロールアップ・保持期間・日次CSVの規約 | Proposed |
| [0009](0009-read-api.md) | 読み取り API の契約 | Proposed |
| [0010](0010-csv-replay.md) | CSV 再生の規約 | Proposed |
| [0011](0011-dashboard.md) | 開発用ダッシュボードの方針 | Proposed |
| [0012](0012-rule-engine.md) | ルールエンジンの規約 | FINAL |
| [0013](0013-notifications.md) | 通知の規約 | Proposed |
| [0014](0014-llm-provider.md) | LLM Provider の規約 | Proposed |
| [0015](0015-llm-tools.md) | 読み取り専用ツールの規約 | Proposed |
| [0016](0016-evidence-alerts.md) | アラート説明の Evidence 形式 | Proposed |
| [0017](0017-daily-report.md) | 日次レポートの規約 | Proposed |
| [0018](0018-tool-exposure.md) | AI 向けツールの公開方法 | Proposed |
| [0019](0019-claude-escalation.md) | Claude へのエスカレーション | Proposed |
| [0020](0020-decision-memory.md) | 運用メモリへの記録 | Proposed |
| [0021](0021-public-repo-hygiene.md) | public リポジトリの衛生 | Proposed |
| [0022](0022-firmware-v1.md) | 本番ファームウェア v1 | Proposed |
| [0023](0023-serial-source.md) | シリアル取り込み | Proposed |
| [0024](0024-calibration.md) | 較正の手順と記録 | Proposed |
| [0025](0025-probe-identity.md) | プローブの同定 | Proposed |
| [0026](0026-three-zone-fan-control.md) | Front / Rear / Top を独立Fan zoneとして制御する | FINAL |
| [0027](0027-fan-control-architecture.md) | Fan 制御アーキテクチャ（Supervisor + Learned MPC + Reactive Guard + Critical Safety） | FINAL |
| [0028](0028-fan-control-contracts.md) | Fan 制御の層間契約（入出力・優先順位・状態遷移・周期・故障時の扱い・設定・承認点） | FINAL |
| [0029](0029-telemetry-loss-classes.md) | 制御入力の欠測の分類（Critical / Degraded / Advisory） | FINAL |
| [0030](0030-control-decision-trace-storage.md) | Control decision trace の保存先 | Proposed |
| [0031](0031-thermal-dataset-contract.md) | Thermal Dataset v1 の時刻対応と再生成契約 | FINAL |
| [0032](0032-internal-telemetry-metric-names.md) | Internal Telemetry のメトリクス名 | FINAL |
| [0033](0033-air-balance-config-boundary.md) | Air Balance characterization の設定境界 | FINAL |
| [0034](0034-unavailable-fan-tach-safety.md) | 制御対象 Fan の tach 読み取り不能を Safety fault にする | FINAL |
| [0036](0036-transient-cpu-gpu-regime.md) | Workload Regime に TRANSIENT_CPU_GPU を加える | FINAL |
| [0037](0037-model-registry-rollback-target.md) | Model Registry の promotion 時の rollback target | Proposed |
| [0038](0038-internal-telemetry-outage-counting.md) | Internal Telemetry の欠測の数え方（有効な間の停止は欠測、無効期間は数えない） | FINAL |
| [0039](0039-dashboard-labels-and-catalog.md) | ダッシュボードの表示名と `GET /api/v1/metrics` | FINAL |
| [0040](0040-server-health-api.md) | Server Health API の契約（`/server-health` への一本化、signal 規則、機種が公開しない metric の扱い） | FINAL |
| [0041](0041-supervisor-proposal-freshness.md) | 非同期 RL Supervisor 提案の有効性（期限の起点と Regime の一致） | FINAL |
| [0042](0042-server-health-signal-rules.md) | Server Health の signal 判定規則の詳細（パネルは表示専用、監視対象と quality・source 状態の導出、アラート全件、1スナップショット） | FINAL |
| [0043](0043-cpu-die-and-gpu-throttle-metrics.md) | CPU die 温度（k10temp）と GPU の T.Limit margin・throttle reason・fan speed のメトリクス名 | FINAL |
| [0044](0044-node-in-ci-for-dashboard-tests.md) | ダッシュボードの JS テストのために CI へ Node を入れる（テスト専用。ビルドには使わない） | FINAL |
| [0045](0045-local-socket-write-entry.md) | 書き込み専用のローカル Unix ソケット入口（GPU Mode イベント / Workload Hint） | FINAL |
| [0046](0046-airflow-ui.md) | エアフロー / ファン制御の可視化画面（置き場所・模擬データの分離・色分けの設定） | FINAL |
| [0047](0047-cpu-utilization-metric.md) | CPU 使用率のメトリクス名（`cpu.utilization`） | FINAL |
| [0048](0048-thermal-model-artifact-and-inference.md) | Thermal Model v1 artifactと読み取り専用推論境界 | Proposed |
