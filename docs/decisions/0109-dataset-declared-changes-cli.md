# 決定記録 0109: `coldaisle-dataset` の v2 で宣言された変更（`DeclaredChange`）を渡す形（必須の `--declared-changes <path>` の YAML ファイル・空も `changes: []` で明示・較正の変更の記録との和集合）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-07
- **Supersedes**: なし（0094 §2.4 / §5 #1 が「CLI の v2 対応の PR で決める」とした点をここで決める。0087 §2.6・0099 §2.6・
  0094・0102 の決定は置き換えない。§5 #1 の推奨を採ると、builder の宣言の扱いが 0099 §2.6 の記録の行と同じ区間になる
  （狭める向きだけ）。これも置き換えではなく、0087 §2.6 の検査に区間を**足す**ものとして書く）
- **関連**: [0087](0087-dataset-v2-action-grid.md) §2.6 / [0094](0094-dataset-v2-settled-points.md) §2.4 / §5 #1 /
  [0099](0099-calibration-change-log.md) §2.6 / §5 #6 / §5 #9 /
  [0102](0102-calibration-change-log-settled-points.md) §2.3 /
  [0100](0100-replay-export-binding.md) §2.6 / §2.8 / §2.10 /
  [0056](0056-model-drift-detection-and-retraining-triggers.md) §2.5（`DeclaredChange` と drift の証拠 YAML の `changes`）/
  [0031](0031-thermal-dataset-contract.md) §2.3 / §2.4 /
  [0079](0079-model-artifact-formats.md) §2.3（既定値を置かない）/
  `src/coldaisle/dataset.py`（`ThermalDatasetV2Builder` / CLI）/ `src/coldaisle/control/drift/model.py`（`DeclaredChange` /
  `ChangeKind` / `MAX_DECLARED_CHANGES`）/ `src/coldaisle/drift.py`（`DriftEvidenceManifest.changes`）/
  `src/coldaisle/calibration_log.py`（`calibration_change_points` / `reject_changes_overlapping`）/
  `docs/thermal-dataset.md` / AGENTS.md「絶対に守るルール」4 / 9 / 10
- **対象 Issue**: #83

## 1. Context

`ThermalDatasetV2Builder.build()` は `declared_changes: tuple[DeclaredChange, ...]` を**既定値なしで**受け取る（0087 §2.6。
宣言が無い場合も空を明示させる）。PR #221 は builder までを入れ、`coldaisle-dataset` の CLI は v1 のままにした。
CLI でその値をどう渡すかが決まっていなかったためである（0094 §2.4 / §5 #1）。0102 §2.3 は、学習の入口と v2 の CLI で
`--calibration-history-db` を必須にする配線も、この CLI の v2 対応の PR で行うと決めた。

いまの事実（コードで確かめた）。

- `DeclaredChange` は `kind`（`ChangeKind`: `fan_replaced` / `sensor_replaced` / `calibration_changed` /
  `hardware_config_changed`）・`ts_ms`（`int`, `>= 0`）・`detail`（`str`, 500 文字まで、既定 `""`）を持つ strict な型である
- 人が `DeclaredChange` を書く既存の入口は、`coldaisle-drift --evidence` の YAML の `changes:` の一覧だけである
  （`DriftEvidenceManifest.changes`。`kind` の文字列を `ChangeKind` に写してから検証し、未知の種別は拒む。
  検知器は `MAX_DECLARED_CHANGES = 64` を上限にし、重複を除いて並べる）
- `DeclaredChange` の永続的な記録先は無い（0099 §1）。較正の変更だけは、0099 で `calibration_activations` に記録される
- builder は宣言のうち `calibration_changed` の `ts_ms` だけを取り出し、記録の行の区間 `[floor(ts), ts]` の両端と
  **和集合**にして 0087 §2.6 の検査（`reject_calibration_changes`。点が期間に入れば拒否）へ渡す（0099 §5 #6）。
  記録の行には区間の交わりの検査（`reject_changes_overlapping`）も掛かるが、**宣言は点のまま**で区間を持たない
