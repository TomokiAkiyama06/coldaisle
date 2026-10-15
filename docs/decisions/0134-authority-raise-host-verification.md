# 決定記録 0134: 導入先で `coldaisle-authority raise` を使えるようにする前に足す点（隔離モードで起動するコード・入口の書き分け・権限の確かめ方・段階 C の時期）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-14、リポジトリ所有者の承認）
- **Date**: 2026-10-14
- **Supersedes**: なし（[0104](0104-authority-raise-least-privilege.md) / [0105](0105-registry-shared-root-settled-points.md) の
  権限の形・グループ・mode・setgid は**変えない**。0104 §2.9 の確認の手順に足すだけ）
- **Superseded by**: §2.4 の「1 の確かめ方」のうち走査の範囲（`sys.base_prefix` の下をすべて辿る）と「`roots.txt` に `/opt/coldaisle` と interpreter の置き場所以外が出たら C1 は失敗」は [`0141`](0141-c1-import-scan-scope.md)（Proposed。FINAL になるまでは本記録のまま）
- **関連**: [0104](0104-authority-raise-least-privilege.md) §2.1〜§2.10 /
  [0105](0105-registry-shared-root-settled-points.md) /
  [0086](0086-authority-approver-uid.md) §2.1〜§2.4・§3・§5 の 1 /
  [0072](0072-control-admin-entry.md) §2.1 / §2.5 / §4 の D /
  [0089](0089-authority-bound-to-loaded-artifact.md) / [0090](0090-authority-bound-to-loaded-config.md) /
  [0115](0115-learned-worker-units.md) §2.3 / §2.8 /
  [0121](0121-hardware-test-procedure.md) /
  `docs/ubuntu-deploy.md` 6.1 / 6.2 / 6.4 / 6.7 / 6.8、`docs/authority-rollout.md` /
  `src/coldaisle/authority_cli.py`（`run_raise()`・`--registry-limits`） /
  AGENTS.md「絶対に守るルール」1・2・9・10
- **対象 Issue**: #217

---

## 1. Context

### 1.1 #217 の設計の問いは、すでに 0104 / 0105 で決まっている

#217（操作者が本番で `raise` を使うための制御設定の読み取りと Registry の lock の最小権限）の未決 1〜6 は、
0104（2026-10-07 FINAL）と 0105（同日 FINAL）で次のとおり決まり、段階 A（コード。PR #248）と段階 B
（`docs/ubuntu-deploy.md` 6.7・`docs/authority-rollout.md`・`docs/model-registry.md`）も済んでいる。

| Issue の未決 | 決まったこと |
|---|---|
| 1 制御設定の読み取り | 所有者・mode（`root:coldaisle-fan`・`0750` / `0640`）を変えず、POSIX ACL で `coldaisle-authority` に読み取りだけ。`/etc/coldaisle` は `--x` だけ（0104 §2.2） |
| 2 Registry の置き場所 | `root:coldaisle-registry`・`2770`（setgid）の専用ディレクトリ。書き手は自分の uid。承認者と fand は ACL で読む（0104 §2.3） |
| 3 lock | 承認者は lock の `r` だけ。共有の root モードは lock を作らず `O_RDONLY` で `flock` だけ（0104 §2.4 / 0105 §2.1） |
| 4 `--config-dir` の取り違え | CLI では確かめず 0090 の fand 側の照合に任せる。`raise` の後に効いていることを確かめる手順を置く（0104 §2.6） |
| 5 記録 | 0104（0086 §5 の未決 3 を閉じる） |
| 6 実機の確認 | 0104 §2.9 / `docs/ubuntu-deploy.md` 6.7 の手順 9（段階 C。人が行う） |

残っているのは**段階 C（導入先での確認）だけ**で、`docs/ubuntu-deploy.md` 6.1 / 6.7 の「本番での `raise` は
手順 9 が通り #217 に結果を残すまで使えない」は、段階 C まで外さない。

本記録は 0104 を**やり直さない**。権限の形を変える理由が見つかっていないためである（§4 の 1）。

### 1.2 段階 C の前に読み直して見つかった点

0104 の後に入った変更と、導入先での作業（2026-10-10〜14。fand は disabled のまま 0121 の試験中、制御の4ファイルは
6.2 のとおり root の所有で置いてある、操作者は `coldaisle-admin` で管理ソケットを使っている、人は1人）を踏まえて
0104 §2.9 と 6.7 を読み直すと、次の4点が記録に無い。

