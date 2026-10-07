# 決定記録 0115: Learned worker の systemd unit のテンプレートと、`coldaisle-fand` の unit に Learned・較正の引数を足す形（0077 段階 6）

- **種別**: Decision Record
- **Status**: Proposed（所有者の承認待ち。§5 の判断点 1〜12 に推奨案を付けた）
- **Date**: 2026-10-08
- **Supersedes**: なし（0077 / 0080 / 0095 / 0101 / 0104 / 0110 が「段階 6 で決める」と送った点を決める。
  既存の決定は置き換えない。§2.10）
- **関連**: [0077](0077-learned-proposal-handoff.md) §2.1 / §2.6 / §2.7 / §2.10 段階 6 / §5「別の場所で決める点」 /
  [0080](0080-fand-systemd-unit.md) §2.1 / §2.2 / §2.6 / §2.7 / §2.9 /
  [0095](0095-learned-channel-stage1-settled-points.md) §2.7 /
  [0101](0101-calibration-via-learned-frame.md) §2.6 (b) /
  [0104](0104-authority-raise-least-privilege.md) §2.1 / §2.2 / §2.3 / §5 の 6 /
  [0105](0105-registry-shared-root-settled-points.md) /
  [0110](0110-t-sensor-ceiling-and-enablement.md) §2.8 /
  0113（PR #262 でレビュー中。worker は接続が切れたら終了し、再起動は unit の `Restart=`・終了コード 2 / 3 / 5） /
  `deploy/systemd/coldaisle-fand.service` / `docs/ubuntu-deploy.md` 6 節 /
  `src/coldaisle/local_socket.py`（`prepare_parent` / `check_group_can_traverse`） /
  `src/coldaisle/learned_channel/server.py`（`_check_groups_do_not_overlap` / `set_socket_group`） /
  `src/coldaisle/learned_worker/cli.py` / AGENTS.md「絶対に守るルール」1・2・6・9・10
- **対象 Issue**: #57（0077 §2.10 段階 6 / 0060 未決 7）・#86

---

## 1. Context

0077 §2.10 の段階 6 は「worker の systemd unit のテンプレート（実行ユーザー・グループ・`RuntimeDirectory`・
資源の上限）。仮の値だけ」である。段階 3（MPC worker `coldaisle-learnd --role mpc`。PR #260）がマージされ、
前提がそろった。あわせて、次の点が「段階 6 で入れる」と送られている。

| 送った記録 | 送られた点 |
|---|---|
| 0077 §5 | 実行ファイル・ユーザー・グループ・ソケットの path の名前、worker の unit の `Restart`・資源の上限 |
| 0095 §2.7 | 2つの役割のソケットの共通の祖先（本番では `RuntimeDirectory`）の権限・path |
| 0101 §2.6 (b) | `coldaisle-fand` の unit に `--calibration` を足す |
| 0104 §5 の 6 | Learned worker が artifact を読む権限（役割ごとのグループへ ACL の読み取り。`coldaisle-authority` には入れない） |
| 0110 §2.8 | `--t-sensor-metric`（T_SENSOR を有効にした導入先だけ） |
| 0077 §2.6 / §2.7 | `coldaisle-fand` の `--registry-root` と `--learned-channel-config` |

実装を読むと、テンプレートの名前を決めるだけでは済まない事実が3つある。**どれも unit の値で解くが、
既存の記録に無い判断を含む**ので、実装より先に本記録で決める（AGENTS.md「決定記録」）。

1. **`coldaisle-fand` を worker のグループに入れると、Learned の経路が開かない。**
   受付は、役割ごとのソケットとその親ディレクトリのグループを役割のグループへ付け替える
   （`prepare_parent` の `os.chown(directory, -1, gid)`・`set_socket_group`）。capability を持たない非 root の
   プロセスは自分の属するグループにしか付け替えられない（0080 §2.1 の管理ソケットと同じ）。ところが
   0077 §2.7 の起動時の検査は、2つのグループに属する uid の集合が重なれば経路全体を開かない
   （`_check_groups_do_not_overlap`。fand 自身の uid を除かない）。管理ソケットの形（fand をグループに入れる）を
   そのまま写すと、fand の uid が両方に入り、**本番では必ず `channel_disabled` になる**