- 他の種別（`fan_replaced` など）は builder の結果に効かない（0087 §2.6 が `calibration_changed` だけを取り出すと決めた）
- 宣言は生成を**拒否するかどうか**にだけ効き、作った example と manifest の bytes には効かない

## 2. Decision

### 2.1 渡し方: 必須の `--declared-changes <path>` に YAML ファイルを渡す

`coldaisle-dataset` の v2 は `--declared-changes <path>` を**必須**の引数とし、既定値を置かない（0079 §2.3 / 0087 §2.6）。
ファイルは次の形の YAML とする。

```yaml
schema_version: 1
changes:
  - kind: calibration_changed
    ts_ms: 1790000000000          # Unix ms（UTC）。本番の取り込みの時刻と同じ軸
    detail: "プローブを差し替えて再較正した"
  - kind: fan_replaced
    ts_ms: 1790500000000
```

- 一覧の1件の形は、drift の証拠 YAML の `changes:`（0056 §2.5）と**同じ** `DeclaredChange` とする。同じ宣言を
  2つのファイルへ写せる。`kind` の文字列から `ChangeKind` への写しは drift と同じ関数を共有する（実装で
  レイヤ横断の場所へ移す。合成の起点どうしを import させない）
- `ts_ms` は整数の Unix ms（UTC）だけを受け付ける。日時の文字列（ISO 8601 など）は受け付けない（§5 #4）
- `detail` は説明用で、検査には使わない。実機の個体識別子（ROM・ホスト名・絶対パス）を書かないことを
  `docs/thermal-dataset.md` に書く（AGENTS.md ルール 10。ファイルはコミットしないが、dataset と同じく持ち出されうる）

### 2.2 宣言が無いことも明示する

- 宣言が無い場合は `changes: []` を書いたファイルを渡す。**`changes` の鍵は省略できない**（型に既定値を置かない）
- 次はすべて拒否する: `--declared-changes` を渡さない（argparse のエラー）・空のファイル・`null` だけのファイル・
  最上位が mapping でない・`changes` の鍵が無い・`changes` が list でない
- 「宣言なし」を表す専用の flag（`--no-declared-changes` など）は作らない。空の宣言も同じ形のファイルとして残し、
  dataset を作った手順と一緒に保存できるようにする

### 2.3 検証と拒否（builder を呼ぶ前、DB を開く前）

ファイルは builder を呼ぶ前、専用 DB と `--calibration-history-db` を開く前に読み、次をすべて満たさなければ
dataset を作らずに終了する（終了コードは非 0。構造化ログに、ファイルの basename・何件目か・どの検査かを出す）。

1. regular file である（symlink は `O_NOFOLLOW` で拒む。`--replay-path` と同じ向き）。bytes を1回だけ読み、
   その bytes から解析する（検証した bytes と使う bytes を同じにする。0031 §2.3 の「path を再 open しない」）
2. UTF-8 として読め、`yaml.safe_load` で読める。**mapping の重複した鍵は拒否する**（§5 #5）
3. 最上位の鍵は `schema_version` と `changes` だけ（`extra="forbid"`）。`schema_version` は `1` だけ
4. 各件は `kind` / `ts_ms` / `detail` だけを持つ。`kind` は `ChangeKind` の値（未知の種別は拒否。黙って落とすと
   宣言したはずの変更が無かったことになる）。`ts_ms` は `bool` でも浮動小数でもない `>= 0` の整数（strict）。
   `detail` は 500 文字まで
5. 完全に同じ件の重複は1件にまとめる（drift の検知器と同じ。結果に効かない）。並びの順は問わない
6. 重複をまとめた**後の**件数が `MAX_DECLARED_CHANGES`（drift と同じ構造上限）以下（drift の検知器と同じ順。PR #250 の Codex の指摘）
7. `ts_ms` が source run の期間の外にあっても拒否しない（期間の外の変更は検査に効かないだけである）

### 2.4 較正の変更の記録との和集合（0099 §5 #6）

