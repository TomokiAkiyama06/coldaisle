# 決定記録 0105: 0104 段階 A（Model Registry の共有の root モード。PR #248）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が推奨案で承認）
- **Date**: 2026-10-07
- **Supersedes**: なし（0104 に書かれていない点への追加と、0104 の文面の読み方の明記。0104 / 0086 / 0062 の
  決定は置き換えない）
- **関連**: [0104](0104-authority-raise-least-privilege.md) §2.3 / §2.4 / §2.7 / §5 の 11 /
  [0086](0086-authority-approver-uid.md) §2.4 / [0062](0062-model-registry-operations.md) §2.1 /
  `src/coldaisle/control/model_registry.py` / `src/coldaisle/control/authority.py` /
  `src/coldaisle/authority_cli.py` / `docs/model-registry.md` / `docs/authority-rollout.md` / `AGENTS.md`
- **対象 Issue**: #217（実装 PR #248）/ #92

## 1. Context

0104 §2.7 の段階 A（`ModelRegistry(require_shared_root=True)`・作るファイルの mode・`raise` で Registry を読めないときの
終了コード・制御が Registry の lock を取らないことの構造の試験）を PR #248 で実装した。その実装で、0104 に書かれて
いない点、または文面だけでは読み方が分かれうる点が6点あり、PR #248 の「人間レビューが必要な点」と Codex の指摘への
返信で推奨案とともに示した。2026-10-07、所有者が6点すべてを推奨案で承認した。決定記録は追記のみなので、0104 の
本文には手を入れず、ここに残す。記録だけを読んだ人が、実装と違う挙動を前提にしないためである。

## 2. Decision

### 2.1 開発環境でも `raise` の Registry は共有の root の形にする

**決着（2026-10-07 所有者の決定、推奨案）。** `coldaisle-authority raise` は、開発でも Registry を共有の root
モードで開く。`--registry-root` は `chmod 2770`（setgid・other 権限なし）済みで `.registry.lock` のある root を
指す。`var/model-registry`（`0700`・setgid なし）をそのまま指すと、終了コード 1・`registry_error` になる。

- authority のディレクトリ（0086 §2.4。CLI はディレクトリを作らず、setgid を確かめる）と同じ扱いにする。
  開発だけ緩める経路（フラグ・環境変数）を作ると、本番でその経路を通る誤りが起きうる
- AGENTS.md のコマンド例と `docs/authority-rollout.md` に注記した

### 2.2 共有の root モードでも、書き込み操作は型やフラグで禁じない

**決着（2026-10-07 所有者の決定、推奨案）。** `require_shared_root=True` の `ModelRegistry` でも、
`register_candidate` / `mark_validated` / `promote` / `rollback` / `retire` を呼べる形のままにする。

- 使い手は `coldaisle-authority raise` だけで、呼ぶのは `pinned()` と `inspect()` だけ（`raise_stage()`）
- 導入先では、承認者に Registry の root への `w` が無く（0104 §2.3）、lock も `O_RDONLY` で開くので、書き込みは
  権限で失敗する。Registry を書けないことは権限が担保する（0104 §2.5）
- 書かないことの構造の試験は、`authority_cli.py` が共有の root モードで開くことと、`pinned()` を呼ぶのが
  `control/authority.py` だけであることで足りるとした

### 2.3 setgid だけで共有の root と判定し、`mkdir` と `fchmod` の間で止まったディレクトリは自動で直さない

**決着（2026-10-07 所有者の決定、推奨案。Codex P2 への対応）。** 0104 §2.4 の「共有の root（setgid 付き）では、
書き手も lock を作らない」のとおり、既定のモードの書き手は root の setgid だけで共有の root と判定する。

- Registry は新しいディレクトリを `mkdir` した後に `fchmod` で mode を決め（root と祖先は `0700`、root の下は親の
  bit を `2770` の mask で写す）、**mode を決めてから子と親を fsync する**（PR #248 で Codex P2 の2件に対応）
