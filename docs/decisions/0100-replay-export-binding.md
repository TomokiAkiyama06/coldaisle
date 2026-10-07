# 決定記録 0100: 再生の入力を export と照合する（日次 CSV の横に export の manifest を置いて timezone と元の DB を束縛し、dataset 用の再生と Dataset v2 の生成で照合して食い違えば拒否する）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-07
- **Supersedes**: 0099 §2.6 の2つの暫定運用の部分のみ（§2.9。段 3 のマージをもって、人の手順をコードの検査に置き換える）。
  (1)「再生の timezone」の項の「それまで、v2 の学習に使う再生は本番（export）と同じ timezone で行う」、
  (2)「再生の入力と記録の DB の束縛」の項の「それまでの暫定運用: v2 の学習には、記録と同じ本番の DB から export した
  CSV だけを使う」。旧記録側への `Superseded by` の追記は、0099（PR #234）がマージされた後、本記録を FINAL にする PR で
  行う（README「追記のみ」の例外。§5 #8）。0008 §2.8 の CSV の形と 0010 §2.7 の「タイムゾーンは呼び出し側から受け取る」は
  変えない。dataset 用の再生と Dataset v2 の生成にだけ、export の manifest との照合を**足す**（§5 #1 / #4）
- **関連**: [0099](0099-calibration-change-log.md) §2.6（「再生の timezone」と「再生の入力と記録の DB の束縛」。PR #234）/
  [0087](0087-dataset-v2-action-grid.md) §2.6 / [0094](0094-dataset-v2-settled-points.md) §2.4 /
  [0031](0031-thermal-dataset-contract.md) §2.3（1 source run 専用の DB と `dataset_source_run`）/
  [0010](0010-csv-replay.md) §2.1 / §2.7 / [0008](0008-rollup-and-retention.md) §2.8 /
  [0007](0007-ingest-pipeline.md) §2.11 / [0002](0002-metric-naming.md) §2.11（migration は追記のみ）/
  [0096](0096-calibration-binding-digest.md) / [0021](0021-public-repo-hygiene.md)（manifest に識別子を書かない）/
  `src/coldaisle/store/csv_export.py` / `src/coldaisle/store/rollup.py`（`--export-day` / `--timezone`）/
  `src/coldaisle/ingest/replay.py` / `src/coldaisle/daemon.py`（`--timezone`）/ `src/coldaisle/dataset.py`
  （`replay_fingerprint`）/ `src/coldaisle/control/model/dataset.py`（`SourceRun` / `DatasetManifestV2`）/
  `src/coldaisle/store/db.py`（`dataset_source_run`）/ `src/coldaisle/clock.py` / `docs/thermal-dataset.md` /
  `config/retention.yaml`
- **対象 Issue**: #237（関連: #233 / #83 / #84 / #86）

## 1. Context

0099（PR #234）は較正の変更を**絶対時刻**（Unix ms, UTC）で本番の DB に記録し、Dataset v2 と学習の入口が
それを読んで、再生（`--source replay`）で作った専用 DB の時刻と照合する（被覆と変更の検査。0099 §2.6）。
0087 §2.6 の検査（期間の中の `calibration_changed`）も同じ照合に頼る。この照合が正しいには、次の2つが要る。

- **(T) 時刻**: 専用 DB の時刻が、本番の DB の絶対時刻と（CSV の秒の切り捨てを除いて）一致する
- **(D) 出どころ**: 照合に使う較正の記録（`--calibration-history-db` に渡す DB）が、再生した CSV を書き出した**元の DB**の
  記録である

PR #234 の Codex は、どちらも保証されていないことを P1 で指摘した
（(T) は https://github.com/TomokiAkiyama06/coldaisle/pull/234#discussion_r4205092439 。(D) は同 PR の head `ab3ee61` への指摘）。

コードで確かめた事実:

- 日次 CSV の時刻は**オフセットを持たないローカル時刻**（`csv_export.TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"`。
  0008 §2.8）。`export_day()` は `datetime.fromtimestamp(ts_ms / 1000, tz=tz)` で書く
- export の timezone は `coldaisle-rollup --timezone`（既定 `Asia/Tokyo`。コードの既定値）から来る。
  **どこにも記録しない**（CSV にも、ファイル名にも、横のファイルにも、DB にも無い）
- 再生の timezone は `coldaisle-daemon --timezone`（既定 `Asia/Tokyo`。こちらもコードの既定値）から来る。
  `ReplaySource._parse_row` は、時刻にオフセットが**無ければ** `replace(tzinfo=tz)` で当てはめ、
  **あれば**そのオフセットを使う（オフセット付きの時刻は既に読める）
