# Thermal Dataset v1

Learned Thermal Model / MPC向けのdatasetは、保存済みTelemetryとControlTick decision trace
から生成する。実機へ接続せず、SQLiteを読み取ってartifactを作るだけである。

契約の正本は[決定記録0031](decisions/0031-thermal-dataset-contract.md)、型は
`coldaisle.control.model.dataset`、生成処理は`coldaisle.dataset`にある。

## artifact

出力ディレクトリには次の2ファイルを置く。

- `manifest.json`: schema version、生成条件、source run alias、raw source / 正規化済み
  Telemetry / ControlTick / `examples.jsonl`のSHA-256
- `examples.jsonl`: 1行1 actionのwindow / action / multi-horizon target

全例は`source_run_id`を持つ。windowのセルは元観測時刻、quality、missing / stale maskを
持ち、targetも期待時刻と実観測時刻を区別する。Fan actionにはControlTickの
`effective_demand`を使い、`requested_demand`と介入理由も残す。

## 生成

以下の数値とmetricは説明用の短い例であり、本番確定値ではない。実データで選んだ値を
すべて明示して実行する。CLIにはwindow / horizon等の既定値が無い。
現在のCLIは、実機なしで入力bytesのhashを開始前に固定できるReplayだけを受け付ける。
Serial / Mock / Importのprovenance bindは、各収集経路を接続する残作業である。

```bash
uv run coldaisle-dataset \
  --db var/replay.db \
  --quality-config config/quality.yaml \
  --output-root data/thermal \
  --artifact-name dataset-00000000000000000000000000000001 \
  --run-alias run-00000000000000000000000000000001 \
  --source-kind replay \
  --source-alias source-00000000000000000000000000000001 \
  --replay-path server_sensor_logs \
  --start-ms 1000000 \
  --end-ms 2000000 \
  --window-ms 10000 \
  --sample-period-ms 1000 \
  --horizon-ms 5000 \
  --horizon-ms 10000 \
  --target-tolerance-ms 250 \
  --stale-after-ms 3000 \
  --feature-metric air.room \
  --feature-metric air.gpu_intake \
  --target-metric air.gpu_exhaust
```

ReplayではCLIがCSV集合の正規化されたSHA-256を計算する。これは絶対パスを含めず、
Replayが読む順序でbasename・境界・内容をhashする。ただしbasename自体はmanifestへ
保存しない。run / source / artifactには、hostname・IP address・ROM code等を含まない
公開用の不透明alias（32桁のhex）を別途払い出す。Pythonから使う場合は
`replay_fingerprint(path)`で同じdigestを得られる。

## Replayからの再生成

Telemetry CSVは既存経路で同じ時刻のままSQLiteへ投入できる。DBはrunごとに新規作成し、
別runへ再利用しない。builderはrun期間外のreading / ControlTick、または一意に一致しない
`sys.ingest_source`を検出すると拒否する。Replay入力のrun alias・source kind・SHA-256は
取り込み開始前に上書き不能な`dataset_source_run`へbindされ、manifestへ渡すSourceRunとの
完全一致も検証される。bind済みDBへの再投入は、同じ開始時刻・同じCSVでも拒否される。
dataset用Replayはconstructorで入力を定数memoryのchunkごとにunlink済み一時fileへcopyし、
copyと同時にhashする。先頭時刻と全sampleは同じsnapshot bytesから読み、元pathを再openしない。
CSVが何本あってもsnapshotは1つの一時fileに連結し、保持するfile descriptorは1つにする。
`--dataset-run-alias`付きのReplayは`--bulk`でも待ち行列が溢れたsampleを捨てず、空くまで待つ（backpressure）。`--max-samples`での途中停止も拒否する。DBがCSVの一部だけになると、provenanceの全体hashと食い違うためである。
入力をEOFまで取り込め、かつ下表の取りこぼしが1件も無いときだけ、上書き不能な完了の印（`dataset_source_run_complete`）をDBへ記録する。SIGTERM / SIGINTなどで途中停止したrun、または取りこぼしのあったrunには印を付けず、`coldaisle-daemon`は終了コード1を返す（取り込みループ自体は1件の失敗で落ちずに最後まで進む）。builderは完了の印が無いDBを拒否する。そのDBは破棄し、新しいDBで取り込み直す。完了の印はその時点のreadingsの件数とSHA-256（主キー順の全行）を封印として持つ。完了後はtriggerがreadingsへのINSERT / UPDATE / DELETEを拒否するため、`coldaisle-telemetry`の追記は失敗する。`coldaisle-rollup`はbind済みのdataset DBではロールアップと保持期間の適用自体を拒否する（削除0件でもControlTickは消え得るため）。builderは封印したdigestを再計算して照合し、triggerを外したDBなどで完了後にreadingsが変わっていれば拒否する。bind済みのDBでは、完了の印より前（取り込み中）でも、bindしたReplay取り込みのStore以外はreadingsを書けない（別プロセス・別接続は拒否する）。`coldaisle-telemetry`はbind済みのDBでは起動しない。ControlTickは取り込み完了後に記録するため封印せず、manifestの`control_trace_sha256`で追跡する。
通常Replayはこのcopy / eager hashをしない。後のdataset生成時に元CSVが変わっていれば、CLIの
再hashがDBのsnapshot hashと一致せず生成を拒否する。

