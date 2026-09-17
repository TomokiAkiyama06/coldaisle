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
読み替えない。artifactはmanifest JSONとexamples JSON Linesで保存し、manifestには
実際のexamples bytesのSHA-256を持つ。SQLiteとControlTickが正本であり、artifactは
再生成物とする。

maskは単なる補助列にせず、値・quality・元観測時刻との整合をschemaで検証する。
未観測cellは値・quality・元時刻を持たず`missing=true`、`stale=false`とする。
観測時刻があるcellでは`quality=missing`と`value=null`が同値でなければならない。
各metricの元観測時刻はwindow / target内で逆行させず、frame、期待時刻、元観測時刻を
すべてsource run期間内に限定する。

### 2.2 時刻対応

- input windowはaction時刻を含めてよいが、actionより後の観測を入れない
- windowの各時点では、その時点以前の直近観測だけをas-ofで使う
- as-ofで保持した値は元観測時刻を残し、設定された鮮度を超えたら`stale_mask`を立てる
- targetは期待時刻に最も近い実観測を、明示された許容誤差内でだけ採用する。同距離なら
  過去側を選び、採用した時刻を保存する
- targetの探索は必ずactionより後に限定する
- 熱応答のactionは`effective_demand`である。`requested_demand`は介入の分析用に併記する

### 2.3 source runと再生成

manifestはrun alias、source種別、半開期間、公開用source alias、入力SHA-256を持つ。
aliasは`run-<32 hex>` / `source-<32 hex>`という不透明値に限定し、hostname、IP address、
ROM code、絶対パス、CSV basename等の実機識別子を持たない。Replay CSV集合はbasename・
ファイル境界・内容を順序付きでhashするが、basenameそのものはartifactへ保存しない。
さらに、生成時に読んだ正規化済みTelemetryとControlTick集合をそれぞれhashし、同名run
でも入力内容が変わった場合に検出できるようにする。

dataset生成に使うSQLiteは**1 source run専用**とする。保存済みreading / ControlTickが
run期間外にもあるDB、または`sys.ingest_source`がSourceRunのkindと一意に一致しないDBは
拒否する。同時刻の別runが主キー重複として混ざることを避けるため、新規DBをrun開始前に
用意し、別runへ再利用しない。複数metric / ControlTickのSELECTは1つのSQLite read
transactionで囲み、生成中の並行ingestを一部の系列だけへ混ぜない。
Replayではreadingを1行も保存する前に、run alias、source kind、同じfingerprintを
`dataset_source_run`へINSERTする。この表はsingletonかつtriggerで2回目のINSERT / UPDATE /
DELETEを拒否し、builderはSourceRunの3値との完全一致を要求する。これにより、同じ開始時刻の
別Replayでmarkerだけを置換したり、別のCSVに付けた任意hashを同じDBのprovenanceとして
扱ったりできない。既にbind済みのDBは、同じalias / CSVの再投入であっても拒否する。

同一のTelemetryとControlTick traceをSQLiteへ入れれば、同じmanifest / examplesを
生成する。旧来のセンサーCSVにはFan actionが無いため、CSV単体から過去のactionを
復元できるとはみなさない。Control EngineがReplay中に生成したtrace、または同じrunで
保存済みのtraceが必要である。

このPRで提供するCLIの自動bind経路は、入力bytesを開始前にhashできるReplayに限定する。
schemaは`serial` / `mock` / `import`のsource kindを区別できるが、それらのraw source hashを
いつ確定しimmutableにbindするかは、実収集・生成経路を接続する際に別途決める。未確定の
hashを仮置きして非Replayを通さない。

### 2.4 artifactの安全な公開

出力rootとartifact名を分離し、artifact名も`dataset-<32 hex>`の公開用aliasに限定する。
出力rootは実行user所有かつgroup / world writableでないdirectoryに限定する。全path
componentをdirectory file descriptorと`O_NOFOLLOW`で開き、symlinkやFIFOを追従しない。
同じroot内の0700 staging directoryへ、`O_EXCL | O_NOFOLLOW`で2ファイルを書き、file /
directoryをfsyncしてからLinux `renameat2(RENAME_NOREPLACE)`で公開する。

既存artifactは既定で拒否する。明示的な`--force`でも、同じdirfdから読んだ2 regular file
だけで構成され、schema・件数・examples checksumまで検証できたartifactに限り、
`RENAME_EXCHANGE`でstagingと原子的に交換する。symlink、FIFO、通常ファイル、未知entry、
破損artifactは削除しない。安全なrename flagが使えない環境では通常renameへfallbackせず
生成を失敗させる。readerも将来、artifact directoryを一度dirfdで開き、同じdirfdから
manifest / examplesを読みchecksum検証する。

### 2.5 time leakageを防ぐsplit

比率によるランダムsplitは提供しない。時刻境界を明示し、各例が使う全期間
`[history_start_ms, label_end_ms]`を1つの集合だけへ入れる。windowまたはtarget探索範囲が
境界を跨ぐ例はpurgeする。

### 2.6 実測前に固定しない値

`window_ms`、`sample_period_ms`、`horizons_ms`、`target_tolerance_ms`、
`stale_after_ms`、feature / target metric集合はすべて生成時の必須指定とし、コードに
本番既定値を置かない。Issue本文の30 / 60 / 120秒等は候補であり確定値ではない。

## 3. Consequences

- actionと未来targetの対応、および欠測・staleを例単位で監査できる
- Replayで実機なしにdataset builderとprovenance bindingを試験できる
- runごとの専用DBが必要になるが、別runや並行ingestの混入をfail closedで防げる
- artifactの公開にはLinux `renameat2`を必要とする
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
| 複数runでSQLiteを再利用する | 同じ時刻の観測が主キー重複で無視され、source provenanceを証明できない |
| 既存artifactへ2ファイルを直接truncateする | 途中失敗で版が混ざり、symlink経由でroot外のファイルを破壊し得る |

## 5. 未決事項

- window / sampling period / horizon / tolerance / metric集合の本番値は、GPUサーバーと
  外付けセンサーモジュールでbaseline・characterization・負荷試験を収集して決める
- workload regimeは#87の出力がControlTickへ接続された後に実値を収集する
- Replay時にControl Engine全体からControlTickを生成する運用導線は#74以降と統合する
- Serial / Mock / Importのraw source hash確定・immutable bind経路を、それぞれの収集経路と
  統合する（現CLIはReplayだけを許可する）
- 大規模データでParquet等が必要かは、実収集量と学習I/Oを測って決める
