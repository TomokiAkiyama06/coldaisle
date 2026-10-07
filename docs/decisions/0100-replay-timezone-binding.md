# 決定記録 0100: 再生の timezone を export と照合する（日次 CSV の横に export の manifest を置き、dataset 用の再生はそれと照合して食い違えば取り込む前に拒否する）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-07
- **Supersedes**: 0099 §2.6 の「再生の timezone」の項のうち、暫定運用（「それまで、v2 の学習に使う再生は本番（export）と
  同じ timezone で行う」）の部分のみ（§2.8。段 3 のマージをもって、人の手順をコードの検査に置き換える）。旧記録側への
  `Superseded by` の追記は、0099（PR #234）がマージされた後、本記録を FINAL にする PR で行う（README「追記のみ」の例外。
  §5 #8）。0008 §2.8 の CSV の形と 0010 §2.7 の「タイムゾーンは呼び出し側から受け取る」は変えない。dataset 用の再生に
  だけ、export の manifest との照合を**足す**（§5 #1 / #4）
- **関連**: [0099](0099-calibration-change-log.md) §2.6（「再生の timezone」の暫定運用。PR #234）/
  [0087](0087-dataset-v2-action-grid.md) §2.6 / [0094](0094-dataset-v2-settled-points.md) §2.4 /
  [0031](0031-thermal-dataset-contract.md) §2.3（1 source run 専用の DB と `dataset_source_run`）/
  [0010](0010-csv-replay.md) §2.1 / §2.7 / [0008](0008-rollup-and-retention.md) §2.8 /
  [0007](0007-ingest-pipeline.md) §2.11 / [0002](0002-metric-naming.md) §2.11（migration は追記のみ）/
  [0096](0096-calibration-binding-digest.md) / [0021](0021-public-repo-hygiene.md)（manifest に識別子を書かない）/
  `src/coldaisle/store/csv_export.py` / `src/coldaisle/store/rollup.py`（`--export-day` / `--timezone`）/
  `src/coldaisle/ingest/replay.py` / `src/coldaisle/daemon.py`（`--timezone`）/ `src/coldaisle/dataset.py`
  （`replay_fingerprint`）/ `src/coldaisle/control/model/dataset.py`（`SourceRun`）/ `src/coldaisle/store/db.py`
  （`dataset_source_run`）/ `src/coldaisle/clock.py` / `docs/thermal-dataset.md` / `config/retention.yaml`
- **対象 Issue**: #237（関連: #233 / #83 / #84 / #86）

## 1. Context

0099（PR #234）は較正の変更を**絶対時刻**（Unix ms, UTC）で本番の DB に記録し、Dataset v2 と学習の入口が
それを読んで、再生（`--source replay`）で作った専用 DB の時刻と照合する（被覆と変更の検査。0099 §2.6）。
0087 §2.6 の検査（期間の中の `calibration_changed`）も同じ照合に頼る。PR #234 の Codex の P1 の指摘
（https://github.com/TomokiAkiyama06/coldaisle/pull/234#discussion_r4205092439）は、この照合の片側である
専用 DB の時刻が、次の理由で**本番の絶対時刻と一致する保証が無い**ことである。

コードで確かめた事実:

- 日次 CSV の時刻は**オフセットを持たないローカル時刻**（`csv_export.TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"`。
  0008 §2.8）。`export_day()` は `datetime.fromtimestamp(ts_ms / 1000, tz=tz)` で書く
- export の timezone は `coldaisle-rollup --timezone`（既定 `Asia/Tokyo`。コードの既定値）から来る。
  **どこにも記録しない**（CSV にも、ファイル名にも、横のファイルにも無い）
- 再生の timezone は `coldaisle-daemon --timezone`（既定 `Asia/Tokyo`。こちらもコードの既定値）から来る。
  `ReplaySource._parse_row` は、時刻にオフセットが**無ければ** `replace(tzinfo=tz)` で当てはめ、
  **あれば**そのオフセットを使う（オフセット付きの時刻は既に読める）
