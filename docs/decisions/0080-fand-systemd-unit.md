# 決定記録 0080: `coldaisle-fand` の systemd unit（deadman・引き継ぎ・権限・順序）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-30
- **Supersedes**: なし。ただし承認されると、[`0060`](0060-control-loop-runtime.md) §2.7
  「deadman が『ある』と言える条件」の表の4行目（`WATCHDOG_USEC` と `watchdog_timeout_ms` が違うときは
  warning で環境側を採る）のうち、**環境側が長い場合だけ**を §2.3 で置き換える。
  0060 側への `Superseded by` の追記は所有者の判断で行う（PR の「所有者に判断してほしい点」9）
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
| `coldaisle-admin` | 管理ソケットのグループ（0072 §2.5。`config/control-admin.yaml` の仮の値と同じ） | 操作する人だけ。**`coldaisle`（API / 取り込み）と AI 層のユーザーは入れない** |
| `coldaisle` | 既存の取り込み・API・rollup・report のユーザー（0069） | 変えない |

- **fand を `coldaisle` と同じ uid で動かさない。** 0072 §2.5 の配置の必須条件（API / AI サーバと同じユーザーにしない）
- **fand を root で動かさない。** 書き込みの権限は hwmon の対象属性だけに絞る（§2.6）
- fand は `SupplementaryGroups=coldaisle` で DB（`/var/lib/coldaisle`）と `/etc/coldaisle` を読み書きする。
  decision trace（0030）と管理操作の監査（0072 §2.7）は同じ DB に書くためである。
  SQLite の `-wal` / `-shm` をグループで共有するため、DB を書く unit（取り込み・fand）は `UMask=0007` にする
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
| `ExecStart=` | `coldaisle-fand --require-watchdog --config-dir <root 所有の制御設定> --admin-config <root 所有の管理設定> --db /var/lib/coldaisle/coldaisle.db --authority-root /var/lib/coldaisle-fand/authority` | `--require-watchdog` で「deadman が無いなら制御を取らない」（0060 §2.7）。**制御の4ファイルは root 所有**にし、fand 自身が `safety.yaml` を緩められないようにする |
| `WorkingDirectory=` | `/opt/coldaisle`（0069 と同じ。root 所有） | — |
| `KillMode=` | `control-group`（既定。**明示する**） | `ExecStopPost` の前に cgroup の全プロセスを止め、書き手を1つにする（0028 §2.7） |
| `WatchdogSec=` | `safety.yaml` の `watchdog_timeout_ms` と**同じ値**（§2.3） | — |
| `Restart=` | `always` | 0028 §2.7 で決定済み。起動のたびに `STARTUP` の Max を通る |
| `StartLimitIntervalSec=` | `0` | 0069 §2.3 と同じ。諦めると tach の監視も Safety の裁定も止まる（§2.4） |
| `RestartPreventExitStatus=` | `3 4` | §2.4 |
| `ExecStopPost=` | `+/usr/bin/python3 -I -S /opt/coldaisle/src/coldaisle/safety_handoff.py`（引数なし） | §2.5 |
| `RuntimeDirectory=` | `coldaisle`、`RuntimeDirectoryMode=0711`、`RuntimeDirectoryPreserve=yes` | §2.7 |
| `StateDirectory=` | `coldaisle-fand` だけ（**`coldaisle` は書かない**） | §2.1。`coldaisle` を書くと所有者が付け替えられる |
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
| `WATCHDOG_USEC < watchdog_timeout_ms` | 起動はする（より厳しい deadman）。warning を残す（0060 のまま） |

- 環境側が**長い**と、所有者が承認した値より遅い deadman で運転することになる。0060 §2.7 は
  「実際に効くのは環境側」として warning で続けるが、それは安全でない側への食い違いを
  運用者の目視に任せることになる。**長い向きだけを起動拒否に締める**（Supersedes 欄）。
  拒否は takeover より前なので、BIOS の制御（Safety-0）のまま残る
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
| 4（deadman が使えない・§2.3 の不一致） | 取っていない | **しない** | unit か `safety.yaml` の食い違いで、人が直すまで変わらない |
| 5（DB・Metric Catalog など制御設定以外） | 取っていない | **する** | 取り込みが DB を作る前など、一時的でありうる |
| シグナル（SIGKILL・watchdog の SIGABRT・OOM） | 取った | **する** | `ExecStopPost` が Max にしてから再起動する |