- 両者を照合する仕組みが無い。`replay_sha256`（dataset の `source_sha256`）は CSV の basename・境界・内容だけを
  hash し、`dataset_source_run`（0031 §2.3）も `SourceRun` も timezone を持たない。**同じ CSV を違う timezone で
  再生しても、fingerprint も provenance も同じになる**
- `replace(tzinfo=...)` は `fold=0` で当てはめる。DST のある timezone では、時計が戻る区間（同じローカル時刻が
  2回ある）の2回目の観測を1回目の時刻へ写し、時計が進む区間（存在しないローカル時刻）も例外にならず
  何かの時刻へ写す。**曖昧さに気づく箇所が無い**
- export は元の DB に何も書かない。CSV にも元の DB を示すものが無い。したがって `--calibration-history-db` に
  **任意の DB を渡せる**（0099 §2.6 は path を必須の引数にしただけで、出どころを確かめない）
- 本番の運用は `Asia/Tokyo`（DST なし）で、export と再生の既定値はたまたま一致している。
  `docs/ubuntu-deploy.md` は日次 CSV の自動実行をまだ配線していない

影響（#237）:

- (T) が崩れると、専用 DB の時刻全体がオフセットの差だけずれる。0099 の被覆の検査は古い行で満たされ、CSV の中に
  ある較正の変更 A→B がずれた期間全体より前に見え、較正の混ざった example が被覆と変更の両方の検査を通りうる
- (D) が崩れると、別の DB の記録で検査することになる。例: DB A から export した CSV が A の較正の変更 A→B を
  またいでいるのに、「較正 B が期間全体を覆う」と記録した DB B を渡すと、被覆と変更の検査が通る
- (T) のずれは、専用 DB の readings と**本番で保存した絶対時刻の ControlTick**（0031 §2.3 の「同じ run で
  保存済みの trace」を専用 DB へ持ち込む経路を作る場合）の対応も狂わせる

所有者は 2026-10-07 に、これらを 0099 から切り離して #237 と本記録で扱い、(T) と (D) を同じ「export と再生の束縛」として
1つの設計にまとめると決めた。それまでの暫定運用（v2 の学習に使う再生は本番と同じ timezone で行う・記録と同じ本番の DB
から export した CSV だけを使う）は 0099 §2.6 と `docs/thermal-dataset.md` に書かれている。**人の手順であり、
コードは検査しない。** 本記録はそれをコードの検査へ置き換える方法を決める。

## 2. Decision

### 2.1 export ごとに manifest を CSV の横に置き、同じ内容を元の DB に記録する

`export_day()` は1回の export ごとに、

- CSV と同じディレクトリに **export manifest**（JSON）を書く。名前は仮に `sensors_YYYY-MM-DD.export.json` とする
  （`sensors_*.csv` の glob に掛からない名前。§5 #2）
- 読んだ元の DB（`coldaisle-rollup --db`）の新しい追記のみの表 **`csv_exports`** に、manifest と同じ内容の行を1つ
  追記する（§2.6）

**CSV の形（列・時刻の書式・ファイル名）は変えない**（0008 §2.8。表計算で開く人と既存の CSV との互換を保つ）。

manifest の欄（仮。実装の PR で型と golden vector を固定する）:

| 欄 | 内容 |
|---|---|
| `schema` / `schema_version` | `coldaisle.daily_csv_export` / `1` |
| `export_id` | export ごとに払い出す不透明な識別子（`export-<32 hex>`。乱数。§2.6） |
| `csv_name` | 対になる CSV の basename（`sensors_2026-08-24.csv`）。**basename だけ**で、ディレクトリや絶対パスは書かない（0021） |
| `csv_sha256` | 対になる CSV の bytes の SHA-256 |
| `day` | `YYYY-MM-DD`（ローカルの日） |
| `timezone` | export に使った IANA の timezone 名（設定に書かれた文字列そのまま。`Asia/Tokyo`） |
| `day_start_ms` / `day_end_ms` | その日の `[開始, 終了)` の Unix ms（`day_bounds_ms` の値。DST の日は 23 / 25 時間になる） |
| `timestamp_format` | `csv_export.TIMESTAMP_FORMAT` の値（0099 §2.6 の秒の切り捨ての幅と同じ出どころ） |
| `row_count` | CSV のデータ行の数 |
| `row_seconds_sha256` | 書いた各行の**絶対時刻**（`ts_ms // 1000`、すなわち書いた秒の UTC の Unix 秒）を行の順に並べた列の SHA-256（§2.4 の照合に使う） |

