# 決定記録 0099: 較正の変更の記録先（取り込みが新しい較正を使い始めた時点を追記のみの表に残し、Dataset v2 と学習の入口が必ず読む）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が §5 の10点をすべて推奨案で承認。§6。本記録が頼る 0096 は同日 FINAL）
- **Date**: 2026-10-07
- **Supersedes**: なし（0087 §2.6 の「呼び出し側が `DeclaredChange` を明示して渡す」に、較正の変更については
  記録先から読む経路を**足す**。呼び出し側の宣言は残す。0087 §2.6 を置き換えるかは §5 #6 で確認する）
- **関連**: [0096](0096-calibration-binding-digest.md)（§2.1 / §2.2 / §2.3 / §2.4 / §5 #3 / §5 #4 / §5 #9）/
  [0087](0087-dataset-v2-action-grid.md) §2.6 / [0094](0094-dataset-v2-settled-points.md) §2.4 /
  [0079](0079-model-artifact-formats.md) §1 / §2.3 / §2.4 / §6 の質問 5 /
  [0056](0056-model-drift-detection-and-retraining-triggers.md) §2.5 /
  [0031](0031-thermal-dataset-contract.md)（1 source run 専用の dataset DB）/
  [0024](0024-calibration.md) / [0010](0010-csv-replay.md) §2.1 / §2.9 / [0008](0008-rollup-and-retention.md) §2.8 /
  [0002](0002-metric-naming.md) §2.11（migration は追記のみ）/ [0045](0045-local-socket-write-entry.md) §2.5（`events` の書き手）/
  `src/coldaisle/calibrate.py` / `src/coldaisle/daemon.py`（`_calibration_for`）/ `src/coldaisle/ingest/calibration.py` /
  `src/coldaisle/ingest/normalize.py` / `src/coldaisle/dataset.py` / `src/coldaisle/control/model/dataset.py`
  （`reject_calibration_changes`）/ `src/coldaisle/control/drift/model.py`（`DeclaredChange`）/
  `src/coldaisle/store/csv_export.py` / `src/coldaisle/store/migrations/`
- **対象 Issue**: #233（関連: #83 / #84 / #86）

## 1. Context

0096（PR #229）§5 #4 で、所有者は 2026-10-07 に「学習データの期間の**後**に較正を変えてから学習した場合、学習の入口で
`calibration_changed` を拒否する」と決めた。この検査と 0087 §2.6 の検査（Dataset v2 の期間の中に較正の変更があれば
生成を拒否する）は、どちらも**較正の変更が記録として残っていること**に頼る。ところが、いまはその記録が無い。
PR #229 の Codex の指摘を受け、記録先の形を本記録で決める。

いまの仕組みは次のとおりである（コードで確かめた事実）。

- `coldaisle-calibrate --apply` は `config/calibration.json` を書き換え、差し替えたプローブを受け入れるだけで、
  変更の記録を残さない。出力は「取り込みを再起動してください」とだけ言う
- `calibration.json` は**手でも書き換えられる**（ファイルの `note` が「値を手で変えたら決定記録に残すこと」と言う）
- 較正を当てるのは取り込み（`coldaisle-daemon` の `Normalizer`）で、較正ファイルは**起動時に1回だけ**読む
  （`daemon._calibration_for`）。したがって store の値の意味が変わるのは、ファイルを書いた時刻ではなく、
  **取り込みが新しい較正で起動した時刻**である。`--source replay` は較正を当てない（0010 §2.9）
- 0056 §2.5 の `DeclaredChange` は呼び出し側が渡す値で、永続的な記録先が無い。
  `ThermalDatasetV2Builder.build(declared_changes=)` は空の tuple を許す（0087 §2.6。空も明示させるだけ）
- Dataset は**1 source run 専用の DB**から作る（0031。`_validate_dedicated_source_db`）。`coldaisle-dataset` の
  `--source-kind` は `replay` だけで、専用 DB は日次 CSV（0008 §2.8）を再生して作る。**本番の DB の表は
  専用 DB に入らない**
- 日次 CSV の時刻は**秒の精度**（`csv_export.TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"`）で、再生はその時刻のまま
  保存する（0010 §2.1）。本番の ms の時刻と、専用 DB の時刻は最大 1 秒弱ずれる
