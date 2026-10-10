# 決定記録 0080: `coldaisle-fand` の systemd unit（deadman・引き継ぎ・権限・順序）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-30、リポジトリ所有者が承認）
- **Date**: 2026-09-30
- **Supersedes**:
  - [`0060`](0060-control-loop-runtime.md) §2.7「deadman が『ある』と言える条件」の表の4行目
    （`WATCHDOG_USEC` と `watchdog_timeout_ms` が違うときは warning で環境側を採る）のうち、
    **環境側が長い場合のみ**（§2.3 で起動拒否に置き換える）
  - [`0028`](0028-fan-control-contracts.md) §2.7「正常停止」の条件（SIGTERM かつ `SERVICE_RESULT=success`）のうち、
    **引き継ぎ実行部が正常停止を見分ける方法のみ**（§2.8 で「引き継ぎ記録の有無」に置き換える。
    戻す・戻さないの規則と通知は変えない）
  - [`0060`](0060-control-loop-runtime.md) §2.7「起動時の失敗は、種類ごとに報告する」の表の
    「deadman が使えない（通知先・時間切れ・送信のいずれか）→ 終了コード 4」のうち、
    **通知の I/O の失敗（socket を作れない・送信できない）のみ**（§2.4 で再起動する終了コード 6 に分ける）

  旧記録側への `Superseded by` の追記は、本記録を FINAL にした PR で行った（所有者の選択 9 (a)。下の「所有者の選択」）
- **Superseded by**: [0083](0083-fand-exit-settled-points.md)（§2.4 の「運転中の `WATCHDOG=1` の送信の失敗は未捕捉の例外として終了コード 1」のみ。実装どおり捕まえて運転を続け、止めるのは deadman に任せる。他の点は有効） / [0086](0086-authority-approver-uid.md)（§2.1 の `authority.json` の置き場所（`/var/lib/coldaisle-fand` の下）と、§2.2 の表の `ExecStart=` の `--authority-root` の値・`SupplementaryGroups=`・`ReadWritePaths=` のみ。journal を承認者のグループと共有する専用のディレクトリへ移す。他の点は有効） / [0118](0118-hwmon-backend-label-less-headers.md)（§2.5 の記録の検査のうち、実行部が書き込み先を書く時点で探し直すこと・`label: null` の照合・書ける値が `pwmN_enable=0` と `pwmN=255` になること、§2.7 の書き手の4の照合に `pwm_attribute` / `enable_attribute` を足すことのみ。他の点は有効）
- **関連**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.2 / §2.6 / §2.7 / §2.8 / §2.9、未決 6 / 7 /
  [`0060-control-loop-runtime.md`](0060-control-loop-runtime.md) §2.1 / §2.7 / §2.9、未決 1 / 7 /
  [`0069-ubuntu-deploy-templates.md`](0069-ubuntu-deploy-templates.md) §2.2 / §2.3 / §2.4、未決 3 /
  [`0072-control-admin-entry.md`](0072-control-admin-entry.md) §2.5 / §2.8、未決 3 /
  [`0045-local-socket-write-entry.md`](0045-local-socket-write-entry.md) §2.2 / §2.3、未決 4 /
  [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md) /
  [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md) §2.1 /
  [`0021-public-repo-hygiene.md`](0021-public-repo-hygiene.md) /
  `docs/critical-safety.md`「deadman / 異常停止」/ `docs/fan-hardware-backend.md` / `docs/ubuntu-deploy.md` /
  AGENTS.md「絶対に守るルール」1〜4・6・7・9・10
- **対象 Issue**: #57（Ubuntu 常駐化）/ #78（Critical Safety の deadman と引き継ぎの配線）
- **所有者の選択（2026-09-30。同日の最終レビューで承認）**: 所有者は PR の「所有者に判断してほしい点」1〜13 の
  **すべてで推奨案（(a)）を選び**、最終レビュー（97d7470）で残った2つの指摘を推奨の方向で直すよう求めた
  （§2.4 の終了コード 6 / 7、§2.6「書き込みが続けて失敗したら終わる」）。**修正後の本記録を所有者が
  もう一度読んでから承認する**ので、Status は Proposed のままとする。新しい設定値
  `hardware_write_fail_exit_ms`（§2.6）は暫定値であり、実機で使う前に導出と承認が要る

---

## 1. Context

0028 は deadman を**制御プロセスの外の systemd watchdog** に置き、異常終了時は
`ExecStopPost` の引き継ぎ実行部（`coldaisle-safety-handoff`）が Max を書くと決めた。
0060 §2.7 は heartbeat を出す側（`--require-watchdog`、`READY=1` / `WATCHDOG=1` の固定 datagram、
`WATCHDOG_USEC` の検証）を決めた。引き継ぎ実行部は #78 で実装済みで、
固定の `/run/coldaisle/fan-handoff.json` と `/sys/class/hwmon` だけを読む。

しかし、それらを**つなぐ unit の中身**が決まっていない。

| 出典 | 未決 |
|---|---|
| 0060 未決 7 | `coldaisle-fand` の unit（`Type=notify` / `WatchdogSec` / `Restart` / `ExecStopPost`）の中身 |
| 0069 §2.4 / 未決 3 | fand の unit は安全系の人の判断が要るとして #57 の範囲から外した |
| 0072 未決 3 | fand の実行ユーザー・管理グループ名・`RuntimeDirectory`・`authority.json` のディレクトリの所有者 |
| `docs/critical-safety.md` | 「`watchdog_timeout_ms` の consumer、`WatchdogSec`、`ExecStopPost` unit、handoff record producer が無い」「統合と実機検証までサービスで Fan 制御を有効化しない」 |

unit の書き方次第で、決めた安全の性質が**黙って崩れる**。main のコードと systemd の仕様から、
少なくとも次が起こりうる。

| 書き方 | 起きること |
|---|---|
| `ProtectKernelTunables=yes`（よくある hardening の定番） | `/sys` が読み取り専用になり、hwmon へ書けない。takeover も引き継ぎも失敗する |
| fand の unit に `StateDirectory=coldaisle` | systemd が `/var/lib/coldaisle` の所有者を fand のユーザーへ付け替え、取り込み・API が DB を書けなくなる |
| 複数の unit が `RuntimeDirectory=coldaisle` | 片方の停止で `/run/coldaisle` が消え、**走っている fand の引き継ぎ記録が消える**。異常終了しても Max にならない |
| `NotifyAccess=all` | 将来の worker（M9）が `WATCHDOG=1` を送れ、main loop が hang しても deadman が鳴らない |
| `KillMode=process` / `none` | 子プロセスが残ったまま `ExecStopPost` が走り、「書き手が同時に2つにならない」（0028 §2.7）が崩れる |
| fand が取り込みに `Requires=` / `BindsTo=` / `PartOf=` | 取り込みの停止・再起動で**冷却の制御まで止まる**。入力の欠測は Critical Safety が安全側へ倒す設計（0028 §2.7）なのに、その前に制御ごと消える |
| `RestartPreventExitStatus=1` | Python の未捕捉例外の終了コードも 1 なので、Critical Safety の例外（0028 §2.7「捕まえない」）で落ちた後に**再起動しない** |
| `WatchdogSec` が `safety.yaml` の `watchdog_timeout_ms` より長い | 所有者が承認した deadman より遅い deadman で運転する。0060 §2.7 は warning だけで起動を続ける |
| `ExecStopPost` の実行部が「正常停止か」を `SERVICE_RESULT` で判断する | fand が元の自動制御へ戻さずに 0 で終わった不具合で、PWM が最後の低い値のまま残る |

---

## 2. Decision

**この記録が決めるのは方針である。** unit と udev のテンプレートは `deploy/` に仮の値で置き
（0069 §2.2 と同じ）、値は `status` を持つ設定と同じく「暫定」として扱う。テンプレートを置いても
**サービスとして有効化（`systemctl enable`）するのは 0028 §2.9 の承認点 3 の後**である（§2.10）。

### 2.1 ユーザーとグループ（名前はすべて仮の値）

