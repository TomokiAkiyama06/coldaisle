# Thermal Dataset v1

Learned Thermal Model / MPC向けのdatasetは、保存済みTelemetryとControlTick decision trace
から生成する。実機へ接続せず、SQLiteを読み取ってartifactを作るだけである。

契約の正本は[決定記録0031](decisions/0031-thermal-dataset-contract.md)、型は
`coldaisle.control.model.dataset`、生成処理は`coldaisle.dataset`にある。

## artifact

出力ディレクトリには次の2ファイルを置く。

- `manifest.json`: schema version、生成条件、source run ID、raw source / 正規化済みTelemetry /
  ControlTickのSHA-256
- `examples.jsonl`: 1行1 actionのwindow / action / multi-horizon target

全例は`source_run_id`を持つ。windowのセルは元観測時刻、quality、missing / stale maskを
持ち、targetも期待時刻と実観測時刻を区別する。Fan actionにはControlTickの
`effective_demand`を使い、`requested_demand`と介入理由も残す。

## 生成

以下の数値とmetricは説明用の短い例であり、本番確定値ではない。実データで選んだ値を
すべて明示して実行する。CLIにはwindow / horizon等の既定値が無い。

```bash
uv run coldaisle-dataset \
  --db var/replay.db \
  --quality-config config/quality.yaml \
  --output-dir data/thermal/example-run \
  --run-id example-replay-run \
  --source-kind replay \
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
Replayが読む順序でファイル名・境界・内容をhashする。Pythonから使う場合は
`replay_fingerprint(path)`で同じ値を得られる。

## Replayからの再生成

Telemetry CSVは既存経路で同じ時刻のままSQLiteへ投入できる。

```bash
uv run coldaisle-daemon \
  --source replay \
  --csv server_sensor_logs \
  --bulk \
  --db var/replay.db
```

そのDBに同じrunのControlTick traceがあれば、上のdatasetコマンドで決定的に再生成できる。
旧センサーCSVはFan actionを記録していないため、CSVだけからactionを推測してはならない。
Replay中にControl Engineが出したtrace、または保存済みtraceが必要になる。

## time leakageのないsplit

`split_temporally()`へvalidation / testの開始時刻を渡す。各例が使う
`history_start_ms`から`label_end_ms`までが境界を跨ぐ場合、その例は`purged`へ入り、学習・
評価には使われない。ランダムsplitは隣接windowが観測を共有するため使用しない。

## 実データ収集後に残る作業

- baseline / characterization / GPU・CPU・同時負荷 / 通常運用runを収集する
- window、sampling period、horizon、target tolerance、metric集合を比較して確定する
- workload regime producerとControl Engineを接続した実contextを収集する
- 欠測率、run間差、各splitの件数を確認して学習可能性を判定する
