# 決定記録 0170: telemetry は DB の書き込みロックの待ち切れで落ちず、その周期の書き込みだけを捨てる。保持期間の削除は metric ごとに確定する

- **種別**: Decision Record
- **Status**: FINAL（2026-10-26、リポジトリ所有者の承認）
- **Date**: 2026-10-26
- **Supersedes**: なし
- **関連**: [`0002`](0002-metric-naming.md) §2.4 / [`0038`](0038-internal-telemetry-outage-counting.md) / [`0060`](0060-control-loop-runtime.md) §2.2 /
  [`0117`](0117-telemetry-unit.md) §2.2 / [`0167`](0167-opt-update-recheck-scope.md) / AGENTS.md「コード規約」（例外を握りつぶさない・取り込みループだけ例外）
- **対象 Issue**: #378

## 1. Context

2026-10-25 03:00:07（JST）、導入先で `coldaisle-telemetry` が `sqlite3.OperationalError: database is locked` で終了し、
6 s 後に systemd（`Restart=always`、0117 §2.2）が起動し直した。

- 同じ時刻に `coldaisle-rollup`（毎日 03:00）が動いていて、この日は 13.1 s かかった（前日 9.7 s・翌日 2.0 s）
- 落ちた場所は `telemetry_daemon.py` の `run()` → `insert_sample()` → `transaction()` の `BEGIN IMMEDIATE`。`busy_timeout` は 5000 ms
- 同じ時刻の取り込み（`coldaisle-daemon`）は、取り込みループの例外の規約どおり「サンプルを破棄して継続する」で動き続けた

ロックを長く持ったのは `store/rollup.py` の `apply_retention` と見ている。全 metric の `DELETE` を 1 つのトランザクションでまとめていて、
1 日ぶんの生データの削除の間、書き込みロックを持ち続ける。同じファイルの `_rollup_minutes` は「取り込みの書き込みロック待ちを短く保つ」ため
metric ごとのトランザクションにしてあり、削除だけがその方針から外れていた。

fand が常駐している今、telemetry が落ちると内部 Telemetry が数秒欠け、fand は `cpu_telemetry_stale` / `gpu_telemetry_stale` で Max・fault になる（0060 §2.2。安全側）。
冷却は失わないが、夜間に不要な Max と fault・通知が出る。

AGENTS.md は「例外は握りつぶさない。ただし取り込みループだけは例外」とする。telemetry の収集のループは取り込みと同じく周期ごとに sample を書くループだが、
今は 1 回の書き込みの失敗でもプロセスごと落ちる。どの失敗を捨ててよいかは仕様に無いので、ここで決める。

## 2. Decision

### 2.1 telemetry: BUSY / LOCKED のときだけ、その周期の書き込みを捨てて続ける

- `run()` の周期ごとの Store への書き込み（readings の `insert_sample` と、その周期の `set_system_state`）が、
  **SQLite の BUSY / LOCKED（`sqlite_errorcode & 0xFF` が `SQLITE_BUSY`=5 か `SQLITE_LOCKED`=6）で失敗したときだけ**、その周期の残りの書き込みを捨て、
  構造化ログの warning を出して次の周期へ進む
- 捨てた周期の数を `TelemetryStats` に数え、終了時のログ（`as_fields()`）に出す
- **それ以外の失敗は今のまま上げて落ちる**（BUSY / LOCKED 以外の `OperationalError`、ディスクの満杯・読み取り専用・破損、ほかの例外）。
  再起動で直らない失敗を warning の繰り返しで隠さないため。systemd の再起動の回数（`NRestarts`）で見える形を保つ
- ループの前の `set_system_state(TELEMETRY_KIND_KEY, …)`（出どころの種類、0049 §2.3）は対象外で、今のまま失敗すれば落ちる
- 待ち時間（`busy_timeout` 5000 ms）は変えない。待ち切れた周期を捨てても、次の周期（2.5 s 後）で新しい値が書かれる

### 2.2 rollup: 保持期間の削除を metric ごとのトランザクションにする

- `apply_retention` の `DELETE` を metric ごとにコミットする。1 回の書き込みロックは 1 metric の削除の間だけになる
- 途中で失敗しても、消し終えた metric はそのまま、残りは次の実行で同じ条件で消える（`ts_ms < cutoff` の削除は冪等）
- 返す値（削除件数・cutoff）、「1 分ロールアップ済みの範囲しか消さない」「最終バケットの行は消さない」の安全弁、dataset 専用 DB の拒否は変えない

### 2.3 試験（実機不要。ルール 7）

- 別の接続で書き込みロックを持ったまま telemetry の 1 周期を回すと、落ちずに捨てた周期が数えられ、ロックを外した次の周期で書ける
- BUSY / LOCKED 以外の `OperationalError` は上がる
- `apply_retention` の途中の metric で失敗させると、前の metric の削除は残り、次の実行で残りが消える

### 2.4 導入先への反映

`telemetry_daemon.py` は 0167 の区分 I、`store/rollup.py` は区分 F（fand の import の閉包の `store/`）に当たる。
`/opt` へ入れるときは F の確認（0161 §2.4 の R1〜R3）と I1 を行う。急がないので、次の F の更新とまとめてよい。

## 3. Consequences

### 良くなること

- rollup と重なっても telemetry が落ちず、fand の不要な Max・fault・通知が出ない
- rollup の書き込みロックが短くなり、取り込み（`coldaisle-daemon`）が sample を捨てる回数も減る

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 捨てた周期の内部 Telemetry は欠ける | 周期は 2.5 s で、捨てるのは待ち切れた周期だけ。0038 の欠測の数え方でロールアップに欠測として残る。warning と `TelemetryStats` で数が見える |
| ロックが長く続くと、warning が周期ごとに出続ける | 5 s 待っても書けない状態が続くのは異常なので、見えるほうを選ぶ。fand はその間 stale で Max になる（安全側） |
| 削除が途中で止まると、一部の metric だけ古い生データが残る | 次の実行で同じ条件で消える。保持期間はロールアップ済みの範囲だけを消すので、集計は失われない |
| 反映に R1〜R3 と I1 が要る | §2.4。次の F の更新とまとめる |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| telemetry の書き込みの失敗をすべて捨てて続ける（取り込みと同じ） | ディスクの満杯・権限・破損など、再起動でも直らない失敗が warning の繰り返しに埋もれる。telemetry は取り込みと違い 1 つの DB へだけ書くので、失敗の種類で分けられる |
| `busy_timeout` を延ばす（例 30 s） | 待つ間 telemetry の周期が止まり、fand から見ると同じく stale になる。原因（長いトランザクション）が残る |
| rollup の削除だけ直す | ほかの長い書き手（手での `VACUUM`、将来のジョブ）と重なれば同じく落ちる。所有者が両方を選んだ（2026-10-26） |
| rollup の時刻をずらす | 重なる確率が変わるだけで、落ちる作りは残る |

## 5. 未決事項

なし（所有者が 2026-10-26 に「両方」を選んだ）。
