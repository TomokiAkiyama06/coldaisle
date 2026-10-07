# 決定記録 0104: 承認者が本番で `coldaisle-authority raise` を使うための、制御設定の読み取りと Model Registry の lock の最小権限

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-07
- **Supersedes**: なし（0086 §5 の未決 3 を閉じる。0080 / 0086 の決定した値は変えない。§2.8）
- **関連**: [0086](0086-authority-approver-uid.md) §2.1〜§2.4・§2.8、§5 の未決 2・3 /
  [0057](0057-authority-rollout-stage-changes.md) §2.3 / §2.4（承認者が値を持ち込まない・証拠の束縛） /
  [0062](0062-model-registry-operations.md) §2.1 / §2.2（`coldaisle-registry`） /
  [0072](0072-control-admin-entry.md) §2.5（同じ uid と root を暗黙に認めない） /
  [0077](0077-learned-proposal-handoff.md) §2.6（fand は registry を flock を取らずに読む）/ 段階 6 /
  [0080](0080-fand-systemd-unit.md) §2.1 / §2.2 /
  [0089](0089-authority-bound-to-loaded-artifact.md) / [0090](0090-authority-bound-to-loaded-config.md)（fand 側の照合） /
  [0098](0098-registry-binding-settled-points.md) §2.1（fand は `inspect()` だけ） /
  `docs/ubuntu-deploy.md` 6.1 / 6.2 / 6.6、`docs/authority-rollout.md`、`docs/model-registry.md` /
  `src/coldaisle/authority_cli.py`、`src/coldaisle/control/authority.py`（`raise_stage()`）、
  `src/coldaisle/control/model_registry.py`（`pinned()` / `_exclusive_lock()` / `_atomic_write()`） /
  AGENTS.md「絶対に守るルール」1・2・9・10
- **対象 Issue**: #217（#92 の後続。PR #216 の Codex P1「Provision operator access before enabling authority raises」）

---

## 1. Context

PR #216（0086 段階 3b）で `coldaisle-authority raise` / `rollback` が入った。`raise` は承認者の uid のまま
（0086 §2.1。`sudo` を使わない）次を行う。

| 手順 | 読む・取るもの | 必要な権限（いまの実装） |
|---|---|---|
| `ControlConfig.from_directory(--config-dir)` | 制御の4ファイル | 4ファイルの `r`、親ディレクトリをたどる `x`（`Path.read_text()` は path で開くので、親の `r` は要らない） |
| `ModelRegistry.pinned()`（`raise_stage()` の中。lock は **Registry → Authority** の順） | Registry の root、`.registry.lock`、`registry.json` | root までの各ディレクトリを `O_RDONLY｜O_DIRECTORY｜O_NOFOLLOW` で開く `r` と `x`、lock の `O_RDWR｜O_CREAT`、`registry.json` の `r`。**artifact の本体は読まない**（`_production_of()` は snapshot の metadata だけを見る） |
| authority の journal | `/var/lib/coldaisle-authority`（0086 §2.2） | `coldaisle-authority` グループ（いまの導入手順で足りる） |

いまの導入手順（`docs/ubuntu-deploy.md` 6.1 / 6.2）では、承認者は `coldaisle-authority` グループにしか
入っておらず、

- 制御設定 `/etc/coldaisle/control-config`（`root:coldaisle-fan`・`0750`、ファイル `0640`）を読めない。
  親の `/etc/coldaisle` も `root:coldaisle`・`0750` でたどれない
- Model Registry の置き場所・所有者・mode が導入手順に無い

ので、本番の `raise` は `raise_stage()` に届く前に必ず失敗する。2026-10-01、所有者は「#216 では文書で
制限し、権限の設計は #217 で行う」と判断した（PR #216 の判断点 8）。`rollback` は authority のディレクトリ
だけを使うので、いまの手順で動く。

さらに、実装を読むと次の2つの事実がある。**どちらも権限を足すだけでは解けない。**

1. **Registry は作るファイルを `0600`、作るディレクトリを `0700` にする**（`_atomic_write()` の一時ファイル・
   `_open_directory_chain()` の `mkdir`・`_exclusive_lock()` の `O_CREAT`）。書き手が1人でも書けば、
   `registry.json` は書き手だけが読めるファイルに置き換わり、グループや ACL で渡した読み取りは効かなくなる
   （ACL がある場合も、作成時の mode が mask を 0 にする）。0086 §2.4 が authority の journal で塞いだのと同じ穴