| 名前（仮） | 何か | 誰が入る |
|---|---|---|
| `coldaisle-fan` | **fand 専用のユーザーと主グループ**。hwmon の対象属性の書き込み権限（§2.6）はこのグループに渡す | fand だけ |
| `coldaisle-admin` | 管理ソケットのグループ（0072 §2.5。`config/control-admin.yaml` の仮の値と同じ） | **fand（ソケットの所有のため）** と操作する人だけ。**`coldaisle`（API / 取り込み）と AI 層のユーザーは入れない** |
| `coldaisle` | 既存の取り込み・API・rollup・report のユーザー（0069） | 変えない |

- **fand を `coldaisle` と同じ uid で動かさない。** 0072 §2.5 の配置の必須条件（API / AI サーバと同じユーザーにしない）
- **fand を root で動かさない。** 書き込みの権限は hwmon の対象属性だけに絞る（§2.6）
- fand は `SupplementaryGroups=coldaisle coldaisle-admin` を持つ。
  - `coldaisle`: DB（`/var/lib/coldaisle`）を読み書きし、`/etc/coldaisle` を読む。
    decision trace（0030）と管理操作の監査（0072 §2.7）は同じ DB に書くためである
  - `coldaisle-admin`: 管理ソケットは bind の後に `os.chown(path, -1, <socket.group の gid>)` で
    グループを付け替える（`control_admin/server.py`）。capability を持たない非 root のプロセスは
    **自分が属するグループにしか付け替えられない**ので、所属が無いと管理ソケットの起動が EPERM で失敗する。
  - **ただし、いまの認可はこの所属で fand 自身の uid を通してしまう。** `local_socket.Authorizer.allows()` は
    `allow_same_user: false` のとき同じ uid の自動の許可を飛ばすだけで、その後のグループの判定
    （`pw_gid` / `gr_mem`）で fand の uid を認める。このままでは `coldaisle-fan` で動く任意のプロセスが
    `manual` / `max` を送れ、0072 §2.5 の「同じ uid も暗黙には認めない」と 0076 §2.6 の出荷設定の意図に反する
  - そこで本記録は、**fand を `coldaisle-admin` に入れる前提として**、`allow_same_user: false` のとき
    `uid == server_uid` をグループの判定より前に拒否することを求める（段階 1 の前提。§2.10 の段階 0）。
    この変更は 0072 §2.5 の意図をコードに合わせるもので、`allow_same_user: true` の開発用設定と
    eventd（0045、既定 `true`）の挙動は変えない。所有者はこの方式（判断点 13 の (a)）を選んだ
    （2026-09-30。同日に承認）。fand を `coldaisle-admin` に入れない方式は §4 の A2 として残す
- **DB のディレクトリをグループで共有できるようにする**（既存の unit も変える。§2.10 の段階 1）。
  0069 のテンプレートは `StateDirectoryMode=0750` で、systemd はどれかの unit が起動するたびに
  `/var/lib/coldaisle` をこの mode に戻す。0750 ではグループに書き込み権が無く、fand は SQLite の
  `-wal` / `-shm` / `-journal` を作れない。作れても、setgid の無いディレクトリでは fand が作ったファイルの
  gid が主グループ（`coldaisle-fan`）になり、取り込みが開けなくなる（EACCES）。そこで:
  - `/var/lib/coldaisle` を使う**すべての unit**（daemon / api / rollup / report / fand）で
    `StateDirectoryMode=2770`（setgid 付き。どの unit の起動でも同じ mode に戻る）にそろえる。
    fand は `StateDirectory=coldaisle` を持たないので（§2.2）、mode を決めるのは既存の4つの unit である
  - DB を開く**すべての unit** に `UMask=0007` を付ける（SQLite は `-wal` / `-journal` を DB ファイルと同じ
    権限ビットで作るので、最初の DB ファイルがグループで書ける必要がある）
  - **`UMask` だけでは新しく作る DB ファイルがグループで書けるようにならない。** SQLite は DB ファイルを
    `0644` で作るので、`UMask=0007` を掛けても `0640` になり、setgid のディレクトリも gid しか与えない。
    新規の導入や DB を作り直した後に、fand が書けずに再起動を繰り返す。そこで **`SqliteStore` が DB ファイルを
    作るときは、`sqlite3.connect()` の前に `os.open(path, O_CREAT | O_EXCL | O_WRONLY, 0o660)` で空の
    ファイルを作ってから開く**（段階 1。既にあれば何もしない。`UMask=0007` のもとで `0660` になる）。
    `-wal` / `-shm` / `-journal` は SQLite が本体と同じ権限ビットで作るので、本体が `0660` なら揃う。
    試験は `tmp_path` で、umask `0007` のもとで新しく作った DB と、開いた後にできた `-wal` / `-shm` が
    `0660` であることを確かめる。DB を作るのは取り込み（daemon）が先でも fand が先でも同じ経路を通る
  - 段階 1 の静的試験で、既存の4つの unit（daemon / api / rollup / report）の `StateDirectoryMode` と `UMask` がこの値であることを確かめる（fand の `StateDirectoryMode` は `/var/lib/coldaisle-fand` の `0700`。§2.2 の表）。
    setgid が実際の systemd で保たれることは段階 5 で確かめる（保たれなければ下の代替へ移る）
  - **既存の DB ファイルは unit の変更だけでは変わらない。** `StateDirectoryMode` が変えるのはディレクトリの
    mode だけで、`UMask` はこれから作るファイルにしか効かない。0069 のテンプレートで動いてきた導入先や、
    `docs/ubuntu-deploy.md` の移行手順（いまは `install -o coldaisle -g coldaisle -m 0640`）で置いた
    `coldaisle.db` は `0640` のままで、`coldaisle` を補助グループに持つだけの fand は書けない。
    fand は起動のたびに DB を開けず（終了コード 5）再起動を繰り返し、その間 tach の stall と deadman を
    見る者が居なくなる（BIOS の制御のままなので熱の面では安全側だが、監視が止まる）。そこで
    **fand を初めて起動する前に、既存の DB を移行する手順を置く**（段階 1 で `docs/ubuntu-deploy.md` に書く。
    下の「既存の導入先の移行」）。移行を忘れたときに原因がすぐ分かるよう、fand の起動時に DB へ書けるかを
    確かめ、書けなければ path・mode・gid を載せて報告する（段階 2。下の「起動時の書き込み確認」）

  **既存の導入先の移行**（fand を起動する前に1回。新規の導入でも、手順の途中で DB を置いたなら同じ）

  1. DB に触る unit とタイマーを全部止める（daemon / api / rollup / report。0069 の移行手順と同じ並び）。
     動いたままだと `-wal` / `-shm` が古い mode で作り直される
  2. 新しい unit（`StateDirectoryMode=2770`・`UMask=0007`）を置いて `systemctl daemon-reload` する
  3. `/var/lib/coldaisle` を `2770`・グループ `coldaisle` にし、`coldaisle.db` と、あれば
     `coldaisle.db-wal` / `coldaisle.db-shm` / `coldaisle.db-journal` を**すべて**グループ `coldaisle`・`0660` にする。
     `-shm` だけ `0640` で残ると、fand は WAL の索引を開けずに同じ失敗になる。setgid はこれから作る
     ファイルの gid にしか効かないので、既存のファイルの gid も明示して揃える
  4. 確かめる: 上のファイルとディレクトリの所有者・グループ・mode を `stat` で一覧し、fand のユーザーで
     書き込み権があることをディレクトリと各ファイルについて見る。1つでも違えば fand を起動しない。
     **確かめは unit と同じ主グループ・補助グループで行う。** fand が `coldaisle` グループを得るのは unit の
     `SupplementaryGroups=` からで、アカウントの所属からではない（§2.1。`coldaisle` には入れない）。
     `sudo -u <fand のユーザー> test -w` はアカウントのグループ一覧で別のプロセスを起こすので、
     unit だけが持つ `coldaisle` が欠け、実際の fand は書けるのに確かめだけが落ちる。そこで unit と同じ
     `User=` / `Group=` / `SupplementaryGroups=` を渡した一時的な unit で確かめる:
     `sudo systemd-run --pipe --wait --quiet -p User=coldaisle-fan -p Group=coldaisle-fan -p "SupplementaryGroups=coldaisle coldaisle-admin" test -w <対象>`
     （名前は §2.1 の仮の値。`systemd-run` は終了コードを返すので、0 以外なら起動しない）。
     アカウントを `coldaisle` グループに入れて `sudo -u` で済ませる方法は採らない（所属を増やすと unit の外でも
     DB を書けてしまう。変えるなら本記録を改める）
  5. 既存の unit とタイマーを戻し、取り込みが DB を書き続けていること（`-wal` / `-shm` が作り直されても
     グループ `coldaisle`・`0660` であること）をもう一度 `stat` で見てから、fand を起動する（`enable` は段階 5）

  移行の手順そのもの（コマンド）は段階 1 の PR で `docs/ubuntu-deploy.md` に書き、同じ PR で既存の移行手順の
  `install ... -m 0640` を `-m 0660` に直す（直さないと、手順どおりに DB を置き直すたびに同じ状態に戻る）。
  本記録には実機の path・ユーザー名を書かない（AGENTS.md ルール10。名前はすべて §2.1 の仮の値）

  **起動時の書き込み確認**（段階 2。takeover の前）

  - fand は DB を開いた直後、制御を取る前に、DB が書けることを確かめる。`os.access` で
    ディレクトリ・DB・あれば `-wal` / `-shm` の書き込み権を見たうえで、SQLite に実際に書き込みの lock を
    取らせる（`BEGIN IMMEDIATE` → `ROLLBACK`。行は書かない）
  - 書けない（`EACCES`・`SQLITE_READONLY` など、権限による失敗）ときは、専用の例外で
    **`db_not_writable` という event を構造化ログに出し**、書けなかった path・その mode・所有者と gid・
    fand の uid と補助グループを載せて、終了コード 5（制御を取っていない・再起動する）で終える。
    再起動するのは、人が権限を直せば次の起動でそのまま戻れるようにするため（`RestartPreventExitStatus` は
    `{3, 4}` のまま）。繰り返しの通知は §5 の 5 に含める
  - 取り込みの書き込みと重なった `SQLITE_BUSY` は権限の失敗と区別する（`busy_timeout` の範囲で待ち、
    それでも取れなければ lock を取れないだけとして別の文言で報告する。権限の問題と誤って報告しない）
  - 試験（実機不要。`tmp_path`）: DB を `0440`（書けない）にする・`-shm` だけを書けなくする・ディレクトリを
    書けなくする、の各場合に、制御を取らずに終了コード 5 で終わり、`db_not_writable` の event に該当の
    path が載ること。書ける場合は起動が続くこと。root で走る CI では `os.access` が常に真になるので、
    その場合は skip する（権限の試験であることを理由に書く）
  - 代替: fand の主グループを `Group=coldaisle` にし、`coldaisle-fan` を補助グループにする
    （setgid に頼らないが、fand のファイルが既定で `coldaisle` のグループになる）。所有者は判断点 11 で
    setgid の方式（(a)）を選んだ。この代替は段階 5 で setgid が保たれないと分かったときの移り先として残す
