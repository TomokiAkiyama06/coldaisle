# 決定記録 0157: 認証ランナー・`coldaisle-fand`・`coldaisle-learnd` の起動の形をそろえ、`sys.path` の全項目を実行環境の指紋に入れる

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし（[0066](0066-workload-hint-stage-b-conditions.md) §2.1 (c) の「実行環境の指紋」と
  [0152](0152-hint-monotonic-gate-certificate.md) §2.3 の部品 `environment` の定義を**補う**。置き換えではない。
  [0115](0115-learned-worker-units.md) の worker の unit と、`coldaisle-fand` の unit の `ExecStart=` の形を変える）
- **関連**: [0066](0066-workload-hint-stage-b-conditions.md) §2.1 (c) /
  [0080](0080-fand-systemd-unit.md) /
  [0104](0104-authority-raise-least-privilege.md) §2.3 /
  [0115](0115-learned-worker-units.md) §2.3 / §2.4 /
  [0134](0134-authority-raise-host-verification.md) §2.2 /
  [0141](0141-c1-import-scan-scope.md) /
  [0152](0152-hint-monotonic-gate-certificate.md) §2.1 / §2.3 / §2.4 / §2.6 /
  [0156](0156-hint-gate-spec-schema.md) /
  `deploy/systemd/coldaisle-fand.service` / `coldaisle-learnd-mpc.service` / `coldaisle-learnd-supervisor.service` /
  `docs/ubuntu-deploy.md` 2 / 6.5 / 6.8 /
  `src/coldaisle/hint_certificate_digest.py`（PR #349）/
  AGENTS.md「絶対に守るルール」2・4・7・9・10
- **対象 Issue**: #288（PR #349 の Codex P1 の残り）

---

## 1. Context

### 1.1 PR #349 に残った指摘

0152 §2.3 の部品 `environment` は「処理系の実装と版・`sysconfig` のプラットフォーム・`platform.machine()`・入った配布物の
名前・版・`RECORD` のハッシュから作る指紋」で、PR #349 はそれに `sys.path` の順位（配布物を持つ項目だけを前から数えた
順位）と、同じ項目でのファイルの重なりの拒否を足した。残った Codex の P1 は次のとおり。

- 配布物を持たない `sys.path` の項目（ソースのチェックアウト・標準ライブラリのディレクトリなど）は順位を取らない。
  その項目を site-packages の前後に動かすと、import されるコードが変わっても指紋が変わらない

項目の位置をすべて指紋に入れれば検出できる。ただし、いまは**起動の形がプロセスごとに違う**ので、そのまま入れると
認証ランナーと fand・worker の指紋が決して合わなくなる。

### 1.2 いまの起動の形と `sys.path`（Python 3.12 で確かめた）

| プロセス | いまの起動の形 | `sys.path[0]` |
|---|---|---|
| `coldaisle-fand`（unit） | console script `/opt/coldaisle/.venv/bin/coldaisle-fand` | script のディレクトリ（`/opt/coldaisle/.venv/bin`） |
| `coldaisle-learnd`（2 つの unit） | console script `/opt/coldaisle/.venv/bin/coldaisle-learnd` | 同上 |
| `coldaisle-authority raise`（0134 §2.2） | `/opt/coldaisle/.venv/bin/python -I -m coldaisle.authority_cli` | **入らない** |
| 開発（`uv run coldaisle-fand`） | console script | `.venv/bin` |

- console script（`.venv/bin/coldaisle-fand`）の中身は shebang の venv の interpreter で `from coldaisle.control_daemon import main`
  を呼ぶだけで、`-I` が付かない。`PYTHONPATH` などの環境変数が効き、`sys.path[0]` に script のディレクトリが入る
- Python 3.12 の `-I` は `-E`（`PYTHON*` の環境変数を無視）・`-P`（`sys.path[0]` に script / 作業ディレクトリを入れない。
  3.11 で入った safe path）・`-s`（user site を入れない）を含む。`-I -m <module>` の `sys.path` は、標準ライブラリの3項目
  （`python312.zip`・`lib/python3.12`・`lib-dynload`）・venv の site-packages・editable の導入の `.pth` が足す `src` だけに
  なり、作業ディレクトリ・起動した uid・環境変数に依らない（導入先の venv は `uv sync` の editable の導入。`docs/ubuntu-deploy.md` 2）
- `-I` は site の処理（`.pth` の実行）を止めない（0134 §2.2）。`multiprocessing` の spawn の子は親の interpreter の
  フラグ（`-I`）を引き継ぐ（`subprocess._args_from_interpreter_flags()`）ので、0152 §2.6 の Shadow solver の子も同じ形になる

---

## 2. Decision