- 既存の追記のみの表は `events`（0045。書き手は `coldaisle-eventd` だけ）と `control_admin_audit`（0072。書き手は
  `coldaisle-fand` だけ）で、どちらも UPDATE / DELETE を trigger で拒否している（migration 0006 / 0008）

PR #229 のエージェントが挙げた要件は4つある。(R1) 追記のみで、学習の入口が必ず読む。(R2) 実効の offset が
変わったときだけ追記する。(R3) 全温度 metric で比べる（artifact の投影に依らない）。(R4) 取り込みが新しい較正を
使い始めた時点で記録する（または書いた時刻から再起動までを保守的な区間として扱う）。

## 2. Decision

### 2.1 記録先は、取り込みが書く SQLite DB の追記のみの新しい表

記録先は **`coldaisle-daemon` が readings を書くのと同じ SQLite DB** に置く新しい表 `calibration_activations`
（「取り込みがある較正を使い始めた」ことの記録）とする。migration `0011_calibration_activations.sql` で足す。
適用済みの migration は書き換えない（0002 §2.11）。

同じ DB に置く理由は、記録とそれが意味を決める readings が**同じファイル・同じ時計・同じ書き手**にそろうためである。
mock（`--speed` の `SimulatedClock` を含む）の DB には mock の記録が、本番の DB には本番の記録が残り、混ざらない。

### 2.2 書き手は取り込みで、記録する時点は「新しい較正で起動した時点」

- **書き手は `coldaisle-daemon` だけ**とする（`--source serial` と `--source mock`。較正を当てる経路）。
  `--source replay` は較正を当てないので記録を読みも書きもしない（0010 §2.9）
- 書くのは**起動時に1回**、較正ファイルを読み（`_calibration_for`）store を開いた後、**ソースを読み始める前**である。
  最初の sample を正規化する前に記録が確定していなければならない
- 手順:
  1. 読んだ `Calibration` から §2.3 の**実効の offset の写像**を作り、canonical bytes とその SHA-256 を求める
  2. 表の最後の行の SHA-256 と比べる。**同じなら何も書かない**（R2）。表が空、または違えば1行を追記する
  3. 追記する行の `ts_ms` は `clock.now_ms()` とする。時計は store と同じもの（#42。合成の起点が配る
     `source.clock`）で、`Normalizer` が sample に付ける時刻も同じ時計から来る。したがって新しい較正の最初の
     sample の時刻は記録の時刻**以上**になる
  4. ただし `clock.now_ms()` が「readings の最大の `ts_ms`」または「最後の行の `ts_ms`」**以下**なら（壁時計が戻った、
     圧縮再生の mock が時刻を先へ進めた DB を再び開いた、など）、**記録を書かず取り込みを起動しない**。記録の時刻だけを
     先へずらすと、時計は進まないので新しい較正の sample が記録より前の時刻で保存され、古い較正の値に見える
     （PR #234 の Codex の指摘）。時計を記録に合わせて進めることもしない（`WallClock` は進められず、ホストの時刻と
     ずれた値を保存することになる）。この判定は**較正が変わらず記録を書かない起動では行わない**（値の意味が
     変わらないので、時刻の前後は本記録の関心ではない）
  これで記録の時刻は、古い較正で保存した最後の値より**厳密に後**、新しい較正の最初の値**以前**になる
- **`coldaisle-calibrate --apply` は記録を書かない。** ファイルを書いた時刻は store の値の意味が変わる時刻ではない
  （R4）。手で書き換えたファイルも、取り込みの起動時の比較で同じように記録される。`--apply` の出力と
  `docs/calibration.md` には「変更は取り込みを再起動した時点で記録される」と書く（0096 §5 #9 の再起動の順と同じ PR）
- 本記録は**シリアルポートに関わらない**（AGENTS.md ルール 6）。取り込み・Dataset・学習のどれも、
  API・AI・control から記録先に書く経路を作らない。`coldaisle-fand` はこの表を読み書きしない（runtime の照合は
  0096 の L9 が較正ファイルで行う）

### 2.3 比べる範囲と正規化（全温度 metric）

比べる写像は**artifact に依らず**、次の手順で作る（R3）。正規化は 0096 §2.3 と同じ規約に従う。読み手の便のため、
本記録が頼る規約を下の4に書き写す（0096 と食い違えば 0096 を正とする）。