- `StartLimitIntervalSec=0` で**諦めない**。諦めた unit は最後の `ExecStopPost` の Max で止まるので
  熱の面では安全側だが、tach の stall を見る者も、`pwmN_enable` を外から戻されたことに気づく者も居なくなる。
  再起動の繰り返しそのものの通知は §5
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
グループを `coldaisle-fan`、権限を `0664` にする。driver 名と channel 番号は仮の値
（`REPLACE-WITH-DRIVER-NAME` など）で置き、実機の値は導入先の `/etc/udev/rules.d/` にだけ書く（0069 §2.2 と同じ）。
ドライバの再 bind で属性が作り直されると権限は元に戻るが、その時点で `pwmN_enable` もドライバの既定
（通常は自動制御）に戻るので、制御は BIOS へ返っている。fand は書き込み失敗を
0028 §2.7 の fault として扱い、Top なら `EMERGENCY` になる。

### 2.7 引き継ぎ記録の置き場所と書き手の配線

- 置き場所は実行部が固定で読む `/run/coldaisle/fan-handoff.json`。**`/run/coldaisle` は fand の unit だけが
  `RuntimeDirectory` として持つ。** 取り込み・API・`coldaisle-eventd` などほかの unit は別の名前を使う
  （テンプレートの試験で、`RuntimeDirectory=coldaisle` が fand の unit にしか無いことを確かめる）
- `RuntimeDirectoryPreserve=yes`。記録の寿命を systemd の後片付けに任せない。記録を消すのは
  fand の正常停止（§2.8）だけで、`/run` は再起動（OS）で必ず空になる
- `RuntimeDirectoryMode=0711`。管理ソケット（`0660`、グループ `coldaisle-admin`）は other の `x` で
  たどれ（0045 §2.2 の親ディレクトリの検査を満たす）、一覧は見せない。記録は `0600`（fand のユーザーと root だけが読む）
- 管理ソケットの本番の `socket.path` も `/run/coldaisle/` の下にする

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
| いずれかが `1`（manual） | 戻さない。Max のまま | 消さない | Max を書き直す（同じ値） |
| 戻す途中で失敗・timeout・SIGKILL | — | 消さない | Max |

- 実行部は変えない（「記録が無ければ何もしない」「あれば Max」）。`SERVICE_RESULT` を読まないので、
  **fand が戻さずに 0 で終わる不具合があっても Max になる**
- 戻した後、記録を消す前に死んだ場合は `ExecStopPost` が Max を書く（安全側に重なる）
- 0028 §2.7 の「正常停止」の条件（SIGTERM かつ `SERVICE_RESULT=success`）を、実行部から見た判定として
  言い換えたものであり、戻す・戻さないの規則そのものは変えない

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
| 1 | #57 | `deploy/systemd/coldaisle-fand.service` と `deploy/udev/` の hwmon テンプレート（仮の値）。`tests/test_deploy_templates.py` の `test_fand_is_not_templated_here` を、§2.2〜§2.9 を確かめる静的試験に置き換える（`Type=notify`・`NotifyAccess=main`・`--require-watchdog`・`KillMode=control-group`・`StartLimitIntervalSec=0`・`RestartPreventExitStatus` が `{3, 4}` ちょうど・`ExecStopPost` が `+` と `-I -S` で引数なし・`ProtectKernelTunables` が `yes` でない・`StateDirectory=coldaisle` が無い・`RuntimeDirectory=coldaisle` が fand にだけある・取り込みへの強い依存が無い・`User` が `coldaisle` でも root でもない・時間切れ3つが明示されている）。`docs/ubuntu-deploy.md` に導入手順を足すが、**`enable` はしない**と書く | 本記録の承認。`--authority-root` は PR #192 のマージ後に使える |
| 2 | #74 | `create_watchdog` に §2.3 の「環境側が長ければ終了コード 4」を足す（`environ` を渡す既存の試験の形で、等しい・短い・長いの3通り）。fand の起動時に `faulthandler` を有効にする | 段階 1 と独立。`control_daemon.py` を触るので PR #192 のマージ後 |
| 3 | #78 | 実行部を `python -I -S <ファイル>` で起動できることの試験（subprocess。記録の無い環境で何も書かずに 0 で終わる）。標準ライブラリだけを import する試験は既にある | 段階 1 |
| 4 | #77 | 実機 backend の記録の書き手（§2.7 の 1〜6）と正常停止の返却（§2.8）。偽の sysfs（`tmp_path`）で、書いた記録を `emergency_handoff` が読んで Max にできること・既存記録の値の引き継ぎ・返却の成功で記録が消え失敗で残ること・記録を書けなければ `pwmN_enable` に触らないことを確かめる | #75 の profile、段階 1〜3 |
| 5 | 人（所有者） | simulated backend のまま、systemd のある環境（VM 可）で unit を動かし、`kill -STOP`（watchdog）・`kill -KILL`・`systemctl stop`・再起動の連続で、`ExecStopPost` が走ること・`/run/coldaisle` が残ること・順序を確かめる。その後 0028 §2.9 の承認点 3（実機で kill・watchdog・正常停止・引き継ぎを確認）を経て `enable` する | 段階 1〜4 |