- 両者を照合する仕組みが無い。`replay_sha256`（dataset の `source_sha256`）は CSV の basename・境界・内容だけを
  hash し、`dataset_source_run`（0031 §2.3）も `SourceRun` も timezone を持たない。**同じ CSV を違う timezone で
  再生しても、fingerprint も provenance も同じになる**
- `replace(tzinfo=...)` は `fold=0` で当てはめる。DST のある timezone では、時計が戻る区間（同じローカル時刻が
  2回ある）の2回目の観測を1回目の時刻へ写し、時計が進む区間（存在しないローカル時刻）も例外にならず
  何かの時刻へ写す。**曖昧さに気づく箇所が無い**
- 本番の運用は `Asia/Tokyo`（DST なし）で、export と再生の既定値はたまたま一致している。
  `docs/ubuntu-deploy.md` は日次 CSV の自動実行をまだ配線していない

影響（#237）:

- export と違う timezone で再生すると、専用 DB の時刻全体がオフセットの差だけずれる。0099 の被覆の検査は
  古い行で満たされ、CSV の中にある較正の変更 A→B がずれた期間全体より前に見え、較正の混ざった example が
  被覆と変更の両方の検査を通りうる
- 同じずれは、専用 DB の readings と**本番で保存した絶対時刻の ControlTick**（0031 §2.3 の「同じ run で
  保存済みの trace」を専用 DB へ持ち込む経路を作る場合）の対応も狂わせる。観測と action の組がずれた
  example になる

所有者は 2026-10-07 に、これを 0099 から切り離して別の Issue（#237）と決定記録（本記録）で扱うと決めた。
それまでの暫定運用（v2 の学習に使う再生は本番と同じ timezone で行う）は 0099 §2.6 と `docs/thermal-dataset.md` に
書かれている。**人の手順であり、コードは検査しない。** 本記録はそれをコードの検査へ置き換える方法を決める。

## 2. Decision

### 2.1 timezone の記録先は、日次 CSV の横に置く export の manifest

`export_day()` は CSV と同じディレクトリに、1日1つの **export manifest**（JSON）を書く。名前は仮に
`sensors_YYYY-MM-DD.export.json` とする（`sensors_*.csv` の glob に掛からない名前。§5 #2）。
**CSV の形（列・時刻の書式・ファイル名）は変えない**（0008 §2.8。表計算で開く人と既存の CSV との互換を保つ）。

manifest の欄（仮。実装の PR で型と golden vector を固定する）:

| 欄 | 内容 |
|---|---|
| `schema` / `schema_version` | `coldaisle.daily_csv_export` / `1` |
| `csv_name` | 対になる CSV の basename（`sensors_2026-08-24.csv`）。**basename だけ**で、ディレクトリや絶対パスは書かない（0021） |
| `csv_sha256` | 対になる CSV の bytes の SHA-256 |
| `day` | `YYYY-MM-DD`（ローカルの日） |
| `timezone` | export に使った IANA の timezone 名（設定に書かれた文字列そのまま。`Asia/Tokyo`） |
| `day_start_ms` / `day_end_ms` | その日の `[開始, 終了)` の Unix ms（`day_bounds_ms` の値。DST の日は 23 / 25 時間になる） |
| `timestamp_format` | `csv_export.TIMESTAMP_FORMAT` の値（0099 §2.6 の秒の切り捨ての幅と同じ出どころ） |
| `row_count` | CSV のデータ行の数 |
| `row_seconds_sha256` | 書いた各行の**絶対時刻**（`ts_ms // 1000`、すなわち書いた秒の UTC の Unix 秒）を行の順に並べた列の SHA-256（§2.4 の照合に使う） |

- ホスト名・ROM・パスなどの個体識別子は入れない（AGENTS.md ルール 10）。`tests/test_repo_hygiene.py` の対象外の
  場所（`~/server_sensor_logs`）に置くが、manifest の欄自体が識別子を持たないようにする