1. `METRIC_TO_CHANNEL` にあるすべての metric のうち、`Normalizer` が較正を当てない metric（いまは湿度。
   0096 §2.2 の3 と**同じ述語**を共有する）を除く
2. 残った各 metric に `Calibration.offset_for(channel)` を対応させる（`offsets_c` に無ければ 0.0。0096 §5 #2）。
   `-0.0` は `0.0` にし、**丸めない**
3. `offsets_c` にあっても `METRIC_TO_CHANNEL` に無いキー（`Normalizer` が使わない）は入れない
4. `{metric 名: offset}`（値は `float`。直列化は `float.__repr__`）を次の bytes にし、その SHA-256（小文字16進64文字）を
   `offsets_sha256` とする（0096 §2.3 / 既存の `canonical_json_bytes` と同じ）

   ```python
   (
       json.dumps(
           obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
       ).encode("utf-8")
       + b"\n"
   )
   ```

この写像は artifact の digest（0096。artifact の `metric_binding` へ投影したもの）の**上位集合**で、どの artifact の
投影も1行の写像から 0096 §2.2 の手順で求められる。`note` / `calibrated_at` / `reference` / `samples` は比べない
（0096 §2.1 と同じ向き。値を変えない再較正で変更を記録しない）。0096 §5 #2 / #3 は推奨案で決着しており、本節はそれに
従っている。

### 2.4 記録の内容

1行は次を持つ（DDL は実装 PR で書く。列名は仮）。

| 列 | 内容 |
|---|---|
| `id` | `INTEGER PRIMARY KEY` |
| `ts_ms` | §2.2 の3 / 4 の時刻（Unix ms, UTC）。直前の行より厳密に大きい |
| `source_kind` | `serial` / `mock`（`replay` は書かない） |
| `offsets_json` | §2.3 の canonical bytes を**そのまま**（末尾の改行 `\n` を含む）UTF-8 の文字列として持つ。`offsets_sha256` はこの文字列の UTF-8 の bytes の SHA-256 で、両者は同じ bytes を指す。**後の値**。前の値は直前の行の `offsets_json` |
| `offsets_sha256` | §2.3 の4 の digest |
| `previous_sha256` | 直前の行の `offsets_sha256`。最初の行だけ `NULL` |
| `calibrated_at` | 読んだ `calibration.json` の `calibrated_at`（説明用。比べない） |
| `calibration_file_sha256` | 読んだ `calibration.json` の bytes の SHA-256（説明用。どのファイルの版かを git と突き合わせるため。比べない） |

- 「前後の実効 offset」は、`previous_sha256` で直前の行を指し、前の値を複製しない（複製すると食い違いうる）
- **自由記述の理由は持たない**（§5 #4）。理由は `calibration.json` の `note` と git の履歴、`coldaisle-memory` の
  運用メモリに残す
- 実機の個体識別子（ROM・ホスト名・絶対パス）は入れない（AGENTS.md ルール 10）。`calibration_file_sha256` は
  ファイルの path ではなく内容の hash である

### 2.5 追記のみの保証

- UPDATE / DELETE を trigger で拒否する（`events` / `control_admin_audit` と同じ）
- INSERT の trigger で、`ts_ms` が直前の行より大きいこと、`previous_sha256` が直前の行の `offsets_sha256` と
  一致すること（表が空なら `NULL`）、`offsets_sha256 <> previous_sha256`（同じ値の行を足さない。R2）を検査する
- CHECK で `json_valid(offsets_json)` / `json_type = 'object'` / digest の形（64桁の小文字16進）/ `ts_ms >= 0` /
  `source_kind IN ('serial', 'mock')` を持つ
- 保持期間の削除（0008）の対象にしない。行は較正の変更ごとに1行で、量は問題にならない
- trigger を外した DB を読んでも気づけるよう、読む側（§2.6）が**全行を検証**する: 各行の `offsets_json` を
  §2.3 の規約で解析・再直列化して同じ文字列（末尾の `\n` を含む）になること、その UTF-8 の bytes の SHA-256 が `offsets_sha256` と一致すること、
  `previous_sha256` の鎖と `ts_ms` の単調増加。途中の行の削除・改変は鎖で見つかる。**最後の行の削除は
  鎖では見つからない**（§3 の表）

### 2.6 Dataset v2 と学習の入口が必ず読む