2. **worker は制御設定を読む。** `coldaisle-learnd` は frame の `config` と照らすために Control Config の
   4ファイルを起動時に読む（0107 §2.6 / 0113）。本番の置き場所 `/etc/coldaisle/control-config` は
   `root:coldaisle-fan`・`0750`、親の `/etc/coldaisle` は `root:coldaisle`・`0750` で、worker は読めない。
   0104 §5 の 6 が決めたのは artifact の読み取りだけで、制御設定の読み取りは決まっていない
3. **fand は2つの役割のソケットを必ず両方開く**（`LearnedRole` のすべて）。RL worker（#89）がまだ無くても、
   `learned-channel.yaml` が名指す RL のグループが解決できなければ、MPC を含む経路全体が開かない

## 2. Decision（案。§5 の推奨案をまとめたもの）

名前（unit・ユーザー・グループ・path）はすべて**仮の値**である。実機のユーザー名・ホスト名・path・uid は
リポジトリに書かない（AGENTS.md ルール10）。

### 2.1 役割ごとに別の unit ファイル（インスタンスの `@` テンプレートにしない）

| unit（仮の名前） | `ExecStart` の役割 | 実行ユーザー（仮） | 主グループ＝役割のグループ（仮） |
|---|---|---|---|
| `coldaisle-learnd-mpc.service` | `--role mpc` | `coldaisle-learn-mpc` | `coldaisle-learn-mpc` |
| `coldaisle-learnd-supervisor.service` | `--role supervisor` | `coldaisle-learn-rl` | `coldaisle-learn-rl` |

- グループ名は 0077 §2.7 と `config/learned-channel.yaml` の仮称のまま。ユーザーは**役割のグループを主グループに
  持つ専用のシステムユーザー**で、補助グループを持たない。受付の認可は `pwd` / `grp` の所属
  （主グループか `gr_mem`）で数える（`local_socket.uid_in_group`）ので、unit の `SupplementaryGroups=` ではなく
  アカウントの主グループで与える
- worker のユーザーを `coldaisle`・`coldaisle-fan`・`coldaisle-admin`・`coldaisle-authority`・`coldaisle-registry`・
  `dialout` に入れない（0077 §2.1 / §2.7、0080 §2.9、0104 §5 の 6）。読み取り API・AI 層・`coldaisle-eventd` の
  ユーザーを worker のグループに入れない（0077 §2.7。ルール1）
- **`coldaisle-fan`（fand）も worker のグループに入れない**（§1 の 1。グループの付け替えは §2.2 の setgid で不要にする）
- `@` テンプレートにしない理由: 役割（`mpc` / `supervisor`）とグループ名（`-mpc` / `-rl`）の語が一致せず、
  `%i` から組み立てると取り違えが unit の外から見えない。2つのファイルの差は役割・ユーザー・path だけで、
  試験で「役割以外が同じ」ことを確かめる

### 2.2 ソケットの置き場所：`/run/coldaisle` の下の役割ごとの setgid ディレクトリを `tmpfiles.d` で作る

| path（仮） | 種類 | 所有者:グループ | mode | 作る者 |
|---|---|---|---|---|
| `/run/coldaisle` | 共通の祖先（0095 §2.7） | `coldaisle-fan:coldaisle-fan` | `0711` | fand の `RuntimeDirectory=`（0080 §2.7。変えない）と、同じ値の `tmpfiles.d` の行 |
| `/run/coldaisle/learned-mpc` | MPC のソケットの親 | `coldaisle-fan:coldaisle-learn-mpc` | `2750`（setgid） | `tmpfiles.d` |
| `/run/coldaisle/learned-rl` | RL のソケットの親 | `coldaisle-fan:coldaisle-learn-rl` | `2750`（setgid） | `tmpfiles.d` |
| `…/learned-mpc/mpc.sock`・`…/learned-rl/supervisor.sock` | ソケット | `coldaisle-fan:<役割のグループ>`（setgid で継承） | `0660`（`learned-channel.yaml`） | fand |