1. **承認者が実行するコードと、`--registry-limits` が読むファイル。** 0104 §1 の表は `raise` が読むものとして
   制御設定・Registry・authority のディレクトリだけを挙げる。実際には `run_raise()` は
   `--registry-limits`（既定は作業ディレクトリの相対 path `config`）の `model-registry.yaml` も読む。
   また `docs/authority-rollout.md` の例は開発の形の `uv run coldaisle-authority raise` で、本番でどのコードを
   どの uid で実行するかが書かれていない。**CLI の検証（承認の形・uid の束縛・証拠の hash）は承認者自身の
   process で走る**ので、承認者が書けるコードで実行すると、検証そのものが承認者の手に移る。
   **console script（`.venv/bin/coldaisle-authority`）を絶対 path で実行しても足りない。** console script は
   `PYTHONPATH` などの環境変数を読むので、承認者の環境の `PYTHONPATH` が書き換えられる場所を指していると、
   そこにある `coldaisle` が root の所有のコードより先に読まれる（開発の環境で、`PYTHONPATH` の先に置いた偽の
   `coldaisle` が console script から読まれ、`python -I -m coldaisle.authority_cli` では読まれないことを確かめた）
2. **「昇格の入口が1つ」が何によって保たれているかの書き分け。** AGENTS.md は `coldaisle-authority raise` を
   「人が自分の uid で実行する唯一の昇格の入口」とする。これは (a) コードの構造（`raise_stage()` を呼ぶのは
   `authority_cli.py` だけ。管理ソケット・fand・API・AI・eventd・Learned worker に上げる経路が無い。
   `tests/test_authority_cli.py` が走査）と、(b) 権限（authority のディレクトリに書けるのが誰か）で保たれる。
   一方、(c) `coldaisle-authority` に入った人は `authority.json` を直接書ける（0086 §3。改ざん検知は 0086 §5 の 1 で
   未決）。0104 §2.9 の確認は (b) のうち「承認者が書けない」「Registry の書き手が authority に書けない」を見るが、
   **管理ソケットの人（`coldaisle-admin` だけの人）が authority のディレクトリにも Registry にも書けないこと**は
   確かめていない。導入先では操作者が `coldaisle-admin` を日常的に使っており、この境界が最も踏まれやすい
3. **権限の判定を `test -r` / `test -w` で行っていること。** 6.4 の手順 4・6.7 の手順 9 の 4・6.8 の確認は
   `test` の結果で「読める・書けない」を判定する。導入先の作業で、6.4 の手順 4 の外部コマンドの `test -w`
   （`systemd-run` が直接実行するもの）が、補助グループで書けるはずのファイルを「書けない」と報告した
   （shell の組み込みの `test` では書けると出た）。`test` の答えは実装（外部コマンドか組み込みか、補助グループと
   ACL をどう扱うか）で変わりうる。**「書けない」の確認が誤って通ると、権限が広すぎることを見逃す**（危険な向き）。
   0104 §2.9 の 2 の承認者の確認は実際の `cat` / `: >>` / `touch` で行っており、この問題は無い
4. **段階 C をいつ行うか。** 6.7 の手順 9 の 5（`expected_revision` の不一致で拒まれるところまで届く）は
   Registry に thermal_model の Production artifact があることが前提である。導入先にはまだ学習済みの model が無く、
   Production を置けない。手順 9 の 1〜4（権限の形）はいつでも確かめられるが、5 は Production ができるまで
   `approval_rejected` まで届かない

## 2. Decision（2026-10-14、所有者が §5 の判断点 1〜6 をすべて推奨案で承認。7 は所有者の判断）

名前（ユーザー・グループ・path）はすべて 0104 と同じ**仮の値**である。実機のユーザー名・ホスト名・path は
リポジトリに書かない（AGENTS.md ルール 10）。

### 2.1 権限の形は 0104 / 0105 のまま

グループ・所有者・mode・setgid・ACL は 0104 §2.2〜§2.4 のとおりとし、本記録では変えない。承認者の uid が
`raise` のために要るものを、§2.2 の2行を足して一覧にする（**太字**が本記録で足す行）。