2. **承認者の側の `ModelRegistry` も root と lock を作りうる**（`_exclusive_lock()` は `_open_root(create=True)` と
   `O_CREAT`）。root や lock が無いときに承認者が `raise` すると、承認者の uid が所有する `0700` / `0600` の
   root・lock ができ、以後 Registry の書き手が lock を取れなくなる（0086 §2.4 の「CLI はディレクトリを作らない」と
   同じ穴）

### Issue #217 の未決の点と、本記録の対応

| # | Issue の未決 | 本記録 |
|---|---|---|
| 1 | 制御設定の読み取りをどう与えるか | §2.2（推奨: POSIX ACL で `coldaisle-authority` に読み取りだけ） |
| 2 | Registry の置き場所・所有者・mode | §2.3 |
| 3 | lock と `registry.json` の読み取りだけを与えるか。lock を取れる人が書き込みを待たせられること | §2.4 / §2.5 |
| 4 | `--config-dir` が fand の使っている設定と同じことを CLI が確かめるか | §2.6（推奨: 0090 の fand 側の照合に任せ、CLI は足さない） |
| 5 | 新しい記録を作るか、既存記録との関係 | 本記録。§2.8 |
| 6 | 実機の導入先での確認 | §2.9（手順の案。実行は人。`requires:server`） |

### 崩さない前提（0086）

- 承認者は**実行した uid**（`uid.<os.getuid()>`）。`sudo` も `sudo -u coldaisle-fan` も使わない（§2.1 / §2.2）
- **root は承認者になれない**、**authority の root の所有者（fand の実行ユーザー）は承認者になれない**、
  `uid != euid` を拒む（§2.3。`raise_stage()` の中で判断）
- 承認者のグループに DB の書き込み権を渡さない（§2.8）
- fand 自身が `safety.yaml` を緩められない（制御設定は root の所有。`docs/ubuntu-deploy.md` 6.2）

本記録の権限はすべて**読み取り**と **flock** だけで、上の4つのどれにも触れない。承認者を root にする経路も、
承認者を fand のユーザーにする経路も作らない。

## 2. Decision（案。所有者の判断待ち。§5 の各項目に推奨案と代替案）

名前（ユーザー・グループ・path）はすべて**仮の値**である。実機のユーザー名・ホスト名・path は
リポジトリに書かない（AGENTS.md ルール 10、0086 §5 の 4）。

### 2.1 役割と権限の表（目標の形）

| 役割（仮の名前） | 実行の形 | 制御設定 | Registry（`registry.json`・artifact） | `.registry.lock` | authority のディレクトリ |
|---|---|---|---|---|---|
| fand（ユーザー `coldaisle-fan`） | unit の `User=` と `SupplementaryGroups=` | 読む（主グループ `coldaisle-fan`。**変えない**） | `--registry-root` を渡したときだけ読む（`inspect()`。flock を取らない。0077 §2.6 / 0098 §2.1） | 使わない（§2.5 の構造の試験） | 読み書き（0086 §2.2。変えない） |
| 承認者（`coldaisle-authority` の人） | 自分の uid のまま `coldaisle-authority raise` / `rollback` | **読む**（§2.2。ACL） | **`registry.json` を読む**（§2.3。ACL） | **flock だけ**（§2.4） | 読み書き（0086 §2.2。変えない） |
| Registry の書き手（`coldaisle-registry` グループの人） | 自分の uid のまま `coldaisle-registry`（#104） | 触らない | 読み書き（グループ） | 読み書き（グループ） | **触らない**（グループに入らない限り） |
| `coldaisle`（API / 取り込み）・AI 層 | — | 触らない | 触らない | 触らない | 触らない |
| Learned worker（0077 段階 6 の unit） | 役割ごとの unit | — | artifact を読む（**本記録では決めない**。§5 の 6） | 使わない | 触らない |
| root | — | 管理（所有者） | 管理（root の所有者） | — | `rollback` だけ（0086 §2.6） |

### 2.2 制御設定の読み取り：POSIX ACL で `coldaisle-authority` に読み取りだけを足す（Issue の未決 1）

**推奨案**: 所有者・グループ・mode（`root:coldaisle-fan`・`0750` / `0640`）は**変えず**、名前付きグループの ACL
だけを足す。