- ホスト名・ROM・パス・DB の path などの個体識別子は入れない（AGENTS.md ルール 10）。`export_id` は乱数で、
  機器の情報を含まない
- 書く順序: (1) CSV と manifest を同じディレクトリの一時ファイルへ書いて fsync、(2) 元の DB へ `csv_exports` の行を
  追記して commit、(3) **CSV を先に、manifest を後に** rename する。どこで落ちても、残るのは「manifest の無い CSV」
  か「どの CSV とも対にならない `csv_exports` の行」で、どちらも §2.7 / §2.6 で v2 の学習に使えない側（安全側）に倒れる。
  同じ日を書き直すときは、rename の前に古い manifest を消す。`csv_exports` の古い行は消さない（追記のみ。新しい
  export は別の `export_id` の行になる）

### 2.2 export では timezone を必須にする

- export の timezone は `config/retention.yaml` の新しいキー（仮に `csv_timezone`）だけから取る。**コードに既定値を
  置かない**（AGENTS.md ルール 9。いまの `--timezone` の既定 `Asia/Tokyo` をやめる）。キーが無い・`ZoneInfo` で
  読めないときは、`--export-day` を**拒否して CSV も manifest も `csv_exports` の行も書かない**（ロールアップと
  保持期間の適用は従来どおり行う。export だけを止める）
- `coldaisle-rollup --timezone` は残すが、指定したときは設定の値と文字列で一致しなければ拒否する（§5 #3）。
  日次 CSV の日境界とレポートの日境界（`config/report.yaml`）は別の設定のまま
- manifest の `timezone` は設定の文字列をそのまま書く。別名（`Japan` と `Asia/Tokyo`）を正規化しない（§2.3 で
  文字列のまま照合する）

### 2.3 再生の照合（dataset 用の再生）

`--dataset-run-alias` を付けた再生（0031 §2.3。dataset の専用 DB を作る再生）では、まず入力の manifest の有無で
2つに分ける。再生の時点ではどの版の dataset を作るかが決まっていない（builder を選ぶのは後の `coldaisle-dataset`）ので、
manifest の無い入力を再生で拒否すると Dataset v1 の再生成まで止まる（PR #238 の Codex の指摘）。

- **すべての CSV に manifest が無い**: 従来どおり `--timezone` で再生し、`dataset_source_run` には「照合していない」
  （timezone と `export_id` の digest を `NULL`）として bind する。v1 の builder は従来どおり使える。**v2 の builder と
  学習の入口は拒否する**（§2.8）。いまの v1 の手順と `test_replay_can_regenerate_the_same_dataset_without_hardware` は
  そのまま通る
- **一部の CSV にだけ manifest がある**: 拒否する（照合した入力としていない入力を1つの run に混ぜない）
- **すべての CSV に manifest がある**: `ReplaySource` の constructor が**DB に何かを書く前**
  （`bind_dataset_source_run` の前）に次をすべて確かめ、1つでも満たさなければ拒否する（`SystemExit`。DB は空のまま
  残り、作り直せる）

1. 各 manifest を CSV と同じく `O_NOFOLLOW` で開き、regular file でなければ拒否する。CSV と同じ snapshot に取り込む
   （0031 §2.3 の「path を再 open しない」を manifest にも当てる）
2. manifest の `csv_name` と `csv_sha256` が対の CSV と一致する
3. すべての manifest の `timezone` が同じ文字列である（1 run に1つの timezone。§5 #5）
4. 再生に使う timezone は**manifest の `timezone`**とする。`--timezone` を明示して、それが manifest と
   文字列で違えば拒否する（§5 #4）
5. 各行を §2.4 の規則で絶対時刻へ写し、その秒の列の SHA-256 が `row_seconds_sha256` と、行数が `row_count` と
   一致する。各行の時刻が `[day_start_ms, day_end_ms)` に入る
6. §2.5 の曖昧な時刻・存在しない時刻が1行も無い
7. `export_id` が入力の中で重複しない

5 の照合は、取り込みの前に snapshot を1回読み通して行う（定数メモリ。hash を取るだけ）。取り込みの途中で
食い違いに気づいて止めると、DB が入力の一部だけになるからである（0031 §2.3 と同じ理由）。
元の DB との照合（§2.6）は再生では行わない（再生は本番の DB を開かない。照合は Dataset の生成で行う）。

**食い違いはすべて拒否する。** 片方を信じて読み替えたり、ずらして直したりしない。拒否の理由は構造化ログに
「どの CSV の、どの検査か」で出す（行の値は最初の1行だけ）。

dataset 用でない再生（デバッグや画面の確認。0010）は次のとおりとする。