- テンプレート `deploy/tmpfiles.d/coldaisle-learned.conf` に上の3行（`d`）を置く。導入時に
  `systemd-tmpfiles --create` で作り、OS の再起動のたびに `systemd-tmpfiles-setup.service` が作り直す
  （fand の起動より前）
- **setgid の親の中で fand が作ったソケットは、作った時点で役割のグループを持つ。** fand の
  `set_socket_group`（`chown(path, -1, gid)`）は gid を変えない呼び出しになり、Linux は所有者自身による
  「いまと同じ gid への chown」を所属に依らず許す（`chgrp_ok`）。親ディレクトリは既にあるので
  `prepare_parent` は作らず付け替えず、`check_group_can_traverse` で「グループが一致して g+x」を確かめる。
  **これで fand を worker のグループに入れずに済み、§1 の 1 の重なりが起きない**
- `/run/coldaisle` は 0080 §2.7 の `0711` のまま。worker は other の `x` でたどり（0095 §2.7 の「o+x」）、
  一覧は見えない。各役割の親は `2750` で other に何も無いので、**RL のユーザーは MPC のソケットの手前まで
  たどれない**（認可の `SO_PEERCRED` に加えてファイル権限でも役割が分かれる）
- `tmpfiles.d` の `/run/coldaisle` の行は fand の unit の `User=` / `Group=` / `RuntimeDirectoryMode=` と同じ値に
  する（試験で照らす）。systemd は `RuntimeDirectory=` の所有者が unit の `User=` / `Group=` と食い違うと、
  中身ごと再帰的に付け替える（`systemd.exec(5)`）。そうなると役割のディレクトリのグループが失われるが、
  fand の起動時の検査で `channel_disabled` になるだけで、制御は止まらない（0077 §2.7）
- `learned-channel.yaml` の本番の `sockets.*.path` は上の2つにする（リポジトリの開発用の `var/run/...` は変えない）
- **RL のグループとディレクトリは RL worker が無くても作る**（§1 の 3）。グループは空のままでよい
  （所属が無ければ重なりも無い）。RL のユーザーと読み取りの ACL は #89（0077 段階 4）で RL worker を動かすときに作る

### 2.3 `coldaisle-fand` の unit の `ExecStart` に足す引数

| 引数 | 値（仮） | テンプレートに | 理由 |
|---|---|---|---|
| `--calibration` | `/opt/coldaisle/config/calibration.json` | **常に入れる** | 0101 §2.6 (b)。取り込み（`WorkingDirectory=/opt/coldaisle` の既定 `config/calibration.json`）と同じファイル。読めなくても `unavailable` で起動は止まらない |
| `--registry-root` | `/var/lib/coldaisle-registry`（0104 §2.3 の仮の名前） | **常に入れる** | 0077 §2.6。fand は既にある補助グループ `coldaisle-authority` の ACL で `registry.json` を読む（0104 §2.3。unit の権限は変えない）。読めなければ Learned を無効にして起動を続ける |
| `--learned-channel-config` | `/etc/coldaisle/learned-channel.yaml` | **常に入れる** | 0077 §2.7 / 0095 §2.6。無い・不正なら `channel_disabled` で Fallback / RulePolicy のまま |
| `--t-sensor-metric` | `board.connector_12v2x6`（0110） | **入れない**（コメントで示す） | `safety.yaml` の `telemetry.t_sensor.enabled` と食い違うと起動時に拒否される（0110 §2.8）。導入先の `safety.yaml` で有効にしたときだけ、置いた unit の `ExecStart=` に足す |