| もの | 承認者の uid に要る権限 | 与え方 | 根拠 |
|---|---|---|---|
| `/etc/coldaisle` | `x`（たどるだけ。一覧・`coldaisle.env`・`control-admin.yaml` は読めない） | ACL `g:coldaisle-authority:--x` | 0104 §2.2 |
| 制御設定の4ファイルとそのディレクトリ | `r`（ディレクトリは `r-x`）。書けない | ACL（default ACL も） | 0104 §2.2。証拠の設定 hash を CLI が読んだ bytes から作る |
| Registry の root・`registry.json` | `r-x` / `r`。書けない | ACL（default ACL `r-X`） | 0104 §2.3。Production の metadata を lock の下で読む |
| `.registry.lock` | `r` と `flock` だけ | ACL `g:coldaisle-authority:r--` | 0104 §2.4 / §5 の 5 |
| authority のディレクトリ | `rw`（journal と lock） | グループ `coldaisle-authority`・`2770`（setgid） | 0086 §2.2 / §2.4 |
| **実行するコード（`/opt/coldaisle` の `.venv` と、それが使う Python）** | **`r-x` だけ。書けない** | **root の所有（`docs/ubuntu-deploy.md` 2 のまま）** | **§2.2** |
| **`/opt/coldaisle/config/model-registry.yaml`** | **`r` だけ。書けない** | **root の所有（同上）** | **§2.2** |
| 承認ファイル・評価報告 | `r` | 承認者自身のファイル | 0086 §2.1 |

要らないもの（与えない）: DB（0086 §2.8）、管理ソケット（`coldaisle-admin`。§2.3）、`coldaisle-fan` グループ
（hwmon を書ける。0086 §4）、Registry と制御設定への書き込み、root。

**artifact の本体の読み取りは、`raise` には要らないが、0104 §2.3 の default ACL で承認者にも付く。** 本記録は
これを狭めない（§5 の 3）。承認する人が、上げる対象の artifact を自分で見られるのは承認の役割の範囲で、artifact は
秘匿情報ではなく、狭めると Registry が作るファイルごとに ACL を付け分ける必要が出るためである。

### 2.2 本番の `raise` は root の所有の interpreter を隔離モード（`-I`）で起動し、`--registry-limits` も絶対 path で渡す

- 本番では、root の所有の venv の interpreter を `-I` で起動して入口のモジュールを実行する。承認者の uid のまま
  実行する（path は仮の値）

  ```bash
  /opt/coldaisle/.venv/bin/python -I -m coldaisle.authority_cli raise \
      --authority-root /var/lib/coldaisle-authority \
      --approval <承認.json> --report <報告.json> \
      --config-dir /etc/coldaisle/control-config \
      --registry-root /var/lib/coldaisle-registry \
      --registry-limits /opt/coldaisle/config
  ```

  - `-I` は `PYTHON*` の環境変数（`PYTHONPATH` など）を無視し、作業ディレクトリと user site を `sys.path` に
    入れない。venv の site-packages（root の所有）は `-I` でも読まれる。`src/coldaisle/authority_cli.py` には
    `if __name__ == "__main__": raise SystemExit(main())` があるので、`-m` でそのまま入口が動く（console script と
    同じ `main()`。引数も終了コードも同じ）
  - **console script `.venv/bin/coldaisle-authority` を本番の手順に書かない**（§1.2 の 1。`-I` が付かない）
  - 打ち間違いを減らすためにラッパーを置くなら、root の所有・承認者が書けない場所に置き、中身は
    `#!/bin/sh` と `exec /opt/coldaisle/.venv/bin/python -I -m coldaisle.authority_cli "$@"` の2行だけにする
    （ラッパーを置くかは §5 の 2。置かなければ上の1行を手順に書く）
  - `-I` は site の処理（`-S`）を止めない。site-packages の `.pth` は `-I` でも実行されるので、承認者が
    site-packages（や `sys.path` のどこか）に書ければ `-I` は役に立たない。書けないことを C1 で確かめる（§2.4 の 1）
  - `-I` が防ぐのは Python の起動時の環境変数と作業ディレクトリ・user site からの探索だけで、`LD_PRELOAD` など動的リンカの環境変数は防がない。
    承認者本人の意図した書き換えは §2.3 の (c) の限界のままで、ここで守るのは、手順に従った承認者の環境に
    たまたま残った `PYTHONPATH` や、作業ディレクトリの checkout が検証を黙って置き換えないことである
