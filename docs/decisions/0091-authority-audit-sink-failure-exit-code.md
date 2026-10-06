# 決定記録 0091: `coldaisle-authority` は監査ログを書けなかったことを握りつぶさず、終了コード 6 で知らせる

- **種別**: Decision Record
- **Status**: FINAL（2026-10-06、リポジトリ所有者が Issue #218 の判断点 1〜4 をすべて推奨案で承認。§6）
- **Date**: 2026-10-06
- **Supersedes**: なし（0086 §2.8 の「1操作1行の構造化ログ」と、#216 で決めた終了コード 0〜5 は変えない。
  「journal は確定したが監査の記録に失敗した」を区別する終了コードを1つ足すだけ）
- **関連**: [0086](0086-authority-approver-uid.md) §2.8（CLI は昇格・rollback のたびに構造化ログを1行出す） /
  [0089](0089-authority-bound-to-loaded-artifact.md) / [0090](0090-authority-bound-to-loaded-config.md)
  （同じ PR #216 で決めた authority の CLI まわり） /
  `docs/authority-rollout.md`（`coldaisle-authority` の終了コード表） /
  AGENTS.md コード規約「例外は握りつぶさない」
- **対象 Issue**: #218（PR #216 の Codex P2「Handle audit sink write failures」を切り出したもの）

---

## 1. Context

`coldaisle-authority raise` / `rollback` は、journal（`authority.json`）を書いた後に、stderr へ JSON Lines の
構造化ログを1行出す（0086 §2.8。以下「監査の行」）。行は `logging.StreamHandler` が書く。

`StreamHandler.emit()` は、書き込みや flush で起きた例外を `handleError()` へ渡し、既定では traceback を
stderr へ出そうとするだけで**呼び出し元へは返さない**。stderr が閉じたパイプ・切れた journald の stream・
`ENOSPC` などで書けないと、次のことが起きる。

- `LOGGER.info(...)` は普通に戻る
- コマンドは終了コード 0 を返す
- 監査の行は残らない

#216 で直したのは encode の失敗（`ensure_ascii=True`）だけで、書き込みそのものの失敗は残っている。

journal の操作そのものは確定している（v4 の journal は承認者の uid と event を持つので、何が起きたかは
journal から追える）。そのため、これを「操作を行わなかった（1）」として報告してはならない。一方で、
監査の行が欠けたことを人もスクリプトも終了コードで気づけないのは「例外を握りつぶさない」に反する。

## 2. Decision

### 2.1 拾い方：書き込みの失敗を記録する `StreamHandler`

- `logging.StreamHandler` を継承した handler（`logs.FailureRecordingStreamHandler`）を作る。
  `handleError()` を上書きし、「書き込み（`emit` の中の `write` / `flush`）が失敗した」ことを数えて残す。
  **例外は投げない**（journal の操作や stdout の結果の出力を止めない）。traceback も出さない
  （書けない stderr へもう一度書こうとするだけで、意味が無い）
- `flush()` も上書きし、失敗を同じく数える（`flush()` は `emit()` の外から呼ばれると `handleError()` を
  通らないため。終了直前の flush と `logging.shutdown()` の flush が該当する）
- 使うのは `coldaisle-authority` だけ。`logs.configure()` に `handler` の引数を足し、
  `coldaisle-authority` がこの handler を渡す。引数を渡さない呼び出し（他のデーモン・CLI）の挙動は変えない
- 既存の `ensure_ascii=True`（0086 §2.8 / #216）はそのまま保つ

### 2.2 終了コード 6

| 終了コード | 意味 |
|---|---|
| 6（新設） | **journal の変更は確定した（`durable`）が、監査の記録（stderr の JSONL）に失敗した** |

- stdout の結果 JSON に `audit_logged: bool` を足す。**常に出す**（成功のときは `true`）
- 判定の範囲は「**操作の行を書いた時点から、終了直前の flush まで**」。操作の行（とその flush）が
  失敗したか、終了直前に handler を flush して失敗したら、監査の記録に失敗したとする
  - 結果の `audit_logged` は、結果を stdout へ出す時点（操作の行とその flush の後）までの判定である。
    終了直前の flush だけが失敗したときは、結果が `audit_logged: true` のまま終了コードが 6 になりうる。
    **終了コードを正とする**（§5 の 1）
- **ディレクトリの `fsync` の失敗（5）と重なったら 5 を優先する。** 永続化を確かめられないことのほうが
  運用者の次の手（rollback のやり直し）に効くため。その場合も結果には `audit_logged: false` を出す
