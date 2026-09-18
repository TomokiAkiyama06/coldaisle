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
通常Replayはこのcopy / eager hashをしない。後のdataset生成時に元CSVが変わっていれば、CLIの
再hashがDBのsnapshot hashと一致せず生成を拒否する。

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

## 実データ収集後に残る作業

- baseline / characterization / GPU・CPU・同時負荷 / 通常運用runを収集する
- window、sampling period、horizon、target tolerance、metric集合を比較して確定する
- workload regime producerとControl Engineを接続した実contextを収集する
- 欠測率、run間差、各splitの件数を確認して学習可能性を判定する