- `authority.json`（0057）は **`/var/lib/coldaisle` に置かない。** そこは `coldaisle` のグループが書けるので、
  API のユーザーが journal を書き換えられる。fand 専用の `StateDirectory=coldaisle-fand`
  （`/var/lib/coldaisle-fand`、所有者 `coldaisle-fan`、`0700`）の下に置き、`--authority-root` で指す。
  昇格を行う人がどう書くか（0072 未決 5）は #92 の段階 3 で決める
- 名前はテンプレートと手順書にだけ書き、**実機のユーザー名・ホスト名・絶対パスはコミットしない**（AGENTS.md ルール10）

### 2.2 unit の骨格

| 項目 | 決定 | 理由 |
|---|---|---|
| `Type=` | `notify` | 0060 §2.7。`READY=1` は fand が送る |
| `NotifyAccess=` | **`main` を明示** | 将来の worker や子プロセスが `WATCHDOG=1` を送れないようにする。main loop の hang を子が隠さない |
| `ExecStart=` | `/opt/coldaisle/.venv/bin/coldaisle-fand --require-watchdog --config-dir <root 所有の制御設定> --admin-config <root 所有の管理設定> --db /var/lib/coldaisle/coldaisle.db --authority-root /var/lib/coldaisle-fand/authority` | `--require-watchdog` で「deadman が無いなら制御を取らない」（0060 §2.7）。**制御の4ファイルは root 所有**にし、fand 自身が `safety.yaml` を緩められないようにする。実行ファイルは**導入先の venv の絶対パス**で書く。`uv sync` は `/opt/coldaisle/.venv/bin` に入口を置くだけで、systemd の実行ファイルの探索パスには入らず、`WorkingDirectory=` も探索には効かない（素の `coldaisle-fand` は `systemd-analyze verify` で `is not executable` になり、fand が `READY=1` に届かず deadman も tach 監視も動かない）。既存の daemon / rollup / report と同じ形 |
| `WorkingDirectory=` | `/opt/coldaisle`（0069 と同じ。root 所有） | — |
| `User=` / `Group=` / `SupplementaryGroups=` / `UMask=` | `coldaisle-fan` / `coldaisle-fan` / `coldaisle coldaisle-admin` / `0007` | §2.1 |
| `KillMode=` | `control-group`（既定。**明示する**） | `ExecStopPost` の前に cgroup の全プロセスを止め、書き手を1つにする（0028 §2.7） |
| `WatchdogSec=` | `safety.yaml` の `watchdog_timeout_ms` と**同じ値**（§2.3） | — |
| `Restart=` | `always` | 0028 §2.7 で決定済み。起動のたびに `STARTUP` の Max を通る |
| `StartLimitIntervalSec=` | `0` | 0069 §2.3 と同じ。諦めると tach の監視も Safety の裁定も止まる（§2.4） |
| `RestartPreventExitStatus=` | `3 4` | §2.4 |
| `ExecStopPost=` | `+/usr/bin/python3 -I -S /opt/coldaisle/src/coldaisle/safety_handoff.py`（引数なし） | §2.5 |
| `RuntimeDirectory=` | `coldaisle`、`RuntimeDirectoryMode=0711`、`RuntimeDirectoryPreserve=yes` | §2.7 |
| `StateDirectory=` | `coldaisle-fand` だけ（**`coldaisle` は書かない**） | §2.1。`coldaisle` を書くと所有者が付け替えられる |
| `StateDirectoryMode=` | `0700` | §2.1 の authority の状態ディレクトリ。書かないと systemd の既定の `0755` になり、他のローカルユーザーが中を読める |
| `ReadWritePaths=` | `/var/lib/coldaisle` | DB。`ProtectSystem=strict` の下で必要な分だけ開ける |
| 時間切れ | `TimeoutStartSec` / `TimeoutStopSec` / `TimeoutAbortSec` を**必ず明示**（値は §2.3 の式を満たす暫定値） | 既定値（90 秒）のままだと、止まらない fand を待つあいだ PWM が最後の値に残る |
| ログ | `StandardOutput=journal`、`SyslogIdentifier=coldaisle-fand` | 0069 と同じ |

### 2.3 `WatchdogSec` と tick の予算

**正本は `safety.yaml`**（0060 §2.1）。unit はそれを写すだけで、独自の値を持たない。

| 関係 | 誰が確かめる |
|---|---|
| `watchdog_timeout_ms >= (tick_ms + tick_deadline_ms) * 2` | `safety.yaml` の読み込み（0060 §2.1。実装済み） |
| `WATCHDOG_USEC >= (tick_ms + tick_deadline_ms) * 2` | fand の起動時（0060 §2.7。実装済み。満たさなければ終了コード 4） |
| **`WATCHDOG_USEC <= watchdog_timeout_ms`**（新規） | fand の起動時。**満たさなければ終了コード 4 で制御を取らない** |
| **`hardware_write_fail_exit_ms <= watchdog_timeout_ms`**（新規。§2.6） | `safety.yaml` の読み込み |
| `WATCHDOG_USEC < watchdog_timeout_ms` | 起動はする（より厳しい deadman）。warning を残す（0060 のまま） |