- 操作が行われなかった失敗（1 / 3 / 4 など）は**従来の終了コードのまま**にする。監査の失敗で上書きしない
  （「行わなかった」ことのほうが重要で、journal も変わっていない）。この経路は stdout に結果を出さないので
  `audit_logged` も無い
- stdout に書けない（`result_not_written`）ことは従来どおり終了コードを変えない

### 2.3 raise と rollback を分けない

raise・rollback とも、監査の記録に失敗したら 6 にする。rollback の no-op（`already_baseline`。何も書かず、
ディレクトリの `fsync` をやり直して通ったとき）も同じ。

### 2.4 運用：やり直さない

終了コード 6 は journal の変更が確定していることを意味する。**操作をやり直さない**（raise は同じ承認では
revision が進んでいてやり直せず、別の承認で上げ直すと2段上がる）。journal（v4 は承認者の uid と event を
持つ）で何が起きたかを確かめ、監査の記録の欠けを手で補う。rollback はやり直しても no-op（`already_baseline`）で
安全である。`docs/authority-rollout.md` の終了コード表と注記に書く。

## 3. Consequences

良くなること。

- 監査の行が欠けたことを、人もスクリプトも終了コードと結果の `audit_logged` で気づける
- journal の操作は監査の記録の成否に左右されない（失敗を journal の失敗として報告しない）

悪くなること。

- 終了コードが1つ増え、0 以外でも「変更は確定している」ものが 5 と 6 の2つになる。緩和: 終了コード表に
  「やり直すか」を並べて書く
- 終了直前の flush だけが失敗したとき、結果の `audit_logged` と終了コードが食い違いうる（§2.2。§5 の 1）

## 4. 却下した代替案

- **監査の行を書けなければ例外にして終了コード 1 にする**: journal は変わっているのに「行わなかった」と
  伝え、運用者がやり直しや逆向きの操作に進みかねない（`emit_after_commit` と同じ理由。codex P1）
- **rollback は成功（0）のまま警告にとどめる**: 安全側の操作でも、監査の行が欠けたことは同じく気づける
  べきで、扱いを分けると表が複雑になる。rollback はやり直しても安全なので、6 で困ることは無い
- **専用の sink（ファイル・journald へ直接）を足す**: 書き込み先を増やすと、その権限と保持の設計が要る。
  stderr の JSONL を監査の記録にする 0086 §2.8 の形を保つ
- **6 と 5 が重なったら 6 を優先する**: 永続化を確かめられないことのほうが次の手（rollback のやり直し）に効く

## 5. 未決事項

| # | 内容 | 推奨案（実装） | 決める場所 |
|---|---|---|---|
| 1 | 終了直前の flush だけが失敗したときの、結果の `audit_logged` と終了コードの食い違い | **終了コードを正とする**（結果は先に出しているので書き直せない）。実際には `StreamHandler.emit()` が行ごとに flush するので、操作の行の時点で失敗が見えることがほとんどである | PR の人間レビュー |
| 2 | 失敗した経路（1 / 3 / 4）で監査の行を書けなかったことを知らせるか | **知らせない**（終了コードは従来どおり。stdout に結果が無い経路で、journal も変わっていない） | 必要になったとき |

## 6. 承認記録

**2026-10-06、リポジトリ所有者は Issue #218 の「決めること」を、すべて推奨案で決めた。**

| Issue #218 の判断点 | 決着（2026-10-06 所有者の決定、推奨案） | 本記録 |
|---|---|---|
| 1. handler の作り | `StreamHandler` を継承し `handleError` を上書きして書き込みの失敗を記録する（例外は投げない）。flush の失敗も拾う。使うのは `coldaisle-authority` だけ。`ensure_ascii=True` は保つ | §2.1 |
| 2. 終了コード | 新設の 6。stdout の結果に `audit_logged` を常に出す。5 と重なったら 5 を優先し `audit_logged: false` を出す。行われなかった失敗（1/3/4 など）は従来のまま。判定は操作の行の後・終了直前の flush まで | §2.2 |
| 3. raise と rollback | 分けない（どちらも 6。rollback の no-op も同じ） | §2.3 |
| 4. 決定記録と運用の手順 | 本記録を実装より先に書く。`docs/authority-rollout.md` の終了コード表と注記に 6 を足す | 本記録 / §2.4 |