source → normalizer → storeの各段で、CSVにあったのにDBへ届かないものは次のとおり。
取り込みは止めずに数え、dataset用Replayではどれか1件でもあれば未完了とする。
時刻の逆行とUTF-8として読めないbytesはsourceが例外で止まるため、完了の印は付かない。

| 段 | 取りこぼし | 完了判定 |
|---|---|---|
| Replay | 時刻が空・読めない行（`dropped_rows`） | 数える |
| Replay | 列数がheaderと合わない行（`malformed_rows`。余りは捨て、不足は欠測） | 数える |
| Replay | 空欄でないのに数値として読めないcell（`unparsed_cells`） | 数える |
| Replay | 同じ列へ正規化される見出し（例: `room`と`room_temp`。後の列だけが残る）と、複数の時刻列（例: `timestamp,ts`。先の1列だけを使う）。`header_collisions` | 数える |
| daemon | 待ち行列の溢れ（`queue_drops`。dataset modeはbackpressureで起きない） | 数える |
| daemon | 正規化・保存の例外で捨てたsample（`discarded`） | 数える |
| normalizer | 対応表に無いchannel（`unknown_channels`） | 数える |
| normalizer | seqの飛び（`dropped_samples`。Replayは連番を合成する） | 数える |
| store | 既にある`(metric, ts_ms)`として書かなかった行（`duplicates`） | 数える |

次は取りこぼしではないため数えない。どれも入力hashに含まれ、同じbytesからは同じDBになる。

- header行: 列名でありsampleではない
- 空行: `csv.DictReader`が飛ばす。値を持たない
- 空欄のcell: 欠測（`quality=missing`）として保存される
- 対応表に無い列（例: `vrm_temp`）: dataset契約の外。Replayは既知channelだけを読む
  （決定記録 0010）
- 非有限値: `nan`は欠測（`quality=missing`）、`inf`は値を保存せず`quality=suspect`として残る
  （決定記録 0003 §2.8）。datasetでは`value=null`・`missing_mask=true`のsuspect cellになる
- 対応表に無い列同士の見出し重複: その列は読まないため失う値が無い

```bash
uv run coldaisle-daemon \
  --source replay \
  --csv server_sensor_logs \
  --bulk \
  --dataset-run-alias run-00000000000000000000000000000001 \
  --db var/replay.db
```

そのDBに同じrunのControlTick traceがあれば、上のdatasetコマンドで決定的に再生成できる。
旧センサーCSVはFan actionを記録していないため、CSVだけからactionを推測してはならない。
Replay中にControl Engineが出したtrace、または保存済みtraceが必要になる。

## artifactの公開

生成物はoutput root内のstaging directoryへ先に書き、checksumを含む2ファイルをfsync後、
固定parent lockでwriterを直列化し、同じparent内の`os.rename`でartifact directoryとして
原子的に公開する。path componentのsymlink / FIFOは追従しない。既存artifactは種別や内容を
問わず常に拒否し、上書き機能は提供しない。output rootは実行user所有・非group/world writable
とし、この権限境界内の全writerが固定lockを使う。実装はmacOS / Ubuntu共通である。

## time leakageのないsplit

`split_temporally()`へvalidation / testの開始時刻を渡す。各例が使う
`history_start_ms`から`label_end_ms`までが境界を跨ぐ場合、その例は`purged`へ入り、学習・
評価には使われない。ランダムsplitは隣接windowが観測を共有するため使用しない。

## Thermal Dataset v2（action 列。決定記録 0079 段 1 / 0087）

v2 は v1 と別の型（`schema_version` 2）で、v1 を v2 として読み替えない。型は
`coldaisle.control.model.dataset` の `DatasetSpecV2` / `DatasetExampleV2` / `ThermalDatasetV2`、
生成は `coldaisle.dataset.ThermalDatasetV2Builder` にある。CLI（`coldaisle-dataset`）はまだ v1 だけを作る。

v1 との違い（規則の正本は [0087](decisions/0087-dataset-v2-action-grid.md)）:

- **action 列**: anchor の tick の時刻 `t_a` から、格子 `[t_a, t_a + action_steps × action_step_ms)` の
  step ごとに、区間の開始時刻以前で直近の ControlTick の `effective` を持つ（as-of。丸め・補間をしない）。
  元にした tick の時刻と `tick_id` を step ごとに残す。step 0 は anchor の tick 自身である。
  step `k` は `ActionPlan.steps[k]`（`offset_ms` は区間の終端）と同じ区間である