- **読む関数**（store 層。仮に `read_calibration_history(path)`）は本番の DB を**読み取り専用**（SQLite の URI
  `mode=ro`）で開く。migration を当てない（読む側が本番の DB の schema を進めない）。§2.5 の検証を通った行だけから
  `CalibrationHistory`（封をした型。この関数の外で作れない）を返す
- **Dataset v2**: `ThermalDatasetV2Builder.build()` は `calibration_history: CalibrationHistory` を**既定値なしで**
  受け取る。専用 DB（再生）には記録が無いので、本番の DB は別の path として渡す。合成の起点は次をすべて満たさなければ
  生成を拒否する
  - **被覆**: `history_start_ms` の最小（全 example の期間の先頭）以前に、少なくとも1行がある
    （期間の先頭で効いていた較正が記録にある）。無ければ「較正の履歴が期間を覆っていない」で拒否する
  - **変更**: 記録の各行の `ts_ms` を変更の時刻として、0087 §2.6 の検査（`reject_calibration_changes`）へ渡す。
    呼び出し側の `declared_changes` の `calibration_changed` も**和集合**として残す（宣言を足すことはできるが、
    記録を消すことはできない）
  - **秒の切り捨て**: 専用 DB の時刻は CSV の秒に切り捨てられている（§1）。各行は `ts_ms` 1点ではなく
    区間 `[floor(ts_ms / 1000) × 1000, ts_ms]` として扱い、両端の2点を検査へ渡す（期間はどれも 1 秒より長いので、
    区間と交わる期間は必ずどちらかの端を含む）。区間の幅は CSV の時刻の精度から導き、コードに 1000 を
    直書きしない（AGENTS.md ルール 9。`csv_export` の書式と1か所で結ぶ）
- **学習の入口**（0096 §5 #4。0094 §2.4 の CLI の v2 対応の後に作る）: 同じ `CalibrationHistory` を読み、
  - dataset の期間の終わりより後に行が1つでもあれば拒否する（0096 §5 #4 の所有者の決定）。結果として、
    期間に効いていた行が**最後の行**である
  - 0096 §2.4 で明示して読む較正ファイルから §2.3 の写像を作り、その `offsets_sha256` が最後の行と一致しなければ
    拒否する（ファイルを書き換えたが取り込みをまだ再起動していない状態、または取り違えたファイル）。
    一致すれば、trainer へ渡す較正の値は記録が裏づけたものになる
- **CLI**: `coldaisle-dataset` の v2 と学習の CLI は、本番の DB の path（仮に `--calibration-history-db`）を
  **必須の引数**とし、既定値を置かない（0079 §2.3 の「既定値を置かない」のまま）
- `control/model` へは引き続き**時刻の列だけ**を渡す（0087 §2.6 の依存の向き）。`CalibrationHistory` は合成の起点と
  store だけが扱う

### 2.7 記録が無い・壊れているとき

すべて**拒否（学習・dataset を作らない）**とし、「記録が無いので変更なし」と読み替えない。

| 状態 | 扱い |
|---|---|
| DB を開けない・`calibration_activations` が無い（migration 前の DB） | 拒否 |
| 表が空、または期間の先頭以前に行が無い | 拒否（被覆が無い） |
| §2.5 の検証（再直列化・digest・鎖・単調）のどれかに外れる | 拒否（どの行かをエラーに出す） |
| 学習の入口で、較正ファイルと最後の行が食い違う | 拒否 |

取り込みの側で、比較や追記に失敗したとき（DB に書けない・最後の行が §2.5 の検証に外れる）の扱いは §5 #3 で確認する。
推奨は**取り込みを起動しない**（構造化ログに理由を出して終了する）である。記録を残さずに新しい較正の値を
保存し始めると、後から見分けられない。

### 2.8 既存データの移行

- migration は空の表を作るだけで、**過去の履歴を埋めない**（いつどの較正が効いていたかを示す記録が無い。推測で
  埋めない）
- migration の後の最初の取り込みの起動で、`previous_sha256 = NULL` の最初の行が入る。それより前のデータは
  §2.6 の「被覆」を満たさないので Dataset v2 に使えない
- 実データから作った v2 の dataset と artifact はまだ無い（0079 §1 / 0096 §2.8）。失うものは小さいが、
  migration 前に取り込んだデータを v2 の学習に使いたい場合の扱いは §5 #5 で確認する