- manifest が**ある** CSV は、上の 1〜6 を同じく行い、食い違えば拒否する（§5 #6）。dataset 用でなくても、
  照合した bytes と取り込む bytes が同じであるよう、manifest のある入力では CSV と manifest の snapshot を取り、
  照合も取り込みもその snapshot から読む（いまの通常の再生は取り込みのときに path を開き直すので、照合の後に
  CSV を差し替えられると、照合を通っていない bytes が入る。PR #238 の Codex の指摘）
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

### 2.6 再生の入力と較正の記録の DB を束縛する（(D)）

- **`csv_exports` 表**: 本番の DB に migration で足す（番号は 0099 の `0011_calibration_activations.sql` の後。適用済みの
  migration は書き換えない。0002 §2.11）。列は manifest の欄（`schema_version` 以外）と `exported_ms`。`export_id` を
  主キーにし、UPDATE / DELETE を trigger で拒否する（`events` / `control_admin_audit` と同じ追記のみ）。保持期間の
  削除の対象にしない（1日1行で小さい。readings を消した後も、その日の CSV の出どころを確かめられる）
- **書き手は `coldaisle-rollup --export-day` だけ**とする（export が readings を読むのと同じ接続で、同じ DB へ書く）。
  dataset の専用 DB（bind 済み）では export を拒否する（いまの `coldaisle-rollup` が bind 済みの DB を拒否するのと同じ）
- **照合は Dataset v2 の生成で行う**。`coldaisle-dataset` の v2 の CLI は `--replay-path` の manifest を読み
  （fingerprint で専用 DB の provenance と一致することは既に確かめている。0031 §2.3）、0099 §2.6 の
  `read_calibration_history` と**同じ読み取り専用の接続・同じ read transaction**で `--calibration-history-db` の
  `csv_exports` を読む。次をすべて満たさなければ生成を拒否する
  1. すべての manifest の `export_id` の行が `csv_exports` にある
  2. その行の各欄（`csv_name` / `csv_sha256` / `day` / `timezone` / `day_start_ms` / `day_end_ms` / `timestamp_format` /
     `row_count` / `row_seconds_sha256`）が manifest と一致する
  3. 1 と 2 を満たす前に、被覆と変更の検査（0099 §2.6）へ進まない
- これで「この CSV は、いま較正の記録を読んでいる DB から export された」ことが、CSV の bytes まで含めて確かめられる。
  別の DB を渡すと、その DB に `export_id` の行が無いので拒否される。CSV を書き換えると `csv_sha256` が合わない
- `CalibrationHistory`（0099 §2.6 の封をした型）と同じく、照合の結果は store 層の封をした型（仮に `ExportBinding`）で
  合成の起点へ返し、`control/model` には渡さない
- **DB の複製**: 本番の DB を複製すると、複製も同じ `csv_exports` の行を持つ。複製の時点までに export した CSV は
  どちらの DB でも照合を通るが、その日までの readings と較正の記録は両者で同じなので、検査の意味は変わらない。
  複製の後に export した CSV は、その DB にしか行が無い。複製の後に片方だけ較正を変えても、期間の後の変更は
  学習の入口で拒否される（0099 §2.6）

### 2.7 timezone と出どころの情報の無い CSV

- manifest の無い CSV（試作時の `~/server_sensor_logs` の記録、本記録の実装前に書いた日次 CSV、rename の途中で
  落ちた CSV）は、dataset 用の再生では「照合していない」run として bind され（§2.3）、**v2 の builder と学習の入口が
  拒否する**。したがって **v2 の学習には使わない**。v1 には従来どおり使える
- manifest はあるが `csv_exports` の行が無い CSV（段 1 の前の DB から export したもの、別の DB から export したもの）は、
  dataset 用の再生は通るが **Dataset v2 の生成で拒否する**（§2.6）
- 人が後から manifest や `csv_exports` の行を書いて足す経路は作らない（「この CSV はこの timezone で、この DB から
  だったはず」という推測で埋める経路になる。0099 §5 #5 と同じ向き）
- 実害は小さい。試作時の CSV には Fan の action が無く、そもそも dataset を作れない（0031 §2.3）。0099 §2.8 で、
  較正の記録の migration より前のデータも被覆が無く v2 に使えない
- dataset 用でない再生には、従来どおり使える（§2.3）

### 2.8 source run に timezone と出どころを束縛する

- **fingerprint**: `replay_fingerprint`（`replay_sha256`）に manifest の bytes も含める（CSV ごとに、CSV の後に
  manifest を、basename・長さ・内容で hash する）。同じ CSV でも manifest（timezone・`export_id`）が違えば
  fingerprint が変わる。fingerprint の規則に版を付け、manifest の無い入力の値はいまの規則のまま変えない（§5 #7）