```bash
# 親をたどれるだけにする（一覧は見せない。coldaisle.env と control-admin.yaml は読めないまま）
sudo setfacl -m g:coldaisle-authority:--x /etc/coldaisle
# 制御設定のディレクトリと4ファイルを読めるようにする。default ACL で、install で置き直したファイルにも付ける
sudo setfacl -m g:coldaisle-authority:r-x /etc/coldaisle/control-config
sudo setfacl -d -m g:coldaisle-authority:r-- /etc/coldaisle/control-config
sudo setfacl -m g:coldaisle-authority:r-- /etc/coldaisle/control-config/*.yaml
```

- 書き込みは誰にも足さない。fand（`coldaisle-fan`）の権限も変わらない。**root の所有のままなので、fand も
  承認者も `safety.yaml` を緩められない**（6.2 の境界を保つ）
- `/etc/coldaisle` には `x` だけ。承認者は `coldaisle.env`（秘匿情報。`root:coldaisle`・`0640`）と
  `control-admin.yaml`（`root:coldaisle-fan`・`0640`）を読めない。いまの `ControlConfig.from_directory()` は
  path で開くので、親の `r` は要らない（将来 `O_NOFOLLOW` の連鎖で開くよう変えるなら、この行を見直す。§2.10 の試験）
- `install -m 0640` でファイルを置き直すと、default ACL から `g:coldaisle-authority:r--` が付き、mode の
  グループの bit（`r`）が mask になるので効く。`sudoedit` は既存のファイルへ書き戻す
- **ACL が外れたときの失敗は安全側である。** 承認者が読めなくなるだけで、`raise` は `io_or_config_error`
  （終了コード 1）で止まり、journal も fand も変わらない。上げる向きには働かない
- `rollback` は制御設定を読まないので、ACL の有無に依らず動く

代替案（§4 / §5 の 1）: 別グループへ付け替える、fand が読んだ内容を別経路で渡す、`/etc/coldaisle` を
`0751` にする。

### 2.3 Registry の置き場所・所有者・mode（Issue の未決 2）

**推奨案**: 専用のディレクトリ `/var/lib/coldaisle-registry`（**仮の名前**）を導入手順で作る。

| 対象 | 所有者:グループ | mode | ACL |
|---|---|---|---|
| root（`/var/lib/coldaisle-registry`） | `root:coldaisle-registry` | `2770`（setgid） | `g:coldaisle-authority:r-x`、default `g:coldaisle-authority:r-X` |
| `registry.json`・`artifacts/` の下 | 書いた人:`coldaisle-registry`（setgid で継承） | ファイル `0640`、ディレクトリ `2770` | default ACL から継承 |
| `.registry.lock` | `root:coldaisle-registry` | `0660` | `g:coldaisle-authority:r--`（§2.4） |

```bash
sudo groupadd --system coldaisle-registry
sudo usermod -aG coldaisle-registry <Registry を書く人のユーザー名>
sudo install -d -o root -g coldaisle-registry -m 2770 /var/lib/coldaisle-registry
sudo setfacl -m g:coldaisle-authority:r-x /var/lib/coldaisle-registry
sudo setfacl -d -m g:coldaisle-authority:r-X /var/lib/coldaisle-registry
# lock は導入手順で作る。どの CLI にも作らせない（§2.4 / §2.5）
sudo install -o root -g coldaisle-registry -m 0660 /dev/null /var/lib/coldaisle-registry/.registry.lock
sudo setfacl -m g:coldaisle-authority:r-- /var/lib/coldaisle-registry/.registry.lock
```

- **root の所有者は root。** 書き手のグループの人がディレクトリの mode や ACL を変えられない
  （mode を変えられるのは所有者だけ）。authority のディレクトリ（所有者 `coldaisle-fan`）と違い、Registry には
  「所有者を承認者にしない」の判断が無いので、どのサービスの uid にもしない
- **書き手は自分の uid のまま** `coldaisle-registry` を実行する（0086 §2.2 と同じ形。`sudo -u` で専用の
  ユーザーにならない）。将来 0086 §5 の未決 2（Registry の承認者も uid に結びつけるか）を進めるときに、
  同じ仕組みを使える。代替案は §5 の 2
- **承認者は Registry を書けない**（グループ `coldaisle-registry` に入らない限り。§5 の 3）。
  承認者に足すのは `registry.json` と artifact の**読み取り**と、lock の flock だけ
- fand は unit の `SupplementaryGroups=` に `coldaisle-authority` を持つ（0086 §2.2）ので、`--registry-root` を
  渡したとき（0077 §2.6）は、unit を変えずに同じ ACL で `registry.json` を読める。`ProtectSystem=strict` の下でも
  読み取りは通る。**fand の unit の値は本記録では変えない**（§2.8）