### 2.1 3 つの入口を「root の所有の venv の interpreter を `-I -m` で起動する」形にそろえる

0134 §2.2 の `authority raise` と同じ形にする。本番の起動の形は次の3つだけとする（path は仮の値）。

```text
/opt/coldaisle/.venv/bin/python -I -m coldaisle.hint_certify   ...   # 認証ランナー（0152 §2.1。module 名は段 2 で決める）
/opt/coldaisle/.venv/bin/python -I -m coldaisle.control_daemon ...   # coldaisle-fand
/opt/coldaisle/.venv/bin/python -I -m coldaisle.learned_worker.cli --role mpc|supervisor ...   # coldaisle-learnd
```

- 入口の module は `if __name__ == "__main__": raise SystemExit(main())` を持つ（`control_daemon.py` は既にある。
  `learned_worker/cli.py` と認証ランナーは実装の PR で足す）。console script と同じ `main()`・引数・終了コード
- argparse の `prog` は module のファイル名にならないよう明示する（`coldaisle-fand` / `coldaisle-learnd` / `coldaisle-hint-certify`）
- console script（`.venv/bin/coldaisle-fand` など）は消さない（開発と手の試験で使う）。**本番の unit と手順には書かない**
- 認証ランナーは、自分が `sys.flags.isolated` で、`__main__` が `-m` で起動された（`__main__.__spec__` がある）ことを確かめ、
  そうでなければ何もせずに終了する（0152 §2.1 の `SCHED_IDLE` の確かめと同じ扱い）。人が console script で
  走らせても、本番と合わない証明書を作らない

### 2.2 部品 `environment` に「起動の形」と「`sys.path` の全項目」を足す

0152 §2.3 の `environment` の指紋の入力に、次を**加える**（いまの欄は残す）。

| 足す欄 | 中身 | 目的 |
|---|---|---|
| `interpreter_flags` | `sys.flags` の全欄と `sys._xoptions`（鍵の昇順） | `-I` / `-O` / `-X` の違いを見分ける |
| `executable` | `sys.executable` の path と、その実体（`realpath`）の bytes の SHA-256 | 別の venv・別の interpreter の取り違えと、interpreter の差し替えを見分ける |
| `sys_path` | `sys.path` の**全項目を順に**、各項目の「位置・絶対 path・種類（ディレクトリ / ファイル / 無い）・内容の digest」 | 項目の順と内容の変化を見分ける（§1.1） |

- **内容の digest**: ディレクトリは `source_tree` と同じ符号化（相対パスの昇順・長さ付き・`__pycache__` を除く。
  symlink・読めないディレクトリは例外）、ファイル（`python312.zip` など）は bytes の SHA-256、無い項目は「無い」の印。
  site-packages も含めて**全項目の中身を読む**（§5 の 2）。これで `RECORD` に載らないファイル（`_virtualenv.pth` など）と、
  `RECORD` を変えずに書き換えたファイルも指紋に入る。配布物の名前・版・`RECORD` の欄は 0152 のまま残す（照合の理由で
  何が変わったかを名指すため）
- 項目の path は指紋の**入力**にだけ使い、証明書・trace・ログには digest しか出さない（ルール 10）
- 計算は起動時に1回だけ（0152 §3 のとおり）。fand は tick を始める前、`READY=1` の前に行う。導入先の目安は
  標準ライブラリと site-packages を合わせて百数十 MB の読み込み（開発機で温まった状態で 1 s 未満）。watchdog の
  起動の猶予に収まることを導入先で確かめる（§5 の 4）

### 2.3 起動の形が違うプロセスは、指紋の不一致で B0 に倒れる

- fand・worker は、起動時に自分の起動の形を閉じた語彙 `launch_form` で判定する: `isolated_module`（`sys.flags.isolated`
  が真・`sys.flags.safe_path` が真・`__main__.__spec__` がある）か `other`
- `other` のとき、指紋は計算しない。fand は 0152 §2.4 の手順 5 の部品 `environment` を不一致として扱い、
  `certification: environment_mismatch` で B0 に留まる（起動は止めない。0152 §2.4）。worker は Shadow solver の子が
  `certification_mismatch` を返す（0152 §2.6）。**`launch_form` で新しい失敗の理由は作らない**（既存の語彙に倒す）
- 起動時のログ（0152 §2.4 の1行）に `launch_form` の欄を足す。運用者が「起動の形が違うから B0」を読めるようにする。
  path・ホスト名は載せない
- `isolated_module` でも、指紋が証明書と合わなければ同じく `environment_mismatch`（項目の順・中身・interpreter・フラグの
  どれかが違う）
- B0 に倒れても `requested_demand` は変わらない（0152 §2.5）。Critical Safety・Reactive Guard・Fallback の経路は変えない