- 検査に使う較正の変更は、**記録（`calibration_activations`）の各行**と**宣言の `calibration_changed`** の和集合とする。
  宣言は変更を**足せる**が、記録の行を**消せない**（0099 §2.6 のまま）
- 宣言は**被覆を満たさない**。被覆（期間の先頭以前に記録の行がある。0099 §2.6）は記録だけで判定する。宣言は
  「その時刻に変わった」とは言うが、「どの較正が効いていたか」を記録しないためである
- 宣言の時刻も記録の行と同じく、本番の取り込みの時刻（Unix ms）の軸で書く。専用 DB の時刻は CSV の書式で
  切り捨てられている（0099 §2.6）ので、**宣言の `calibration_changed` も記録の行と同じ区間 `[floor(ts), ts]` として
  扱い、期間との交わりで拒否する**（§5 #1。いまの builder は宣言を点として扱う）
- example が 0 件の dataset（0094 §2.1）では、記録の行と同じく宣言の検査も効かない（期間が定まらない）。ただし
  §2.3 のファイルの検証は 0 件でも省かない（0099 §2.6 が記録の読み込みの検証を 0 件でも省かないのと同じ）
- `calibration_changed` 以外の種別は、§2.3 で検証したうえで生成に**効かせない**（0087 §2.6 のまま）。期間の中に
  そうした宣言があるときは、構造化ログに警告を出す（「検査に使わない種別の宣言が期間の中にある」）（§5 #2）

### 2.5 出力に残すもの

- 宣言は dataset の manifest にも examples にも**書かない**。宣言は生成の可否にだけ効き、作った dataset の bytes は
  宣言に依らない（§1）。manifest の形（`DatasetManifestV2`）は本記録では変えない（§5 #3）
- 構造化ログ（JSON Lines）に、読んだファイルの bytes の SHA-256・件数・種別ごとの件数・期間の中に入った件数を出す。
  拒否したときは、宣言と記録のどちらの変更で拒否したかを区別して出す

### 2.6 例

宣言の無い dataset の生成（数値は説明用の仮の値。`docs/thermal-dataset.md` の v1 の例と同じく本番の値ではない）。

```yaml
# var/declared-changes.yaml
schema_version: 1
changes: []
```

```bash
uv run coldaisle-dataset \
  --dataset-version 2 \
  --declared-changes var/declared-changes.yaml \
  --calibration-history-db var/coldaisle.db \
  --db var/replay.db \
  ...                                  # v1 と同じ引数と、v2 の spec の欄（§5 #6 の設計メモ）
```

拒否される例。

| ファイル | 理由 |
|---|---|
| 空のファイル / `changes:` だけ（値が `null`） | 空の宣言は `changes: []` で明示する |
| `schema_version: 1` だけ | `changes` の鍵が無い |
| `kind: calibration_change`（綴りの誤り） | 未知の種別 |
| `ts_ms: "2026-10-01T12:00:00"` | 整数の Unix ms だけを受け付ける |
| `ts_ms: 1.79e12` / `ts_ms: true` | strict。浮動小数と bool は拒否 |
| 同じ件の中で `ts_ms` を2回書いた | 重複した鍵 |

## 3. Consequences

### 良くなること

- 空の宣言も含めて、どの宣言で dataset を作ったかをファイルとして残せる。drift の証拠 YAML と同じ形なので、
  宣言を2か所で書き分けない