### 2.9 試験すべき性質

実装 PR で次を確かめる（実機なし。mock と一時 DB で足りる。AGENTS.md ルール 7）。

- **変化のときだけ**: 同じ較正で取り込みを2回起動しても行は1つ。`note` / `calibrated_at` / `reference` / `samples`
  だけを変えても増えない。`0` / `0.0` / `-0.0`、「無いチャネル」と「0.0 の明示」、湿度の offset だけの変更、
  `METRIC_TO_CHANNEL` に無いキーの追加でも増えない
- **全温度 metric**: どの温度チャネルの offset を `math.nextafter` だけ変えても行が増える（artifact の有無に依らない）
- **時点**: 行の `ts_ms` は、その起動より前に保存した readings の最大の `ts_ms` より大きく、その起動で保存した最初の
  sample の `ts_ms` 以下。較正を変えて、時計（`SimulatedClock`）を保存済みの最後の時刻以下へ戻して起動すると、
  記録を書かず起動しない。較正を変えずに同じ条件で起動すると、起動する
- **`--apply` と手の書き換え**: `--apply` は行を足さない。ファイルを書いた後、取り込みを再起動した時点で1行入る。
  手で書き換えたファイルも同じ
- **replay**: `--source replay` は表を読みも書きもしない
- **追記のみ**: UPDATE / DELETE / 鎖の合わない INSERT / 同じ digest の INSERT / `ts_ms` が戻る INSERT が拒否される
- **読む側の検証**: trigger を外して途中の行を改変・削除した DB、非 canonical な `offsets_json`、digest の不一致を
  それぞれ拒否する。`mode=ro` で開き、読み込みで schema を進めない（`schema_version` が変わらない）
- **Dataset v2**: 期間の中に行がある・区間 `[floor, ts]` の下端だけが期間に入る・期間の先頭以前に行が無い・表が無い、
  のそれぞれで拒否する。期間の外にだけ行があれば作れる。`declared_changes` を空にしても記録の行は効く。
  `calibration_history` を渡さない呼び出しは型と実行時の両方で失敗する
- **学習の入口**: 期間の後に行があれば拒否、較正ファイルの写像が最後の行と違えば拒否、一致すれば通る
- **migration**: 追記のみで、既存の表と行と読み取り API を変えない（0094 の migration の試験と同じ）。冪等

## 3. Consequences

良くなること。

- 較正の変更が、store の値の意味が変わった時点で、手の書き換えも含めて残る。Dataset v2 と学習の入口は宣言の
  有無に頼らず変更を見る（0096 §5 #4 の穴が閉じる）
- 学習で使う較正の値が、記録（期間に効いていた行）と較正ファイルの両方で裏づけられる（0079 §6 の質問 5 の
  「store が較正を記録しない」を、変更の時点の粒度で埋める）
- 値を変えない再較正は記録されず、artifact や dataset を失効させない

悪くなること（と緩和策）。