- 環境側が**長い**と、所有者が承認した値より遅い deadman で運転することになる。0060 §2.7 は
  「実際に効くのは環境側」として warning で続けるが、それは安全でない側への食い違いを
  運用者の目視に任せることになる。**長い向きだけを起動拒否に締める**（Supersedes 欄）。
  拒否は takeover より前である。止まった状態は2通りになる。同じ OS の起動のあいだに以前の実行が
  制御を取っておらず引き継ぎ記録が無ければ **BIOS の制御（Safety-0）のまま**、以前の実行の記録が
  `RuntimeDirectoryPreserve=yes`（§2.7）で残っていれば、この実行の `ExecStopPost` も Max・manual を書くので
  **Max のまま**になる（§2.4 の最後の箇条書き）。どちらも安全側である
- `sd_notify` の `WATCHDOG_USEC=` で fand が値を設定し直す方式は採らない（§4 の W3）。
  0060 §2.7 の「固定 datagram だけ」を崩す
- hang から Max までの最悪時間は
  **`WatchdogSec` + `TimeoutAbortSec` + 引き継ぎ実行部の実行時間**になる（§2.5）。
  この合計を decision trace ではなく**手順書と承認記録**に残し、#50 の熱の時定数と比べて承認する（§5）
- `TimeoutStopSec` は `tick_ms + tick_deadline_ms` と正常停止の返却（§2.8）の合計より長くする。
  超えれば SIGKILL になり、`ExecStopPost` が Max を書く（安全側）

### 2.4 再起動と終了コード

0060 §2.7 の終了コードに対して、次のように扱う。

| 終了 | 制御を取ったか | 再起動 | 理由 |
|---|---|---|---|
| 0（SIGTERM で正常停止） | 返した（§2.8） | しない（`systemctl stop` のとき） | — |
| 1（`config_invalid` で Max を書いた） | 取った | **する** | Python の未捕捉例外（Critical Safety・合成の例外）も 1 で終わる。区別できないので止めない。再起動のたびに Max を書き直す |
| 2（`fan-hardware.yaml` が不正・header を特定できない） | 取っていない | **する** | 起動直後に hwmon ドライバが未ロードなど、一時的な原因がありうる。再試行しても制御を取らないので害が無い |
| 3（hardware mapping が `provisional`） | 取っていない | **しない** | 承認点 3 が要る。人が変えるまで結果は変わらない |
| 4（deadman が**恒久的に**使えない: `NOTIFY_SOCKET` / `WATCHDOG_USEC` が無い・不正・短すぎる・§2.3 の不一致） | 取っていない | **しない** | unit か `safety.yaml` の食い違いで、人が直すまで変わらない。**4 はこの意味だけに使う** |
| 5（DB・Metric Catalog など制御設定以外） | 取っていない | **する** | 取り込みが DB を作る前など、一時的でありうる |
| **6（新規。通知の I/O の失敗: 通知用 socket を作れない・`READY=1` を送れない）** | 取っていない | **する** | fd の枯渇・systemd 側の一時的な不調など、同じ設定で次は通りうる。4 に混ぜると `RestartPreventExitStatus` で**一時的な失敗のまま制御が戻らない**。起動時の `READY=1` の送信は takeover の前なので、制御は取っていない |
| **7（新規。takeover 後に hwmon へ書けない状態が `hardware_write_fail_exit_ms` 続いた。§2.6）** | 取った | **する** | 記録を消さずに終わるので、`ExecStopPost` が root で Max を書いてから再起動する。再起動の takeover がまた失敗すれば同じことを繰り返し、Max に重なる |
| シグナル（SIGKILL・watchdog の SIGABRT・OOM） | 取った | **する** | `ExecStopPost` が Max にしてから再起動する |

- `StartLimitIntervalSec=0` で**諦めない**。諦めた unit は最後の `ExecStopPost` の Max で止まるので
  熱の面では安全側だが、tach の stall を見る者も、`pwmN_enable` を外から戻されたことに気づく者も居なくなる。
  再起動の繰り返しそのものの通知は §5
- **運転中の `WATCHDOG=1` の送信の失敗**は、いまの実装では未捕捉の例外として終了コード 1 になり、再起動する
  （0060 §2.7。制御を取った後なので `ExecStopPost` が Max を書く）。6 に寄せるかは未決 7 と一緒に #74 で決める
- `RestartPreventExitStatus` は `3 4` のまま変えない（6・7 を入れない）。段階 1 の試験は `{3, 4}` ちょうどを確かめる
- `RestartPreventExitStatus` で止まった場合も、直前に制御を取っていた実行があれば、その `ExecStopPost` が
  Max を書いて記録を残している（§2.7）。止まった状態は **Max のまま**で、低い値のままにはならない

### 2.5 watchdog で殺されたとき・`ExecStopPost`

**順序**: 時間切れ → `WatchdogSignal`（SIGABRT）→ `TimeoutAbortSec` で SIGKILL →
cgroup の残りを停止（`KillMode=control-group`）→ `ExecStopPost`（引き継ぎ実行部）→ `RestartSec` → 再起動（`STARTUP` の Max）。

| 項目 | 決定 | 理由 |
|---|---|---|
| `WatchdogSignal=` | `SIGABRT`（既定のまま） | fand が起動時に `faulthandler` を有効にし、**全スレッドの traceback を journal へ残して**終わる。hang の原因を後から追える |
| `LimitCORE=` | `0` | core dump の書き出しが終わるまで process が残り、`ExecStopPost` の Max が遅れる |
| `TimeoutAbortSec=` | 明示する（暫定値。§5） | SIGABRT で終わらない場合の上限。これを過ぎたら SIGKILL |
| 実行部の権限 | **`+` 接頭辞（root、sandbox なし）** | fand の書き込み権限（§2.6）が失われていても Max を書ける。最後の防衛線の書き込みを、防衛対象と同じ権限の失敗に巻き込まない |
| 実行部の起動方法 | `/usr/bin/python3 -I -S` で**ソースファイルを直接**実行。引数を渡さない | venv と `coldaisle` パッケージに依存しない（0028 §2.7「壊れた環境でも動く」）。`-I` で環境変数と利用者の site を無視し、`-S` で site-packages を読まない。コードは root 所有の `/opt/coldaisle` にあり、fand のユーザーは書き換えられない |
| 正常停止かの判断 | **記録の有無だけで決める。`SERVICE_RESULT` / `EXIT_STATUS` を読まない** | §2.8。実行部は「記録があれば Max、無ければ何もしない」（実装済み）のまま変えない |
| 実行部の失敗 | 非0で終わる（実装済み）。journal に zone ごとの結果が残る | 通知の配線は §5 |

- 実行部が root で読む入力は fand のユーザーが書いた記録である。実装済みの検査（`O_NOFOLLOW`、上限サイズ、
  `hwmonN/<属性>` の形、driver 名と label の照合、3 zone が別の header）により、記録を細工されても
  **書けるのは hwmon の PWM を Max・manual にすることだけ**で、冷却を弱める方向の値は表現できない。
  fand のユーザーは対象の header をもともと書けるので、増える力は「他の hwmon の PWM を全開にする」だけに留まる
- **ドライバの中で止まる hang（D 状態）** は SIGKILL でも終わらず、実行部も同じドライバで止まりうる。
  0028 §2.7 の「`ExecStopPost` が動かない」と同じ残るリスクとして扱う（0028 未決 6）

### 2.6 hardening（hwmon への書き込みと両立させる）