- 宣言の誤り（綴り・単位・時刻の形）を、DB を読む前に拒否できる
- 宣言の較正の変更が、記録の行と同じ区間で検査され、CSV の秒の切り捨てで期間の境界をすり抜けない（§5 #1 を採る場合）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| dataset を作るたびに、宣言が無くても `changes: []` のファイルが要る | 1行のファイルで足りる。「宣言を忘れた」と「宣言が無い」を区別するための手間で、0087 §2.6 が求めるもの |
| 時刻を Unix ms で書く必要があり、人には読みにくい | timezone の取り違え（0100 が閉じた種類の穴）を宣言の入口で作らない。`detail` に人の読む日時を書いてよい |
| 宣言が manifest に残らず、artifact だけからは何を宣言したかが分からない | 宣言は bytes に効かない。ログにファイルの SHA-256 を残し、ファイルを手順と一緒に保存する。必要になれば §5 #3 で manifest へ足す |
| `fan_replaced` などを宣言しても dataset の生成は拒否されない | 0087 §2.6 のまま。警告をログに出す。拒否に広げるかは §5 #2 |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| A. JSON ファイル（`--declared-changes <path>.json`） | 形は同じにできるが、drift の証拠と `config/*.yaml` が YAML で、人が書くファイルの形が2つになる。コメントを書けない |
| B. 引数の繰り返し（`--declared-change calibration_changed:1790000000000:detail`）と空のための `--no-declared-changes` | 区切り文字と `detail` の quote の規則を別に決めることになる。空の明示に別の flag が要り、2つの flag の組み合わせの検査が増える。宣言を手順と一緒にファイルとして残せない |
| C. DB から読む（`events` の新しい kind、または宣言の新しい追記のみの表） | 書き手が無い（`events` の書き手は `coldaisle-eventd` だけ。0045 §2.5）。宣言の記録先を新しく作るのは本記録の範囲を超える（0099 §4 が `events` を較正の記録先として却下したのと同じ理由も残る）。必要になれば別の記録で決め、そのときも本記録の入口から読める形にする |
| D. drift の証拠 manifest（`--evidence`）をそのまま渡す | `start_ms` / `end_ms` / `shadow_jsonl` / `dataset` など dataset の生成に無関係な欄を持ち、2つの CLI の入力が結びつく。1件の形だけを共有する（§2.1） |
| E. `--declared-changes` を省略したら空として扱う | 0087 §2.6 の「既定値を置かない・空も明示」に反する。宣言を忘れたことと宣言が無いことを区別できない（AGENTS.md ルール 4 の向き） |
| F. 環境変数・標準入力から読む | 既定の値が環境に紛れ込む。手順として残しにくい |
| G. `ts_ms` に加えて ISO 8601 の文字列も受け付ける | offset の無い文字列の timezone が決まらない。offset 付きだけを受け付けても入口の規則が増える。§5 #4 |

## 5. 未決事項（所有者に確認したい点）