- **`uv run` を使わない**（`.venv` と lock を書こうとする。`/opt/coldaisle` は root の所有なので失敗するが、
  失敗の理由が権限の設計と紛らわしい）。**承認者のホームにある checkout など、承認者が書けるコードで本番の
  authority のディレクトリを指さない**
- `--registry-limits /opt/coldaisle/config` を明示する（既定の相対 path `config` は作業ディレクトリで意味が変わる）。
  上限は読み込みの大きさを抑えるだけで、上げる向きの判断には使われない。承認者が別の上限を渡しても権限は
  広がらないので、コードで path を固定はしない（ルール 9。§4 の 5）
- 承認者が `/opt/coldaisle` の下（コード・`config/`・`.venv`）に書けないことを段階 C で確かめる（§2.4 の表の 1）

**これは承認者本人に対する強制ではない**（承認者は自分の uid で任意のコードを走らせられる。§2.3 の (c)）。
守るのは「正しく手順に従った承認者の検証が、承認者の書ける場所で黙って変わっていない」ことである。

### 2.3 「昇格の入口が1つ」を3つに書き分け、(b) を段階 C で確かめる

| 層 | 何が保つか | 誰に効くか | 確かめ方 |
|---|---|---|---|
| (a) コード | `raise_stage()` を呼ぶのは `authority_cli.py` だけ。管理ソケット・fand・API・AI・eventd・Learned worker は上げられない | すべての process | 既存の走査（`tests/test_authority_cli.py` ほか）。変えない |
| (b) 権限 | authority のディレクトリに書けるのは、`coldaisle-authority` の人・fand（unit の補助グループだけ）・root だけ | `coldaisle-admin` だけの人、`coldaisle-registry` だけの人、`coldaisle`（API / 取り込み）、AI 層、Learned worker | 段階 C（§2.4）。本記録で `coldaisle-admin` だけの資格の確認を足す |
| (c) 承認者本人 | 強制しない。0089 / 0090 の fand 側の照合（証拠が fand の読んだ設定・artifact と同じこと）と、構造化ログの監査の記録（0086 §2.8）が残る | `coldaisle-authority` の人 | 改ざん検知は 0086 §5 の 1 のまま（本記録では決めない） |

- **`coldaisle-admin` と `coldaisle-authority` を分けたままにする。** 管理ソケットは運転モードと降格だけを受ける
  入口で（0072 §2.1）、`coldaisle-admin` に入っても `raise` に要る読み取り（§2.1 の表）は1つも付かない。
  1人の導入先では同じ人が両方に入ってよい（0104 §5 の 3 と同じ扱い）
- `raise` の後に効いていることを確かめる `coldaisle-control status`（0104 §2.6）は `coldaisle-admin` の人が行う。
  **確かめるためだけに、承認者の CLI に管理ソケットや DB を読ませない**（0104 §4 で却下したまま）

### 2.4 段階 C では権限を `test` で判定せず、実際に開いて確かめる

0104 §2.9 / 6.7 の手順 9 の確認に、次の決まりと項目を足す。

- **判定は実際の操作で行う。** 読めることは `cat -- <file> > /dev/null`、ディレクトリの一覧は `ls -- <dir>`、
  ファイルに書けないことは `sh -c ': >> "$1"' _ <file>`（開けても中身は変わらない）の**失敗**、ディレクトリに
  書けないことは `touch -- <dir>/.probe-<任意の名前>` の**失敗**で見る（成功したら作ったファイルを消し、
  「期待と違う」として扱う）。`test -r` / `test -w` / `test -x` の結果は判定に使わない
- unit と同じ資格で見るときは、0104 §2.9 の 4 と同じ `systemd-run`（`User=`・`Group=`・`SupplementaryGroups=` を
  unit と同じに）の中で、上の操作を `sh -c` で行う
- 役割ごとに、その役割のグループだけを持つ一時アカウントで行う（6.7 の手順 9 の前置きのとおり。`sudo -u` は
  権限の形を確かめるためだけに使い、本物の `raise` には使わない）

足す項目（0104 §2.9 の 1〜5 はそのまま）:

