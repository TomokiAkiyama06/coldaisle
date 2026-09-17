# 決定記録 0033: Air Balance characterization の設定境界

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-18
- **Supersedes**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.8 の
  3ファイル境界（本記録がFINALになった場合）
- **関連**: [`0026-three-zone-fan-control.md`](0026-three-zone-fan-control.md)、
  [`docs/airflow-model.md`](../airflow-model.md)、GitHub #75 / #81 / #103
- **対象 Issue**: #81

## 1. Context

Air Balance Modelは、Front / Rear / TopのAirflow Indexをzone間で比較できるEFUへ写像する
characterization、目標balance ratio、熱状態の判定条件を必要とする。これらは#75の実測で
差し替わり、manufacturer CFMやコード定数にはできない。

一方、決定記録0028 §2.8はControl Configを`fan-hardware.yaml`、`safety.yaml`、
`fan-policy.yaml`の3ファイルに限定し、#103は3つを一括検証する。独立した
`air-balance.yaml`を通常のloaderから読むと、ほかのControl Configと異なる版を部分適用できる。

## 2. Decision

次を**提案**する。StatusがFINALになるまでは0028の3ファイル境界を変更しない。

- `air-balance.yaml`を4つ目のControl Configとして追加し、#103の`ControlConfig`が4ファイル
  すべてを検証できたときだけ一括採用する
- decision traceには4ファイルすべてのschema versionとSHA-256を残す
- Air Balance設定は`source.status: uncalibrated | calibrated`と`basis`を必須にする
- `uncalibrated`はMock / Replay testで明示的にopt-inした場合だけ利用でき、runtime controllerは
  起動時に拒否する
- `calibrated`へ変えるには#75のinstalled-system characterization記録を`basis`に付ける
- curveはdemandとAirflow Indexの0.0 / 1.0 endpointを持ち、未定義範囲の外挿を行わない
- 設定のlive reloadは行わず、変更は再起動後の`STARTUP`を経て反映する

本Issueの実装はformatと純粋モデルまでに留める。runtimeの`config/`にはファイルを置かず、
未校正例は`tests/fixtures/`だけに置く。4ファイル一括読み込みは、本記録の承認と#103との統合後に
実装する。

## 3. Consequences

- Characterizationだけが別版になる部分適用を防げる
- 実測前でも明示的なtest opt-inでMock / Replayを進められる
- Control Configが4ファイルになり、#103のschema version更新と既存テストの変更が必要になる
- #75完了まではAir Balanceをruntime authorityへ接続できないが、未校正値で制御する事故を防げる

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 独立YAMLを`ControlConfig`外から直接読む | 設定の一括検証・内容hash・再起動境界を迂回する |
| `fan-policy.yaml`へ全curveを埋め込む | zone characterizationと運転policyの更新周期・根拠が異なり、#75の成果物を独立に追いにくい |
| 未校正値を警告だけでruntime利用する | warningを見落とすと、実測根拠のないqとratioがrequested demandへ影響する |
| manufacturer CFMを初期値にする | ケース装着時の抵抗を含まず、#75 / #81の原則に反する |

## 5. 未決事項

- 本記録をFINALにする所有者レビュー
- #103の4ファイル一括schema versionと移行方法
- #75 dataset / measurement recordを`basis`から機械的に照合する形式
- Air Balanceの推定結果を#82 decision traceへ格納するschema
