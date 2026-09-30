# 決定記録 0082: #92 段階 2（authority の配線と管理ソケットでの降格）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-09-30、リポジトリ所有者が承認）
- **Date**: 2026-09-30
- **Supersedes**: [0076](0076-implementation-settled-points.md) §2.2 (a) の「`superseded_by` は自分の
  `command_id` より大きい」のみ（§2.4 で「自分の `command_id` と異なる」に緩める）。0076 の他の点は有効。
  本記録の他の節は、0057 / 0072 が実装 PR へ委ねた点と、どの記録も決めていなかった点を記録するもので、
  どの記録の決定も置き換えない
- **関連**: [0057](0057-authority-rollout-stage-changes.md) §2.1 / §2.6 /
  [0060](0060-control-loop-runtime.md) §2.7 /
  [0072](0072-control-admin-entry.md) §2.2 / §2.3 / §2.6 / §2.7 /
  [0076](0076-implementation-settled-points.md)（同じ形の前例） /
  [0081](0081-drain-authority-on-receiver-death.md)
- **対象 Issue**: #92（PR #192）

## 1. Context

PR #192（0072 §2.10 段階 2）は、`coldaisle-fand` へ `AuthorityRuntime` を配線し、管理ソケットで
`lower_authority` / `rollback_authority` を受ける。実装の途中で、0057 / 0072 が実装 PR に委ねた点や
明記していなかった点を決める必要があった。PR の「人間レビューが必要な点」1〜9 として所有者に示し、
2026-09-30 に実装どおり承認された（項目 10 は #74 の不具合で、決定ではない）。本記録はそれを残す。

## 2. Decision

### 2.1 journal の版を v3 に上げる（0072 §2.6 が委ねた点）

- `authority_journal_unreadable` の event は v3 の journal にだけ置ける
- v1 / v2 の journal は、次に書くときに v3 で書き直す。**版は常に上げる。** 旧 reader に
  「知らない journal」として一律に拒ませるためである
- 切り戻すときは journal の退避と昇格のやり直しが要る（`docs/authority-rollout.md`）

### 2.2 `authority.json` の場所と lock の待ち上限

- 場所は CLI 引数 `--authority-root`（既定 `var/authority`）
- lock の待ち上限は `safety.yaml` の `tick_deadline_ms` を流用する（decision trace の保存の busy timeout と
  同じ扱い。0060 §2.7）。専用の設定値は置かない

### 2.3 起動時に journal が壊れていたら制御を取らない

- `StartupEnvironmentError` として終了コード 5 で終わる（0057 §2.1 の「止める」を起動時の分類に当てはめる）
- `SHADOW` で起動する案は採らない。壊れた journal を「記録の無い状態」と読み替えないため

### 2.4 監査の表の `superseded_by` の CHECK を緩める（migration 0009）

- 「`superseded_by > command_id`」（0076 §2.2 (a)）を「`superseded_by <> command_id`」にする
- authority の枠では、先に届いた深い降格が後から来た浅い降格を置き換える（0072 §2.2 の合成）ため、
  置き換えた側の `command_id` の方が小さいことがある

### 2.5 `lower_authority` の `to_stage` は `shadow` / `limited` / `expanded` だけ

- `full` は `invalid_fields` で拒否する。`full` はどの stage からも下げる向きにならず、受理すると
  「full にした」と読み違えうる

### 2.6 journal へ書けなかった降格の扱い

- 管理ソケット由来の降格と `journal_unreadable` による降格は、書けなければ予約として残し、
  heartbeat の後に1件ずつ書き直す。同じ tick で `observe()` が lock を待ったときは次の tick に回す
- `observe()` の自動降格はその場で1回だけ書き、書けなければ memory 上の上限だけを持つ（従来どおり。
  書き直しの対象には揃えない）
- 停止の手順では、書き残せていない降格を1回だけ書き直す（lock の待ち上限つき）。例外での停止では待たずに
  error に残す

### 2.7 decision trace の `authority` 欄の時点

- Gate が stage を読んだ時点（tick の先頭で降格を入れた後）の写しとする
- heartbeat の後の書き残しと journal の読み直しの結果は、次の tick の記録に現れる

### 2.8 読み直した journal が既知の履歴を延長していないとき

- 新しい原因を足さず、`authority_journal_unreadable` として扱う（`SHADOW` へ下げる）
- ログの文言と error の中身で、壊れた journal と区別する

### 2.9 journal に人の event が残らない場合

- 書く時点で journal が既にその stage 以下なら、journal の上では何も変わらないので event を足さない
  （例: 読めない journal による自動降格が先に `shadow` を書いた）
- そのときの人の指令は、監査の表（`accepted` / `applied`）にだけ残る

## 3. Consequences

- 良くなること: 実装で選んだ扱いが決定として残り、後から読む人が PR の議論を探さずに済む
- 悪くなること: journal v3 への一方向の書き直しで、旧版への切り戻しに手作業が要る。緩和策: 手順を
  `docs/authority-rollout.md` に書いた

## 4. 却下した代替案

- **journal の版を必要なときだけ上げる**（v3 の event を書くときだけ）: 旧 reader が v3 の journal を
  一部だけ読み違えうる
- **起動時の壊れた journal で `SHADOW` から始める**: 0057 §2.1 の「止める」に反し、壊れた記録を
  正常な初期状態と区別できなくなる
- **lock の待ち上限を専用の設定値にする**: 同じ意味の上限が2つになり、tick の締め切りとずれうる
- **`observe()` の自動降格も予約として書き直す**: 所有者の判断で今回は揃えない（§2.6）

## 5. 未決事項

なし（PR #192 の項目 10 の受付スレッドの停止の不具合は、#74 の別 PR で直す）