- 書き方: CSV と manifest をそれぞれ同じディレクトリの一時ファイルへ書いて fsync し、**CSV を先に、manifest を
  後に** rename する。途中で落ちると「manifest の無い CSV」が残るが、それは §2.6 で v2 の学習に使えない側
  （安全側）に倒れる。同じ日を書き直すときも、古い manifest が新しい CSV と対にならないよう、rename の前に
  古い manifest を消す（`csv_sha256` の照合でも見つかる）

### 2.2 export では timezone を必須にする

- export の timezone は `config/retention.yaml` の新しいキー（仮に `csv_timezone`）だけから取る。**コードに既定値を
  置かない**（AGENTS.md ルール 9。いまの `--timezone` の既定 `Asia/Tokyo` をやめる）。キーが無い・`ZoneInfo` で
  読めないときは、`--export-day` を**拒否して CSV も manifest も書かない**（ロールアップと保持期間の適用は
  従来どおり行う。export だけを止める）
- `coldaisle-rollup --timezone` は残すが、指定したときは設定の値と文字列で一致しなければ拒否する（§5 #3）。
  日次 CSV の日境界とレポートの日境界（`config/report.yaml`）は別の設定のまま
- manifest の `timezone` は設定の文字列をそのまま書く。別名（`Japan` と `Asia/Tokyo`）を正規化しない（§2.3 で
  文字列のまま照合する）

### 2.3 再生の照合（dataset 用の再生）

`--dataset-run-alias` を付けた再生（0031 §2.3。dataset の専用 DB を作る再生）では、`ReplaySource` の constructor が
**DB に何かを書く前**（`bind_dataset_source_run` の前）に次をすべて確かめ、1つでも満たさなければ拒否する
（`SystemExit`。DB は空のまま残り、作り直せる）。

1. 入力のすべての CSV に、対になる manifest がある。manifest は CSV と同じく `O_NOFOLLOW` で開き、regular file で
   なければ拒否する。CSV と同じ snapshot に取り込む（0031 §2.3 の「path を再 open しない」を manifest にも当てる）
2. manifest の `csv_name` と `csv_sha256` が対の CSV と一致する
3. すべての manifest の `timezone` が同じ文字列である（1 run に1つの timezone。§5 #5）
4. 再生に使う timezone は**manifest の `timezone`**とする。`--timezone` を明示して、それが manifest と
   文字列で違えば拒否する（§5 #4）
5. 各行を §2.4 の規則で絶対時刻へ写し、その秒の列の SHA-256 が `row_seconds_sha256` と、行数が `row_count` と
   一致する。各行の時刻が `[day_start_ms, day_end_ms)` に入る
6. §2.5 の曖昧な時刻・存在しない時刻が1行も無い

5 の照合は、取り込みの前に snapshot を1回読み通して行う（定数メモリ。hash を取るだけ）。取り込みの途中で
食い違いに気づいて止めると、DB が入力の一部だけになるからである（0031 §2.3 と同じ理由）。

**食い違いはすべて拒否する。** 片方を信じて読み替えたり、ずらして直したりしない。拒否の理由は構造化ログに
「どの CSV の、どの検査か」で出す（行の値は最初の1行だけ）。

dataset 用でない再生（デバッグや画面の確認。0010）は次のとおりとする。

- manifest が**ある** CSV は、上の 2〜6 を同じく行い、食い違えば拒否する（§5 #6）
- manifest が**無い** CSV は、従来どおり `--timezone` で当てはめる（0010 §2.7 のまま）。起動時に
  「timezone を照合していない」警告を1回出す

### 2.4 manifest と再生で同じ写像を使う

- export 側の秒: `ts_ms // 1000`（`fromtimestamp` へ渡す時刻を秒へ切り捨てたもの。書いた文字列と同じ秒）
- 再生側の秒: CSV の文字列を `datetime.fromisoformat` で読み、manifest の timezone を当てはめて UTC の Unix 秒にする
- 両者の比較は**秒の列の hash** で行う。timezone の名前の一致（§2.3 の 3 / 4）だけでは、tzdata の版の違い
  （規則の変わった過去の日付）や DST の区間の当てはめ方の違いを見つけられない。秒の列の hash は、export が
  知っていた本当の絶対時刻と、再生が作る絶対時刻が**全行で同じ**であることを確かめる