- **`dataset_source_run`**: bind のときに、再生に使った timezone と、入力の `export_id` の集合の digest（`export_id` を
  並べ替えて連結した列の SHA-256）も記録する（段 2。migration で列を足す。manifest の無い run は両方 `NULL`。§2.3）。
  builder は DB の値と、`--replay-path` の manifest から計算した値（manifest が無ければ `NULL`）の一致を
  `_validate_dedicated_source_db` で求める。v1 の builder は `NULL` を受け入れ、v2 の builder は拒否する
- **Dataset v2 の manifest**: v1 と共有している `SourceRun` には**足さない**。v1 の `DatasetManifest` と v2 の
  `DatasetManifestV2` は同じ `SourceRun` を埋め込み、`model_dump_json()` で書くので、`SourceRun` に欄を足すと
  版を上げないまま v1 の artifact に知らない欄が入り、`extra="forbid"` の古い v1 の reader が拒否する
  （PR #238 の Codex の指摘）。代わりに v2 専用の型（仮に `ReplayBindingV2`: `local_timezone` と `export_ids`）を
  `DatasetManifestV2` の source run ごとの欄として持つ。v2 の builder と学習の入口（0099 §2.6）は、この欄の無い・
  `--calibration-history-db` との照合（§2.6）を経ていない dataset を拒否する。v1 の型と書き出しは変えない
- `coldaisle-dataset` の CLI は `--replay-path` の manifest から timezone と `export_id` を読み、引数では受け取らない
  （人が打つ値を増やさない。食い違う値を入れる経路を作らない）

### 2.9 0099 の暫定運用の解除

0099 §2.6 の2つの暫定運用（「v2 の学習に使う再生は本番（export）と同じ timezone で行う」「v2 の学習には、記録と同じ
本番の DB から export した CSV だけを使う」）は、§2.10 の段 1〜3 がすべて main に入った時点で、**人の手順からコードの
検査に置き換わる**。

その PR で `docs/thermal-dataset.md` の2つの注意を「照合される（決定記録 0100）」に書き換える。
0099 の本文は書き換えない。ただし 0099 だけを読んだ人が失効した手順に従わないよう、本記録の Supersedes に
その部分を書き、0099 のヘッダへ `Superseded by` を追記する（README「追記のみ」の例外。追記は 0099 のマージ後、
本記録を FINAL にする PR で行い、「段 3 のマージをもって」と条件を添える。§5 #8。PR #238 の Codex の指摘）。

### 2.10 実装の段

| 段 | 内容 | 依存 |
|---|---|---|
| 1 | 写像の関数（§2.4）・export の manifest と timezone の必須化（§2.1 / §2.2）・`csv_exports` の migration と export による追記（§2.6） | 0099 の migration（番号の順） |
| 2 | 再生の照合（§2.3 / §2.5 / §2.7）と fingerprint の版（§2.8）、`dataset_source_run` の timezone と `export_id` の digest の migration と bind 時の記録（§2.8） | 段 1 |
| 3 | `ReplayBindingV2`・builder の DB との照合・`csv_exports` との照合（§2.6）・v2 の builder と学習の入口の検査（§2.8）、`docs/thermal-dataset.md` の更新（§2.9） | 段 2、0099 の実装（`CalibrationHistory`） |

`dataset_source_run` への記録は段 2 に含める。照合した値を段 2 で DB に残さないと、段 2 と段 3 の間に作った
専用 DB は照合を通ったのに値を持たず、段 3 の検査を通れない。後から推測で埋めることも認めない（§2.7）ので、
再生し直すしかなくなる（PR #238 の Codex の指摘）。`csv_exports` は段 1 に含める。段 1 より前の export には行が無く、
v2 に使えない（§2.7）。

### 2.11 試験すべき性質

export（段 1）:

- manifest の `csv_sha256` / `row_count` / `row_seconds_sha256` が書いた CSV と一致し、`csv_exports` の行が manifest と
  同じ内容を持つ
- `csv_timezone` が無い・読めない設定では、CSV も manifest も `csv_exports` の行も書かず拒否する。ロールアップは行われる
- `--timezone` を設定と違う値で渡すと拒否する
- manifest と `csv_exports` に絶対パス・ホスト名・DB の path が入らない（`csv_name` は basename だけ）
- CSV の bytes は本記録の前と同じ（0008 §2.8 の形を保つ。既存の golden の試験がそのまま通る）
- 同じ日を書き直したとき、古い manifest が新しい CSV と対にならず、`csv_exports` には2行が残る
- `csv_exports` の UPDATE / DELETE を trigger が拒否する。保持期間の削除で消えない。bind 済みの dataset DB では export を拒否する
- 各段階（一時ファイル・DB の commit・rename）で落としたとき、残るものが §2.1 のとおり安全側である

