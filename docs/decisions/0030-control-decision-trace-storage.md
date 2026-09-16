# 決定記録 0030: Control decision trace の保存先

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-15
- **Supersedes**: なし
- **関連**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.3 / §5、
  [`0004-storage-read-contract.md`](0004-storage-read-contract.md)、GitHub #82
- **対象 Issue**: #82

## 1. Context

決定記録 0028 は、control tick ごとの判断を decision trace として残すことを定めたが、
保存先（既存SQLiteか別ファイルか）と保持形式は #82 へ未決として残している。

trace は観測値ではなく、requested / effective demand、override、fault、制御状態を
後から再現するための不変な判断記録である。フィールドを通常のメトリクスとして
`readings` に分解すると、schemaの追加時に過去の判断の意味が失われる。

## 2. Decision

同じSQLite databaseに、追記専用の `control_traces` テーブルを置く。

- 主キーは `(ts_ms, tick_id)` とし、同じtraceを上書きしない
- `schema_version` と、そのtickの完全な `ControlTick` JSONを保存する
- 読み出しの期間は既存Storage契約に合わせ半開区間 `[from, to)` とする
- trace JSONはobjectでなければならず、SQLiteとアプリの両方で検証する
- schemaの意味を変える場合は `ControlTick` のschema versionを上げ、既存traceを
  新しい意味として解釈しない

この記録はControl Loggingだけが書く。AI、API、Telemetry Collector、Hardware Backendは
traceを書き換えられない。PWM・hwmon・NVMLへの書き込み経路は持たない。

## 3. Consequences

- 1 tickの理由を単一の不変なrecordとして追跡できる
- 後続のOffline Evaluation / Shadow Modeはschema versionを見て安全に読み分けられる
- 読み取りAPIやexport形式は別Issueで追加する。保存と公開を同時に広げない

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `readings`へmetricとして分解する | 判断の構造・理由・schema versionを失い、派生値を保存しない既存契約にも反する |
| CSV / Parquetだけへ保存する | SQLiteのTelemetryと同じtime baseで原子的に追跡できず、ローカルの再現性が下がる |
| JSONを上書き可能にする | その時点の判断根拠を後から改変でき、事故調査に使えない |

## 5. 未決事項

- traceは `config/retention.yaml` の `control_trace_days`（初期値30日）を保持し、
  `coldaisle-rollup` が期限を過ぎた record を削除する。これは設定必須項目であり、
  コード側に既定値を持たない
- SQLite外へのexportは #90 / #91 で決める
- GPU Cooling SubsystemやVRM Fanを追加する場合のactuator固有フィールドは、責務境界を
  決める後続のDecision Recordでschema versionとともに定める