- 常に入れる3つは、**どれも失敗が安全側**である（Learned が使われないだけで、冷却の制御は止まらない）。
  Learned を配備しない導入先でも、起動時に warning / error のログが出るだけで、運転は 0080 のテンプレートと同じ
  （§3 の悪くなること）
- `ReadWritePaths=` は足さない（どれも読み取りだけ。`ProtectSystem=strict` のもとでも読める）。
  `/run/coldaisle` は `RuntimeDirectory=` なので書ける
- `SupplementaryGroups=` は変えない（worker のグループを足さない。§2.1）

### 2.4 worker の `ExecStart` と、fand と同じでなければならない値

```text
/opt/coldaisle/.venv/bin/coldaisle-learnd --role mpc --config-dir /etc/coldaisle/control-config
  --registry-root /var/lib/coldaisle-registry --learned-channel-config /etc/coldaisle/learned-channel.yaml
```

- `--config-dir`・`--registry-root`・`--learned-channel-config` は **fand の `ExecStart=` と同じ path**、
  `WorkingDirectory=/opt/coldaisle` も同じにする。`--metrics`・`--registry-limits` は既定（作業ディレクトリ
  基準の `config/metrics.yaml`・`config`）のままで、fand と同じファイルを読む。frame の `config` と
  `metric_catalog_sha256` の照合（0107 §2.6）が、取り違えで常に外れることを防ぐ。試験で照らす
- worker は較正ファイルを読まない（0101 §2.1。`--calibration` を持たない）

### 2.5 `Restart` と終了コード

| 項目 | 値 | 理由 |
|---|---|---|
| `Type=` | `simple` | worker は `sd_notify` を送らない。deadman は fand だけ（0080 §2.2 の `NotifyAccess=main`） |
| `Restart=` | `on-failure` | fand の再起動・停止で接続が切れると worker は終了コード 3 で終わる（0113 §2.1 の 1）。それが通常の経路なので戻す。SIGTERM の正常停止（0）では戻さない |
| `RestartSec=` | `5`（**暫定値**） | fand の再起動（`STARTUP` の Max を通る）より遅れて接続すればよい。fand が止まっている間は5秒ごとに終了コード 3 を繰り返す |
| `StartLimitIntervalSec=` | `0` | fand の長い停止の後でも、worker が諦めて戻らない状態を作らない |
| `RestartPreventExitStatus=` | `2 5` | 2: `--role` が未対応（RL は #89 まで）。5: 設定・Metric Catalog・`learned-channel.yaml`・registry の設定を読めない。どちらも人が直すまで結果が変わらない（0080 §2.4 の `3 4` と同じ考え方）。3 は入れない |
| 依存 | `After=coldaisle-fand.service` だけ。**`Wants=` / `Requires=` / `BindsTo=` / `PartOf=` / `Requisite=` を使わない** | worker の起動で fand（Fan 制御）を引き起こさない（fand の有効化は 0080 段階 5 の後）。fand の側も worker を参照しない（0077 §2.1「fand は worker に依存しない」。試験で禁止） |

### 2.6 資源の上限（すべて**暫定値**。段階 3 の shadow で測ってから決める）

| 項目 | 値 | 理由 |
|---|---|---|
| `MemoryMax=` | `1G` | 片方の worker のメモリの枯渇でもう片方と fand を巻き込まない（0077 §2.1）。artifact の大きさの実測を待つ |
| `TasksMax=` | `32` | 推論と heartbeat の2本＋数値ライブラリのスレッド |
| `CPUWeight=` / `IOWeight=` | `20` | fand（既定 100）が CPU・I/O を取り負けない。上限（`CPUQuota=`）は置かない（推論が遅れても期限切れで Fallback になるだけ。0077 §2.4） |
| `Nice=` | `10` | 同上 |
| `OOMScoreAdjust=` | `500` | メモリが逼迫したら fand より先に worker を落とす |
| `LimitCORE=` | `0` | fand と同じ |