- **コードの変更が要る（§2.7 の段階 A）**: Registry が作るファイルを `fchmod(0o640)`、作るディレクトリを
  親（root）の permission の bit（`0o2770` で mask した値）に揃える。`umask` に依らない（0086 §2.4 と同じ理由）。
  開発用の `var/model-registry`（`0700` の root）では、子も `0700` のままになる

### 2.4 承認者に与えるもの：`registry.json` の読み取りと、lock の flock だけ（Issue の未決 3）

**推奨案**: 承認者の側の `ModelRegistry` を「共有の root」モードで開き、次の3つを満たす。

1. **root も lock も作らない。** 無ければ error。journal は書かない（終了コードは §5 の 11）
2. **lock を `O_RDONLY` で開いて `flock(LOCK_EX)` を取る。** Linux の `flock(2)` は fd の開き方に依らず
   排他 lock を取れる（ローカルのファイルシステム。Registry を NFS に置かない）。したがって承認者に lock の
   `w` を与えない（Issue は `rw` と書いたが、`r` で足りる）
3. 書く前ではなく**開いた直後**に、root が「ディレクトリ・other に権限が無い・setgid 付き」であることを確かめる
   （0086 §2.4 の authority の root と同じ確認。導入の誤りを、Registry を壊す前に見つける）

- 書き手（`coldaisle-registry`）の側も、共有の root（setgid 付き）では lock を作らない（無ければ error）。
  lock が消えた導入先で書き手が `0600` の lock を作り、承認者が flock を取れなくなる経路を塞ぐ
- 承認者は `registry.json` を書けず、artifact を置けず、lock を消せない（root への `w` が無い）

**lock を取れる人が Registry の書き込みを待たせられること**（Issue の未決 3 の後半）:

- 承認者（と、ACL を共有する fand の uid）は lock を握り続けて、Registry の書き手（人の経路。0060 §2.7 で
  待ち続ける）を**待たせられる**。これは flock を与える以上、避けられない
- **制御は待たない。** 制御プロセス（fand・Learned worker）は Registry の lock を取らない（0077 §2.6 の
  「flock を取らない」・0098 §2.1 の `inspect()` だけ）。§2.10 の構造の試験で、`pinned()` / `_exclusive_lock()` を
  呼ぶのが `AuthorityStore.raise_stage()` と `coldaisle-registry` だけであることを固定する
- 待たされた書き手は止まるだけで、Registry を壊さない（`expected_revision` で後勝ちも起きない。0062 §2.1）
- 誰が握っているかは `/proc/locks` と `fuser -v`（root）で分かる。導入手順の「困ったとき」に書く（段階 B）
- 正常な `raise` が lock を握る時間は、報告（上限 `max_artifact_bytes`）と証拠の検証の間だけ

### 2.5 lock の順序と、0090 の束縛との関係

- **lock の順序は Registry → Authority のまま**（`raise_stage()`。`docs/authority-rollout.md`）。本記録は
  Registry の lock の開き方（`O_RDONLY`・作らない）を変えるだけで、順序も「Registry の lock を authority の
  commit まで握る」も変えない。降格（`lower_stage()` / `rollback`）は Registry の lock を取らないまま
  （Registry の権限が壊れていても安全側へは動ける）
- 承認者は Registry を書けないので、`raise_stage()` の「読むだけで書かない」（0057 §2.3）が権限でも担保される
- **0090（config）・0089（artifact）の fand 側の照合は変えない。** 承認者が読む制御設定は fand が読むのと
  同じファイル（root の所有）で、承認者は書けない。証拠の hash は CLI が読んだ bytes から、fand は起動時に
  読んだ bytes から計算し、fand が照らし合わせる

### 2.6 `--config-dir` の取り違え：CLI では確かめず、0090 の fand 側の照合に任せる（Issue の未決 4）

**推奨案**: CLI に「fand が使っている設定と同じディレクトリか」の確認を**足さない**。

- 取り違えた（または fand の起動後に差し替えた）設定で `raise` しても、fand は journal の証拠の4つの hash と
  起動時に読んだ設定を照らし、1つでも違えば実効 stage の上限を Baseline にする（0090 §2.2）。**取り違えは
  上げる向きに働かない**（安全側。journal は上がるが効かない）
- CLI で確かめるには、fand が読んだ設定の hash を CLI へ渡す経路（DB の trace・管理ソケットの `status`）が要る。
  前者は承認者に DB の読み取り権を、後者は `coldaisle-admin` への所属を要求し、最小権限に反する（§4）