| 悪くなること | 緩和策 |
|---|---|
| 取り込みが起動時に DB へ1行書く経路が増え、失敗すると起動しない（§5 #3 の推奨） | 書けない DB には readings も書けない。行は変化のときだけで、起動の大半は比較だけ |
| 較正を変えたとき、時計が保存済みの最後の時刻より進んでいなければ取り込みが起動しない（§2.2 の4。圧縮再生で時刻を先へ進めた mock の DB など） | 較正が変わらない起動では判定しない。mock の DB は作り直せる。本番では壁時計が大きく戻ったときだけで、そのとき保存を始めると時刻の順も壊れる |
| migration より前のデータは v2 の学習に使えない | 実データの v2 dataset はまだ無い。§5 #5 で例外の手順を確認する |
| 最後の行の削除は鎖では見つからない（trigger を外した場合） | trigger で拒否する。学習の入口は較正ファイルと最後の行の一致を見るので、削除後にファイルだけ新しい状態は拒否される。完全には閉じない（DB への書き込み権限を持つ人を信頼する前提。`events` / `control_admin_audit` と同じ） |
| Dataset の CLI が本番の DB の path を必須で受け取る（専用 DB と2つの DB を扱う） | 読み取り専用で開き、migration を当てない |
| 秒の切り捨てで、変更の直前・直後の最大 1 秒を含む期間も拒否する | 狭める向きにだけ厳しい（0087 §2.6 と同じ向き） |
| 取り込みを止めずにファイルだけ書き換えた状態では記録が無い | その間の store の値は古い較正のままで、記録と一致している。学習の入口はファイルと最後の行の食い違いで拒否する |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| `coldaisle-calibrate --apply` が記録を書く | ファイルを書いた時刻は store の値の意味が変わる時刻ではない（R4。PR #229 の Codex の指摘）。手の書き換えを取りこぼす。「書いた時刻から次の起動まで」を区間にするには、結局取り込みの起動の記録が要る |
| 追記のみの JSONL ファイル（例 `var/calibration-history.jsonl`） | 追記のみが規約だけで、制約にできない。readings と別のファイルなので、mock と本番の取り違え・path の設定・fsync と部分行の扱いを別に決める必要がある。取り込みと同じトランザクションの境界に乗らない |
| 運用メモリ（`memory/`。`coldaisle-memory --apply --commit`） | 人の確認を経て書く記録で、取り込みの起動の時点を機械的に残せない。git で書き換えられ、追記のみではない。学習の入口が必ず読む形にならない |
| 既存の `events` 表に `kind = calibration_activated` で足す | `events` の書き手は `coldaisle-eventd` だけ（0045 §2.5）。鎖と digest の形を CHECK で持てない（`payload` は自由な JSON object） |
| 取り込みの起動ごとに無条件で1行書く | 値を変えない起動も「変更」に見え、Dataset v2 の生成を不必要に拒否する（R2） |
| readings の各行に較正の digest の列を足す | readings の schema（0002 のロング形式）を変え、行数ぶん保存量が増える。変更の時点だけで足りる |
| dataset の manifest に取り込み時点の較正の写しを束縛する（0096 §5 #4 の代替 (b)） | 写しを取る時点の正しさが人の手順に依る（store が較正を記録しない）。本記録の記録があれば写しは記録から導ける |
| 記録が無いときは「変更なし」として進める | 記録が無いことと変更が無いことを区別できない。AGENTS.md ルール 4 の向き（未知を通常として扱わない） |

## 5. 未決事項（所有者に確認した点）

2026-10-07、所有者が10点すべてを推奨案で決めた（§6）。番号は本文からの参照を保つため提案時のまま残し、各行の論点の先頭に「決着」と書いた。「代替」の列は判断前の記録である。