- 写像の関数（ローカル時刻の文字列 ↔ UTC の秒）は1か所に置き、export と再生と試験が同じものを使う
  （0096 §2.4 の「trainer と loader が同じ関数」と同じ向き）

### 2.5 DST の曖昧な時刻と存在しない時刻

- **曖昧な時刻**（時計が戻る区間で、同じローカル時刻に2つの絶対時刻がある）と**存在しない時刻**
  （時計が進む区間）は、dataset 用の再生では1行でもあれば拒否する（§2.3 の 6）。判定は
  `fold=0` と `fold=1` の UTC オフセットが違うかどうか、およびローカル → UTC → ローカルの往復で元に戻るかで行う
- export は存在しない時刻を書かない（`fromtimestamp` の結果は常に存在する）。曖昧な時刻は書くことがあるが、
  manifest の `row_seconds_sha256` は本当の絶対時刻で作るので、仮に 6 を抜けても 5 で食い違う
- `Asia/Tokyo` には DST が無いので、本番の運用ではこの拒否は起きない。DST のある timezone で運用するなら、
  切り替わる日の CSV は v2 の学習に使えない（§3）。曖昧さを解く経路（行ごとのオフセット）は作らない（§4）

### 2.6 timezone の情報の無い CSV

- manifest の無い CSV（試作時の `~/server_sensor_logs` の記録、本記録の実装前に書いた日次 CSV、rename の途中で
  落ちた CSV）は、**dataset 用の再生では拒否する**（§2.3 の 1）。したがって **v2 の学習には使わない**
- 人が後から manifest を書いて足す経路は作らない（「この CSV はこの timezone だったはず」という推測で埋める
  経路になる。0099 §5 #5 と同じ向き）
- 実害は小さい。試作時の CSV には Fan の action が無く、そもそも dataset を作れない（0031 §2.3）。0099 §2.8 で、
  較正の記録の migration より前のデータも被覆が無く v2 に使えない
- dataset 用でない再生には、従来どおり使える（§2.3）

### 2.7 source run に timezone を束縛する

- **fingerprint**: `replay_fingerprint`（`replay_sha256`）に manifest の bytes も含める（CSV ごとに、CSV の後に
  manifest を、basename・長さ・内容で hash する）。同じ CSV でも manifest（timezone）が違えば fingerprint が変わる。
  fingerprint の規則に版を付け、manifest の無い入力の値はいまの規則のまま変えない（§5 #7）
- **`dataset_source_run`**: bind のときに再生に使った timezone も記録する（段 2。migration で列を足す。適用済みの
  migration は書き換えない。0002 §2.11）。builder は DB の timezone と `SourceRun` の timezone の一致を
  `_validate_dedicated_source_db` で求める
- **`SourceRun` と Dataset v2 の manifest**: `SourceRun` に `local_timezone`（manifest 由来の IANA 名）を足す。
  v2 の builder と学習の入口（0099 §2.6）は、`local_timezone` の無い source run を拒否する。v1 は開いたまま
  （v1 の builder は timezone を求めない）
- `coldaisle-dataset` の CLI は `--replay-path` の manifest から timezone を読み、引数では受け取らない
  （人が打つ値を増やさない。食い違う値を入れる経路を作らない）

### 2.8 0099 の暫定運用の解除

0099 §2.6 の「それまで、v2 の学習に使う再生は本番（export）と同じ timezone で行う」は、次がすべて main に入った
時点で、**人の手順からコードの検査に置き換わる**。

1. export が manifest を書き、timezone を必須にする（§2.1 / §2.2）
2. dataset 用の再生が manifest を必須にし、§2.3 の検査を行う
3. fingerprint・`dataset_source_run`・`SourceRun` の束縛（§2.7）と、v2 の builder の検査