| 項目 | 決定 | 理由 |
|---|---|---|
| `ProtectKernelTunables=` | **`no` を明示**（テンプレートの試験で `yes` を禁止する） | `yes` は `/sys` を読み取り専用にする。hwmon へ書けなくなる |
| `ProtectSystem=` | `strict` | `/sys` は対象外なので hwmon と両立する。書けるのは `ReadWritePaths` と State / Runtime のディレクトリだけ |
| `PrivateDevices=` | `yes` | fand は `/dev` を開かない。**シリアルを開くのは取り込みだけ**（AGENTS.md ルール6）を構造で守る |
| `RestrictAddressFamilies=` | `AF_UNIX` | sd_notify と管理ソケットだけ。TCP を持たないので読み取り API や LLM の経路にならない（ルール1） |
| `IPAddressDeny=` | `any` | 同上 |
| `CapabilityBoundingSet=` / `AmbientCapabilities=` | 空 | 非 root で、書き込みはファイルの権限だけで行う |
| その他 | `NoNewPrivileges` `ProtectHome` `PrivateTmp` `ProtectKernelModules` `ProtectKernelLogs` `ProtectControlGroups` `ProtectClock` `ProtectHostname` `RestrictNamespaces` `RestrictSUIDSGID` `LockPersonality` `SystemCallArchitectures=native` を `yes` | 一般的な hardening。どれも `/sys` の書き込みに影響しない |
| 入れない | `MemoryDenyWriteExecute`、`SystemCallFilter` の絞り込み | worker（M9）の数値ライブラリと両立するか未確認（§5） |
| `ProtectKernelModules=yes` の帰結 | hwmon のドライバは fand が読み込まず、`modules-load.d` で起動時に読み込む | fand にカーネルモジュールを読み込む力を与えない |

**hwmon の書き込み権限は udev で渡す。** `deploy/udev/` に hwmon 用のテンプレートを足し、
`SUBSYSTEM=="hwmon"` と driver 名で chip を選び、**対象 zone の `pwmN` と `pwmN_enable` だけ**の
グループを `coldaisle-fan`、権限を `0664` にする。udev の `GROUP=` / `MODE=` は `/dev` のデバイスノード
にしか効かず、hwmon には `/dev` のノードが無いので、**sysfs の属性を `RUN+=` で直接変える**
（例: `RUN+="/bin/chgrp coldaisle-fan /sys%p/pwmN /sys%p/pwmN_enable"` と
`RUN+="/bin/chmod 0664 /sys%p/pwmN /sys%p/pwmN_enable"`）。段階 1 の試験で、テンプレートがこの形であり
`GROUP=` / `MODE=` に頼っていないことを確かめる。権限が付かなかった場合、fand の takeover は EPERM で失敗するが、
記録を先に書く順序（§2.7）なので `ExecStopPost` が Max を書く（安全側）。driver 名と channel 番号は仮の値
（`REPLACE-WITH-DRIVER-NAME` など）で置き、実機の値は導入先の `/etc/udev/rules.d/` にだけ書く（0069 §2.2 と同じ）。
ドライバの再 bind で属性が作り直されると権限は元に戻る。そのとき `pwmN_enable` がドライバの既定
（通常は自動制御）に戻る保証は無く、**manual の低い PWM のまま fand だけが書けなくなる**ことがありうる。
fand は書き込み失敗を 0028 §2.7 の fault として扱い（Top なら `EMERGENCY`）、それが続けば次の規則で終わる。

#### 書き込みが続けて失敗したら終わる（特権の引き継ぎに任せる）

takeover の後、fand の中の `EMERGENCY` は「Max を**求める**」ことしかできない。書き込みそのものが
失敗していれば、求めた Max は hwmon に届かない。一方で loop は tick を回し続けるので heartbeat は止まらず、
`ExecStopPost`（root で Max を書く最後の防衛線）も走らない。これを次のように閉じる。

- **いずれかの zone で、書き込みの失敗・読み戻しの不一致・`pwmN_enable` が外から戻されたこと
  （0028 §2.7 の Fan / Hardware の表の fault）が、その zone の書き込みと読み戻しが一度も成功しないまま
  `hardware_write_fail_exit_ms` の間続いたら、fand は終了コード 7 で終わる。** takeover の書き込み
  （`pwmN_enable` を manual へ切り替える・最初の Max）の失敗も同じく数える
- 終わるときは**§2.8 の返却をしない**（BIOS へ戻す書き込みも同じ理由で失敗しうる）。引き継ぎ記録を消さずに
  終わり、`ExecStopPost` が root・sandbox なしで Max・manual を書く。その後 `Restart=always` で起動し直し、
  takeover からやり直す
- 期間は**連続**で数える。その zone の書き込みと読み戻しが1回成功すれば数え直す。
  `write_fail_emergency_after`（回数。fand の中で `EMERGENCY` へ上げる）は変えず、その**後ろの段**として働く
- 判定は Critical Safety の fault の結果（decision trace の `write_ok` / `readback_ok`。0028 §2.2）から
  決定論的に行い、ML に依存しない（AGENTS.md ルール3）。decision trace の形は変えない（`ControlTick` の版を上げない）
- simulated backend では書き込みの失敗を注入して、終了コード 7 と「記録を消さない」ことを実機なしで確かめる（§2.10 の段階 2）

**新しい設定値 `hardware_write_fail_exit_ms`**（`config/safety.yaml`。コードに置かない。AGENTS.md ルール9）

| 項目 | 決定 |
|---|---|
| 置き場所 | `safety.yaml` の最上位、`watchdog_timeout_ms` の隣。ほかの Safety の値と同じ `{value, status, basis}` の形。追加に合わせて `safety.yaml` の `schema_version` を 3 から 4 へ上げ、v3 を v4 として補完しない（0028 §2.8・`docs/control-config.md` の既存の移行の規則と同じ） |
| 不変条件（読み込み時に検証） | `(tick_ms + tick_deadline_ms) * write_fail_emergency_after <= hardware_write_fail_exit_ms <= watchdog_timeout_ms` |
| 下限の理由 | fand の中の再試行と `EMERGENCY` への昇格（`write_fail_emergency_after` 回）を先に試す。一時的な書き込みの失敗1回で再起動しない |
| 上限の理由 | 「書けないまま制御を持ち続ける」時間を、所有者が承認する hang の deadman（`watchdog_timeout_ms`）より長くしない。書けない fand は、冷却の面では hang した fand と同じだからである。これで書き込みの失敗から Max までの最悪時間は、§2.3 の hang の最悪時間（`WatchdogSec` + `TimeoutAbortSec` + 実行部の実行時間）の内に収まる |
| 暫定値 | **`value` = その `safety.yaml` の `watchdog_timeout_ms` と同じ値、`status: provisional`、`basis` なし。** 上限いっぱいに置くのは、新しい数を作らず、承認待ちの既存の値に揃えるためである。**仮の値であり、実機で使う前に #50 の熱の時定数と #75 の書き込み・読み戻しの失敗の実測から導き、所有者の承認（0028 §2.9 の承認点 2）を経て `confirmed` にする** |
| 承認前の扱い | ほかの `provisional` の Safety の値と同じく `provisional_values()` に並べ、起動時の一覧に出す。`confirmed` になるまで unit を `enable` しない（§2.10 の段階 5） |

### 2.7 引き継ぎ記録の置き場所と書き手の配線

- 置き場所は実行部が固定で読む `/run/coldaisle/fan-handoff.json`。**`/run/coldaisle` は fand の unit だけが
  `RuntimeDirectory` として持つ。** 取り込み・API・`coldaisle-eventd` などほかの unit は別の名前を使う
  （テンプレートの試験で、`RuntimeDirectory=coldaisle` が fand の unit にしか無いことを確かめる）
- `RuntimeDirectoryPreserve=yes`。記録の寿命を systemd の後片付けに任せない。記録を消すのは
  fand の正常停止（§2.8）だけで、`/run` は再起動（OS）で必ず空になる
- `RuntimeDirectoryMode=0711`。管理ソケット（`0660`、グループ `coldaisle-admin`）は other の `x` で
  たどれ（0045 §2.2 の親ディレクトリの検査を満たす）、一覧は見せない。記録は `0600`（fand のユーザーと root だけが読む）
- 管理ソケットの本番の `socket.path` も `/run/coldaisle/` の下にする。いまの `config/control-admin.yaml` は
  開発用の相対 path（`var/run/...`）なので、導入先の設定を `/run/coldaisle/` の下にすることを段階 1 の手順書に書く