- 代わりに運用で塞ぐ: 導入手順と `docs/authority-rollout.md` で、本番の `--config-dir` を unit の
  `ExecStart=` と**同じ path**（`/etc/coldaisle/control-config`）と書き、`raise` の後に fand のログの
  `authority_config_matched` / `authority_artifact_matched`（0089 / 0090 §2.5）と `trace_metadata()` の
  `authority_config_binding_ceiling` で、上げた stage が効いていることを確かめる手順を置く（段階 B）
- 「journal は上がったが効いていない」状態は 0090 §3 のとおり残る。気づく手段は上のログと trace

### 2.7 実装の段階

| 段階 | 担当 Issue | 内容 |
|---|---|---|
| A | #217 | `ModelRegistry` の共有の root モード（§2.4: 作らない・lock を `O_RDONLY`・root の形の確認）と、作るファイル `0640`・ディレクトリを親に揃える（§2.3）。`authority_cli.py` が共有モードで開く。試験（§2.10） |
| B | #217 | **本記録が FINAL になった後**: `docs/ubuntu-deploy.md`（6.1 / 6.2 に ACL、新しい節に Registry のディレクトリ・グループ・lock、6.1 と末尾の「決まるまで使えない」の書き換え）、`docs/authority-rollout.md`（`--config-dir` は unit と同じ path・`raise` の後の確認・「使えない」の注記）、`docs/model-registry.md`（mode と共有モード） |
| C | #217（`requires:server`） | 実機の導入先で §2.9 の確認を**人が**行う。通るまで、B の「本番の `raise` は使えない」の注記は外さない |

**`docs/ubuntu-deploy.md` への反映は、本記録が FINAL になった後の実装 PR（段階 B）で行う。** 本 PR は文書
（本記録と索引）だけで、導入手順・コード・unit・udev には触れない。

### 2.8 既存記録との関係（Issue の未決 5）

- **新しい記録（本記録）を作る。** 0086 §5 の未決 3 を閉じる。0086 は「3c の導入手順。足りなければ別の記録」と
  送っており、ACL・Registry の置き場所・コードの変更（§2.3 / §2.4）を含むので、導入手順だけでは足りない
- **置き換える（Supersedes）ものは無い。**
  - 0080 §2.1 / §2.2・0086 §2.2 の unit の値（`User=`・`SupplementaryGroups=`・`ReadWritePaths=`・`--config-dir`）は
    変えない。fand が Registry を読むのに要る権限は、既にある `coldaisle-authority` の補助グループで足りる（§2.3）
  - 0086 §2.2 の「`coldaisle-authority` には昇格・rollback を行う人だけを入れる」は変えない。そのグループに
    **読み取り**（制御設定・Registry）と lock の flock を足すだけで、入れる人の範囲は同じ
  - `docs/ubuntu-deploy.md` 6.2 の所有者・mode は変えない（ACL を足すだけ）
- 本記録を FINAL にするとき、0086 §5 の未決 3 の行へ「本記録で決めた」の追記は**しない**（追記が許されるのは
  `Superseded by` だけ。docs/decisions/README.md「追記のみ」）。索引の 0086 の行も変えない

### 2.9 実機での確認の手順案（Issue の未決 6。実行は人。`sudo` は導入先の手順として書く）

段階 C で、導入先で次を確かめる。**この手順で fand を `enable` も `start` もしない**（`docs/ubuntu-deploy.md` 6）。

1. ACL が付いたこと: `getfacl /etc/coldaisle /etc/coldaisle/control-config /etc/coldaisle/control-config/*.yaml
   /var/lib/coldaisle-registry /var/lib/coldaisle-registry/.registry.lock` で、§2.2 / §2.3 の行と `mask` が
   期待どおりであること（`mask::r--` などで実効の権限が削られていないこと）
2. **承認者の uid で**（ログインし直し、`id -nG` に `coldaisle-authority` が出てから。`sudo` を使わない）:
   - 読める: `cat /etc/coldaisle/control-config/*.yaml > /dev/null`、`cat /var/lib/coldaisle-registry/registry.json > /dev/null`
   - flock を取れる: `flock -n /var/lib/coldaisle-registry/.registry.lock true; echo $?`（`0`）
   - **読めない・書けない**（すべて権限の error になること）: `cat /etc/coldaisle/coldaisle.env`、
     `cat /etc/coldaisle/control-admin.yaml`、`ls /etc/coldaisle`、
     `touch /etc/coldaisle/control-config/x`、`touch /var/lib/coldaisle-registry/x`、
     `sh -c ': >> /var/lib/coldaisle-registry/registry.json'`、`sh -c ': >> /var/lib/coldaisle-registry/.registry.lock'`