再生（段 2）:

- dataset 用の再生で、manifest のある CSV と無い CSV が混ざれば、`dataset_source_run` に行を書く前に拒否する（DB は空）
- dataset 用の再生で、すべての CSV に manifest が無ければ「照合していない」run として bind し、v1 の dataset は従来どおり
  再生成できる（`test_replay_can_regenerate_the_same_dataset_without_hardware` が通る）
- 通常の再生でも、manifest のある入力は snapshot から照合し取り込む。照合の後に CSV の path を差し替えても、取り込む
  bytes は照合した bytes のまま
- `--timezone` を manifest と違う値で明示すると拒否する。同じ値、または省略なら通る
- manifest の `timezone` だけを書き換える（CSV はそのまま）と、`row_seconds_sha256` の照合で拒否する
- `csv_sha256` の食い違い、`csv_name` の食い違い、manifest が symlink・FIFO、`export_id` の重複なら拒否する
- run の中で `timezone` の違う manifest が混ざれば拒否する
- DST のある timezone（試験では `America/New_York` などの合成データ）で、時計が戻る日に曖昧な時刻の行を含む CSV は
  拒否する。時計が進む日に存在しない時刻を手で入れた CSV は拒否する。切り替わりの区間に行の無い日は通る
- `Asia/Tokyo` の任意の日は通り、保存される時刻が export 前の `ts_ms` の秒の切り捨てと全行で一致する
  （export → 再生の往復）
- dataset 用でない再生: manifest の無い CSV は従来どおり読める（いまの `tests/test_replay.py` が通る）。
  manifest があり食い違えば拒否する
- fingerprint: manifest の無い入力の値は本記録の前と同じ。manifest の bytes を変えると値が変わる
- 照合した timezone と `export_id` の digest を、bind と同じ transaction で `dataset_source_run` に記録する

Dataset v2 の生成（段 3）:

- DB A から export した CSV の専用 DB に、DB B を `--calibration-history-db` として渡すと拒否する（#237 の (D) の例。
  A の較正の変更をまたぐ CSV と、期間全体を覆う B の記録の組み合わせ）
- `csv_exports` の行の欄が manifest と1つでも違えば拒否する。行が無ければ拒否する
- `csv_exports` の照合に失敗したら、被覆と変更の検査へ進まない
- `dataset_source_run` の timezone・`export_id` の digest が manifest から計算した値と違えば拒否する
- v2 の builder は `ReplayBindingV2` の無い source run と、「照合していない」（`NULL`）run を拒否する。v1 の manifest の bytes と型は本記録の前と同じ
  （v1 の golden の試験がそのまま通る）
- migration は追記のみで、既存の行と読み取り API を変えない

## 3. Consequences

### 良くなること

- 0099 の被覆と変更の検査、0087 §2.6 の検査が、(T) 専用 DB の時刻が本番の絶対時刻と（秒の切り捨てを除いて）
  一致することと、(D) 照合に使う記録が CSV の元の DB のものであることを前提にできる。人の手順に頼らない