**書き手**（Hardware Backend。#77）

1. header を特定・検証した**後**、`pwmN_enable` を manual へ切り替える**前**に記録を書く（0028 §2.7）
2. 書き方は同じディレクトリの一時ファイル（`O_EXCL | O_NOFOLLOW`、`0600`）へ書いてから `rename` する。
   実行部が途中まで書かれた記録を読まない
3. **記録を書けなければ制御を取らない**（`pwmN_enable` に触らず、終了コード 5 で BIOS の制御のまま終える）。
   記録の無い takeover は、異常終了しても Max にならない
4. **起動時に記録が既にあり、driver 名と label が一致するなら、`original_pwm` / `original_enable` は
   既存の記録の値を引き継ぐ。** 前回が異常終了なら、いまの値は実行部が書いた Max・manual で、
   それを「元の値」として上書きすると、正常停止で BIOS の自動制御へ返せなくなる。
   一致しない記録は上書きする前に error を残す
5. `config_invalid` の経路（0060 §2.9、`run_config_invalid_max`）でも、Max を書く前に同じ手順で記録を書く
6. 記録の path は、**実行部の定数（`coldaisle.safety_handoff.HANDOFF_RECORD_PATH`）を合成の起点
   （`control_daemon.py`）が backend へ渡す**。backend（`coldaisle.control.hardware`）は path を持たず、
   上位の module を import しない（レイヤの向き）。試験で両者が同じ path を指すことを確かめる

### 2.8 正常停止（SIGTERM）での返却と記録の削除

0028 §2.7 の「正常停止」を、**記録を消すかどうか**で実行部へ伝える。

| 元の `pwmN_enable` | fand の停止の手順 | 記録 | `ExecStopPost` の結果 |
|---|---|---|---|
| すべて `0` または `2` 以上 | PWM を下げずに元の値へ戻し、読み戻しで確かめる | **全 zone を戻せたときだけ消す** | 何もしない（BIOS の制御） |
| いずれかが `1`（manual） | 戻さない。Max のまま。**通知する**（0028 §2.7） | 消さない | Max を書き直す（同じ値） |
| 戻す途中で失敗・timeout・SIGKILL | — | 消さない | Max |

- 実行部は変えない（「記録が無ければ何もしない」「あれば Max」）。`SERVICE_RESULT` を読まないので、
  **fand が戻さずに 0 で終わる不具合があっても Max になる**
- 戻した後、記録を消す前に死んだ場合は `ExecStopPost` が Max を書く（安全側に重なる）
- 元が manual で Max のまま終えたときの通知は、fand が停止の手順の中で journal に構造化ログの event
  （名前は仮に `shutdown_left_max_manual`、zone を含む）を残す。通知層（0013）への渡し方は §5 の 5 で
  実行部の失敗と一緒に決める
- **これは 0028 §2.7 の「正常停止」の条件（SIGTERM かつ `SERVICE_RESULT=success`）の一部の置き換えである**
  （Supersedes 欄）。戻す・戻さないの規則と通知は変えないが、次の2つの場面で挙動が 0028 の文言と変わる。
  - 返却して記録を消した後に fand が非 0 で落ちた: 0028 の文言では異常終了で Max だが、本記録では何もしない。
    記録を消す時点で全 zone の `pwmN_enable` は読み戻しで BIOS の自動制御へ戻っていることを確かめており、
    書き手はもう居ないので、BIOS の制御のまま残るのは Safety-0 と同じ安全側の状態である
  - `SERVICE_RESULT=success` なのに記録が残っている: 0028 の文言では何もしないが、本記録では Max。安全側に倒れる
  - Critical Safety の引き継ぎの発火条件を変えるので、0028 §2.9 の承認点に準じて所有者の承認を要する（所有者は判断点 4 で (a) を選んだ。本記録の最終承認で確定する）

### 2.9 取り込み・Telemetry との順序

| 項目 | 決定 | 理由 |
|---|---|---|
| 起動順 | `After=` と `Wants=` に `coldaisle-daemon.service` と `coldaisle-telemetry.service`。`After=systemd-modules-load.service` | DB と入力を先に動かす。無ければ入力が stale になり、Critical Safety が安全側へ倒す（0028 §2.7） |
| 依存の強さ | **`Requires=` / `BindsTo=` / `PartOf=` / `Requisite=` を使わない**（試験で禁止） | 取り込みの停止・再起動が制御の停止に波及しない。GPU AI Service にも依存させない（AGENTS.md） |
| 停止順 | `After=` の逆順で、**fand が先に止まる** | OS の停止時、入力が生きているうちに BIOS へ返す |
| worker（M9） | 別の unit にし、`coldaisle-fan` グループに入れない | hwmon を書けず、deadman を養えない（§2.2 の `NotifyAccess=main`） |

`coldaisle-telemetry.service` は未作成（0069 未決 3）。存在しない unit への `Wants=` は無視されるので、
fand のテンプレートを先に置いてよい。

### 2.10 実装の段階

| 段階 | 担当 Issue（1段階ずつ別の PR） | 内容 | 前提 |
|---|---|---|---|
| 0 | #74 | `local_socket.Authorizer.allows()` が `allow_same_user: false` のとき `uid == server_uid` をグループの判定より前に拒否する（§2.1）。試験は「サーバと同じ uid がグループのメンバーでも拒否」「`allow_same_user: true` なら許可」「別の uid のメンバーは許可」の3通り。管理ソケット（`control_admin`）と eventd の両方の既存試験が通ること | 本記録の承認（所有者の判断 13 が (a) のとき） |
| 1 | #57 | `deploy/systemd/coldaisle-fand.service` と `deploy/udev/` の hwmon テンプレート（仮の値）。`tests/test_deploy_templates.py` の `test_fand_is_not_templated_here` を、§2.2〜§2.9 を確かめる静的試験に置き換える（`Type=notify`・`NotifyAccess=main`・`ExecStart` の実行ファイルが `/opt/coldaisle/.venv/bin/coldaisle-fand` ちょうどで、`coldaisle-fand` が `pyproject.toml` の入口にある（既存の `test_exec_start_points_at_a_real_entry_point` に fand を足す）・`--require-watchdog`・`KillMode=control-group`・`StartLimitIntervalSec=0`・`RestartPreventExitStatus` が `{3, 4}` ちょうど・`ExecStopPost` が `+` と `-I -S` で引数なし・`ProtectKernelTunables` が `yes` でない・`StateDirectory=coldaisle` が無い・fand の `StateDirectoryMode` が `0700` ちょうど・`RuntimeDirectory=coldaisle` が fand にだけある・取り込みへの強い依存が無い・`User` が `coldaisle` でも root でもない・`SupplementaryGroups` に `coldaisle` と `control-admin.yaml` の `socket.group` が含まれる・時間切れ3つが明示されている・udev のテンプレートが `RUN+=` の chgrp / chmod で `GROUP=` / `MODE=` に頼らない）。CI に `systemd-analyze` は無いので（`tests/test_deploy_templates.py` の冒頭）、導入先では fand の unit を置いた後・起動する前に `systemd-analyze verify` を人が走らせ、`ExecStart` の実行ファイルが実行できること（`is not executable` が出ないこと）を確かめる手順を `docs/ubuntu-deploy.md` に書く。**既存の daemon / api / rollup / report の unit を §2.1 に合わせて変える**（`StateDirectoryMode=2770`・`UMask=0007`。これら4つの unit でそろっていることも試験する。fand の `StateDirectoryMode` は `/var/lib/coldaisle-fand` のもので、`0700`）。`docs/ubuntu-deploy.md` に導入手順（管理ソケットの `socket.path` を `/run/coldaisle/` の下にすることを含む）を足すが、**`enable` はしない**と書く。同じ PR で `docs/ubuntu-deploy.md` に**既存の導入先の移行**（§2.1。DB と `-wal` / `-shm` / `-journal` のグループと mode を揃えて `stat` と、unit と同じグループを渡した `systemd-run ... test -w` で確かめる。fand の起動より前）を足し、既存の移行手順の `install ... -m 0640` を `0660` に直す。`SqliteStore` が新しく作る DB ファイルを `0660` で作る（§2.1。umask `0007` のもとで本体と `-wal` / `-shm` が `0660` になる試験） | 本記録の承認・段階 0（fand を `coldaisle-admin` に入れるため）。`--authority-root` は PR #192 のマージ後に使える |
| 2 | #74 | `create_watchdog` に §2.3 の「環境側が長ければ終了コード 4」を足す（`environ` を渡す既存の試験の形で、等しい・短い・長いの3通り）。通知の I/O の失敗（socket の作成・`READY=1` の送信）を別の例外にして終了コード 6 で終える（§2.4。4 のままでない試験）。`safety.yaml` v4 に `hardware_write_fail_exit_ms`（§2.6）を足し、不変条件の下限・上限を読み込みで検証し、書き込みの失敗が続いたら記録を消さずに終了コード 7 で終える（simulated backend に失敗を注入。途中で1回成功すれば数え直す試験を含む）。fand の起動時に `faulthandler` を有効にする。§2.1「起動時の書き込み確認」（`db_not_writable` で終了コード 5。`SQLITE_BUSY` と区別する。権限の試験は root では skip） | 段階 1 と独立。`control_daemon.py` を触るので PR #192 のマージ後 |
| 3 | #78 | 実行部を `python -I -S <ファイル>` で起動できることの試験（subprocess。記録の無い環境で何も書かずに 0 で終わる）。標準ライブラリだけを import する試験は既にある | 段階 1 |
| 4 | #77 | 実機 backend の記録の書き手（§2.7 の 1〜6）と正常停止の返却（§2.8）。偽の sysfs（`tmp_path`）で、書いた記録を `emergency_handoff` が読んで Max にできること・既存記録の値の引き継ぎ・返却の成功で記録が消え失敗で残ること・記録を書けなければ `pwmN_enable` に触らないことを確かめる | #75 の profile、段階 1〜3 |
| 5 | 人（所有者） | simulated backend のまま、systemd のある環境（VM 可）で、0069 のテンプレートで作った `0640` の DB から §2.1 の移行手順を通して fand が起動できること（移行を飛ばすと `db_not_writable` で起動しないこと）を確かめ、unit を動かし、`kill -STOP`（watchdog）・`kill -KILL`・`systemctl stop`・再起動の連続で、`ExecStopPost` が走ること・`/run/coldaisle` が残ること・順序を確かめる。その後 0028 §2.9 の承認点 3（実機で kill・watchdog・正常停止・引き継ぎを確認）を経て `enable` する | 段階 1〜4 |