### 2.7 hardening

fand（0080 §2.6）と同じものに加え、**hwmon へ書けないことを unit でも保証する**。

- `ProtectSystem=strict`（`ReadWritePaths=` 無し。worker は何も書かない）、`ProtectHome=yes`、`PrivateTmp=yes`
- **`ProtectKernelTunables=yes`**（fand と違い `/sys` を読み取り専用にする。ルール2）、`PrivateDevices=yes`
  （NVML・シリアルを開かない。ルール6）
- `RestrictAddressFamilies=AF_UNIX`、`IPAddressDeny=any`、`PrivateNetwork=yes`（ファイルの path の Unix ソケットは
  network namespace を越えて使える）
- `CapabilityBoundingSet=`（空）、`AmbientCapabilities=`（空）、`NoNewPrivileges=yes`、`UMask=0077`
- `ProtectKernelModules=yes`・`ProtectKernelLogs=yes`・`ProtectControlGroups=yes`・`ProtectClock=yes`・
  `ProtectHostname=yes`・`RestrictNamespaces=yes`・`RestrictSUIDSGID=yes`・`LockPersonality=yes`・
  `SystemCallArchitectures=native`
- `InaccessiblePaths=-/var/lib/coldaisle -/var/lib/coldaisle-fand -/var/lib/coldaisle-authority`
  （DB・fand の状態・authority の journal。ファイル権限でも読めないが、権限の設定の誤りに備えて二重にする）
- `MemoryDenyWriteExecute` と `SystemCallFilter` は入れない（数値ライブラリと未確認。0080 §5 の 10 と同じ）
- `RuntimeDirectory=` / `StateDirectory=` を持たない（`/run/coldaisle` を持つのは fand だけ。0080 §2.7）

### 2.8 worker の読み取り権限（POSIX ACL。0104 §2.2 / §2.3 と同じ形）

MPC worker のグループ `coldaisle-learn-mpc` に**読み取りだけ**を足す。所有者・グループ・mode は変えない。

| 対象 | ACL | 理由 |
|---|---|---|
| `/etc/coldaisle` | `g:coldaisle-learn-mpc:--x` | たどるだけ。`coldaisle.env`（秘匿情報）と `control-admin.yaml` は読めないまま |
| `/etc/coldaisle/control-config` と4ファイル | ディレクトリ `r-x`・default `r--`・ファイル `r--` | §1 の 2。frame の `config` との照合（0107 §2.6）。書けない（root の所有） |
| `/etc/coldaisle/learned-channel.yaml`（`root:coldaisle-fan`・`0640`） | `r--` | worker もソケットの path・`max_message_bytes` を読む |
| `/var/lib/coldaisle-registry` と中身 | `r-x`・default `r-X`（既にある中身には `-R` で `r-X`） | 0104 §5 の 6。`registry.json` と artifact を `inspect()` / `load_version()` で読む（lock を取らない・作らない） |
| `/var/lib/coldaisle-registry/.registry.lock` | **足さない** | worker は lock を使わない（0104 §2.4。制御は待たない） |

- `coldaisle-authority` には入れない（0104 §5 の 6。journal を書けてしまう）
- RL のグループ `coldaisle-learn-rl` への同じ ACL は、#89 で RL worker を動かすときに足す（いまは何も読めなくてよい）
- `/opt/coldaisle`（コード・`config/metrics.yaml`・`config/model-registry.yaml`）は root の所有で誰でも読める
  （`docs/ubuntu-deploy.md` 2 節）ので、足すものは無い
- ACL が外れたときの失敗は安全側である（worker が終了コード 5 で止まる、または artifact を読めず
  `model_unusable` / Fallback になる。0113）

### 2.9 有効化