- timezone の名前だけでなく、全行の絶対時刻を照合するので、tzdata の版の違いや DST の当てはめの違いも見つかる
- 元の DB との照合は CSV の bytes まで含むので、CSV を書き換えた場合も見つかる
- CSV の形を変えないので、表計算で開く人と既存の CSV、いまの `ReplaySource` の読み方はそのまま
- 再生の照合は取り込みの前に起きるので、食い違った DB が作られない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| CSV と manifest の2ファイルになり、CSV だけを複写すると v2 に使えなくなる（v1 には使える） | 安全側（拒否）に倒れる。拒否の理由に「manifest が無い」と出す。複写の手順は `docs/thermal-dataset.md` に書く |
| 既存の CSV は v2 の学習に使えない | action が無く、もともと dataset を作れない（§2.7）。dataset 用でない再生には使える |
| export が本番の DB に書くようになる（いまは読むだけ） | `coldaisle-rollup` は既にロールアップと削除で同じ DB に書いている。行は1日1つ |
| Dataset v2 の生成に、本番の DB（の読み取り専用の複製でもよい）が要る | 0099 §2.6 で既に `--calibration-history-db` は必須。同じ接続で読む |
| DST のある timezone では切り替わりの日が v2 に使えない | 本番は `Asia/Tokyo`。DST のある場所で運用するなら §5 #9 の代替（UTC の列）を別の記録で検討する |
| dataset 用の再生で、取り込みの前に入力を1回余分に読む | snapshot を読むだけ（定数メモリ）。dataset 用の再生は一括投入で、待ち時間に効かない |
| `--timezone` の既定値をやめるので、`coldaisle-rollup --export-day` の呼び出しに設定が要る | 自動実行はまだ配線していない（`docs/ubuntu-deploy.md`）。設定の例を `config/retention.yaml` に置く |
| fingerprint の規則に版が増える | manifest の無い入力の値は変えない（§2.8）。既に作った dataset の照合は壊れない |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| CSV の時刻を UTC オフセット付き（`2026-08-24T20:16:40+09:00`）にする | DST の曖昧さを根本から消し、`ReplaySource` は既にオフセット付きの時刻を読める。しかし 0008 §2.8 の「従来の出力と同じ形」を変え、表計算が時刻として読めなくなる。既存の CSV と同じディレクトリで形が混ざる。0099 §5 #9 で所有者が「CSV の時刻を ms にする」（CSV の形を変える）を採らなかったのと同じ理由。DST の無い本番では利益が小さい。(D) も解かない（§5 #9） |
| CSV に絶対時刻の列（`timestamp_utc_ms`）を足す | 行ごとに曖昧さが無く、ms の精度も戻る（0099 の秒の切り捨ても消える）。いまの `ReplaySource` は知らない列を捨てるので古い再生でも読める。ただし 0008 §2.8 の「従来の列だけを保つ」を変え、(D) は別に要る。本記録では採らず、§5 #9 で所有者に聞く |
| CSV の先頭にコメント行で timezone を書く | 表計算と `csv.DictReader` の見出しの読み方を壊す。既存の再生が見出しを読み違える |
| ファイル名に timezone を入れる（`sensors_2026-08-24_Asia-Tokyo.csv`） | IANA 名は `/` を含み、変換の規則が要る。ファイル名を変えると `csv_files()` の並び順と既存の名前の互換が崩れる。内容の照合（秒の列）もできない |
| timezone の名前だけを照合する（秒の列の hash を持たない） | tzdata の版の違いと DST の当てはめの違いを見つけられない（§2.4） |
| `SourceRun` / `dataset_source_run` にだけ timezone を記録する（export 側は記録しない） | 再生した人が指定した値を記録するだけで、export の値と照合できない。#237 の穴そのもの |
| (D) を DB の識別子だけで束縛する（DB に1つの乱数 `db_id` を持たせ、manifest に写す） | 「どの DB から来たか」は分かるが、その CSV が本当にその DB から export されたか（書き換えられていないか）は分からない。`csv_exports` の行は CSV の bytes まで束縛する（§5 #11） |
| (D) を manifest の署名で束縛する | 鍵の管理が増える。照合の相手（較正の記録の DB）に行を持たせれば、同じ DB を読むだけで足りる |
| 曖昧な時刻を `fold` で解く（manifest の秒の列に合う `fold` を選ぶ） | 照合のための値を解の選択に使うと、照合が意味を失う。DST の無い本番では利益が無い |
| manifest の無い CSV や `csv_exports` の行の無い export に、人が後から足せるようにする | 推測で埋める経路になる（§2.7。0099 §5 #5 と同じ） |
| 食い違ったときに manifest の timezone で読み替えて続ける | 人が打った値と違う動きを黙ってする。`--timezone` を明示したなら食い違いは人の誤りで、知らせて止めるほうがよい（§5 #4） |
| `SourceRun` に `local_timezone` を足す | v1 と v2 が共有する型で、版を上げずに v1 の artifact の形が変わる（§2.8。PR #238 の Codex の指摘） |

## 5. 未決事項（所有者に確認したい点）