- **どの段階も `ControlTick` の版を上げない。** 実効の `WatchdogSec` や記録の状態を decision trace に
  載せると決めた場合（§5）は、**マージ時点の次の空き番号**（#192 が v13 を使えば v14 以降）とし、
  ほかの版上げの PR と直列にする。
  `src/coldaisle/web/airflow-trace.js` の `KNOWN_VERSIONS` と fixture を同じ PR で更新する
- 段階 1 のテンプレートを置いただけでは Fan 制御は有効にならない（`docs/critical-safety.md` の
  「統合と実機検証までサービスで有効化しない」を保つ）

---

## 3. Consequences

### 良くなること

- 0060 未決 7・0069 §2.4 / 未決 3（fand の分）・0072 未決 3 が閉じ、#57 の unit と #78 の deadman が配線できる
- hang・異常終了・権限の喪失のどれでも、最後に Max を書く経路が root の独立した実行部として残る
- 承認した deadman より遅い deadman では制御を取らない（§2.3）
- 取り込みや API の再起動・停止が、制御の停止に波及しない（§2.9）
- シリアル・TCP・`/dev` に届かない sandbox で、ルール1・6 を構造でも守る（§2.6）
- すべての性質を、実機なしの静的試験・偽の sysfs・subprocess で確かめられる（§2.10 の 1〜4）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| ユーザー・グループ・udev のルールが1つずつ増える | 名前は仮の値のテンプレートにし、手順書にまとめる。試験で仮の値のままであることを確かめる |
| 引き継ぎ実行部が root で動く | 標準ライブラリだけ・引数なし・固定の値・照合してから書く（実装済み）。コードは fand が書き換えられない場所にある |
| `-I -S` でソースを直接実行するため、ファイルの位置に依存する | 段階 1 の試験で `ExecStopPost` の path が実在のファイルを指すことを確かめる |
| 設定が不正（終了コード 1）のとき、`RestartSec` ごとに再起動と Max の書き込みを繰り返す | 書くのは Max だけで、安全側に重なる。journal が増えるので、繰り返しの通知は §5 |
| `RuntimeDirectoryPreserve=yes` で、正常停止で返せなかった記録が OS の再起動まで残る | 残った記録が指すのは照合済みの header で、次の `ExecStopPost` が書くのは Max だけ |
| fand の DB へのアクセスを `coldaisle` グループと共有する | decision trace と監査は同じ DB に置く既存の決定（0030 / 0072）に従う。`authority.json` だけは別のディレクトリにする |
| `WatchdogSec` と `safety.yaml` を人が揃える必要がある | 長い向きは起動しない（§2.3）ので、揃え忘れは「制御を取らない」で表に出る |
| 書き込みの失敗が続くと、fand が再起動と `ExecStopPost` の Max を繰り返す（終了コード 7） | 書くのは Max だけで安全側に重なる。繰り返しの通知は §5 の 5。期間の下限（§2.6）で、一時的な失敗1回では再起動しない |
| Safety の設定値が1つ増え、`safety.yaml` の schema が v4 になる | 値は既存の `watchdog_timeout_ms` に揃えた暫定値で、上限・下限を既存の値との不変条件で縛る。承認までは `provisional` として一覧に出る |
| 既存の unit の `StateDirectoryMode` を 0750 から 2770 へ広げ、`UMask=0007` を足す（§2.1） | 広がるのは `coldaisle` グループの書き込みだけで、other は変わらない。全 unit でそろっていることを試験する |
| 既存の導入先では、fand を起動する前に DB と `-wal` / `-shm` の権限を人が移行する必要がある（§2.1） | 手順を `docs/ubuntu-deploy.md` に置き、`stat` と、unit と同じグループを渡した `systemd-run ... test -w` で確かめてから起動する。忘れても fand は制御を取らずに `db_not_writable` で原因を報告する（BIOS の制御のまま） |
| **API / AI のユーザー（`coldaisle`）は、fand が制御入力として読む Telemetry の DB を書ける。** `authority.json` は別のディレクトリで守る（§2.1）が、同じ論理で制御入力は守っていない | 0069 の既存の構成から来る残るリスクとして明記する。制御入力の改ざんへの対策は本記録の範囲外（§5 の 14） |

---

## 4. 却下した代替案