- **どの段階も `ControlTick` の版を上げない。** 実効の `WatchdogSec` や記録の状態を decision trace に
  載せると決めた場合（§5）は、PR #192 の v13 の後に直列で v14 とし、
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
| **H1. `ProtectKernelTunables=yes` + `ReadWritePaths=` で hwmon だけ開ける** | `/sys` の他を守れる | `/sys/class/hwmon/hwmonN` は `/sys/devices/...` へのリンクで、実体の path は機械ごとに違う。テンプレートに書けず、個体の情報に近づく |

---

## 5. 未決事項

| # | 内容 | どこで |
|---|---|---|
| 1 | **値の確定**（実機の測定待ち）: `WatchdogSec`（= `watchdog_timeout_ms` の確定値）、`TimeoutAbortSec`、`TimeoutStopSec`、`TimeoutStartSec`、`RestartSec`。hang から Max までの最悪時間（§2.3）が熱の時定数に対して許されるか | #50 / #75 の後、0028 §2.9 の承認点 2 と合わせて所有者 |
| 2 | CPU フルロード時の overrun を抑える `Nice=` / `CPUWeight=`、GPU 学習時のメモリ圧迫で殺されにくくする `OOMScoreAdjust=` の要否と値 | #50（CPU / GPU の負荷試験で overrun と OOM を見る） |
| 3 | 実際の systemd で、後片付けと `ExecStopPost` の順序・SIGABRT と `faulthandler` の出力・D 状態の hang の振る舞いを確かめる | 段階 5（人） |
| 4 | `ExecStopPost` が動かないとき、チップが PWM をどう保つか | 0028 未決 6（#74 の実機確認） |
| 5 | 引き継ぎ実行部の失敗・再起動の繰り返しの通知（`OnFailure=` の unit から通知層へ渡すか） | 通知（0013）と合わせて別途 |
| 6 | `config_invalid` の Max を周期的に書き直すか（本記録の再起動で結果として書き直されるが、決定としては未決） | 0060 未決 1 |
| 7 | 終了コード 1 が `config_invalid` と未捕捉例外で重なっている。分けるか（分けても再起動の扱いは変わらない） | #74 |
| 8 | `authority.json` への昇格を人がどう書くか（`/var/lib/coldaisle-fand` の権限と承認者の uid の束縛） | 0072 未決 5 / #92 の段階 3 |
| 9 | 実効の `WATCHDOG_USEC` や記録の状態を decision trace に載せるか。載せるなら `ControlTick` を PR #192 の v13 の後に直列で上げ、`KNOWN_VERSIONS` を更新する | 必要になったら（#82） |
| 10 | `MemoryDenyWriteExecute` と `SystemCallFilter` の絞り込み | worker（#86 / #89）を入れるとき |
| 11 | hwmon の udev ルールの driver 名と channel 番号（実機の値は導入先だけ） | #75 / `docs/fan-header-mapping.md` |
| 12 | `coldaisle-telemetry` / `coldaisle-eventd` の unit（`RuntimeDirectory` は `coldaisle` 以外の名前にする） | 0069 未決 3 / 0045 未決 4 |
| 13 | コンテナ化（#64）で fand をホストで直接動かすか。本記録はホストで直接動かす前提 | 0028 未決 7 / #64 |