| # | 論点 | 推奨 | 代替 |
|---|---|---|---|
| 1 | timezone の記録先 | **CSV の横の manifest（1日1つ）**（§2.1）。CSV の形を変えない | (a) CSV に UTC の列を足す（#9）。(b) ディレクトリに1つの manifest（日ごとの export と書き直しの単位が合わない） |
| 2 | manifest のファイル名 | `sensors_YYYY-MM-DD.export.json`（`sensors_*.csv` の glob に掛からず、CSV と並んで見える） | `sensors_YYYY-MM-DD.csv.json`、または `.manifest/` のような隠しディレクトリ |
| 3 | export の timezone の出どころ | **`config/retention.yaml` の `csv_timezone`（必須・既定値なし）**。`--timezone` は残し、設定と違えば拒否（§2.2） | (a) `--timezone` を `--export-day` のとき必須にし、設定には置かない。(b) `--timezone` を廃止する（呼び出しが壊れる） |
| 4 | dataset 用の再生の timezone の決め方 | **manifest の値を使い、`--timezone` を明示して食い違えば拒否**（§2.3 の 4） | `--timezone` を必須にし、manifest と一致しなければ拒否（人が打つ値が1つ増え、意味は同じ） |
| 5 | 1 run に timezone の違う CSV が混ざるとき | **拒否**（§2.3 の 3）。source run に1つの timezone を持たせる | ファイルごとに manifest の値で読む（照合は効くが、source run の timezone が1つに定まらない） |
| 6 | dataset 用でない再生で、manifest があり食い違うとき | **拒否し、照合と取り込みを同じ snapshot から読む**（§2.3）。デバッグ用でも、ずれた時刻の DB を作る利益が無い | 警告して `--timezone` の値で続ける（0010 §2.7 のまま。デバッグの自由度を残す） |
| 7 | fingerprint と source run への束縛 | **3つとも行う**: fingerprint に manifest を含める（版を付け、manifest の無い入力の値は変えない）・`dataset_source_run` に列を足す（段 2）・v2 専用の `ReplayBindingV2` を `DatasetManifestV2` に持たせる（`SourceRun` は変えない）（§2.8） | (a) fingerprint だけ（manifest が timezone と `export_id` を持つので推移的に束縛される。migration が要らないが、DB と artifact から直接読めない）。(b) fingerprint は変えず v2 の manifest だけ（同じ CSV に別の manifest を付けた入力を区別できない） |
| 8 | 0099 の暫定運用の扱い | **0100 の Supersedes に 0099 §2.6 の2つの暫定運用の部分を書き、0099 へ `Superseded by`（「段 3 のマージをもって」の条件つき）を追記する**（§2.9）。0099 だけを読んだ人が失効した手順に従わないため（AGENTS.md「決定記録」。PR #238 の Codex の指摘）。追記は 0099 のマージ後、本記録を FINAL にする PR で行う | 0099 を書き換えず `Superseded by` も付けない（暫定運用は「それまで」の条件付きなので段 3 で自然に失効する。ただし 0099 から辿れない） |
| 9 | DST の扱い（行ごとのオフセットへ移すか） | **移さない。曖昧・存在しない時刻を含む CSV は dataset 用の再生で拒否する**（§2.5）。本番は `Asia/Tokyo` で DST が無い | (a) CSV に `timestamp_utc_ms` の列を足し、再生はそれを正とする（DST と秒の切り捨てが両方消える。0008 §2.8 の「従来の列だけ」を変える。0099 §5 #9 の判断と整合させる必要がある）。(b) 時刻をオフセット付きにする（§4） |
| 10 | 実装の Issue の切り方 | **#237 で段 1〜3 を順に別 PR**（§2.10）。段 3 は 0099 の実装の後 | 段 1 / 段 2 を1つの PR にする |
| 11 | (D) の束縛の形 | **export ごとに `export_id` を払い出し、manifest と同じ内容を元の DB の追記のみの表 `csv_exports` に残す。Dataset v2 の生成で、`--calibration-history-db` の `csv_exports` と manifest を全欄で照合する**（§2.6） | (a) DB に1つの `db_id` を持たせ manifest に写す（CSV の書き換えを見つけられない）。(b) `csv_exports` に加えて `db_id` も持つ（複製の区別はできないので、得るものが小さい）。(c) 較正の記録の写しを manifest に入れ、DB を読まない（記録の鎖（0099 §2.5）の検証が manifest の写しに対してはできない） |
| 12 | `csv_exports` の書き手と置き場所 | **`coldaisle-rollup --export-day` だけが、readings を読んだのと同じ DB に書く**（§2.6）。保持期間の削除の対象にしない | export を別の CLI に分け、その CLI だけを書き手にする（書き手の境界ははっきりするが、運用の手順が1つ増える） |
| 13 | export の書く順序 | **一時ファイル → DB の commit → CSV の rename → manifest の rename**（§2.1）。どこで落ちても安全側 | DB の commit を最後にする（rename の後に落ちると、manifest はあるのに DB に行が無い CSV が残る。これも生成で拒否されるので安全側だが、export の成否の見分けが遅れる） |
| 14 | manifest の無い dataset 用の再生 | **「照合していない」run として bind し、v1 には使え、v2 の builder と学習の入口で拒否する**（§2.3）。混在は拒否 | 再生の時点で拒否する（v1 の再生成と既存の試験が止まる。PR #238 の Codex の指摘） |