| 案 | 得 | 失（却下の理由） |
|---|---|---|
| **U2. fand を root で動かし、`CapabilityBoundingSet=` を空にする** | root 所有の sysfs 属性をそのまま書け、udev のルールが要らない。権限が再 bind で消えない | uid 0 は sandbox の穴から root 所有のファイルを書ける。DB を root:root で作ると取り込みが書けなくなる。**所有者が udev の管理を嫌うなら次善** |
| **U3. fand を root のまま（capability も制限しない）** | 最も簡単 | 乗っ取られた fand がホスト全体を握る。0072 §2.5 が警戒する「高い権限で動く fand」そのもの |
| **E2. 実行部を fand と同じユーザーで動かす（`+` なし）** | root で動くコードが無い | fand の書き込み権限が失われた状況（udev の未適用・再 bind）で、最後の Max も書けない |
| **E3. 実行部を venv の `coldaisle-safety-handoff` で起動する** | 試験と同じ入口 | venv の更新中・破損時に動かない。`coldaisle` パッケージの import が挟まる（0028 §2.7「壊れた環境でも動く」） |
| **N2. 実行部が `SERVICE_RESULT` を見て、`success` なら何もしない** | 0028 §2.7 の文言にそのまま沿う | fand が戻さずに 0 で終わる不具合で、最後の低い PWM が残る。環境変数という入力で Max を省ける経路になる |
| **W2. `WatchdogSec` と `safety.yaml` の食い違いは warning のまま（0060 のまま）** | 変更が要らない | 長い向きの食い違いが目視頼みになる |
| **W3. fand が `sd_notify("WATCHDOG_USEC=…")` で `safety.yaml` の値を設定する** | unit に値を書かなくてよい | 0060 §2.7 の「固定 datagram だけ」を崩し、設定から組み立てた値を deadman へ送る。unit を見ても効いている値が分からない |
| **W4. `WatchdogSec` を書かず、テンプレートを `safety.yaml` から生成する CLI を作る** | 食い違いが起きない | 道具が1つ増え、生成物の管理が要る。起動時の検査（§2.3）で足りる |
| **S1. `WatchdogSignal=SIGKILL`** | 最も早く Max になる | hang の原因（どのスレッドがどこで止まったか）が残らない。`LimitCORE=0` と `TimeoutAbortSec` で遅れは抑えられる |
| **S2. 既定の SIGABRT のまま core dump を残す** | 解析の情報が最も多い | core の書き出しが終わるまで `ExecStopPost` が走らず、Max が遅れる |
| **R1. `StartLimitBurst` で有限回にして諦める** | 再起動の嵐が止まる | 諦めた後は tach の stall も `pwmN_enable` の書き戻しも見る者が居ない |
| **R2. `RestartPreventExitStatus` を付けない** | 設定が単純 | 承認が無い（3）・unit の食い違い（4）で、結果の変わらない再起動を永久に繰り返す |
| **R3. 終了コード 2 も再起動しない** | ログが静か | 起動直後にドライバが未ロードなど一時的な原因で、制御が戻らないまま残る |
| **O1. 取り込みに `Requires=` / `BindsTo=`** | 入力が無いまま動かない | 取り込みの停止で制御も止まる。欠測の安全側の扱い（0028 §2.7）より前に冷却の制御が消える |
| **D1. `/run/coldaisle` を複数の unit で共有する** | ディレクトリが1つ | 片方の停止で記録ごと消えうる。所有者の付け替えも起きる |
| **D2. `RuntimeDirectoryPreserve=restart`** | 停止後に `/run/coldaisle` が片付く | 後片付けと `ExecStopPost` の順序という systemd の実装に安全が依存する。残っても害が無いので `yes` にする |
| **F1. 書き込みが失敗し続けても fand は動き続ける（`EMERGENCY` のまま）** | 再起動が起きない | fand の中の Max は hwmon に届かず、heartbeat が止まらないので `ExecStopPost` も走らない。低い manual の PWM のまま残りうる（最終レビュー（97d7470）の P1） |
| **F2. 書き込みの失敗1回で終わる** | 最も早く引き継ぎへ移る | 一時的な失敗でも再起動と takeover を繰り返す。`write_fail_emergency_after` の再試行が意味を失う |
| **F3. 期間を fand の unit（`TimeoutSec` 等）やコードの定数に置く** | 設定が増えない | Safety の値をコードに埋めることになる（AGENTS.md ルール9）。`safety.yaml` の承認の外で決まる |
| **C1. 通知の I/O の失敗も終了コード 4 のまま** | 終了コードが増えない | `RestartPreventExitStatus` で、一時的な送信の失敗でも制御が戻らない（最終レビューの P2） |
| **A2. fand を `coldaisle-admin` に入れず、root が setgid の `RuntimeDirectory` でソケットのグループを用意する** | 認可のコードを変えない | `prepare_parent` との整合を別途確かめる必要があり、unit の構成が複雑になる。所有者は判断点 13 で (a) を選んだ |
| **H1. `ProtectKernelTunables=yes` + `ReadWritePaths=` で hwmon だけ開ける** | `/sys` の他を守れる | `/sys/class/hwmon/hwmonN` は `/sys/devices/...` へのリンクで、実体の path は機械ごとに違う。テンプレートに書けず、個体の情報に近づく |

---

## 5. 未決事項

| # | 内容 | どこで |
|---|---|---|
| 1 | **値の確定**（実機の測定待ち）: `WatchdogSec`（= `watchdog_timeout_ms` の確定値）、`hardware_write_fail_exit_ms`（§2.6。暫定値は `watchdog_timeout_ms` と同じ）、`TimeoutAbortSec`、`TimeoutStopSec`、`TimeoutStartSec`、`RestartSec`。hang から Max までの最悪時間（§2.3）が熱の時定数に対して許されるか | #50 / #75 の後、0028 §2.9 の承認点 2 と合わせて所有者 |
| 2 | CPU フルロード時の overrun を抑える `Nice=` / `CPUWeight=`、GPU 学習時のメモリ圧迫で殺されにくくする `OOMScoreAdjust=` の要否と値 | #50（CPU / GPU の負荷試験で overrun と OOM を見る） |
| 3 | 実際の systemd で、後片付けと `ExecStopPost` の順序・SIGABRT と `faulthandler` の出力・D 状態の hang の振る舞い・`StateDirectoryMode=2770` の setgid が保たれるかを確かめる。**導入先の `kernel.core_pattern`（apport / systemd-coredump への pipe）のもとで、SIGABRT から `ExecStopPost` の開始までの時間**も測る（`LimitCORE=0` でも pipe 側の処理は走りうる）。遅れるなら §2.3 の最悪時間の式に加えるか、S1（SIGKILL）へ移るかを所有者が決める | 段階 5（人） |
| 4 | `ExecStopPost` が動かないとき、チップが PWM をどう保つか | 0028 未決 6（#74 の実機確認） |
| 5 | 引き継ぎ実行部の失敗・再起動の繰り返し（終了コード 7 の繰り返しを含む）・元が manual で Max のまま正常停止したこと（§2.8 の `shutdown_left_max_manual`）の通知（`OnFailure=` の unit や journal の event から通知層へどう渡すか） | 通知（0013）と合わせて別途 |
| 6 | `config_invalid` の Max を周期的に書き直すか（本記録の再起動で結果として書き直されるが、決定としては未決） | 0060 未決 1 |
| 7 | 終了コード 1 が `config_invalid` と未捕捉例外で重なっている。分けるか（分けても再起動の扱いは変わらない） | #74 |
| 8 | `authority.json` への昇格を人がどう書くか（`/var/lib/coldaisle-fand` の権限と承認者の uid の束縛） | 0072 未決 5 / #92 の段階 3 |
| 9 | 実効の `WATCHDOG_USEC` や記録の状態を decision trace に載せるか。載せるなら `ControlTick` をマージ時点の次の空き番号へ、ほかの版上げの PR と直列に上げ、`KNOWN_VERSIONS` を更新する | 必要になったら（#82） |
| 10 | `MemoryDenyWriteExecute` と `SystemCallFilter` の絞り込み | worker（#86 / #89）を入れるとき |
| 11 | hwmon の udev ルールの driver 名と channel 番号（実機の値は導入先だけ） | #75 / `docs/fan-header-mapping.md` |
| 12 | `coldaisle-telemetry` / `coldaisle-eventd` の unit（`RuntimeDirectory` は `coldaisle` 以外の名前にする） | 0069 未決 3 / 0045 未決 4 |
| 13 | コンテナ化（#64）で fand をホストで直接動かすか。本記録はホストで直接動かす前提 | 0028 未決 7 / #64 |
| 14 | API / AI のユーザーが Telemetry の DB を書ける（§3）。制御入力の書き手を取り込みに限るか（DB の分離・読み取り専用の接続など） | 所有者が別の Issue を立てる（本記録の範囲外） |
| 15 | 管理ソケットのグループ付け替えのために fand を `coldaisle-admin` に入れる方式（§2.1）。所有者は 2026-09-30 に同じ uid の拒否を認可に足す方式（判断点 13 の (a)、段階 0）を選んだ（同日に承認）。段階 0 がマージされるまで fand の unit は `enable` しない | #74（段階 0） |