3. **Registry の書き手の uid で**: `coldaisle-registry status --root /var/lib/coldaisle-registry` が通り、
   書く操作（開発用の artifact で `register` など）の後も 2 の「読める」が通ること（作られたファイルが `0640`・
   グループ `coldaisle-registry`・ACL 付きであることを `getfacl` で見る）。書き手が authority のディレクトリへ
   書けないこと（`touch /var/lib/coldaisle-authority/x` が失敗）
4. **fand と同じグループで**（6.4 の手順 4 と同じ `systemd-run`。`User=coldaisle-fan`・
   `SupplementaryGroups=coldaisle coldaisle-admin coldaisle-authority`）: 制御設定と `registry.json` を読めて、
   制御設定にも Registry にも書けないこと
5. `coldaisle-authority raise` を**導入先の手順（`docs/authority-rollout.md`）の承認と証拠で**1回通すかは、
   所有者が authority を上げると決めたときに行う（本記録の確認には含めない。§5 の 7）。代わりに、承認者の uid で
   **期限内で形の正しい、`expected_revision` だけが journal と合わない承認**を渡す。`raise_stage()` はこれを
   Registry の lock と authority の lock を取った**後**に拒む（期限切れの承認は Registry を読む前に拒まれるので、
   確認に使えない）。期待する結果は終了コード 4・`code` が `approval_rejected`（revision の不一致）で、
   `io_or_config_error`（制御設定を読めない。終了コード 1）や `evidence_rejected`（理由が「Model Registry の状態を
   読めない」）なら権限が足りていない。journal は変わらない
6. 結果（どの手順が通ったか・`getfacl` の形）を #217 に残す。**実機のユーザー名・ホスト名・path は書かない**
   （仮の名前に置き換える。AGENTS.md ルール 10）

### 2.10 試験できる性質（実機不要。`tmp_path`。段階 A）

- 共有の root モードの `ModelRegistry` は、root が無ければ作らずに error、lock が無ければ作らずに error
  （どちらも root の中に何も増えない）。`raise_stage()` は journal を書かない
- 共有の root モードは lock を `O_RDONLY` で開き、別の process（または別の fd）が `LOCK_EX` を持つ間は待つ
  （`O_RDONLY` の fd で排他になることを、もう1つの fd の `LOCK_NB` の失敗で確かめる）
- root が setgid でない・other に権限がある・ディレクトリでない（symlink を含む）なら、lock を取る前に error
- `umask 0022` / `0077` のもとで、Registry の書き込みが作るファイルは `0640`、ディレクトリは親の bit に揃う
  （共有の root `2770` の下では `2770`、開発用の `0700` の下では `0700`）
- 構造の試験: `src` で `ModelRegistry.pinned()` を呼ぶのは `control/authority.py` の `raise_stage()` だけ。
  制御プロセスの起点（`control_daemon.py`・`learned_channel/`・`control/loop.py`）は `pinned()` も Registry の
  書き込み操作（`register_candidate` / `mark_validated` / `promote` / `rollback` / `retire`）も呼ばない
  （制御が Registry の lock を待たない。いまは `inspect()` だけ）
- `authority_cli.py` は Registry を共有の root モードで開く（`AuthorityStore(require_shared_root=True)` と対にする）
- `ControlConfig.from_directory()` が親のディレクトリを `O_RDONLY` で開かない（`x` だけで読める）ことを、
  `0711` の親の下で読める試験で固定する（§2.2 の前提が実装の変更で黙って崩れないように）
- 既存の「管理ソケットは昇格を受けない」「`model_registry.py` は authority を参照しない」の走査は変えない
- root で走る CI では mode と権限の試験の一部が意味を持たないので、理由を書いて skip する（0080 / 0086 と同じ）。
  ACL（`setfacl`）そのものは CI では試験しない（ファイルシステムと権限に依る。§2.9 で人が確かめる）

## 3. Consequences

### 良くなること

- 0086 §5 の未決 3 が閉じ、本番で `raise` を使える条件（段階 A〜C）がそろう
- 承認者に足すのは読み取りと flock だけで、root にも fand のユーザーにもならない。0086 の承認者の束縛
  （実行した uid・root と fand のユーザーの拒否）はそのまま効く