| # | 資格 | 期待 |
|---|---|---|
| 1 | 承認者（`coldaisle-authority` だけ） | **隔離した interpreter が import に使う場所のすべてに書けない**（下の「1 の確かめ方」。`sys.path` の各ディレクトリ、特に site-packages と、`sys.prefix`（venv）・`sys.base_prefix`（interpreter）の下のすべてと、それらの祖先のディレクトリ）。`/opt/coldaisle/config` に書けず、`config/model-registry.yaml` を読める。ラッパーを置くならラッパーとその親にも書けない |
| 1b | 承認者（同上） | **`PYTHONPATH` を承認者の書ける場所（中に偽の `coldaisle/__init__.py` を置く。読まれたら目印を出して止まるだけの1行）に向けても、§2.2 の起動の形では root の所有のコードが読まれる**。`/opt/coldaisle/.venv/bin/python -I -c 'import coldaisle; print(coldaisle.__file__)'` が `/opt/coldaisle` の下を指し、`-I -m coldaisle.authority_cli --help` が使い方を出す（目印が出ない）こと。確かめた後に偽の package を消す |
| 2 | 管理ソケットの人（`coldaisle-admin` だけ） | authority のディレクトリに書けない・`authority.json` に書けない。制御設定の4ファイル・`registry.json`・`.registry.lock` を読めない（`flock -n` も失敗）。`/etc/coldaisle` を一覧できない |
| 3 | `coldaisle`（API / 取り込みの unit と同じ資格） | 2 と同じ（authority・制御設定・Registry のどれにも触れない） |
| 4 | 制御設定の4ファイルを置き直した後（導入先の手順で `install` し直したとき） | 0104 §2.9 の 1 の `getfacl` に加えて、承認者の資格で4ファイルを `cat` できること（default ACL が効いたことを、表示ではなく読み取りで確かめる） |

**1 の確かめ方。** `-I` は `-E`・`-s`・`-P` を含むが `-S` を含まない。site の処理は走るので、site-packages の
`.pth`（`import` で始まる行は実行される。editable の導入の `.pth` は `src` を `sys.path` に足す）は `-I` でも効く
（開発の環境で、venv の site-packages に置いた `.pth` の `import` の行が `-I` のもとで実行され、`sys.flags` が
`isolated=1`・`no_site=0` であることを確かめた）。したがって `.venv/bin`・`config`・`src` だけを見るのでは足りず、
**隔離した interpreter 自身が報告する場所**を、承認者の uid で実際に開いて確かめる（path は仮の値）。