| # | 論点 | 推奨 | 代替 |
|---|---|---|---|
| 1 | 宣言の `calibration_changed` を、記録の行と同じ区間 `[floor(ts), ts]` として期間との交わりで検査するか（§2.4。builder の挙動の変更） | **区間で検査する**。宣言の時刻は本番の ms の軸で、専用 DB の時刻は秒に切り捨てられているので、点のままだと同じ秒の中の変更が期間の終端をすり抜ける（例: 変更が `12:00:00.700` で、期間の終わりが切り捨てた `12:00:00`）。狭める向きだけで、0087 §2.6 の検査に区間を足すもの。実装は記録の行と同じ関数（`calibration_change_points` / `reject_changes_overlapping` の区間の扱い）を共有する | 点のまま（いまの builder）。宣言は人が書く値で精度の意味が曖昧だが、すり抜けは残る |
| 2 | `calibration_changed` 以外の種別（`fan_replaced` / `sensor_replaced` / `hardware_config_changed`）が期間の中にあるとき | **0087 §2.6 のまま生成には効かせず、警告をログに出す**（§2.4）。拒否へ広げるのは 0087 §2.6 の一部を置き換える判断で、本記録（渡し方）の範囲を超える | 期間の中にあれば生成を拒否する（ファンやセンサーの交換をまたぐ dataset を作らない。AGENTS.md ルール 4 の向きでは安全側。採るなら 0087 §2.6 を部分的に置き換える別の記録にする） |
| 3 | 宣言を dataset の manifest に残すか | **残さない**（§2.5）。宣言は bytes に効かず、manifest の形（`DatasetManifestV2`）を変えずに済む。ログに SHA-256 を残す | `DatasetManifestV2` に宣言の正規化した一覧（または digest）を足す（artifact だけから宣言を辿れるが、v2 の manifest の形が変わり、`detail` の自由記述が artifact に入る） |
| 4 | 時刻の形 | **整数の Unix ms（UTC）だけ**（§2.1）。drift の証拠 YAML と同じで、timezone の取り違えを作らない | ISO 8601 の offset 付き文字列も受け付ける（読みやすいが、規則が2つになる） |
| 5 | YAML の重複した鍵 | **拒否する**（§2.3 の 2）。`yaml.safe_load` は後の値で黙って上書きするので、重複を拒否する loader を使う。生成の可否を決める入力なので黙って読み替えない。drift の証拠 YAML（`DriftEvidenceManifest.from_file`）にも同じ穴があるが、本記録では変えない（直すなら drift の別 PR） | `yaml.safe_load` のまま（drift と同じ。重複の片方が黙って捨てられる） |
| 6 | CLI で v1 と v2 をどう選ぶか（本記録の主題ではないが、同じ PR の配線で決めることになる。仕様に無いのでここで確認する） | **`--dataset-version {1,2}` を必須にし、既定値を置かない**。v2 では `--declared-changes` / `--calibration-history-db` / `--action-step-ms` / `--action-steps` / `--action-stale-after-ms` を必須にし、v1 ではこれらを渡すと拒否する（v1 には較正の検査が無く、渡しても効かない値を黙って受け取らない）。既存の v1 の呼び出し（`docs/thermal-dataset.md` と試験）には `--dataset-version 1` を足す | 省略時は v1（既存の呼び出しを壊さないが、v2 のつもりで v1 を作る経路が残る）／サブコマンド（`coldaisle-dataset v1 ...` / `v2 ...`。形は明確だが既存の呼び出しが同じく変わる） |
| 7 | v2 の CLI を 0100 の段 3（`ReplayBindingV2` と `csv_exports` との照合）より前に入れるか | **待たずに入れる**。段 3 までは 0099 §2.6 の2つの暫定運用（同じ timezone で再生・同じ本番の DB から export した CSV だけ）を `docs/thermal-dataset.md` に残したまま使う。学習の入口はまだ無く、段 3 の後は builder が束縛の無い run を拒否する（0100 §2.8）ので、段 3 の前に作った v2 の dataset は作り直すことになる | 段 3 を待ってから v2 の CLI を入れる（暫定運用の期間に v2 を CLI から作れない） |

### 実装の設計メモ（`DeclaredChange` の渡し方に依存しない部分。本記録の FINAL 後の PR）

- `--calibration-history-db <path>` を v2 で必須にし、`read_calibration_history(path)`（読み取り専用・migration を
  当てない）の結果を builder の `calibration_history=` へ渡す。専用 DB（`--db`）と同じ path（実体）を渡したら拒否する
  （専用 DB は migration で空の `calibration_activations` を持ちうる。example があれば 0099 §2.7 の被覆の無さで
  拒否されるが、example が 0 件の dataset では通ってしまうので、取り違えとして先に拒否する）
- v2 の spec は `DatasetSpecV2`（v1 の欄 + `action_step_ms` / `action_steps` / `action_stale_after_ms`）を CLI の
  必須の引数から作る。既定値は置かない（0087 §2.2）
- 生成は `ThermalDatasetV2Builder(store).build(source_run=, spec=, declared_changes=, calibration_history=)`、出力は
  v1 と共通の `write_dataset()`（版は入力の型で決まる）。0 件の dataset も書き出し、`excluded` の件数を構造化ログに出す
  （0094 §2.1）
- 読む順序: 宣言のファイルの検証 → `--replay-path` の fingerprint → `--calibration-history-db` の読み込み → 専用 DB の
  builder。DB を開く前に引数とファイルの誤りで止める
- `docs/thermal-dataset.md` の「CLI はまだ v1 だけ」を書き換え、v2 の例と宣言のファイルの例を足す
