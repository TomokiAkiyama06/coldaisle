# 決定記録 0051: エアフロー画面の CPU 使用率の表示（`cpu.utilization` と「未計測」の判断）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-20、リポジトリ所有者が承認）
- **Date**: 2026-09-20
- **Superseded by**: [0049](0049-internal-telemetry-source-kind.md)（§2.1 のうち、CPU 使用率の
  **いまの値**の札を「内部テレメトリの『読み取り値』」に固定する部分のみ。いまの値の札は
  `sys.telemetry_kind` から実測 / 模擬 になる。「未計測 / 未取得」の判断（§2.2）・§2.3・
  その他の節は有効）
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

### 2.2 「未計測」と「未取得」は collector が保存した状態で分ける

`GET /api/v1/airflow/config` に `cpu_utilization.measured`（`true` / `false` / `null`）を追加する。
値は、collector が毎周期保存する `sys.telemetry_source.proc_stat`（`system_state`。
`telemetry_daemon` が `SourceStatus` の値を書く）の直近の値から決める。

| collector の状態 | `measured` | 画面の CPU 使用率（現在の状態） |
|---|---|---|
| `disabled`（`proc_stat.enabled: false`） | `false` | **未計測**。`/latest` に行（無効にする前の値）があっても使わない |
| `ok` / `degraded` | `true` | 値があれば値。値が無い・`missing` なら **未取得**（起動直後の1サンプル等。0047 §2.2） |
| `unavailable`・状態が無い・知らない値 | `null` | 分からない。「未計測」と決めつけず、値の有無で表示する |
| （`airflow/config` を読めない） | — | `null` と同じ |

- **API 側の設定ファイルや OS からは決めない。** collector は `coldaisle-telemetry --config` で
  API と別の設定を使えるため、API 側の設定では collector の実態と食い違う（PR #146 の Codex P2）
- `unavailable` は、Linux 以外（0047 §2.2。`detail: unsupported_platform`）と一時的な読み取り失敗の
  両方で使われ、保存される状態は `detail` を持たないため区別できない。Linux 以外を `disabled` として
  報告するよう adapter を変えることは 0047 §2.2 の定めに反するため行わない。そのため Linux 以外で
  collector を動かした場合、画面は missing の行を「未取得」と出す（誤って値を出すことはない）
- 値は測定値ではなく collector の収集状態（`airflow/config` の測定値を含まない、という 0046 §2.4 の
  性質は保つ）。collector が止まった後は最後に保存された状態が残る
- 画面はページを開いたときに1回読む
- 模擬データの表示は `measured` に左右されない（画面側の値であり、実環境の状態とは無関係）
- `schema_version` は 1 のまま（項目の追加のみ）

### 2.3 グラフの過去の値

収集を無効にした後でも、グラフの帯は過去に保存された実測値を描く。その時点では計測していた値であるため。

### 2.4 範囲

「未計測」の判断を collector の状態から行うのは CPU 使用率だけ。空気の温度（`air.*`）などは 0046 のとおり
値が無ければ「未取得」。Server Health（0040）には `proc_stat` を加えない。

## 3. Consequences

- CPU 温度を、同じ画面で負荷（使用率）と並べて読める
- 収集を無効にした環境で、残った古い行を今の値や「未取得」（取れるはずなのに取れていない）と
  誤読させない

| トレードオフ | 緩和策 |
|---|---|
| Linux 以外で collector を動かすと「未計測」ではなく「未取得」になる | 値は出さない（誤読は「取れていない」側に倒れる）。区別が必要になれば、状態の `detail` の保存を別の決定記録で決める |
| `airflow/config` が設定ファイル以外（collector の状態）も返す | 測定値は含めない。状態は collector 自身が保存したものだけを読む |
| 画面はページを開いたときに1回しか読まない | 収集設定の変更は collector の再起動を伴う稀な操作。再読み込みで反映される |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `/latest` にキーが無いときだけ「未計測」 | Linux 以外の missing の行、無効化前の行を区別できない（Codex P2） |
| Server Health の `sources` に `proc_stat` を加える | 0040 の契約と signal の判定に影響する。表示のためだけに監視の判定を変えない |
| API 側の `config/internal-telemetry.yaml` と OS から決める | collector は別の設定・別のホストで動きうる（Codex P2）。PR #146 の途中の版で採用し、差し替えた |
| Linux 以外では adapter が `disabled` を報告する | 0047 §2.2（Linux 以外は `unavailable`）に反する |
| 0046 の記載を書き換える | 既存の記録は書き換えない（README「追記のみ」） |

## 5. 未決事項

なし