```bash
py=/opt/coldaisle/.venv/bin/python
# 隔離した interpreter が使う場所: sys.path の各項と、venv と interpreter の prefix
"$py" -I -c 'import sys; print("\n".join(p for p in [*sys.path, sys.prefix, sys.base_prefix] if p))' \
  | sort -u > roots.txt
: > unresolved.txt
# symlink の実体を、増えなくなるまで roots に足す。項そのものと、項の下で出会うすべての symlink
# （ディレクトリを指すものもファイルを指すものも）を1つずつ readlink -f で解決する。
# 解決できない symlink（切れたもの・途中のディレクトリが無いものを含む）は飛ばさず unresolved.txt に残す。
# find は symlink をたどらない（-P）ので循環しても止まり、roots は増えるだけの有限の集合なので繰り返しも止まる
while :; do
  while IFS= read -r p; do
    # path の途中の要素（/opt/coldaisle 自体など）が symlink なら、その1段先を項として足す。
    # 足した項の祖先も下の検査に掛かり、項はまたここで要素ごとに解決される（-s -m は文字の上だけで整える）
    q=$p
    while [ "$q" != / ]; do
      if [ -L "$q" ]; then
        t=$(readlink -- "$q")
        case $t in /*) ;; *) t=$(dirname -- "$q")/$t ;; esac
        realpath -s -m -- "$t" >&3
        printf '%s\n' "$q"                    # 解決できるかは下で確かめる
      fi
      q=$(dirname -- "$q")
    done
    [ -e "$p" ] || [ -L "$p" ] || continue   # symlink でもなく存在もしない項は、下の祖先の検査で見る
    find "$p" -type l -print                  # 項そのものが symlink なら、項も出る
  done < roots.txt 3>hops.txt | while IFS= read -r l; do
    if t=$(readlink -f -- "$l") && [ -e "$t" ]; then
      printf '%s\n' "$t"
    else
      printf '%s\n' "$l" >> unresolved.txt
    fi
  done | cat - hops.txt roots.txt | sort -u > roots.next
  if cmp -s roots.txt roots.next; then rm -f roots.next; break; fi
  mv roots.next roots.txt
done
sort -u unresolved.txt | while IFS= read -r l; do echo "解決できない symlink: $l"; done
# 調べるディレクトリ: 各項（実体を含む）の下のすべてと、各項の祖先（/ の手前まで）。
# 祖先は項が存在しなくても見る（無い項を後から作れるかは、親に書けるかで決まる）
while IFS= read -r p; do
  [ -d "$p" ] && [ ! -L "$p" ] && find "$p" -type d -print
  d=$p; while [ "$d" != / ]; do d=$(dirname -- "$d"); [ "$d" = / ] || printf '%s\n' "$d"; done
done < roots.txt | sort -u > dirs.txt
# ディレクトリ: 作れたら「書ける」（作ったものは消す）。無いディレクトリは作れないので出ない（祖先で見る）
while IFS= read -r d; do
  if touch -- "$d/.probe-0134" 2>/dev/null; then rm -f -- "$d/.probe-0134"; echo "書ける: $d"; fi
done < dirs.txt
# ファイル（.pth・.py・pyvenv.cfg・interpreter の実体を含む）: 追記で開けたら「書ける」（中身は変わらない）。
# symlink のファイルは -type f に出ないが、その実体は上で roots に入っており、ここで実体として調べる
while IFS= read -r p; do
  [ -e "$p" ] || continue
  find "$p" -type f -exec sh -c 'for f; do if (: >> "$f") 2>/dev/null; then echo "書ける: $f"; fi; done' _ {} +
done < roots.txt
```

- 「書ける」か「解決できない symlink」が1行でも出たら、C1 は失敗で、本番の `raise` は使わない。
  `roots.txt` に `/opt/coldaisle` と interpreter の置き場所（`docs/ubuntu-deploy.md` 2）以外の場所が出たときも同じ
  （承認者の書ける場所が import に使われうる。symlink の実体がそこにあるときを含む）
- **解決できない symlink は、理由を問わず失敗にする（安全側）。** 切れた symlink や途中のディレクトリが無い
  symlink の先は、いまは検査できず、後で承認者がその場所を作れれば書き換え可能なコードが読まれうる。
  直すときは symlink を消すか、先を root の所有で作ってから C1 をやり直す
- symlink そのものは書き換えられない（symlink の mode は使われない）。置き換えられるかは、symlink のある
  ディレクトリに書けるかで決まり、それは上のディレクトリの検査で見ている。実体とその祖先は、実体を
  `roots.txt` に足したことで同じ検査に掛かる（venv の `bin/python` は interpreter の実体への symlink で、
  その実体と親もここで見る）
- path の途中の要素が symlink のとき（`/opt/coldaisle` 自体が別の場所への symlink など）は、その symlink の
  1段先（文字の上で整えたもの）を項に足す。足した項の祖先（＝途中の symlink の実体の祖先）と、最後まで解決した
  `readlink -f` の結果とその祖先が、同じ書けないことの検査に掛かる
- `sys.path` の項が symlink でもなく存在もしない（`python312.zip` など）ときは、その項の祖先のディレクトリに
  書けないことで、承認者が後から作れないことを見る
- `roots.txt`・`dirs.txt`・`unresolved.txt` は承認者の作業ディレクトリに置き、結果を #217 に残すときは path を仮の名前に置き換える

結果は 0104 §2.9 の 6 のとおり #217 に残し、実機のユーザー名・ホスト名・path は仮の名前に置き換える。

### 2.5 段階 C を2つに分け、#217 は C2 まで閉じない

| 段階 | 内容 | 時期 |
|---|---|---|
| C1 | 6.7 の手順 1〜8 の配置と、手順 9 の 1〜4 と本記録 §2.4 の項目（権限の形） | 6.7 を配置した後ならいつでも。fand を `start` しない。0121 の試験と並行してよい |
| C2 | 手順 9 の 5（承認者の uid の `raise` が `expected_revision` の不一致の `approval_rejected` まで届く） | Registry に thermal_model の Production artifact が初めてできた後。それまでは `evidence_rejected`（Production が無い）までの確認に留める |