- Registry の書き手・承認者・fand の権限が分かれる。承認者は Registry を書けず、Registry の書き手は authority を
  書けず、fand はどちらにも昇格を書けない
- Registry の「作るファイルが `0600`」の穴が、本番の導入より前に塞がる（承認者だけでなく fand と worker の
  読み取りにも効く）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| POSIX ACL に依存する（ファイルシステムの対応・`getfacl` の読み方・`mask`） | Ubuntu の ext4 は既定で対応。外れたときは `raise` が読めずに止まるだけ（安全側）。§2.9 の 1 で `mask` まで確かめる |
| 承認者（と fand の uid）が Registry の lock を握って書き手を待たせられる | 制御は lock を取らない（構造の試験）。待たせるだけで壊さない。`/proc/locks`・`fuser` で見つけられる |
| fand の uid が `coldaisle-authority` の補助グループ経由で Registry の lock を flock できる | fand は既に authority の journal を書け、hwmon を書ける。Registry の書き込みを待たせることはそれより弱い。分けるなら §5 の 4 |
| `coldaisle-authority` グループの意味が「journal を書く」から「journal を書き、制御設定と Registry を読む」へ広がる | 入れる人の範囲（0086 §2.2）は同じ。昇格を判断する人が証拠の対象を読めるのは当然の範囲 |
| `--config-dir` の取り違えを CLI では止めない（journal は上がるが効かない） | 0090 で安全側に倒れる。`raise` の後の確認（ログと trace）を手順に置く |
| Registry のコードの変更（mode・共有モード）が要る | 段階 A。0086 段階 3a の authority の変更と同じ形で、試験で固定する |
| lock を導入手順で作る前提になる（消えると書き手も承認者も止まる） | 止まるだけで安全側。手順に作り直しを書く。書き手も作らない（§2.4） |

## 4. 却下した代替案

**制御設定の読み取り（§2.2）**

- **別のグループ（例 `coldaisle-config`）へ付け替え、fand と承認者を入れる**: ACL は要らないが、6.2 の
  `root:coldaisle-fan` と unit の `SupplementaryGroups=` を変え（0080 の値の置き換え）、なお `/etc/coldaisle`
  （`root:coldaisle`・`0750`）をたどれない問題が残る（そこにも ACL か mode の変更が要る）。§5 の 1 の代替案として残す
- **fand が読んだ設定（または hash）を別の場所へ書き出し、CLI はそれを読む**: fand の uid が昇格の証拠の
  照合に使う bytes を書けることになる。0086 は fand のユーザーを昇格の経路から外しており、その向きに反する。
  照合は 0090 で既に fand の側にある
- **`/etc/coldaisle` を `0751` にする**: ACL の行が1つ減るが、全ユーザーにたどる権限を与える（中のファイルは
  mode で守られているが、最小権限ではない）
- **承認者を `coldaisle-fan` グループに入れる**: そのグループは hwmon の PWM を書ける（0080 §2.6。0086 §4 で却下済み）
- **`sudo` で root として `raise` する**: 0086 §2.3 で root は承認者になれない

**Registry の置き場所と書き手（§2.3）**

- **専用のシステムユーザー（例 `coldaisle-registry`）を所有者にし、書き手は `sudo -u` で実行する**: ACL が要らず
  （グループで読み手を表せる）単純だが、書き手の入力ファイル（ホームの metadata・payload・承認）をそのユーザーが
  読めず、置き直しが要る。Registry の承認者の uid の束縛（0086 §5 の 2）を将来行うときに、`SUDO_UID` に頼る形
  （0086 §4 で却下した形）になる。§5 の 2 の代替案として残す
- **Registry を authority のディレクトリの下に置く**: Registry の書き手に journal の書き込み権を与えることになる
- **Registry を fand の状態ディレクトリ（`0700`）の下に置く**: 承認者も書き手も入れない

**lock（§2.4）**

- **lock に `rw` を与え、いまの `O_RDWR` のまま開く**: 働きは `r` と同じ（flock を取れる）で、空の lock に
  書ける余地だけが増える。開き方を変える小さなコードの変更で済むので、`r` を推奨する。§5 の 5
- **lock を取らずに `inspect()` だけで読む**: 読んでから journal を書くまでの間に別の process が promote できる
  （PR #216 以前に Codex が指摘し、`pinned()` を入れた理由。`raise_stage()` の docstring）
- **lock の待ちに期限を付ける**: 人の経路は待ち続けてよい（0060 §2.7）。期限で止まると、正常な書き手と承認者の
  競合でも失敗になる。必要になったら別に決める

