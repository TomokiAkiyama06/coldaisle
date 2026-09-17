# 決定記録 0031: Thermal Dataset v1 の時刻対応と再生成契約

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-18
- **Supersedes**: なし
- **関連**: [`0010-csv-replay.md`](0010-csv-replay.md)、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md)、
  [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md)
- **対象 Issue**: #83

## 1. Context

Learned Thermal Modelは、過去のTelemetry、Fan action、未来の温度を同じ時刻軸で
対応づけた学習データを必要とする。ランダムsplitや不明確なas-of結合を使うと、未来の
観測が入力へ入る、隣接windowがtrainと評価で同じ観測を共有する、requested demandを
実際のactionと誤認する、といったtime leakageが起きる。

window、sampling period、horizon、target許容誤差は実データで評価して決める値である。
外付けセンサーモジュールが無い現時点で、本番値を既定値として固定してはならない。

## 2. Decision

### 2.1 versioned schema

Thermal Dataset v1は、1つのControlTickを基点とする次の構造にする。

- `window`: action時刻までの過去Telemetry。各セルに値、元観測時刻、quality、
  `missing_mask`、`stale_mask`を持つ
- `action`: Front / Rear / Topの`requested_demand`と、実際に適用した
  `effective_demand`を区別し、介入理由を持つ
- `context`: mode、authority、active controller、safety state、fallback、fault、
  workload regimeとconfidence（未接続なら両方を明示的に`null`）
- `targets`: actionより後の複数horizon。期待時刻と実際に採用した観測時刻、quality、
  `missing_mask`を持つ
- `source_run_id`: 全例からmanifestの元runへ辿れる

schemaの意味を変える場合は`schema_version`を上げ、既存artifactを新しい意味として
読み替えない。artifactはmanifest JSONとexamples JSON Linesで保存する。SQLiteと
ControlTickが正本であり、artifactは再生成物とする。

maskは単なる補助列にせず、値・quality・元観測時刻との整合をschemaで検証する。
未観測cellは値・quality・元時刻を持たず`missing=true`、`stale=false`とする。

### 2.2 時刻対応

- input windowはaction時刻を含めてよいが、actionより後の観測を入れない
- windowの各時点では、その時点以前の直近観測だけをas-ofで使う
- as-ofで保持した値は元観測時刻を残し、設定された鮮度を超えたら`stale_mask`を立てる
- targetは期待時刻に最も近い実観測を、明示された許容誤差内でだけ採用する。同距離なら
  過去側を選び、採用した時刻を保存する
- targetの探索は必ずactionより後に限定する
- 熱応答のactionは`effective_demand`である。`requested_demand`は介入の分析用に併記する

### 2.3 source runと再生成

manifestはrun ID、source種別、半開期間、公開可能な論理参照名、入力SHA-256を持つ。
ホストの絶対パスや実機識別子は持たない。Replay CSV集合はファイル名・境界・内容を
順序付きでhashする。さらに、生成時に読んだ正規化済みTelemetryとControlTick集合を
それぞれhashし、同名runでも入力内容が変わった場合に検出できるようにする。

同一のTelemetryとControlTick traceをSQLiteへ入れれば、同じmanifest / examplesを
生成する。旧来のセンサーCSVにはFan actionが無いため、CSV単体から過去のactionを
復元できるとはみなさない。Control EngineがReplay中に生成したtrace、または同じrunで
保存済みのtraceが必要である。

### 2.4 time leakageを防ぐsplit

比率によるランダムsplitは提供しない。時刻境界を明示し、各例が使う全期間
`[history_start_ms, label_end_ms]`を1つの集合だけへ入れる。windowまたはtarget探索範囲が
境界を跨ぐ例はpurgeする。

### 2.5 実測前に固定しない値

`window_ms`、`sample_period_ms`、`horizons_ms`、`target_tolerance_ms`、
`stale_after_ms`、feature / target metric集合はすべて生成時の必須指定とし、コードに
本番既定値を置かない。Issue本文の30 / 60 / 120秒等は候補であり確定値ではない。

## 3. Consequences

- actionと未来targetの対応、および欠測・staleを例単位で監査できる
- Replay / Mockでも実機なしで同じdataset builderを試験できる
- split境界付近の例は減るが、同じ観測がtrainと評価へ重複することを防げる
- JSON Linesは依存追加なしで検査できる一方、大規模学習の列指向形式より非効率である。
  実データ量を確認後、同じversioned schemaを保ったParquet変換を別Issueで検討する

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 行をランダムにtrain / validation / testへ分ける | 重なるwindowから同じ観測が複数集合へ入り、評価が楽観的になる |
| requested demandをactionとする | Safety / Reactive介入後にHardwareへ適用された値と異なり、熱応答の原因を誤る |
| 欠測を0や直前値だけで埋める | 0という実値と区別できず、staleになった時刻も失う |
| horizonの最近傍時刻を記録しない | sampling jitterの影響を監査できず、未来targetの意味が曖昧になる |
| window / horizonに本番既定値を置く | 実データなしの候補を確定値として固定してしまう |

## 5. 未決事項

- window / sampling period / horizon / tolerance / metric集合の本番値は、GPUサーバーと
  外付けセンサーモジュールでbaseline・characterization・負荷試験を収集して決める
- workload regimeは#87の出力がControlTickへ接続された後に実値を収集する
- Replay時にControl Engine全体からControlTickを生成する運用導線は#74以降と統合する
- 大規模データでParquet等が必要かは、実収集量と学習I/Oを測って決める