- **どちらの worker の unit も、テンプレートを置くだけで `enable` しない。**
  - MPC worker: fand の有効化（0080 §2.10 段階 5・0028 §2.9 の承認点 3）の後に、所有者の判断で `enable` する。
    それまでは段階 5 の確認で `start` して、接続・heartbeat・`model_load_failure`（現行の artifact は
    `observational_replay`）を確かめるだけにする
  - RL worker: `--role supervisor` が終了コード 2 で拒まれる間（0077 段階 4 / #89 まで）は `enable` も `start` も
    しない。誤って起動しても `RestartPreventExitStatus=2` で1回で止まる
- `[Install]` は `WantedBy=multi-user.target`（有効にしたときの形を先に決めておく）

### 2.10 既存記録との関係

- **置き換える（Supersedes）ものは無い。**
  - 0080 §2.2 の `ExecStart=` の表は「引数を足す」だけで、既にある引数・`SupplementaryGroups=`・`RuntimeDirectory=` の
    値は変えない。0080 §2.7 の「`RuntimeDirectory=coldaisle` は fand の unit だけ」は守る（`tmpfiles.d` は unit では
    なく、同じ所有者と mode を宣言するだけ）
  - 0095 §2.7 の「共通の祖先は起動前に用意し、両方の役割のグループがたどれる」を、`/run/coldaisle`（`0711`）と
    `tmpfiles.d` で満たす。0095 の「親は役割のグループで 0750 として作られる」は fand が作る場合の挙動で、
    本番では親を先に `2750` で用意する（fand のコードは変えない）
  - 0104 §2.8 の「fand の unit の値は本記録では変えない」は 0104 の範囲の話で、`--registry-root` を足すのは
    0077 §2.6 が段階 6 に送った点である。必要な権限は 0104 §2.3 のとおり既にある
- **コードは変えない。** テンプレート（`deploy/`）・導入手順（`docs/ubuntu-deploy.md`）・試験
  （`tests/test_deploy_templates.py`）だけで行う

### 2.11 実装（本記録の承認の後、#57 の PR）

| 対象 | 内容 |
|---|---|
| `deploy/systemd/coldaisle-learnd-mpc.service` / `coldaisle-learnd-supervisor.service` | §2.1・§2.4〜§2.7・§2.9 |
| `deploy/tmpfiles.d/coldaisle-learned.conf` | §2.2 の3行 |
| `deploy/systemd/coldaisle-fand.service` | §2.3 の3つの引数と `--t-sensor-metric` のコメント |
| `docs/ubuntu-deploy.md` | 新しい節（ユーザー・グループ・`tmpfiles.d`・`learned-channel.yaml` の置き場所と path・ACL・`systemd-analyze verify`・unit と同じ資格の `systemd-run` での読み取りの確認・RL のユーザーが MPC のディレクトリをたどれないことの確認）。「まだ含めないもの」と 6.2 の段落の更新 |
| `tests/test_deploy_templates.py` | §2.1〜§2.7 の静的試験（下） |

試験すべき性質（実機不要。CI に `systemd-analyze` は無いので静的に読む）:

- worker の unit が2つあり、`ExecStart` が `/opt/coldaisle/.venv/bin/coldaisle-learnd`（`pyproject.toml` の入口）で、
  `--role` がそれぞれ `mpc` / `supervisor`。2つの unit の差が役割・`User=`・`Group=`・`Description=` だけであること
- `User=` / `Group=` が `learned-channel.yaml` の役割のグループと同じで、互いに違い、`coldaisle`・`coldaisle-fan`・
  root でないこと。`SupplementaryGroups=` が無いこと。fand の `SupplementaryGroups=` に worker のグループが無いこと
- `--config-dir`・`--registry-root`・`--learned-channel-config` と `WorkingDirectory=` が fand と同じで、
  `--metrics` / `--registry-limits` を渡さない（既定を共有する）こと。worker に `--calibration` が無いこと