その PR で `docs/thermal-dataset.md` の「再生の timezone」の注意を「照合される（決定記録 0100）」に書き換える。
0099 の本文は書き換えない。ただし 0099 だけを読んだ人が失効した手順に従わないよう、本記録の Supersedes に
その部分を書き、0099 のヘッダへ `Superseded by` を追記する（README「追記のみ」の例外。追記は 0099 のマージ後、
本記録を FINAL にする PR で行い、「段 3 のマージをもって」と条件を添える。§5 #8。PR #238 の Codex の指摘）。

### 2.9 実装の段

| 段 | 内容 | 依存 |
|---|---|---|
| 1 | 写像の関数（§2.4）・export の manifest と timezone の必須化（§2.1 / §2.2） | なし |
| 2 | 再生の照合（§2.3 / §2.5 / §2.6）と fingerprint の版（§2.7）、`dataset_source_run` の timezone の migration と bind 時の記録（§2.7） | 段 1 |
| 3 | `SourceRun.local_timezone`・builder の DB との照合・v2 の builder と学習の入口の検査（§2.7）、`docs/thermal-dataset.md` の更新（§2.8） | 段 2、0099 の実装（`CalibrationHistory`） |

`dataset_source_run` の timezone の記録は段 2 に含める。照合した値を段 2 で DB に残さないと、段 2 と段 3 の間に作った
専用 DB は照合を通ったのに timezone を持たず、段 3 の検査を通れない。manifest の path は DB に残らず、後から推測で
埋めることも認めない（§2.6）ので、再生し直すしかなくなる（PR #238 の Codex の指摘）。

### 2.10 試験すべき性質

export（段 1）:

- manifest の `csv_sha256` / `row_count` / `row_seconds_sha256` が書いた CSV と一致する
- `csv_timezone` が無い・読めない設定では、CSV も manifest も書かず拒否する。ロールアップは行われる
- `--timezone` を設定と違う値で渡すと拒否する
- manifest に絶対パス・ホスト名が入らない（`csv_name` は basename だけ）
- CSV の bytes は本記録の前と同じ（0008 §2.8 の形を保つ。既存の golden の試験がそのまま通る）
- 同じ日を書き直したとき、古い manifest が新しい CSV と対にならない

再生（段 2）:

- dataset 用の再生で、manifest の無い CSV が1つでもあれば、`dataset_source_run` に行を書く前に拒否する（DB は空）
- `--timezone` を manifest と違う値で明示すると拒否する。同じ値、または省略なら通る
- manifest の `timezone` だけを書き換える（CSV はそのまま）と、`row_seconds_sha256` の照合で拒否する
- `csv_sha256` の食い違い、`csv_name` の食い違い、manifest が symlink・FIFO なら拒否する
- run の中で `timezone` の違う manifest が混ざれば拒否する
- DST のある timezone（試験では `America/New_York` などの合成データ）で、時計が戻る日に曖昧な時刻の行を含む CSV は
  拒否する。時計が進む日に存在しない時刻を手で入れた CSV は拒否する。切り替わりの区間に行の無い日は通る
- `Asia/Tokyo` の任意の日は通り、保存される時刻が export 前の `ts_ms` の秒の切り捨てと全行で一致する
  （export → 再生の往復）
- dataset 用でない再生: manifest の無い CSV は従来どおり読める（いまの `tests/test_replay.py` が通る）。
  manifest があり食い違えば拒否する
- fingerprint: manifest の無い入力の値は本記録の前と同じ。manifest の bytes を変えると値が変わる

source run（段 2 / 段 3）:

- 段 2: dataset 用の再生は、照合した timezone を bind と同じ transaction で `dataset_source_run` に記録する
- 段 3: `dataset_source_run` の timezone と `SourceRun.local_timezone` が違えば builder が拒否する
- v2 の builder は `local_timezone` の無い source run を拒否する。v1 の builder の挙動は変わらない
- migration は追記のみで、既存の行と読み取り API を変えない