**`--config-dir` の照合（§2.6）**

- **CLI が DB の最新の trace（fand が読んだ設定の hash）と照らす**: 承認者に DB の読み取り権が要る。DB の
  ファイルは `coldaisle` の `0660` で、グループで渡すと書き込みも渡る（0086 §2.8 が避けたもの）
- **CLI が管理ソケットの `status` で fand の設定の hash を聞く**: 承認者に `coldaisle-admin` への所属を要求する。
  管理ソケットは昇格を受けない入口で、昇格の前提として使い始めると 0072 §4 の D の境界に近づく
- **`--config-dir` を消し、path を固定値にする**: 開発・試験で別の path を使えなくなる。path をコードに持つと
  ルール 9 にも反する

## 5. 未決事項（所有者の判断。いずれも推奨案つき）

| # | 判断点 | 推奨案 | 代替案 |
|---|---|---|---|
| 1 | 制御設定の読み取りの与え方 | **POSIX ACL で `coldaisle-authority` に読み取り**（§2.2。所有者・mode を変えない） | 別グループへ付け替え（0080 の unit の値と 6.2 を変え、`/etc/coldaisle` にも別の手当てが要る） |
| 2 | Registry の書き手の形 | **書き手のグループ `coldaisle-registry` の人が自分の uid で書く**（§2.3。root は `root:coldaisle-registry`・`2770`、読み手は ACL） | 専用のシステムユーザーを所有者にし `sudo -u` で書く（ACL 不要。§4） |
| 3 | 同じ人が `coldaisle-registry` と `coldaisle-authority` の両方に入ってよいか（model の promote と authority の raise の兼任） | **認める**（1人で運用する規模。分けてもコードでは強制しない。導入手順に「分けられるなら分ける」と書く） | 導入手順で禁止する（強制はできない） |
| 4 | fand の uid が補助グループ `coldaisle-authority` 経由で Registry の lock を flock できることを許すか | **許す**（§3 の緩和。fand は既に authority と hwmon を書ける） | lock のグループだけを承認者専用の別グループにする（承認者が入るグループが1つ増える） |
| 5 | 承認者の lock の権限 | **`r` だけ。共有の root モードは lock を `O_RDONLY` で開く**（§2.4） | Issue のとおり `rw`（コードは `O_RDWR` のまま） |
| 6 | Learned worker（0077 段階 6 の unit）が artifact を読む権限 | **本記録では決めない。** 0077 段階 6 の unit を決めるときに、役割ごとのグループへ ACL の読み取りを足す（`coldaisle-authority` には入れない。journal を書けてしまう） | 本記録で読み手の共通グループを作る |
| 7 | §2.9 の実機の確認に、本物の `raise` を1回含めるか | **含めない。** 承認の拒否（終了コード 4）まで届くことで読み取りと lock を確かめる。本物の `raise` は所有者が authority を上げると決めたとき | 含める（SHADOW → LIMITED を1回。fand の実効 stage は 0089 / 0090 の照合で決まる） |
| 8 | 評価報告を作る側の権限（`coldaisle-evaluate` などが DB を読む） | **本記録の範囲外。** 承認者は渡された報告を検証するだけ。DB の読み取りを承認者へ渡すかは別に決める | 本記録で決める |
| 9 | journal の改ざん検知（0086 §5 の 1）・Registry の承認者の uid の束縛（0086 §5 の 2） | 変えない（それぞれの場所で決める）。本記録の §2.3 は後者を妨げない形にした | — |
| 10 | 名前（グループ・path）の実際の値 | 導入先で決める（リポジトリには仮の値だけ。0086 §5 の 4） | — |
| 11 | Registry を読めない（権限・lock が無い・root の形が違う）ときの `raise` の終了コード。**いまの実装は `raise_stage()` が `ModelRegistryError` / `OSError` を `AuthorityEvidenceError`（「Model Registry の状態を読めない」）に包むので、終了コード 4・`evidence_rejected`** になり、`docs/authority-rollout.md` の終了コードの表（1 の行に「Registry」）と食い違って読める | **段階 A で、Registry を読めないことを終了コード 1・`registry_error` に揃える**（承認・証拠の拒否と、導入の誤りを終了コードで分ける。store が「Registry を読めない」を `AuthorityEvidenceError` と区別できる error の種類で出し、CLI はそれを写すだけ） | いまのまま（終了コード 4。表の「Registry」は上限の設定の読み込みを指すと注記する） |