- それでも `mkdir` と `fchmod` の間で止まると（kill・`fchmod` の失敗）、setgid の親の下では継いだ setgid の付いた
  ディレクトリが残りうる。次の実行は既にあるディレクトリとして開くので直さない
  - 開発用の root なら、共有の root と取り違えて `RegistrySharedRootError`（`registry lock が無い`）で止まる
  - 共有の root の下の `artifacts/...` なら、`2700` のまま残り、他の書き手・読み手が入れない
- **どちらも壊れずに止まる（安全側）。** 自動で直すと、他人の作ったディレクトリの mode を変えようとしたり、
  導入手順で作った共有の root を開発用と取り違えて lock を作ったりする経路ができる。人が直す手順を
  `docs/model-registry.md`（「mode を決める前に止まったディレクトリを直す」）に書いた
- 判定を setgid 以外の根拠（導入手順が置く印のファイルなど）に変えることはしない

### 2.4 snapshot が壊れているときも、`raise` は終了コード 1・`registry_error`

**決着（2026-10-07 所有者の決定、推奨案）。** 0104 §5 の 11 の「Registry を読めない」には、権限・lock が無い・
root の形が違うことに加え、**`registry.json` が壊れている（検証できない）こと**も含める。`raise_stage()` は
`ModelRegistryError` / `OSError` / `ValueError` を `AuthorityRegistryUnavailableError`
（`AuthorityStoreError` のサブクラス）に包み、CLI は終了コード 1・`registry_error` へ写す。

- 壊れた snapshot は証拠の誤りではなく、Registry の状態の誤りである。承認と証拠を作り直しても通らない
- 「Production が無い」「Production が承認の stage を許さない」は従来どおり証拠・承認の拒否（終了コード 4）

### 2.5 開発用の root で作る lock は `0600` のまま

**決着（2026-10-07 所有者の決定、推奨案）。** setgid の無い root（開発用）で書き手が `.registry.lock` を作るときの
mode は、従来どおり `0600` とする。共有の root では lock を作らない（導入手順で作る。0104 §2.3 / §2.4）ので、
`0600` の lock が承認者や他の書き手を締め出すことは起きない。

### 2.6 作ったディレクトリの mode を決められないときは、その理由で報告する

**決着（2026-10-07 所有者の決定、推奨案）。** 新しく作ったディレクトリの `fchmod` または `fsync` の失敗は、
`UnsafeRegistryPathError`（「registry の作ったディレクトリの mode を決められない、または永続化できない」）にする。
PR #248 の当初の実装では「component が symlink または directory ではない」と報告しており、理由が誤っていた。
例外の型は変えない（呼び出し側の扱いは同じ）。

## 3. Consequences

### 良くなること

- 0104 の段階 A の挙動のうち、文面だけでは決まらなかった点が記録に残る
- 開発と本番で `raise` の Registry の扱いが同じになり、開発で通った手順が本番で別の理由で落ちない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 開発で `raise` を試すたびに、Registry を `2770` にして lock を置く手間が要る | AGENTS.md と `docs/authority-rollout.md` に書いた。authority のディレクトリと同じ手間 |
| 共有の root モードの `ModelRegistry` から書き込み操作を呼べてしまう | 導入先では権限で失敗する。使い手は1か所で、構造の試験が固定する |
| `mkdir` と `fchmod` の間で止まると、人が直すまで止まり続ける | 壊れずに止まる。直す手順を `docs/model-registry.md` に書いた |

## 4. 却下した代替案

- **開発だけ共有の root の確認を外すフラグ（§2.1）**: 本番でそのフラグを付けたまま使う誤りの経路ができる
- **共有の root モードで書き込み操作を例外にする（§2.2）**: 使い手が読み取りしかしない今は得るものが小さい。
  必要になったら別の型に分ける
- **継いだ setgid の残ったディレクトリを次の実行で自動で直す（§2.3）**: 導入手順で作った共有の root と区別できない
  （どちらも setgid 付き）。区別のために印のファイルを足すと、0104 §2.4 の判定を変えることになる
- **壊れた snapshot は終了コード 4 のまま（§2.4）**: 承認と証拠を作り直す方向へ運用者を誘導してしまう

## 5. 未決事項

なし（段階 B の `docs/ubuntu-deploy.md` と段階 C の実機の確認は 0104 §2.7 のまま）。