### 2.4 開発（`uv run`）での扱い

- `uv run coldaisle-fand` / `uv run coldaisle-learnd` は今のまま動く。`launch_form` は `other` なので、B1 / B2 を設定しても
  B0（安全側）。B0 の試験・Stage A の運転はそのまま
- 開発で B1 の照合まで通したいときは、ランナー・fand・worker をすべて `uv run python -I -m <module>` で起動する。
  `uv run` が途中で `.venv` を同期し直すと中身が変わって合わなくなる（それも B0 に倒れるだけ）
- CI の試験（ルール 7）は指紋の関数を `tmp_path` の木と偽の `sys.path` で直接呼ぶ。`-I -m` の起動そのものは、
  子プロセスを `sys.executable -I -m` で起動する試験で1つだけ確かめる（`launch_form` の判定）

### 2.5 unit の `ExecStart=` と `docs/ubuntu-deploy.md`

実装の PR で次を変える（値は仮の値。引数は今の unit のまま）。

```ini
# deploy/systemd/coldaisle-fand.service
ExecStart=/opt/coldaisle/.venv/bin/python -I -m coldaisle.control_daemon --backend hwmon --require-watchdog ...（今の引数）
# deploy/systemd/coldaisle-learnd-mpc.service
ExecStart=/opt/coldaisle/.venv/bin/python -I -m coldaisle.learned_worker.cli --role mpc ...（今の引数）
# deploy/systemd/coldaisle-learnd-supervisor.service
ExecStart=/opt/coldaisle/.venv/bin/python -I -m coldaisle.learned_worker.cli --role supervisor ...（今の引数）
```

- `WorkingDirectory=/opt/coldaisle` は残す（`-I` では `sys.path` に入らないが、0115 §2.4 の作業ディレクトリ基準の既定の
  ファイル（`--metrics` など）が使う）
- `ExecStopPost=`（fand の引き継ぎ実行部。システムの `/usr/bin/python3 -I -S`。0080 §2.5）は変えない
- `SyslogIdentifier=` は今のまま（プロセス名は `python` になるが、journal の識別は変わらない）
- `tests/test_deploy_templates.py` に、3つの unit の `ExecStart=` が `-I -m` の形であることの静的な試験を足す
- `docs/ubuntu-deploy.md` の 6.5（`systemd-analyze verify` で確かめる実行ファイルを `/opt/coldaisle/.venv/bin/python` に）と
  6.8（worker の unit）を直し、本番の手順から console script を消す。認証ランナーの手順（段 2）も同じ形で書く

### 2.6 導入先での切り替え

fand は disabled のまま 0121 の試験中なので、`enable` の前に切り替える。実装の PR がマージされた後、次の順で行う。

1. `/opt/coldaisle` を更新し、root で `uv sync --no-dev`（`docs/ubuntu-deploy.md` 2 と同じ形）
2. 3つの unit を `/etc/systemd/system/` に置き直し、`sudo systemctl daemon-reload`
3. `sudo systemd-analyze verify` で3つの unit を確かめる（`is not executable` が出ないこと）
4. `systemctl show -p ExecStart` で `-I -m` の形になったことを確かめる
5. worker を動かしているなら再起動し、`/proc/<pid>/cmdline` に `-I` と `-m` があることを確かめる
6. fand は 0121 の手順のまま（disabled）。手で起動する試験も unit と同じ `-I -m` の形で行う
7. 証明書（0152）は、この切り替えの**後に**本番のホストで作る（切り替えの前に作った証明書は `sys.path` の指紋が合わない）

---

## 3. Consequences

### 良くなること

- 認証ランナー・fand・worker の `sys.path` が、作業ディレクトリ・起動した uid・環境変数に依らず同じになり、全項目を
  指紋に入れられる。項目の順・中身（`RECORD` に載らないファイルを含む）・interpreter の差し替えのどれでも B0 に倒れる