| # | 論点 | 推奨 | 代替 |
|---|---|---|---|
| 1 | **決着**（2026-10-07 所有者の決定、推奨案）。記録先 | **取り込みが書く SQLite DB の追記のみの新しい表 `calibration_activations`**（§2.1 / §2.5） | 追記のみの JSONL（§4）。DB から独立して読めるが、追記のみを制約にできない |
| 2 | **決着**（2026-10-07 所有者の決定、推奨案）。書き手と時点 | **`coldaisle-daemon`（serial / mock）が起動時、ソースを読む前に、実効の写像が変わったときだけ1行**（§2.2）。`--apply` は書かない。0096 §5 #4 が挙げた「`--apply` 以外の書き手（手で `calibration.json` を編集する経路）による変更も漏らさない」は、本項で扱う。取り込みが起動時に実効の写像の変化を検知して記録するので、手の編集も `--apply` と同じく記録される（手編集の経路は禁じない） | `--apply` が「書いた時刻」を書き、取り込みの起動も「使い始めた時刻」を書き、両者の間を変更の区間とする（2か所の書き手になる） |
| 3 | **決着**（2026-10-07 所有者の決定、推奨案）。取り込みの起動時に比較・追記に失敗したとき、または較正が変わったのに時計が保存済みの最後の時刻より進んでいないとき（§2.2 の4） | **取り込みを起動しない**（構造化ログに理由を出して終了。systemd の再起動に任せる。時計が戻った場合は時刻が追いつけば起動できる）（§2.7） | 起動して取り込みを続け、警告だけ出す（監視は止まらないが、その後のデータは記録の無い較正で保存され、被覆の検査で使えなくなる。どの時点からかも分からなくなる） |
| 4 | **決着**（2026-10-07 所有者の決定、推奨案）。自由記述の理由を記録に持つか | **持たない**（§2.4）。`calibrated_at` と較正ファイルの hash で git の履歴と突き合わせる | `coldaisle-calibrate --apply --reason` で `calibration.json` に理由の欄を足し、取り込みがそれを写す（`Calibration` の形が変わる） |
| 5 | **決着**（2026-10-07 所有者の決定、推奨案）。migration より前に取り込んだデータの扱い | **v2 の学習に使わない**（§2.8。被覆が無い） | 人が「この時刻からこの較正」と書いた最初の行を手で入れる経路を作る（推測で埋める経路になるので、作るなら別の決定記録） |
| 6 | **決着**（2026-10-07 所有者の決定、推奨案）。0087 §2.6 の扱い | **置き換えない（足す）。** 呼び出し側の `declared_changes` は残し、記録との和集合を使う | 較正については記録だけを正とし、`declared_changes` の `calibration_changed` を受け付けない（0087 §2.6 の一部を置き換える） |
| 7 | **決着**（2026-10-07 所有者の決定、推奨案）。drift（0056 §2.5。`coldaisle-drift`）もこの記録を読むか | **本記録では決めない。** drift の証拠 YAML の `changes` は人の宣言のまま。読むなら別の記録 | drift の CLI も必須で読み、行を `DeclaredChange(kind=calibration_changed)` に写す |
| 8 | **決着**（2026-10-07 所有者の決定、推奨案）。§2.3 の写像を作る関数の置き場所 | **レイヤ横断のモジュール**（`channels.py` の隣。湿度の述語と一緒に）に置き、取り込みの合成の起点・Dataset の合成の起点・0096 の `calibration_digest`（`control/model`）が共有する | 0096 の関数と同じ `control/model` に置き、`daemon.py`（合成の起点）から import する |
| 9 | **決着**（2026-10-07 所有者の決定、推奨案）。秒の切り捨ての扱い | **区間 `[floor(ts), ts]` の両端を検査へ渡す**（§2.6）。幅は CSV の書式から導く | CSV の時刻を ms にする（0008 §2.8 の CSV の形を変える。既存の CSV と混ざる） |
| 10 | **決着**（2026-10-07 所有者の決定、推奨案）。実装の PR | **0096 の実装 PR（#84 の後続）の後、`coldaisle-dataset` の v2 対応（0094 §2.4）と同じか直前の PR**。migration・取り込みの書き込み・読む関数・builder の引数・`--apply` の出力と `docs/calibration.md`（0096 §5 #9 と合わせる） | 記録（migration と取り込み）だけを先に入れ、読む側は CLI の v2 対応で入れる（記録が早く溜まり始める） |

## 6. 承認記録

**2026-10-07、リポジトリ所有者が §5 の10点をすべて推奨案で承認し、本記録を FINAL にした。**

| §5 の判断点 | 決定 | 本記録 |
|---|---|---|
| 1 | 記録先は、取り込みが書く SQLite DB の追記のみの新しい表 `calibration_activations` | §2.1 / §2.5 |
| 2 | 書き手は `coldaisle-daemon`（serial / mock）だけで、起動時・ソースを読む前に、実効の写像が変わったときだけ1行。`--apply` は書かない。手で `calibration.json` を編集した変更も同じ経路で記録する（0096 §5 #4 の論点） | §2.2 |
| 3 | 起動時に比較・追記に失敗したとき、または較正が変わったのに時計が保存済みの最後の時刻より進んでいないときは、取り込みを起動しない | §2.2 の4 / §2.7 |
| 4 | 自由記述の理由は持たない | §2.4 |
| 5 | migration より前に取り込んだデータは v2 の学習に使わない | §2.8 |
| 6 | 0087 §2.6 は置き換えず、記録と呼び出し側の宣言の和集合を使う | §2.6 |
| 7 | drift（0056）がこの記録を読むかは本記録では決めない | §5 #7 |
| 8 | 写像を作る関数はレイヤ横断のモジュールに置き、0096 の `calibration_digest` と共有する | §2.3 |
| 9 | CSV の秒の切り捨ては区間 `[floor(ts), ts]` の両端を検査へ渡して扱う | §2.6 |
| 10 | 実装は 0096 の実装 PR の後、`coldaisle-dataset` の v2 対応（0094 §2.4）と同じか直前の PR | §5 #10 |

これで 0096 §5 #4 の前提（追記のみの記録先）が決まった。0096 は書き換えない。