- **#217 は C2 が通るまで閉じない。** 6.1 / 6.7 の「本番での `raise` は使えない」の注記は #217 を指しており、
  Production が無い間は `raise` そのものが要らない（上げる対象が無い）ので、待っても運用は困らない
- C1 だけが通った状態で「`raise` を使える」と書き換えない

### 2.6 本 PR は記録だけ。FINAL の後に手順書を直す

本 PR は本記録と索引の行だけで、コード・`docs/ubuntu-deploy.md`・`docs/authority-rollout.md`・`deploy/` には
触れない。FINAL の後の文書の PR（#217）で次を直す。

- `docs/authority-rollout.md`: 本番の例を `/opt/coldaisle/.venv/bin/python -I -m coldaisle.authority_cli`（§2.2）・
  `--registry-limits` の絶対 path にする（開発の例は `uv run` のまま）。`uv run` と console script を本番で使わない
  理由を1行ずつ
- `docs/ubuntu-deploy.md` 6.7: 手順 6 に §2.2、手順 9 に §2.4 の決まりと項目、§2.5 の C1 / C2 の分け方
- 6.7 の手順 9 の 4 の `test -r` / `test -w` を、§2.4 の実際の操作に置き換える

6.4 の手順 4（DB）と 6.8（Learned worker）の `test` の確認も同じ問題を持つが、#217 の範囲の外なので、別の
Issue（#300）で同じ決まりに揃える（§5 の 4）。

## 3. Consequences

### 良くなること

- 承認者の uid が `raise` のために要るものが、実行するコードと上限の設定まで含めて1つの表になる
- 「入口が1つ」が、コード・権限・承認者本人のどれで保たれているか（保たれていないか）が分かれる。
  日常的に使われる `coldaisle-admin` から authority へ届かないことを、導入先で確かめる項目になる
- 権限の確認が `test` の実装の違いで誤って通る（広すぎる権限を見逃す）ことが無くなる
- Production が無い間に C2 を形だけ通して `raise` を使えると書き換える誤りを避けられる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 段階 C の項目が増え、一時アカウントの資格も1つ増える（`coldaisle-admin` だけ） | 1回の作業で済む。導入先の手順に一覧で置く（§2.6） |
| 実際に書いて確かめるので、権限が広すぎるとファイルが1つできる | 中身の変わらない `: >>` と、消せる `.probe-*` に限る。できたら「期待と違う」として扱う |
| #217 が Production ができるまで開いたまま | Production が無い間は `raise` の対象が無い。注記が #217 を指したまま残るほうが、別 Issue へ付け替えるより追いやすい |
| 承認者本人の改ざんは本記録でも塞がない | 0086 §3 と同じ限界。0089 / 0090 の照合と監査の記録が残る。0086 §5 の 1 で扱う |

## 4. 却下した代替案

1. **0104 の権限の形をやり直す（別グループ・別の置き場所など）**: 0104 は FINAL で、段階 A・B まで済んでいる。
   導入先の作業でも、形を変える理由（読めない・書けてしまう）は見つかっていない。見直しは段階 C で形が合わなかった
   ときに、その事実を根拠に新しい記録で行う
2. **`raise` を root の特権の入口（管理ソケットへの昇格の追加・setuid の helper・polkit の action）にし、
   制御設定を root として読ませる**: 承認者に ACL を足さずに済むが、昇格を書く2つ目の入口と、昇格を書く root の
   process ができる。管理ソケットは昇格を受けない（0072 §2.1 / §4 の D）、root は承認者になれない（0086 §2.3）に反する
3. **`coldaisle-admin` と `coldaisle-authority` を1つのグループにまとめる**: 1人の導入先では手間が減るが、運転モードを
   触る人と昇格を任せる人を分けた 0086 §2.2 の境界が消え、複数人になったときに戻せない
4. **新しい記録を作らず、6.7 の手順 9 をそのまま行って #217 を閉じる**: 権限の形については足りるが、
   §1.2 の 1（承認者が書けるコードで検証が走りうる）と 3（「書けない」の確認が `test` の実装で誤って通る）は
   確認の健全さに関わる手順の判断で、記録に無いまま手順書だけを変えると理由が残らない