- `Restart=on-failure`・`StartLimitIntervalSec=0`・`RestartPreventExitStatus` が `{2, 5}` ちょうどで、その値が
  `coldaisle.learned_worker.cli` の定数と一致すること
- worker の unit に `Requires=` / `BindsTo=` / `PartOf=` / `Requisite=` / `Wants=` が無く、fand の unit が worker を
  参照しないこと
- `ProtectKernelTunables=yes`・`PrivateDevices=yes`・`RestrictAddressFamilies=AF_UNIX`・`ReadWritePaths=` 無し・
  `RuntimeDirectory=` / `StateDirectory=` 無し・資源の上限のキーがあること
- `tmpfiles.d` の行: `/run/coldaisle` が fand の `User=` / `Group=` / `RuntimeDirectoryMode=` と同じ、役割の
  ディレクトリが `2750` で fand の `User=` と `learned-channel.yaml` のグループ、`/run/coldaisle` の下で互いに別
- fand の `ExecStart` に `--calibration` / `--registry-root` / `--learned-channel-config` があり、`--t-sensor-metric` が無いこと

## 3. Consequences

### 良くなること

- 本番の配置で Learned の経路が開く（fand をグループに入れたときの重なりの拒否を避ける）
- 役割の境界が `SO_PEERCRED` とファイル権限の二重になる（RL のユーザーは MPC のソケットまでたどれない）
- 較正の掛かる artifact が、テンプレートどおりの配備でも使える（0101 §2.6 (b)）
- worker は hwmon・DB・authority の journal・管理ソケットに、権限でも unit でも届かない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| Learned を配備しない導入先でも、fand の起動のたびに registry・`learned-channel.yaml` を読めない旨のログが出る | 運転は変わらない。気になるなら導入先の unit から2つの引数を外す（手順書に書く）。代替は §4 の B |
| ディレクトリを作る仕組み（`tmpfiles.d`）が unit の外に1つ増える | テンプレートと試験で fand の unit と値を照らす。食い違っても `channel_disabled` で止まるだけ |
| setgid の親で「同じ gid への chown」が通ることに依る | Linux の規則（所有者は自分のファイルを、いまと同じ gid へ付け替えられる）。0080 段階 5 の実機の確認に「Learned の経路が開く」ことを足す |
| fand の停止中、worker が5秒ごとに終了コード 3 で再起動を繰り返す | Fallback に倒れるだけ。頻度が問題なら `RestartSteps=` / `RestartMaxDelaySec=`（systemd 254 以降）で間隔を延ばす（§5 の 7） |
| 資源の上限が実測前 | すべて暫定値と書き、段階 3 の shadow で測る |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| **A. fand を両方の worker のグループに入れ、重なりの検査から fand の uid を除く**（コードの変更） | 0077 §2.7 の検査の文言（所属する uid の集合）を変える記録が要る。fand が worker の読める artifact まで読めるようになる（ML を制御プロセスへ入れない、の権限の側が弱まる）。設定の誤りで fand の uid が worker の役割を持つ形を許す |
| **B. Learned の引数と準備を drop-in（`coldaisle-fand.service.d/learned.conf`）に分ける** | `ExecStart=` を drop-in で置き換えると本体と2か所に同じ行ができ、片方だけ直す取り違えが起きる。失敗が安全側なので本体に入れても失うものが小さい |
| **C. fand の unit に `ExecStartPre=+/usr/bin/install -d -g <役割のグループ> -m 2750 …`（root）** | `/run/coldaisle` は fand のユーザーの所有なので、fand のユーザーが同じ名前の symlink を先に置くと、root の `install` が任意の path の所有者と mode を変えてしまう。`systemd-tmpfiles` は path の途中の所有者の変化と symlink を拒む |
| **D. `/run` の直下に役割ごとのディレクトリ（`/run/coldaisle-learn-mpc` など）** | `RuntimeDirectory=` との関わりが無くなるが、0095 §2.7 と導入手順が「`RuntimeDirectory=` の下」と書いており、管理ソケットと置き場所が分かれる。§5 の 3 の次善 |
| **E. 1つのインスタンス・テンプレート `coldaisle-learnd@.service`** | §2.1 |
| **F. worker の unit に `Wants=coldaisle-fand.service`** | worker の起動で Fan 制御が始まる。fand の有効化は 0080 段階 5 の後 |
| **G. worker を `coldaisle-authority` に入れて 0104 の ACL を共有する** | 0104 §5 の 6 が却下済み（journal を書けてしまう） |