- `PYTHONPATH` などが残った環境で fand・worker が別のコードを import する経路を、本番の unit から無くす（0134 §2.2 と同じ利点）
- 起動の形の違いがログで分かる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| unit の `ExecStart=` が長くなり、プロセス名が `python` になる | `SyslogIdentifier=` は変えない。unit 名で識別する |
| 標準ライブラリ・site-packages の中身を起動のたびに読む | 起動時に1回だけ。tick の外。導入先で時間を測る（§5 の 4） |
| apt による Python の更新・`uv sync` のたびに指紋が変わり、再認証が要る | 変わっても B0 に倒れるだけ（0152 §2.1 の「走らせ忘れは安全側」）。更新の手順に再認証を書く |
| 開発の `uv run coldaisle-fand` では B1 を試せない | `uv run python -I -m ...` でそろえれば試せる（§2.4） |
| 共有ライブラリ（`libpython` 以外の `.so` の依存・`LD_PRELOAD`）は指紋に入らない | 0134 §2.2 と同じ既知の限界。unit は環境変数を最小にし、`/opt/coldaisle` と interpreter は root の所有 |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| **import された module の出どころを束ねる**（起動の後、`sys.modules` の各 module の `__file__` の path と中身の digest を指紋にする） | 指紋が「いままでに import したもの」に依り、ランナー・fand・worker で import する module の集合が違うので合わない。遅延 import の後に変わる。起動の形をそろえても、比べる時点を決める規則が要る。代わりに `sys.path` の全項目の中身を束ねれば、import されうるものをすべて覆う |
| **既知の限界として受け入れる**（配布物を持たない項目の順は指紋に入れず、記録に限界として書く） | 0066 §2.1 (c) は「実行環境が違えば Stage B を無効にする」ことを求める。本番の unit を `-I -m` にすれば項目は固定でき、受け入れる理由が無い。`PYTHONPATH` の残りで別のコードを import した fand が認証済みと扱われる穴が残る |
| `sys.path` の項目の path だけを入れ、中身を読まない | 同じ path の中身（標準ライブラリの更新・site-packages の書き換え）が変わっても合ってしまう |
| site-packages は `RECORD` だけで束ね、中身を読まない（配布物の無い項目だけ中身を読む） | 読む量は減るが、`RECORD` に載らない `.pth`（site の処理で実行される）と、`RECORD` を変えない書き換えが残る（§5 の 2） |
| console script のまま、`sys.path[0]` だけを指紋から除く | `PYTHONPATH` と user site の項目が残る。どの項目を除くかの規則がプロセスの起動の仕方に依る |
| console script の shebang に `-I` を付ける | console script は `uv sync` が作り直す生成物で、手で直しても戻る。生成の設定に依る形を本番の前提にしない |
| `launch_form: other` に新しい失敗の理由を作る | 0152 §2.4 の閉じた語彙で足りる（`environment_mismatch`）。理由の細目はログの `launch_form` で分かる |

---

## 5. 未決事項（所有者の判断を求める点。推奨を先頭に）

| # | 判断点 | 選択肢（推奨を先頭） |
|---|---|---|
| 1 | 起動の形（§2.1） | **(a 推奨) 3つとも root の所有の venv の interpreter を `-I -m` で起動する（0134 §2.2 と同じ）** / (b) root の所有のラッパー（`exec python -I -m ...` の2行）を置き、unit はそれを呼ぶ / (c) console script のまま（全項目を指紋に入れられない） |
| 2 | `sys.path` の項目の中身をどこまで読むか（§2.2） | **(a 推奨) 全項目（site-packages を含む）の中身を読む** / (b) 配布物の無い項目だけ読み、site-packages は `RECORD` で束ねる（`RECORD` に載らない `.pth` が残る）/ (c) path だけ（中身の変化を見逃す） |
| 3 | 起動の形が違うときの扱い（§2.3） | **(a 推奨) 指紋を計算せず `environment_mismatch` で B0。ログに `launch_form` を出す** / (b) 指紋は計算し、不一致にだけ任せる（起動の形が違う理由がログで分からない）/ (c) 新しい理由 `launch_form_mismatch` を足す（0152 の閉じた語彙を広げる） |
| 4 | 起動の時間（§2.2） | **(a 推奨) 実装の後、導入先で fand の起動から `READY=1` までの時間を測り、watchdog の猶予に収まることを確かめる。収まらなければ別の記録で扱う** / (b) 中身の digest を unit の外（root の oneshot）で前もって計算して置く（置き場所と書き手の決定が要る） |
| 5 | 開発での B1 の試し方（§2.4） | **(a 推奨) `uv run python -I -m ...` でそろえたときだけ通る。`uv run coldaisle-fand` は B0** / (b) 開発では指紋の照合を緩める設定を置く（本番の経路に緩める設定を持ち込む） |
| 6 | 認証ランナーの起動の確かめ（§2.1） | **(a 推奨) `-I` と `-m` で起動されていなければ何もせずに終了する** / (b) 警告を出して続ける（本番と合わない証明書ができる） |

### 実装の切り方（参考）

1. PR #349 の部品 `environment` に §2.2 の欄を足し、`launch_form` の判定を置く（PR #349 の残りの P1 をこれで閉じる）
2. `learned_worker/cli.py` の `__main__`・`prog` の明示、3つの unit の `ExecStart=`、`tests/test_deploy_templates.py`、
   `docs/ubuntu-deploy.md`（1 と同じ PR か、その直後の PR）
3. 導入先での切り替え（§2.6）。証明書は切り替えの後に作る
