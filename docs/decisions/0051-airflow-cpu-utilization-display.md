# 決定記録 0051: エアフロー画面の CPU 使用率の表示（`cpu.utilization` と「未計測」の判断）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-20、リポジトリ所有者が承認）
- **Date**: 2026-09-20
- **Supersedes**: [0046](0046-airflow-ui.md) §2.5 の表の「CPU 使用率」の行（「未計測」・理由「取得している入力が無い」）と、
  §5 未決事項 #3（CPU 使用率を取得するか）のみ。0046 の他の節は有効
- **関連**: [0046](0046-airflow-ui.md) / [0047](0047-cpu-utilization-metric.md) / `docs/api-contract.md`
- **対象 Issue**: #145（関連: #106, #65。PR #140, #142, #146）

## 1. Context

0046 はエアフロー画面の CPU 使用率を「未計測」と決めた。理由は「取得している入力が無い
（メトリクスが存在しない）」で、取得するかは §5 未決事項 #3 として別 Issue に回していた。

0047（#65 / PR #142）で `cpu.utilization`（`/proc/stat`、単位 %、Advisory）を収集するように
なり、0046 の理由は成り立たなくなった。画面を `cpu.utilization` に接続するにあたり、
「未計測」（そもそも取得していない）と「未取得」（取得する設定だが値が無い）の区別を
どう決めるかが仕様に無い。

`/latest` のキーの有無では決められない（PR #146 の Codex レビュー P2）。

- `proc_stat.enabled: true` で Linux 以外のとき、collector は `value = null` / `missing` の行を保存する
- 収集を無効にする前に保存した行が `/latest` に残る

2026-09-20、リポジトリ所有者がセッション内で 0046 の該当部分を本記録で置き換えることを承認した。

## 2. Decision

### 2.1 画面は `cpu.utilization` を表示する

- 熱源の CPU（側面図の札・「熱源」パネル）の使用率と、グラフ（案B）の「CPU使用率」の帯に
  `cpu.utilization` を使う
- 表示は GPU 使用率と同じ扱い（quality が `ok` 以外の札、内部テレメトリの「読み取り値」）
- 模擬データ（`?mock=`）にも CPU 使用率を入れる（通常 29 % はデザインの値）

### 2.2 「未計測」と「未取得」は収集設定で分ける

`GET /api/v1/airflow/config` に `cpu_utilization.measured`（bool）を追加する。

| `measured` | 画面の CPU 使用率（現在の状態） |
|---|---|
| `false` | **未計測**。`/latest` に行（missing・無効にする前の値）があっても使わない |
| `true` | 値があれば値。値が無い・`missing` なら **未取得**（起動直後の1サンプル等。0047 §2.2） |
| 設定を読めない | 「未計測」と決めつけず、値の有無で表示する |

- `measured = proc_stat.enabled かつ Linux`（`ProcStatAdapter.measures`）。値は
  `config/internal-telemetry.yaml` と実行環境から決まり、**測定値ではない**
  （`airflow/config` は測定値を含まない、という 0046 §2.4 の性質を保つ）
- API と collector は同じ SQLite を読み書きするため、**同じホストで動く前提**で API のホストの
  platform から決める
- collector の現在の状態（取得に失敗しているか）ではない。Linux で読み取りに失敗している間は
  「未取得」になる
- 模擬データの表示は `measured` に左右されない（画面側の値であり、API のホストの設定とは無関係）
- `schema_version` は 1 のまま（項目の追加のみ）

### 2.3 グラフの過去の値

収集を無効にした後でも、グラフの帯は過去に保存された実測値を描く。その時点では計測していた値であるため。

### 2.4 範囲

「未計測」の判断を設定から行うのは CPU 使用率だけ。空気の温度（`air.*`）などは 0046 のとおり
値が無ければ「未取得」。Server Health（0040）には `proc_stat` を加えない。

## 3. Consequences

- CPU 温度を、同じ画面で負荷（使用率）と並べて読める
- 収集していない環境で、残った古い行や missing の行を「未取得」（取れるはずなのに取れていない）と
  誤読させない

| トレードオフ | 緩和策 |
|---|---|
| API と collector が別ホストだと `measured` が実態と合わない | 同じ SQLite を使う構成では起こらない。前提を api-contract に明記した |
| `airflow/config` が設定ファイル以外（実行環境）にも依存する | 値は設定と OS だけから決まり、起動時に1回決める。測定値・状態は含めない |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `/latest` にキーが無いときだけ「未計測」 | Linux 以外の missing の行、無効化前の行を区別できない（Codex P2） |
| Server Health の `sources` に `proc_stat` を加える | 0040 の契約と signal の判定に影響する。表示のためだけに監視の判定を変えない |
| `sys.telemetry_source.proc_stat` の状態を返す | 状態は `unavailable` で、Linux 以外と一時的な読み取り失敗を区別できない（detail は保存されない） |
| 0046 の記載を書き換える | 既存の記録は書き換えない（README「追記のみ」） |

## 5. 未決事項

なし