## 5. 未決事項（所有者の判断を待つ点。各項の先頭が推奨案）

| # | 判断点 | 推奨案 | 代替案 |
|---|---|---|---|
| 1 | fand と worker のグループの関係（§1 の 1） | fand を worker のグループに入れず、setgid の親で役割のグループを継がせる（§2.2） | §4 の A（コードを変えて fand の uid を除く） |
| 2 | 役割のディレクトリを作る仕組み | `tmpfiles.d`（§2.2） | §4 の C（root の `ExecStartPre`）・導入手順で手で作る（OS の再起動で消える） |
| 3 | ソケットの path | `/run/coldaisle/learned-mpc/mpc.sock`・`/run/coldaisle/learned-rl/supervisor.sock`（0711 の `RuntimeDirectory=` の下） | §4 の D |
| 4 | fand の `ExecStart` | `--calibration`・`--registry-root`・`--learned-channel-config` を常に入れ、`--t-sensor-metric` はコメント（§2.3） | §4 の B（drop-in）・Learned の2つもコメントにする |
| 5 | unit の分け方と名前 | 役割ごとの2ファイル `coldaisle-learnd-mpc` / `coldaisle-learnd-supervisor`（§2.1） | §4 の E |
| 6 | 実行ユーザー | 役割のグループを主グループに持つ専用ユーザー `coldaisle-learn-mpc` / `coldaisle-learn-rl`（§2.1） | 1つの worker ユーザーを補助グループで分ける（0077 §2.1 の「別のユーザー」に反する） |
| 7 | `Restart` | `on-failure`・`RestartSec=5`（暫定）・`StartLimitIntervalSec=0`・`RestartPreventExitStatus=2 5`（§2.5） | `RestartSteps=` での伸長・`Restart=always` |
| 8 | 資源の上限 | §2.6 の暫定値 | 上限を置かない（片方の暴走が fand を巻き込む） |
| 9 | 依存 | `After=coldaisle-fand.service` だけ（§2.5） | §4 の F・`PartOf=`（fand の停止で worker も止まる。終了コード 3 で同じことが起きるので要らない） |
| 10 | worker の制御設定の読み取り（§1 の 2） | 0104 §2.2 と同じ形の ACL を役割のグループに（§2.8） | fand が frame で設定の中身を運ぶ（0107 の照合の意味が変わる。別の記録が要る） |
| 11 | RL のグループ・ディレクトリ・ユーザーをいつ作るか（§1 の 3） | グループとディレクトリはいま（空のグループ）、ユーザーと ACL は #89 で（§2.2 / §2.8） | すべて #89 まで作らない（その間 MPC を含む経路全体が開かない） |
| 12 | 有効化 | どちらも `enable` しない。MPC は fand の有効化の後に所有者が判断、RL は #89 の後（§2.9） | MPC だけ先に `enable`（fand が無効の間は終了コード 3 を繰り返すだけ） |

### 実測を待つ値（`status: provisional` に相当。テンプレートに「暫定値」と書く）

`RestartSec=`・`MemoryMax=`・`TasksMax=`・`CPUWeight=` / `IOWeight=`・`Nice=`・`OOMScoreAdjust=`。
段階 3 の shadow（0077 §3）と、0080 段階 5 の実機の確認で決める。