- **`prior_action`**: anchor の tick より厳密に前で直近の tick の `effective`。v2 の anchor action はこれで、
  `ObservedThermalInput.from_example_v2` はこの値を action に使う。v1 と同じ `action`（anchor の tick 自身）は
  分析用に残す
- **spec の必須の欄**: `action_step_ms` / `action_steps` / `action_stale_after_ms`。**既定値は無い**（値は実データの後に
  選ぶ。0087 §2.2）。各 horizon は `action_step_ms` の整数倍、`action_steps × action_step_ms` は最大の horizon と等しい
- **target**: `[期待時刻 − 許容誤差, 期待時刻]` の観測のうち最も遅いものだけを採る（後ろの観測は、近くても採らない）。
  `label_end_ms` は `t_a + 最大の horizon`
- **作らない example と件数**: 次のどれかに当たる anchor の example は作らず、manifest の `excluded` に理由ごとの
  件数を残す（複数に当たるときは、この順で最初の理由だけに数える）
  1. `stale`: `prior_action` の元の tick、または格子の時刻の as-of の tick が `action_stale_after_ms` 以上古い。
     anchor より前に tick が無い
  2. `discontinuity`: `prior_action` の元の tick から最大の horizon までの隣り合う tick の差、または最後の tick から
     最大の horizon までの差が `action_stale_after_ms` 以上。最大の horizon より後に tick が無い（run の末尾）
  3. `in_step_change`: step の区間の中の tick の `effective` が、どれか1つの zone でも step の値と違う
  4. `restart`: 検査の範囲（`prior_action` の元の tick から、最大の horizon より後の最初の tick まで）で、
     `seq` の順に隣り合う tick の `tick_id` が減る、または同じ
  5. `tick_id_gap`: 同じ範囲で `tick_id` が 2 以上増える（trace の保存に失敗した tick がある）
- **生成全体を拒否する run**: `seq` の順に並べた ControlTick の `ts_ms` が狭義単調増加でない run と、移行前の行
  （`seq ≤ legacy_through_seq`）を含む run。`legacy_through_seq` は migration `0010` が `control_trace_prune` に
  足した列で、行が無い DB では 0、0007 を適用済みの DB では `ts_ms ≤ legacy_until_ms` の行の `MAX(seq)` である
- **較正の変更**: 呼び出し側は宣言された変更（`DeclaredChange`。0056 §2.5）を必ず渡す（無ければ空の tuple）。
  `calibration_changed` が全 example の期間 `[history_start_ms の最小, label_end_ms の最大]` の中にあれば生成を拒否する
- **較正の変更の記録**（決定記録 [0099](decisions/0099-calibration-change-log.md) §2.6）: 呼び出し側は本番の DB から
  `read_calibration_history(path)`（読み取り専用。migration を当てない）で読んだ `CalibrationHistory` を
  `calibration_history` に必ず渡す（既定値は無い。専用 DB には記録が無い）。example が1件以上あれば、
  期間の先頭以前に記録の行が無いとき（被覆が無い）と、記録の行が期間の中にあるときに生成を拒否する。各行は
  CSV の秒の切り捨ての区間 `[floor(ts_ms), ts_ms]`（幅は `csv_export.TIMESTAMP_FORMAT` から導く）の両端を
  変更の時刻とし、宣言との和集合で検査する。example が0件の dataset では被覆と変更の検査をしない（記録の
  読み込みの検証は省かない）。表が無い（migration 0011 の前）・空・検証に外れる記録は「変更なし」と読み替えない
- **再生の timezone**: v2 の学習に使う再生（`--source replay`）は、日次 CSV を書き出したときと同じ `--timezone` で行う。
  CSV の時刻はオフセットを持たず、違う timezone では較正の変更の記録（決定記録 [0099](decisions/0099-calibration-change-log.md)）との
  照合が狂う（照合の仕組みは #237 で決める）
- **再生する CSV**: v2 の学習には、較正の変更の記録（`--calibration-history-db` に渡す DB）と同じ本番の DB から
  書き出した CSV だけを使う（束縛の仕組みは #237 で決める）
- `control_trace_sha256` は run の全 ControlTick を `seq` 付きで hash する（v2 は anchor 以外の tick も使うため）

## 実データ収集後に残る作業

- baseline / characterization / GPU・CPU・同時負荷 / 通常運用runを収集する
- window、sampling period、horizon、target tolerance、metric集合を比較して確定する
- workload regime producerとControl Engineを接続した実contextを収集する
- 欠測率、run間差、各splitの件数を確認して学習可能性を判定する