## 3. Consequences

### 良くなること

- 0099 の被覆と変更の検査、0087 §2.6 の検査が、**専用 DB の時刻が本番の絶対時刻と（秒の切り捨てを除いて）
  一致する**ことを前提にできる。人の手順（同じ `--timezone` を打つ）に頼らない
- timezone の名前だけでなく、全行の絶対時刻を照合するので、tzdata の版の違いや DST の当てはめの違いも見つかる
- CSV の形を変えないので、表計算で開く人と既存の CSV、いまの `ReplaySource` の読み方はそのまま
- 拒否は取り込みの前に起きるので、食い違った DB が作られない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| CSV と manifest の2ファイルになり、CSV だけを複写すると v2 に使えなくなる | 安全側（拒否）に倒れる。拒否の理由に「manifest が無い」と出す。複写の手順は `docs/thermal-dataset.md` に書く |
| 既存の CSV は v2 の学習に使えない | action が無く、もともと dataset を作れない（§2.6）。dataset 用でない再生には使える |
| DST のある timezone では切り替わりの日が v2 に使えない | 本番は `Asia/Tokyo`。DST のある場所で運用するなら §5 #9 の代替（UTC の列）を別の記録で検討する |
| dataset 用の再生で、取り込みの前に入力を1回余分に読む | snapshot を読むだけ（定数メモリ）。dataset 用の再生は一括投入で、待ち時間に効かない |
| `--timezone` の既定値をやめるので、`coldaisle-rollup --export-day` の呼び出しに設定が要る | 自動実行はまだ配線していない（`docs/ubuntu-deploy.md`）。設定の例を `config/retention.yaml` に置く |
| fingerprint の規則に版が増える | manifest の無い入力の値は変えない（§2.7）。既に作った dataset の照合は壊れない |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| CSV の時刻を UTC オフセット付き（`2026-08-24T20:16:40+09:00`）にする | DST の曖昧さを根本から消し、`ReplaySource` は既にオフセット付きの時刻を読める。しかし 0008 §2.8 の「従来の出力と同じ形」を変え、表計算が時刻として読めなくなる。既存の CSV と同じディレクトリで形が混ざる。0099 §5 #9 で所有者が「CSV の時刻を ms にする」（CSV の形を変える）を採らなかったのと同じ理由。DST の無い本番では利益が小さい（§5 #9） |
| CSV に絶対時刻の列（`timestamp_utc_ms`）を足す | 行ごとに曖昧さが無く、ms の精度も戻る（0099 の秒の切り捨ても消える）。いまの `ReplaySource` は知らない列を捨てるので古い再生でも読める。ただし 0008 §2.8 の「従来の列だけを保つ」を変える。本記録では採らず、§5 #9 で所有者に聞く |
| CSV の先頭にコメント行で timezone を書く | 表計算と `csv.DictReader` の見出しの読み方を壊す。既存の再生が見出しを読み違える |
| ファイル名に timezone を入れる（`sensors_2026-08-24_Asia-Tokyo.csv`） | IANA 名は `/` を含み、変換の規則が要る。ファイル名を変えると `csv_files()` の並び順と既存の名前の互換が崩れる。内容の照合（秒の列）もできない |
| timezone の名前だけを照合する（秒の列の hash を持たない） | tzdata の版の違いと DST の当てはめの違いを見つけられない（§2.4） |
| `SourceRun` / `dataset_source_run` にだけ timezone を記録する（export 側は記録しない） | 再生した人が指定した値を記録するだけで、export の値と照合できない。#237 の穴そのもの |
| 曖昧な時刻を `fold` で解く（manifest の秒の列に合う `fold` を選ぶ） | 照合のための値を解の選択に使うと、照合が意味を失う。DST の無い本番では利益が無い |
| manifest の無い CSV に、人が manifest を後から書けるようにする | 推測で埋める経路になる（§2.6。0099 §5 #5 と同じ） |
| 食い違ったときに manifest の timezone で読み替えて続ける | 人が打った値と違う動きを黙ってする。`--timezone` を明示したなら食い違いは人の誤りで、知らせて止めるほうがよい（§5 #4） |

