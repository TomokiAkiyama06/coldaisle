# 決定記録 0108: 0100 段 1（export の manifest と `csv_exports`。PR #253）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が推奨案で承認）
- **Date**: 2026-10-07
- **Supersedes**: なし（0100 が「実装の PR で固定する」とした点と、0100 に書かれていない点への追加。0100 / 0008 /
  0096 の決定は置き換えない）
- **関連**: [0100](0100-replay-export-binding.md) §2.1 / §2.2 / §2.6 / §2.8 / §2.10 / §5 #3 / §5 #17 /
  [0008](0008-rollup-and-retention.md) §2.8 / [0096](0096-calibration-binding-digest.md) §2.3 /
  [0102](0102-calibration-change-log-settled-points.md)（同じ形の記録）/
  `src/coldaisle/csv_export_manifest.py` / `src/coldaisle/store/csv_export.py` / `src/coldaisle/store/rollup.py` /
  `src/coldaisle/store/migrations/0012_csv_exports.sql` / `config/retention.yaml` / `docs/ubuntu-deploy.md`
- **対象 Issue**: #237（実装 PR #253）

## 1. Context

0100 の段 1（写像の関数・export の manifest・`csv_timezone` の必須化・日ごとの lock・`csv_exports` の migration と
追記・`export_record_sha256` の関数。0100 §2.10）を PR #253 で実装した。その実装で、0100 が「実装の PR で型と
golden vector を固定する」とした点と、0100 に書かれていない点が5点あり、PR #253 の「人間のレビューが必要な点」で
推奨案とともに示した。2026-10-07、所有者が5点すべてを推奨案で承認し、PR #253 はマージされた。決定記録は追記のみ
なので、0100 の本文には手を入れず、ここに残す。記録だけを読んだ人が、実装と違う形を前提にしないためである。

## 2. Decision

### 2.1 `csv_exports` は `schema` / `schema_version` の列を持たない

**決着（2026-10-07 所有者の決定、推奨案）。** 0100 §2.6 の「列は manifest の欄（`schema_version` 以外）と
`exported_ms`」は、manifest のファイルの形を示す2つの定数（`schema` = `coldaisle.daily_csv_export` と
`schema_version` = 1）をどちらも列に持たない形とする。

- `schema` は 0100 §2.8 の `export_record_sha256` の入力にも入らない定数なので、表に置く意味が無い
- 表の形そのもの（migration 0012）を版 1 に当たるものとみなす。`csv_exports` の行から `export_record_sha256` を
  計算するときは、`schema_version` に定数 1 を使う（manifest から計算した値と同じになる。golden vector で固定）
- manifest の版を上げるときは、表の側も別の migration で扱いを決める（そのときの記録で決める）

### 2.2 `row_seconds_sha256` の bytes の形

**決着（2026-10-07 所有者の決定、推奨案）。** 0100 §2.1 の `row_seconds_sha256` は、各行の絶対時刻
（`ts_ms // 1000`、UTC の Unix 秒）を10進の ASCII にし、末尾に `\n` を付けて行の順に連結した bytes の SHA-256 とする。
行が無い日は空の bytes の SHA-256 とする。行の数と順序も hash に入る。golden vector
（`1787497200\n1787497202\n`）を試験で固定した。

### 2.3 lock を待つ上限は `csv_export_lock_timeout_s: 60`

**決着（2026-10-07 所有者の決定、推奨案）。** 0100 §2.1 / §5 #17 の「lock を待つ上限は設定で持つ」は、
`config/retention.yaml` の `csv_export_lock_timeout_s`（秒）とし、値は 60 とする。1日ぶんの export は1秒もかからない。
キーが無ければ、`csv_timezone` が無いときと同じく `--export-day` だけを拒否する。

### 2.4 export を拒否したときの終了コードは 1。`--timezone` だけでは照合しない

**決着（2026-10-07 所有者の決定、推奨案）。**

- `coldaisle-rollup --export-day` で export を拒否したとき（0100 §2.2 の timezone の欠落・読めない・`--timezone` との
  食い違い、lock の上限の欠落、lock を上限内に取れない）は、構造化ログに理由を出して終了コード 1 で終わる。
  ロールアップと保持期間の適用は済んでおり、`--vacuum` を付けていればそれも従来どおり実行する
- `--timezone` を `--export-day` なしで渡したときは、従来どおり使わず、設定との照合もしない

### 2.5 設定の2つのキーは、型の上で「未設定」を許す

**決着（2026-10-07 所有者の決定、推奨案）。** `RetentionRules.csv_timezone` と `csv_export_lock_timeout_s` は
`None`（キーが無い）を許す。0100 §2.2 は「キーが無いときは export だけを拒否し、ロールアップと保持期間の適用は
従来どおり行う」としており、必須の欄にすると設定の読み込みそのものが失敗してロールアップまで止まるためである。
`None` は「設定されていない」の印で、どの値の代わりにもならない（コードに既定値を置かない。AGENTS.md ルール 9）。
同梱の `config/retention.yaml` には両方を書いた。

## 3. Consequences

### 良くなること

- 記録だけを読んだ人が、`csv_exports` の列の形や `row_seconds_sha256` の bytes を実装と違う形で前提にしない
- 段 2（再生の照合）と段 3（Dataset v2 と学習の入口の照合）が、段 1 で固定した形の上に作れる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| `csv_exports` の行だけを見ても manifest の版が分からない（§2.1） | 表の形を版 1 とみなす。版を上げるときは別の migration と記録で決める |
| export だけが失敗しても、ロールアップは成功しているので終了コード 1 の意味が「一部の失敗」になる（§2.4） | 構造化ログに「日次CSVを書き出さなかった」と理由を出す |
| 古い設定ファイル（2つのキーが無い）でも起動はでき、export だけが止まる（§2.5） | 拒否の理由にキーの名前を出す。0100 §2.2 の意図どおり |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `csv_exports` に `schema_version` の列（CHECK = 1）を足す | 0100 §2.6 の列の指定と違う。表の形が版を表すので得るものが小さい |
| `row_seconds_sha256` を固定長の binary（8 bytes の整数の列）で hash する | 人が手で検算しにくい。10進 ASCII と改行で足りる |
| export の拒否でロールアップの結果まで失敗扱いにしない（終了コード 0 で警告だけ） | 自動実行で export の失敗に気づけない |
| 2つのキーを pydantic の必須の欄にする | 設定の読み込みが失敗し、ロールアップと保持期間まで止まる（0100 §2.2 に反する） |

## 5. 未決事項

なし（再生の照合と fingerprint の版・`dataset_source_run` の記録は 0100 段 2、`ReplayBindingV2` と学習の入口の
照合は段 3。#237 に残る学習の入口の Codex P1 は段 3 以降）。
