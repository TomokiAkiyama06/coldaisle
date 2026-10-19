# 決定記録 0159: 実行環境の指紋が `__pycache__` を読まないことを既知の限界として受け入れる

- **種別**: Decision Record
- **Status**: FINAL（2026-10-19、リポジトリ所有者の承認）
- **Date**: 2026-10-19
- **Supersedes**: なし（[0157](0157-hint-runtime-launch-form.md) §2.2 の「`__pycache__` を除く」を**補う**。置き換えではない。
  0157 §3「悪くなること・その緩和」の既知の限界の表に1行を足すのと同じ扱いで、0157 は書き換えない）
- **関連**: [0066](0066-workload-hint-stage-b-conditions.md) §2.1 (c) /
  [0134](0134-authority-raise-host-verification.md) §2.2 /
  [0152](0152-hint-monotonic-gate-certificate.md) §2.3 /
  [0157](0157-hint-runtime-launch-form.md) §2.2 / §3 /
  `src/coldaisle/hint_certificate_digest.py`（PR #349）/
  `docs/ubuntu-deploy.md` 2 / 6.7 /
  AGENTS.md「絶対に守るルール」4
- **対象 Issue**: #288（PR #349 の Codex P1）

---

## 1. Context

0157 §2.2 は、部品 `environment` の `sys.path` の各項目の中身の digest を「`source_tree` と同じ符号化（`__pycache__` を除く）」で
作ると決めた。PR #349 はそのとおりに実装した（`__pycache__` の外の `*.pyc` は、ソースの無い module として import されうるので含める）。

PR #349 の Codex の P1 は次のとおり。

- Python 3.12 は `__pycache__` の `.pyc` を実行する。とくに unchecked-hash の `.pyc` は、ソースと照合されずに読まれるので、
  ソースと無関係なコードを入れられる。`__pycache__` を digest から除くと、キャッシュだけを差し替えても `environment` と
  `source_tree` の指紋が変わらず、実行時のコードが変わったのに証明書が合い続ける（B0 に倒れない）

`__pycache__` を指紋に入れる・キャッシュのある環境を拒む、のどちらも 0157 に書かれていない判断なので、所有者が決めた。

## 2. Decision

### 2.1 受け入れる限界

- 部品 `environment`（`sys.path` の項目の中身）と `source_tree` は、`__pycache__` のディレクトリを読まないまま（0157 §2.2 のとおり）とする。
  **実装は変えない**
- その結果、`__pycache__` の `.pyc` だけを差し替えた改変（unchecked-hash の `.pyc`、あるいはソースの mtime と大きさに合わせた
  timestamp 形式の `.pyc`）は、指紋では検出しない。これを**既知の限界**として受け入れる

### 2.2 理由と前提

- 本番で読まれる `__pycache__` は、`/opt/coldaisle`（venv と `src` を含む）と interpreter（システムの `/usr/bin/python3.12` と
  その標準ライブラリ）の下にだけある。どちらも **root の所有**で、fand・worker・承認者・サービス用ユーザーは書けない
  （`docs/ubuntu-deploy.md` 2 の「コードと `config/` は root の所有のまま置く」・venv も root が作る、6.7 の表の
  「実行するコード … `r-x` だけ。書けない」）
- したがって `__pycache__` を書き換えられるのは root だけである。root はソースも `RECORD` も interpreter も書き換えられるので、
  `__pycache__` を指紋に入れても root に対する防御は増えない。指紋が防ぐのは「意図しない取り違え」（別の venv・`PYTHONPATH` の
  残り・項目の順・`uv sync` の後の再認証の忘れ）で、root の悪意ではない（0134 §2.2 の共有ライブラリ・`LD_PRELOAD` の限界と同じ扱い）
- 前提を保つ配置の手順は `docs/ubuntu-deploy.md` 2（root で clone・root で `uv sync --no-dev`）と 6.7（実行するコードは root の所有）のとおり。
  この記録で手順は変えない

### 2.3 前提が崩れたら見直す

次のどれかが起きたら、この受け入れを見直す（新しい決定記録で `__pycache__` の扱いを決め直す）。

- `/opt/coldaisle`・venv・interpreter の標準ライブラリのどこかが、root 以外の uid から書けるようになる（所有・mode・ACL の変更、
  サービス用ユーザーへの書き込みの付与、`/var/lib/coldaisle` のような書ける場所を `sys.path` に入れる構成）
- 本番の起動の形が 0157 §2.1 の `-I -m` から外れ、`PYTHONPYCACHEPREFIX` などの環境変数や `-X pycache_prefix` で、
  root の所有でない場所のキャッシュが読まれうるようになる
- 導入先が root の所有でない Python（uv の管理する Python をサービス用ユーザーの書ける場所に置くなど）を使うようになる

## 3. Consequences

### 良くなること

- 0157 のまま実装でき、指紋がキャッシュの有無・`-O` の有無・遅延の書き込みに依らない。認証ランナー・fand・worker の指紋が、
  キャッシュの状態の違いで食い違わない
- 標準ライブラリの既定の構成（apt が入れた `__pycache__` を含む）で B1 に進める

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| root が `__pycache__` だけを差し替えた改変を検出しない | root はほかの部品も書き換えられるので、指紋の目的の外（§2.2）。前提が崩れたら見直す（§2.3） |
| 前提（root の所有）が崩れても指紋は気づかない | 導入と更新の手順（`docs/ubuntu-deploy.md` 2 / 6.7）で所有を保つ。§2.3 を見直しの条件として残す |

B0 に倒れる条件・`requested_demand`・Critical Safety・Reactive Guard・Fallback の経路は変えない。

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| **`-B` でキャッシュを書かせず、木に `__pycache__` があれば拒む** | apt が入れた標準ライブラリの `__pycache__` を導入先の既定の構成から消す手順が要り、apt の更新のたびに戻る。拒むと既定の構成で指紋を作れず B1 に進めない。消さずにおくと全プロセスが毎回ソースからコンパイルし、起動が遅くなる。root の悪意への防御は増えない（§2.2） |
| **checked-hash の `.pyc` に限る**（`compileall --invalidation-mode checked-hash` で作り直し、unchecked-hash と timestamp 形式の `.pyc` を拒む） | 導入先の全キャッシュを作り直す手順が要り、apt と `uv sync` の更新のたびにやり直す。`.pyc` の形式を読み分ける符号化を足すことになり、0157 の「`source_tree` と同じ符号化」から外れる。root はソースとキャッシュの両方を書き換えられるので、防御は増えない（§2.2） |
| `__pycache__` の中身も digest に入れる | キャッシュは遅延で書かれ、`-O` の有無・import した module の集合で変わるので、ランナーと fand・worker の指紋が合わなくなる（0157 §4 の「import された module の出どころを束ねる」案と同じ問題） |

## 5. 未決事項

なし。§2.3 の条件のどれかが起きたら、新しい決定記録で決め直す。