## 5. 未決事項（所有者に確認したい点）

| # | 論点 | 推奨 | 代替 |
|---|---|---|---|
| 1 | timezone の記録先 | **CSV の横の manifest（1日1つ）**（§2.1）。CSV の形を変えない | (a) CSV に UTC の列を足す（#9）。(b) ディレクトリに1つの manifest（日ごとの export と書き直しの単位が合わない） |
| 2 | manifest のファイル名 | `sensors_YYYY-MM-DD.export.json`（`sensors_*.csv` の glob に掛からず、CSV と並んで見える） | `sensors_YYYY-MM-DD.csv.json`、または `.manifest/` のような隠しディレクトリ |
| 3 | export の timezone の出どころ | **`config/retention.yaml` の `csv_timezone`（必須・既定値なし）**。`--timezone` は残し、設定と違えば拒否（§2.2） | (a) `--timezone` を `--export-day` のとき必須にし、設定には置かない。(b) `--timezone` を廃止する（呼び出しが壊れる） |
| 4 | dataset 用の再生の timezone の決め方 | **manifest の値を使い、`--timezone` を明示して食い違えば拒否**（§2.3 の 4） | `--timezone` を必須にし、manifest と一致しなければ拒否（人が打つ値が1つ増え、意味は同じ） |
| 5 | 1 run に timezone の違う CSV が混ざるとき | **拒否**（§2.3 の 3）。`SourceRun` に1つの timezone を持たせる | ファイルごとに manifest の値で読む（照合は効くが、source run の timezone が1つに定まらない） |
| 6 | dataset 用でない再生で、manifest があり食い違うとき | **拒否**（§2.3）。デバッグ用でも、ずれた時刻の DB を作る利益が無い | 警告して `--timezone` の値で続ける（0010 §2.7 のまま。デバッグの自由度を残す） |
| 7 | fingerprint と source run への束縛 | **3つとも行う**: fingerprint に manifest を含める（版を付け、manifest の無い入力の値は変えない）・`dataset_source_run` に列を足す・`SourceRun.local_timezone` を v2 で必須にする（§2.7） | (a) fingerprint だけ（manifest が timezone を持つので推移的に束縛される。migration が要らないが、DB と artifact から timezone が直接読めない）。(b) fingerprint は変えず `SourceRun` だけ（同じ CSV に別の manifest を付けた入力を区別できない） |
| 8 | 0099 の暫定運用の扱い | **0100 の Supersedes に 0099 §2.6 の「再生の timezone」の暫定運用の部分を書き、0099 へ `Superseded by`（「段 3 のマージをもって」の条件つき）を追記する**（§2.8）。0099 だけを読んだ人が失効した手順に従わないため（AGENTS.md「決定記録」。PR #238 の Codex の指摘）。追記は 0099 のマージ後、本記録を FINAL にする PR で行う | 0099 を書き換えず `Superseded by` も付けない（暫定運用は「それまで」の条件付きなので段 3 で自然に失効する。ただし 0099 から辿れない） |
| 9 | DST の扱い（行ごとのオフセットへ移すか） | **移さない。曖昧・存在しない時刻を含む CSV は dataset 用の再生で拒否する**（§2.5）。本番は `Asia/Tokyo` で DST が無い | (a) CSV に `timestamp_utc_ms` の列を足し、再生はそれを正とする（DST と秒の切り捨てが両方消える。0008 §2.8 の「従来の列だけ」を変える。0099 §5 #9 の判断と整合させる必要がある）。(b) 時刻をオフセット付きにする（§4） |
| 10 | 実装の Issue の切り方 | **#237 で段 1〜3 を順に別 PR**（§2.9）。段 3 は 0099 の実装の後 | 段 1 / 段 2 を1つの PR にする |