5. **`--registry-limits` を必須にする・path をコードで固定する**: 上限は権限を広げないので、コードの変更に見合う
   得が無い。固定の path はルール 9 に反し、開発と試験で別の上限を使えなくなる。手順で絶対 path を渡す
6. **本番でも console script（`.venv/bin/coldaisle-authority`）を絶対 path で実行する**: 短く書けるが、
   console script は `PYTHONPATH` を読み、書き換えられる場所の `coldaisle` が先に読まれうる（§1.2 の 1）。
   console script の shebang に `-I` を足すには、`uv sync` が作るファイルを導入先で書き換えることになり、
   更新のたびに戻る
7. **確認を `test` のまま、外部コマンドではなく shell の組み込みに揃える**: 組み込みでも補助グループ・ACL の扱いは
   shell ごとに違いうる。実際に開く操作なら、カーネルの判定そのものを見られる

## 5. 判断点（2026-10-14、すべて決着。判断前の記録として推奨案と代替案を残す）・未決事項

| # | 判断点 | 推奨案 | 代替案 |
|---|---|---|---|
| 1 | **決着（2026-10-14 所有者の決定、推奨案）。** 本記録を作るか | **作る**（§1.2 の4点だけを足し、0104 の形は変えない） | 作らず 6.7 の手順 9 をそのまま行う（§4 の 4） |
| 2 | **決着（2026-10-14 所有者の決定、推奨案）。** 本番の `raise` で実行するコード | **root の所有の `/opt/coldaisle/.venv/bin/python -I -m coldaisle.authority_cli` を手順に直接書く。ラッパーは置かない（root の所有のファイルが1つ増えるだけで、守る性質は同じ）。`uv run` と console script を使わない。`--registry-limits` も絶対 path**（§2.2。C1 で §2.4 の 1b を確かめる） | root の所有のラッパー（`exec ... -I -m coldaisle.authority_cli "$@"` の2行）を置き、手順はラッパーを呼ぶ / 手順で決めない（いまの `uv run` の例のまま） |
| 3 | **決着（2026-10-14 所有者の決定、推奨案）。** 承認者が artifact の本体を読めること（`raise` には要らない） | **狭めない**（§2.1。承認の役割の範囲・秘匿情報ではない） | `registry.json` だけに狭める（Registry が作るファイルごとに ACL を付け分ける） |
| 4 | **決着（2026-10-14 所有者の決定、推奨案）。** 6.4 / 6.8 の `test` の確認 | **別の Issue（#300）で同じ決まりに揃える**（1 Issue = 1 PR） | #217 の文書の PR に含める |
| 5 | **決着（2026-10-14 所有者の決定、推奨案）。** 段階 C の分け方と #217 を閉じる時期 | **C1 / C2 に分け、#217 は C2（Production ができた後の `approval_rejected`）まで閉じない**（§2.5） | C1 で #217 を閉じ、C2 を `requires:server` の別 Issue にする |
| 6 | **決着（2026-10-14 所有者の決定、推奨案）。** `coldaisle-admin` だけの資格の確認（§2.4 の 2）を段階 C に足すか | **足す**（日常的に使うグループから authority へ届かないことを見る） | 足さない（0104 §2.9 のまま） |
| 7 | **決着（2026-10-14 所有者の判断）。** 検査の手順の細部 | **段階 C の実施で見つかった問題は、別の記録・手順の修正で扱う。決定記録の段階のレビューはここで閉じる（2026-10-14 所有者の判断）** | — |

未決のまま残すもの（本記録では決めない）:

- journal の改ざん検知（0086 §5 の 1）。§2.3 の (c) の限界はそれまで残る
- Registry の承認者の uid の束縛（0086 §5 の 2）
- 段階 C で形が合わなかったときの見直し（§4 の 1。結果を根拠に新しい記録で）

## 6. 承認記録

**2026-10-14、リポジトリ所有者は §5 の判断点 1〜6 をすべて推奨案で承認し、本記録を FINAL にした**（PR #297）。
7（決定記録の段階のレビューをここで閉じる）は同日の所有者の判断である。判断点 4 の別の Issue は #300。

本記録は 0104 / 0105 の決定を置き換えず、段階 C の前の点を足すだけなので、旧記録への `Superseded by` の追記はしない。
