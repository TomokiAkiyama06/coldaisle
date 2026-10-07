# Ubuntu への移行と常駐化（#57 / S-09 / FR-102 / NFR-01）

開発用の Mac から、GPU サーバー（Ubuntu）の常駐運用へ移すための手順です。
テンプレートは `deploy/` にあります。決定の理由は決定記録
[`0069`](decisions/0069-ubuntu-deploy-templates.md) を見てください。

**`deploy/` の中身はテンプレートです。** ユーザー名 `coldaisle`、配置先
`/opt/coldaisle`、udev の VID / PID / シリアルは**仮の値**です。導入先で必要に
応じて書き換え、**書き換えた実機の値はリポジトリへコミットしません**
（AGENTS.md ルール10 / 決定記録 0021）。

| テンプレート | 役割 |
|---|---|
| `deploy/systemd/coldaisle-daemon.service` | 取り込みデーモン（シリアル）。`Restart=always` |
| `deploy/systemd/coldaisle-api.service` | 読み取り API とダッシュボード（`127.0.0.1:8000`）。`Restart=always` |
| `deploy/systemd/coldaisle-rollup.service` / `.timer` | ロールアップと保持期間の適用。毎日 03:00 |
| `deploy/systemd/coldaisle-report.service` / `.timer` | 前日の日次レポート。毎朝 08:05（ロールアップのあと） |
| `deploy/udev/99-coldaisle-sensors.rules` | センサー基板に `/dev/server-sensors` の固定名を付ける |
| `deploy/systemd/coldaisle-fand.service` | 3系統 Fan 制御デーモン。**置くだけで `enable` しない**（6 節） |
| `deploy/udev/99-coldaisle-hwmon.rules` | fand に、対象 zone の hwmon の `pwmN` / `pwmN_enable` だけの書き込み権限を渡す |

## まだ含めないもの

- **`coldaisle-fand`（3系統 Fan 制御）は、テンプレートを置くだけで有効化しません。**
  unit の中身は決定記録 [`0080`](decisions/0080-fand-systemd-unit.md) で決めました。
  サービスとして `systemctl enable` するのは、0080 §2.10 の段階 5（systemd のある環境での
  kill・watchdog・正常停止の確認）と、0028 §2.9 の承認点 3 の後です
  （`docs/critical-safety.md`「統合と実機検証までサービスで Fan 制御を有効化しない」）。
- `coldaisle-telemetry`（Internal Telemetry）と `coldaisle-eventd`（書き込みソケット。
  決定記録 0045 未決 4）の unit も含めません。
- 日次 CSV（`coldaisle-rollup --export-day`）の自動実行は配線していません。
  対象日を毎回渡す必要があり、自動化の方法はまだ決めていません（手で実行はできます）。
  export は CSV の横に `sensors_YYYY-MM-DD.export.json`（manifest）と、隠しの lock ファイル
  `.sensors_YYYY-MM-DD.export.lock` を置き、DB の `csv_exports` に1行を足します（決定記録 0100 §2.1）。
  CSV を複写するときは manifest も一緒に複写してください。`config/retention.yaml` の `csv_timezone` と
  `csv_export_lock_timeout_s` が無いと export だけを拒否します（ロールアップは行う）。

---

## 1. 置き場所

| もの | 場所 |
|---|---|
| コード（`git clone` したもの）と `.venv` | `/opt/coldaisle` |
| DB・レポート・案件資料など実行時のデータ | `/var/lib/coldaisle`（`/opt/coldaisle/var` からリンクする） |
| 日次 CSV | `/var/lib/coldaisle/server_sensor_logs`（下の注記） |
| 秘匿情報（`.env.example` の変数） | `/etc/coldaisle/coldaisle.env`（任意） |
| Model Registry（Fan 制御の artifact。6.7） | `/var/lib/coldaisle-registry`（仮の名前） |
| ログ | journald（`journalctl -u coldaisle-daemon` など） |

`config/retention.yaml` の `csv_dir` は `~/server_sensor_logs` です。サービス用
ユーザーのホームを `/var/lib/coldaisle` にしておくと、設定を変えずに CSV も
`/var/lib/coldaisle` の下に入ります。

各 CLI の既定の出力先（`var/coldaisle.db`、`config/report.yaml` の `var/reports` など）は
作業ディレクトリ基準です。`/opt/coldaisle/var` を `/var/lib/coldaisle` へのリンクに
しておくと、**設定を変えずに**すべて `/var/lib/coldaisle` へ入ります。

## 2. 導入

```bash
# サービス用ユーザー（名前は仮の値。変えるなら unit の User= / Group= も揃える）
sudo useradd --system --home-dir /var/lib/coldaisle --shell /usr/sbin/nologin coldaisle
# シリアルを読むために dialout へ入れる（unit でも SupplementaryGroups=dialout を付けている）
sudo usermod -aG dialout coldaisle

# 手で試験するユーザーも dialout へ（ログインし直すと反映される）
sudo usermod -aG dialout "$USER"

# 2770（setgid）: coldaisle-fand も補助グループ coldaisle で DB を書く（決定記録 0080 §2.1。
# unit の StateDirectoryMode=2770 と同じ。どの unit の起動でもこの mode に戻る）
sudo install -d -o coldaisle -g coldaisle -m 2770 /var/lib/coldaisle
# コードと config/ は root の所有のまま置く（coldaisle からは読み取りだけ）
sudo git clone <このリポジトリの URL> /opt/coldaisle
sudo ln -s /var/lib/coldaisle /opt/coldaisle/var

# venv も root が作る。uv は sudo の PATH に無いことが多いので実体のパスを渡し、
# Python はシステムのもの（Ubuntu 24.04 の python3.12）を使う
cd /opt/coldaisle
sudo UV_PYTHON_DOWNLOADS=never "$(command -v uv)" sync --no-dev --python /usr/bin/python3.12
```

**`/opt/coldaisle` を `coldaisle` の所有にしません。** 常駐する daemon / api が
乗っ取られても、コードや `config/`（`rules.yaml`・`safety.yaml`・`fan-policy.yaml`
など）を書き換えられないようにするためです。サービスが書くのは `var`
（= `/var/lib/coldaisle`。unit の `StateDirectory=`）だけです。
`config/calibration.json` や `memory/` を書く `coldaisle-calibrate --apply` /
`coldaisle-memory --apply` は、確認を経て**管理者が `sudo` で**実行します。

システムに Python 3.12 が無い場合は、uv の管理する Python を `/opt` 側に置きます
（`UV_PYTHON_INSTALL_DIR=/opt/coldaisle-python`）。サービス用ユーザーのホーム
（`/var/lib/coldaisle`。データの置き場所）の下へ Python を入れないためです。

**ただし `coldaisle-fand` の引き継ぎ実行部（`ExecStopPost=`）は、uv の管理する Python を使いません。**
venv と uv の置き場所が壊れていても Max を書けるよう、システムの `/usr/bin/python3` で動きます
（決定記録 0080 §2.5）。`/usr/bin/python3` が 3.12 より古い導入先では、fand の unit を置く前に
6.5 の確かめが通りません（3.9 以前では import の時点で落ち、異常終了の後に Max を書けません）。

秘匿情報を使う場合（通知の宛先など）。

```bash
sudo install -d -m 0750 -g coldaisle /etc/coldaisle
sudo install -m 0640 -g coldaisle /opt/coldaisle/.env.example /etc/coldaisle/coldaisle.env
sudoedit /etc/coldaisle/coldaisle.env
```

## 3. udev（固定デバイス名）

1. センサー基板を挿し、`/dev/ttyACM*` のどれになったかを確かめる
2. 値を調べる

   ```bash
   udevadm info --attribute-walk --name=/dev/ttyACM0 | grep -E 'idVendor|idProduct|serial'
   ```

3. テンプレートを置き、**導入先のファイルだけ**を実機の値に書き換える

   ```bash
   sudo cp /opt/coldaisle/deploy/udev/99-coldaisle-sensors.rules /etc/udev/rules.d/
   sudoedit /etc/udev/rules.d/99-coldaisle-sensors.rules
   sudo udevadm control --reload-rules && sudo udevadm trigger
   ls -l /dev/server-sensors
   ```

`coldaisle-daemon --source serial` はポートを省略すると自動検出し、
**`/dev/server-sensors` があれば最優先で使います**（FR-102）。無ければ従来どおり
`/dev/cu.usbmodem*` / `/dev/ttyACM*` を名前の順で選びます（決定記録 0023 §2.6）。
明示したいときは `--port /dev/server-sensors` を足します。

## 4. systemd

```bash
# coldaisle-fand.service はここでは置かない（6 節。置くだけで enable しない）
cd /opt/coldaisle/deploy/systemd
sudo cp coldaisle-daemon.service coldaisle-api.service \
        coldaisle-rollup.service coldaisle-rollup.timer \
        coldaisle-report.service coldaisle-report.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coldaisle-daemon.service coldaisle-api.service
sudo systemctl enable --now coldaisle-rollup.timer coldaisle-report.timer
```

DB を開く unit はどれも `StateDirectoryMode=2770` と `UMask=0007` を持ちます。
新しく作る DB は `0660`（グループ `coldaisle`）になり、SQLite の `-wal` / `-shm` / `-journal` も
同じ mode で作られます（決定記録 0080 §2.1）。0069 のテンプレート（`0750`）で動かしてきた
導入先は、fand を起動する前に 6.4 の移行が要ります。

取り込み（`coldaisle-daemon` の serial / mock）は、DB の実体の path の隣に `<db>.ingest.lock` を作り、
DB ごとに取り込みを1つに限ります（決定記録 0099 §2.2 / 0102 §2.1）。lock ファイルも同じ mode で作られ、
消さずに残ります。symlink（`/opt/coldaisle/var` → `/var/lib/coldaisle` を含む）は実体の path に揃うので
構いません。hard link の別名がある DB では取り込みは起動しません。**DB ファイルだけを bind mount した別名は
検出できない**ので、DB をそのような形で配置しないでください（別名から起動した取り込みとは lock を共有できず、
較正の記録が壊れうる）。

タイマーの時刻は OS の時刻帯で解釈されます（`timedatectl` で確認）。
日境界は `Asia/Tokyo`（日次 CSV は `config/retention.yaml` の `csv_timezone`、レポートは
`config/report.yaml`）なので、
OS の時刻帯が異なるなら `OnCalendar=` を合わせてください。

`coldaisle-report.service` は `Wants=` + `After=coldaisle-rollup.service` を持ちます。
停止をまたいで両タイマーが起動直後に追いついても、report を起動すると rollup が先に
queue され、その完了を待ってから走ります。手で `systemctl start coldaisle-report.service`
を実行したときも、先に rollup が1回走ります。

確認。

```bash
systemctl status coldaisle-daemon coldaisle-api
systemctl list-timers 'coldaisle-*'
journalctl -u coldaisle-daemon -f
curl -s http://127.0.0.1:8000/api/v1/health
```

### 受入基準の確かめ方

- **OS を再起動して、監視が自動で戻ること。** `sudo reboot` のあと
  `systemctl status coldaisle-daemon` が `active (running)` で、ダッシュボードに
  新しいサンプルが届いている
- **USB の差し込み口を変えても名前が変わらないこと。** 別のポートへ挿し直し、
  `ls -l /dev/server-sensors` が引き続き存在し、デーモンが再接続する
  （抜線中もデーモンは落ちず、再接続を待ちます。決定記録 0023）
- **落ちても10秒以内に戻ること**（NFR-01）。`sudo systemctl kill coldaisle-daemon`
  のあと、`RestartSec=5` で再起動する

### コードを更新するとき（DB の migration）

DB の migration は、更新後に**最初にストアを開いたプロセス**が当てます（API の最初の要求、
`coldaisle-rollup` のタイマーなど）。書き手を動かしたまま更新すると、移行前のコードを
読み込んだままの書き手が新しいスキーマに書くことになります。そのため、更新は次の順で行います。

1. **DB に書くものをすべて止める。** 取り込み（`coldaisle-daemon`）・API・タイマーに加え、
   制御デーモン（`coldaisle-fand`）・`coldaisle-telemetry`・`coldaisle-eventd` を常駐させていれば
   それも止めます
2. コードを更新し、`coldaisle-rollup` を1回だけ手で走らせて migration を当てる
3. 書き手とタイマーを起動し直す

```bash
sudo systemctl stop coldaisle-rollup.timer coldaisle-report.timer
sudo systemctl stop coldaisle-daemon coldaisle-api   # 常駐させている他の書き手も
# ここでコードを更新する（git pull と venv の同期）
sudo systemctl start coldaisle-rollup.service        # migration を当てる（終わるまで待つ）
sudo systemctl start coldaisle-daemon coldaisle-api
sudo systemctl start coldaisle-rollup.timer coldaisle-report.timer
```

- **表を作り直す migration は、その間ほかの書き込みを止めます。** たとえば 0007（#106）は
  decision trace の表（保持期間いっぱい、最大30日分の JSON）を作り直し、1つの書き込み
  トランザクションで行います。その間は取り込みも制御の trace も書けません。書き手を止めて
  から当てるのはこのためでもあります
- 手順を守らずに移行前の制御デーモンが動き続けた場合でも、trace は黙って欠けません。
  `control_traces` は `seq` の無い INSERT を trigger で拒否するので、制御ループのログに
  trace の保存失敗として出ます。気付いたら制御デーモンを再起動してください

## 5. Mac からのデータ移行

1. **Mac 側で DB に書くプロセスをすべて止める。** 取り込み（`coldaisle-daemon`）だけでなく、
   `coldaisle-telemetry`（周期メトリクス）・`coldaisle-eventd`（イベント）・
   `coldaisle-fand`（decision trace）・`coldaisle-rollup` / `coldaisle-report` も同じ DB に書きます。
   launchd などで常駐・定期実行させているなら、移行が終わるまで止めたままにします。
   動いたままバックアップすると、スナップショットのあとに書かれた行が移行先に入りません
2. **SQLite のオンラインバックアップで1ファイルに書き出す。** `var/coldaisle.db` を
   `cp` / `rsync` で直接運ぶのは避けます。WAL モードでは確定済みの書き込みが
   `-wal` に残っていることがあり、`PRAGMA wal_checkpoint(TRUNCATE);` も
   API やダッシュボードなどの読み手が DB を開いている間は `busy` を返して
   途中で止まるためです。`.backup` は読み手が残っていても整合した1ファイルを作ります

   ```bash
   sqlite3 var/coldaisle.db ".backup /tmp/coldaisle-migrate.db"
   sqlite3 /tmp/coldaisle-migrate.db 'PRAGMA integrity_check;'   # ok と出ること
   ```

   `-wal` / `-shm` を伴わない `/tmp/coldaisle-migrate.db` だけを運びます。
   `.backup` を使わずに元のファイルを運ぶ場合は、Mac 側の読み手（API・ダッシュボード・
   `sqlite3` のシェルなど）も**すべて**止めてから
   `sqlite3 var/coldaisle.db 'PRAGMA wal_checkpoint(TRUNCATE);'` を実行し、
   結果の1列目（busy）が `0`（`0|0|0` のように出る）であることを確かめます。
   `1` のときは読み手が残っています。止め切れない場合は `coldaisle.db` と
   `-wal`（あれば `-shm` も）を**組で**同じディレクトリへ運び、そこで
   `sqlite3 coldaisle.db ".backup /tmp/coldaisle-migrate.db"` を実行して1ファイルにしてから
   手順 3 へ進みます
3. **Ubuntu 側に置く。** DB に触るものを全部止めてから置き、所有者を揃えます。
   デーモンと API のほか、ロールアップと日次レポートのタイマーも止めます
   （置き換えの途中で起動して古い DB や書きかけの DB を開かないように）。
   前の DB の `-wal` / `-shm` が残っていると新しい DB に誤って重なるため、消してから置きます

   ```bash
   sudo systemctl stop coldaisle-rollup.timer coldaisle-report.timer
   sudo systemctl stop coldaisle-rollup.service coldaisle-report.service \
        coldaisle-daemon coldaisle-api
   rsync -av <mac>:/tmp/coldaisle-migrate.db /tmp/coldaisle.db
   sudo rm -f /var/lib/coldaisle/coldaisle.db-wal /var/lib/coldaisle/coldaisle.db-shm
   # 0660: coldaisle-fand も補助グループで書く（決定記録 0080 §2.1。0640 では fand が書けない）
   sudo install -o coldaisle -g coldaisle -m 0660 /tmp/coldaisle.db /var/lib/coldaisle/coldaisle.db
   sudo systemctl start coldaisle-daemon coldaisle-api
   sudo systemctl start coldaisle-rollup.timer coldaisle-report.timer
   ```

   タイマーは置き換えが済んでから戻します。手順 5 の再生を続けて行うなら、
   デーモンとタイマーは再生が終わってから起動しても構いません

4. **日次 CSV を運ぶ。** Mac の `~/server_sensor_logs/` を
   `/var/lib/coldaisle/server_sensor_logs/` へコピーし、所有者を `coldaisle` にします
5. **DB に無い古い CSV があれば、再生して取り込む**（決定記録 0010）

   ```bash
   cd /opt/coldaisle
   sudo -u coldaisle .venv/bin/coldaisle-daemon --source replay \
        --csv /var/lib/coldaisle/server_sensor_logs --bulk \
        --db /var/lib/coldaisle/coldaisle.db
   ```

   取り込みデーモン（シリアル）と同じ DB へ同時に書いてよいかは試していません。
   再生の間は `coldaisle-daemon.service` を止めておくのが安全です
6. **較正値と運用メモリ。** `config/calibration.json` と `memory/` をリポジトリで
   管理しているなら、`sudo git -C /opt/coldaisle pull` で揃います。手元だけで変えたものがあれば Mac から
   コピーします
7. 移行後に1回 `sudo systemctl start coldaisle-rollup.service` を実行し、
   `journalctl -u coldaisle-rollup` でロールアップが通ることを確かめます

## 6. coldaisle-fand（Fan 制御）の unit

決定の理由は決定記録 [`0080`](decisions/0080-fand-systemd-unit.md) と、authority の journal の置き場所・
承認者のグループについては [`0086`](decisions/0086-authority-approver-uid.md) §2.2 / §2.7 を見てください。
ここに書く名前（ユーザー `coldaisle-fan`・グループ `coldaisle-admin` / `coldaisle-authority`・
`/etc/coldaisle/` の下の置き場所・`/var/lib/coldaisle-authority`）は**すべて仮の値**です。
変えるなら unit・udev ルール・`control-admin.yaml` を揃えます。
**実機のユーザー名・ホスト名・path・driver 名・channel 番号はコミットしません**（AGENTS.md ルール10）。

> **この節の手順で `systemctl enable` も `systemctl start` もしません。** 有効化は 0080 §2.10 の
> 段階 5 と、0028 §2.9 の承認点 3 の後です。テンプレートを置いただけでは Fan 制御は有効になりません。

### 6.1 ユーザーとグループ

```bash
# fand 専用のユーザーと主グループ。coldaisle（API / 取り込み）と同じ uid にも root にもしない
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin coldaisle-fan
# 管理ソケットのグループ。入れるのは fand と操作する人だけ（coldaisle と AI 層のユーザーは入れない）
sudo groupadd --system coldaisle-admin
sudo usermod -aG coldaisle-admin coldaisle-fan
sudo usermod -aG coldaisle-admin <操作する人のユーザー名>

# Authority の昇格・rollback を行う人のグループ（決定記録 0086 §2.2）。入れるのは昇格を任せる人だけ
# （coldaisle と AI 層のユーザーは入れない）
sudo groupadd --system coldaisle-authority
# ログインし直すと反映される（いま開いている shell には新しいグループが付かない）
sudo usermod -aG coldaisle-authority <昇格を行う人のユーザー名>
# authority.json の置き場所。所有者 coldaisle-fan・グループ coldaisle-authority・2770（setgid）。
# systemd の StateDirectory= にはしない（グループが coldaisle-fan へ付け替えられる）ので、ここで作る
sudo install -d -o coldaisle-fan -g coldaisle-authority -m 2770 /var/lib/coldaisle-authority
```

- `coldaisle-fan` を **`coldaisle` グループには入れません。** DB を書く `coldaisle` は unit の
  `SupplementaryGroups=` だけで与えます。アカウントに入れると、unit の外でも DB を書けてしまいます
- `coldaisle-admin` への所属は、管理ソケットのグループを付け替えるために要ります
  （非 root のプロセスは自分の属するグループにしか付け替えられない）。同じ uid の接続は
  `allow_same_user: false` のもとで拒否されます（0080 §2.10 の段階 0）
- `coldaisle-fan` を **`coldaisle-authority` グループにも入れません。** fand が人の書いた `0660` の
  `authority.json` と lock を読み書きするための所属は、unit の `SupplementaryGroups=` だけで与えます
  （`coldaisle` と同じ考え方。アカウントに入れると unit の外でも journal を書けてしまいます）
- 昇格と、fand が止まっているときの rollback は `coldaisle-authority raise` / `rollback`
  （0086 §2.10 の段階 3b。#92）で行います。使い方・終了コードは `docs/authority-rollout.md`。
  承認者が制御設定を読み、Model Registry の lock を取るための権限は 6.7（決定記録 0104 / 0105）で与えます。
  **本番での `raise` は、6.7 の手順 9（実機での確認。0104 の段階 C）が導入先で通るまで使えません（#217）。**
  6.7 の手順の外で個別に権限を足して回避しないでください。
  `rollback` は authority のディレクトリだけを使うので、この手順のままで使えます
- グループへの所属は、`usermod` の後に**ログインし直してから**効きます（`dialout` と同じ）。
  いまの shell のまま CLI を実行すると `2770` のディレクトリへ入れず、権限の error で止まります。
  `id -nG` に `coldaisle-authority` が出ることを確かめてから使います
- **承認者は自分の uid のまま** `coldaisle-authority raise` / `rollback` を実行します（`sudo` も
  `sudo -u coldaisle-fan` も使いません）。記録される承認者は実行した人の `uid.<数値>` です
  （0086 §2.1）。root と fand のユーザー（= `/var/lib/coldaisle-authority` の所有者）の昇格は拒まれます
  （0086 §2.3。rollback は誰でも通ります）
- `/var/lib/coldaisle-authority` は **setgid（`2770`）が要ります。** 人が書いたファイルのグループが
  `coldaisle-authority` になり、fand が読み書きできるためです。CLI は書く前にディレクトリが
  「ディレクトリ・other に権限が無い・setgid 付き」であることを確かめ、違えば書かずに止まります。
  CLI はこのディレクトリを作りません（無ければ error。0086 §2.4）
- unit の `ReadWritePaths=` がこのディレクトリを指すので、**作る前に fand の unit を起動すると、
  起動の段階で失敗します**（制御は取りません）

### 6.2 設定の置き場所（root 所有）

制御の4ファイル（`fan-hardware.yaml` / `safety.yaml` / `fan-policy.yaml` / `air-balance.yaml`。
[`docs/control-config.md`](control-config.md)）と管理ソケットの設定は **root の所有**にし、fand 自身が
`safety.yaml` を緩められないようにします。

```bash
sudo install -d -o root -g coldaisle-fan -m 0750 /etc/coldaisle/control-config
# 4ファイルを置く（中身は docs/control-config.md。実運用のものをリポジトリに置かない）
sudo install -o root -g coldaisle-fan -m 0640 <用意した4ファイル> /etc/coldaisle/control-config/
sudo install -o root -g coldaisle-fan -m 0640 /opt/coldaisle/config/control-admin.yaml \
     /etc/coldaisle/control-admin.yaml
sudoedit /etc/coldaisle/control-admin.yaml
```

`/etc/coldaisle/control-admin.yaml` では次の2つを変えます。

- **`socket.path` を `/run/coldaisle/` の下にする**（例: `/run/coldaisle/fand-admin.sock`）。
  リポジトリの `config/control-admin.yaml` は開発用の相対 path（`var/run/...`）です。
  `/run/coldaisle` は fand の unit の `RuntimeDirectory=`（`0711`）で、引き継ぎ記録
  `/run/coldaisle/fan-handoff.json` と同じ場所です。`/var/run` はリンクなので使いません
  （親までのリンクを拒否します）
- `socket.group` を 6.1 で作ったグループ名にする（unit の `SupplementaryGroups=` と同じ名前）

Learned worker の経路（`config/learned-channel.yaml`。#86）を使うときも、2つの役割のソケットの
共通の祖先は起動前に用意し、両方の役割のグループがたどれるようにします（`0711` の
`RuntimeDirectory=` の下に役割ごとのディレクトリを置く。決定記録 0095 §2.7。unit テンプレートは
0077 の段階 6 で確定します）。

`/etc/coldaisle` は 2 節で `root:coldaisle`・`0750` にしてあります。fand は補助グループ
`coldaisle` でたどります。承認者には 6.7 で POSIX ACL の読み取りだけを足します（所有者・グループ・mode は
この節のまま変えません）。

**unit の `WatchdogSec=` は `safety.yaml` の `watchdog_timeout_ms` と同じ値にします。**
テンプレートの `5s` は試験の fixture と同じ仮の値です。unit の側が長いと fand は終了コード 4 で
制御を取らず、再起動もしません（0080 §2.3 / §2.4）。

### 6.3 hwmon の書き込み権限（udev）とドライバ

```bash
# hwmon のドライバは起動時に読み込む（fand は ProtectKernelModules=yes で読み込めない）
echo '<ドライバ名>' | sudo tee /etc/modules-load.d/coldaisle-hwmon.conf

sudo cp /opt/coldaisle/deploy/udev/99-coldaisle-hwmon.rules /etc/udev/rules.d/
# 導入先のファイルだけを、docs/fan-header-mapping.md で調べた driver 名と各 zone の pwmN に書き換える
sudoedit /etc/udev/rules.d/99-coldaisle-hwmon.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hwmon
# 対象の pwmN / pwmN_enable だけが coldaisle-fan・0664 になったことを見る
ls -l /sys/class/hwmon/hwmon*/pwm*
```

hwmon には `/dev` のノードが無いので、udev の `GROUP=` / `MODE=` は効きません。テンプレートは
`RUN+=` の `chgrp` / `chmod` で sysfs の属性を直接変えます（0080 §2.6）。

### 6.4 既存の導入先の移行（fand を起動する前に1回）

0069 のテンプレート（`StateDirectoryMode=0750`）や、以前の手順（`install ... -m 0640`）で置いた DB は
`0640` のままで、補助グループで `coldaisle` を持つだけの fand は書けません。unit の変更は
ディレクトリの mode しか変えず、`UMask` はこれから作るファイルにしか効かないためです。
**新規の導入でも、手順の途中で DB を置いたなら同じことを行います。**

1. DB に触る unit とタイマーを全部止めます（動いたままだと `-wal` / `-shm` が古い mode で作り直されます）

   ```bash
   sudo systemctl stop coldaisle-rollup.timer coldaisle-report.timer
   sudo systemctl stop coldaisle-rollup.service coldaisle-report.service \
        coldaisle-daemon coldaisle-api
   ```

2. 新しい unit（`StateDirectoryMode=2770`・`UMask=0007`）を置きます（4 節の `cp`）

   ```bash
   sudo systemctl daemon-reload
   ```

3. ディレクトリを `2770`・グループ `coldaisle` にし、DB と、あれば `-wal` / `-shm` / `-journal` を
   **すべて**グループ `coldaisle`・`0660` にします。`-shm` だけ `0640` で残ると fand は WAL の索引を
   開けません。setgid はこれから作るファイルの gid にしか効かないので、既存のファイルの gid も揃えます

   ```bash
   sudo chgrp coldaisle /var/lib/coldaisle
   sudo chmod 2770 /var/lib/coldaisle
   for f in /var/lib/coldaisle/coldaisle.db /var/lib/coldaisle/coldaisle.db-wal \
            /var/lib/coldaisle/coldaisle.db-shm /var/lib/coldaisle/coldaisle.db-journal; do
     # ディレクトリは coldaisle グループ以外から中を見られないので、存在の確認も sudo で行う
     if sudo test -e "$f"; then sudo chgrp coldaisle "$f" && sudo chmod 0660 "$f"; fi
   done
   ```

4. 確かめます。所有者・グループ・mode を一覧し、**unit と同じ主グループ・補助グループで**書き込み権を
   見ます。fand が `coldaisle` グループを得るのは unit の `SupplementaryGroups=` からなので、
   `sudo -u coldaisle-fan test -w` では確かめられません（アカウントのグループ一覧で動くため、
   実際の fand は書けるのに確かめだけが落ちます）。一時的な unit で同じグループを渡します

   ```bash
   for f in /var/lib/coldaisle /var/lib/coldaisle/coldaisle.db \
            /var/lib/coldaisle/coldaisle.db-wal /var/lib/coldaisle/coldaisle.db-shm \
            /var/lib/coldaisle/coldaisle.db-journal; do
     sudo test -e "$f" || continue
     sudo stat -c '%A %U:%G %n' "$f"
     sudo systemd-run --pipe --wait --quiet \
          -p User=coldaisle-fan -p Group=coldaisle-fan \
          -p "SupplementaryGroups=coldaisle coldaisle-admin coldaisle-authority" \
          test -w "$f" || echo "書けない: $f"
   done
   ```

   `systemd-run` は終了コードを返します。**1つでも「書けない」が出たら fand を起動しません。**
   ディレクトリは `drwxrws---`（`2770`）、ファイルは `-rw-rw----`（`0660`）・グループ `coldaisle` であること

5. 既存の unit とタイマーを戻し、取り込みが書き続けていること（`-wal` / `-shm` が作り直されても
   グループ `coldaisle`・`0660` であること）をもう一度 `stat` で見ます

   ```bash
   sudo systemctl start coldaisle-daemon coldaisle-api
   sudo systemctl start coldaisle-rollup.timer coldaisle-report.timer
   sudo sh -c "stat -c '%A %U:%G %n' /var/lib/coldaisle/coldaisle.db*"
   ```

移行を忘れても、fand は制御を取らずに起動時に `db_not_writable` を報告して終わります
（BIOS の制御のまま。終了コード 5 で再起動を繰り返します）。

### 6.5 unit を置いて検証する（`enable` しない）

```bash
sudo cp /opt/coldaisle/deploy/systemd/coldaisle-fand.service /etc/systemd/system/
sudoedit /etc/systemd/system/coldaisle-fand.service   # WatchdogSec= を safety.yaml に揃える（6.2）
sudo systemctl daemon-reload
# 実行ファイル・ExecStopPost・設定の構文を確かめる
sudo systemd-analyze verify /etc/systemd/system/coldaisle-fand.service
# 引き継ぎ実行部が導入先のシステムの Python で動くことを確かめる（unit の ExecStopPost= と同じ形）
/usr/bin/python3 --version
sudo /usr/bin/python3 -I -S /opt/coldaisle/src/coldaisle/safety_handoff.py; echo "exit=$?"
```

- `systemd-analyze verify` は CI に無いので（`tests/test_deploy_templates.py` は静的な試験だけ）、
  **導入先で人が走らせます。** 何も出なければ通っています。特に
  `... is not executable` が出ないこと（`ExecStart=` の `/opt/coldaisle/.venv/bin/coldaisle-fand` と
  `ExecStopPost=` の `/usr/bin/python3` が実在すること）を確かめます。出たまま起動すると fand は
  `READY=1` に届かず、deadman も tach の監視も動きません
- 引き継ぎ実行部（`ExecStopPost=`）は venv を使わず、システムの `/usr/bin/python3` で
  `/opt/coldaisle/src/coldaisle/safety_handoff.py` を引数なしで実行します。`/opt/coldaisle` は
  root の所有のままにします（fand のユーザーが書き換えられないように）
- 上の `safety_handoff.py` の実行は、`/usr/bin/python3` が **3.12 以上**（`pyproject.toml` の
  `requires-python` と同じ）で、`"record_found":false` と `exit=0` が出れば通っています。fand を
  まだ起動していないので引き継ぎ記録（`/run/coldaisle/fan-handoff.json`）は無く、何も書きません
  （記録が残っていれば Max・manual を書きます。冷却を弱める方向には書きません）。
  **3.12 より古い、または `exit=0` にならない導入先では fand の unit を置きません。**
  uv の管理する Python（2 節）や venv の `python` を `ExecStopPost=` に書き換えないでください
  （0080 §2.5 の「venv と `coldaisle` パッケージに依存しない」を崩します）
- **`sudo systemctl enable coldaisle-fand` は実行しません。** 0080 §2.10 の段階 5（simulated backend
  のまま、`kill -STOP`・`kill -KILL`・`systemctl stop`・再起動の連続で `ExecStopPost` が走ること・
  `/run/coldaisle` が残ること・順序を確かめる）と、0028 §2.9 の承認点 3 の後に行います。
  時間切れ（`TimeoutStartSec` / `TimeoutStopSec` / `TimeoutAbortSec`）・`WatchdogSec`・`RestartSec` は
  暫定値で、実機の測定と所有者の承認で決めます（0080 §5 の 1）

### 6.6 authority.json を専用のディレクトリへ移す（以前のテンプレートで fand を動かした導入先）

> **前提: 導入先のコードが 0086 §2.10 の段階 3a（#92。journal と lock を `0660` で作る `AuthorityStore`）を
> 含むこと。** それより前の版の fand は `0600` で作るので、このディレクトリで一度でも書くと承認者のグループが
> journal を読めず lock も取れなくなります。古いコードのまま、この節の移行も新しい unit への差し替えも
> 行いません（`enable` は 0080 §2.10 の段階 5 と承認点 3 の後）。

以前のテンプレート（決定記録 0080 のまま）は `authority.json` を fand 専用の状態ディレクトリ
（`/var/lib/coldaisle-fand/authority`。`0700`）に置いていました。決定記録 0086 §2.2 で、承認者のグループと
共有する `/var/lib/coldaisle-authority` へ移しました。以前のテンプレートで fand を一度でも起動した導入先は、
**新しい unit で起動する前に** journal と lock を移します（0086 §2.7）。fand をまだ起動したことが無ければ、
6.1 でディレクトリを作るだけで構いません。

**順序**: コード（パッケージ）を更新し → この手順で journal を移して unit を差し替え → fand を起動し →
それから `coldaisle-authority` の CLI を使います。journal v4（0086 §2.7）を読めない古い fand が
CLI の書いた journal を読むと、走行中なら `SHADOW` へ下がり、起動時なら終了コード 5 で止まります
（安全側ですが制御を取れません）。切り戻しの手順は `docs/authority-rollout.md` を見てください。

1. fand を止めます（6.1 のグループとディレクトリは先に作っておきます）

   ```bash
   sudo systemctl stop coldaisle-fand
   ```

2. 退避を取ってから、`authority.json` と、あれば `.authority.lock` を新しいディレクトリへ移します。
   退避は fand 専用の `0700` のディレクトリの中に置きます（他のユーザーから読めない場所）。
   手順 2〜5 は `old` / `new` を使うので、**同じシェルで続けて**実行します

   ```bash
   old=/var/lib/coldaisle-fand/authority
   new=/var/lib/coldaisle-authority
   if sudo test -d "$old"; then sudo cp -a "$old" /var/lib/coldaisle-fand/authority.before-0086; fi
   for name in authority.json .authority.lock; do
     if sudo test -e "$old/$name"; then sudo mv "$old/$name" "$new/$name"; fi
   done
   ```

3. グループを `coldaisle-authority`・mode を `0660` に揃えます。setgid はこれから作るファイルの gid にしか
   効かないので、移したファイルの gid も明示します

   ```bash
   for name in authority.json .authority.lock; do
     if sudo test -e "$new/$name"; then
       sudo chown coldaisle-fan:coldaisle-authority "$new/$name" && sudo chmod 0660 "$new/$name"
     fi
   done
   sudo stat -c '%A %U:%G %n' "$new" "$new"/authority.json "$new"/.authority.lock
   ```

   ディレクトリは `drwxrws---`（`2770`）・`coldaisle-fan:coldaisle-authority`、ファイルは
   `-rw-rw----`（`0660`）・グループ `coldaisle-authority` であること。6.4 の手順 4 と同じく、
   unit と同じグループを渡した一時的な unit で fand から書けることも確かめます

   ```bash
   for f in "$new" "$new"/authority.json "$new"/.authority.lock; do
     sudo test -e "$f" || continue
     sudo systemd-run --pipe --wait --quiet \
          -p User=coldaisle-fan -p Group=coldaisle-fan \
          -p "SupplementaryGroups=coldaisle coldaisle-admin coldaisle-authority" \
          test -w "$f" || echo "書けない: $f"
   done
   ```

   **1つでも「書けない」が出たら fand を起動しません。**

4. 新しい unit（`--authority-root /var/lib/coldaisle-authority`・`SupplementaryGroups=` と
   `ReadWritePaths=` に1つずつ足したもの）を 6.5 の手順で置き、`daemon-reload` と
   `systemd-analyze verify` を通します。導入先で unit を書き換えていた場合（`WatchdogSec=` など）は、
   その変更を新しいテンプレートへ移してから置きます

5. 古い置き場所が空になったことを確かめ、空のサブディレクトリを消します。何か残っていれば
   消さずに中身を確かめます（`rmdir` は空でなければ失敗します）。
   `/var/lib/coldaisle-fand` 自体と手順 2 の退避は `0700` のまま残します（unit の `StateDirectory=` が持つ）

   ```bash
   if sudo test -d "$old"; then sudo ls -A "$old"; sudo rmdir "$old"; fi
   ```

6. fand を起動し、`authority.json` を読めたことをログ（`journalctl -u coldaisle-fand`）で確かめます。
   起動時に `authority.json` を読めなければ fand は制御を取らずに終了コード 5 で終わります（0072 §2.6）。
   **`enable` はしません**（6 節の冒頭のとおり）

承認者が Model Registry の lock を取り、報告と制御設定を読むための権限は 6.7 で与えます（決定記録 0104）。

### 6.7 承認者の最小権限（制御設定の読み取りと Model Registry。決定記録 0104 / 0105）

決定の理由は決定記録 [`0104`](decisions/0104-authority-raise-least-privilege.md)（§2.1〜§2.6・§2.9）と
[`0105`](decisions/0105-registry-shared-root-settled-points.md) を見てください。ここに書く名前（グループ
`coldaisle-registry` / `coldaisle-authority`・`/var/lib/coldaisle-registry`）は**すべて仮の値**です。
**実機のユーザー名・ホスト名・path はコミットしません**（AGENTS.md ルール10）。

> **本番での `coldaisle-authority raise` は、この節の手順 9（実機での確認。0104 の段階 C）が導入先で通り、
> 結果を #217 に残すまで使えません。** 手順 1〜8 で権限を置いても、確認が済むまでは `raise` しません。
> `rollback` はこの節に関係なく使えます。この節の手順でも fand を `enable` も `start` もしません。

**誰が何をできるか**（0104 §2.1）

| 役割（仮の名前） | 制御設定 | Registry（`registry.json`・artifact） | `.registry.lock` | authority のディレクトリ |
|---|---|---|---|---|
| fand（`coldaisle-fan`） | 読む（6.2 のまま） | `--registry-root` を渡したときだけ読む（unit の補助グループ `coldaisle-authority` 経由の ACL） | 使わない | 読み書き（6.1） |
| 承認者（`coldaisle-authority` の人） | **読む**（ACL） | **読む**（ACL） | **flock だけ**（`r`） | 読み書き（6.1） |
| Registry の書き手（`coldaisle-registry` の人） | 触らない | 読み書き（グループ） | 読み書き（グループ） | 触らない |
| `coldaisle`（API / 取り込み）・AI 層 | 触らない | 触らない | 触らない | 触らない |

- 承認者も Registry の書き手も**自分の uid のまま**実行します（`sudo` も `sudo -u` も使いません。0086 §2.1）
- 同じ人が `coldaisle-registry` と `coldaisle-authority` の両方に入ることは認めます（0104 §5 の 3）。
  分けられるなら分けてください
- Learned worker の unit が artifact を読む権限は 0077 の段階 6 で決めます。worker を `coldaisle-authority` には
  入れません（journal を書けてしまうため）
- POSIX ACL を使います（Ubuntu の ext4 は既定で対応。`getfacl` / `setfacl` は `acl` パッケージ）。
  ACL が外れたときは `raise` が読めずに止まるだけで、journal も fand も変わりません（安全側）

1. **制御設定の読み取り**（0104 §2.2）。所有者・グループ・mode（6.2 の `root:coldaisle-fan`・`0750` / `0640`）は
   変えず、名前付きグループの ACL だけを足します

   ```bash
   # 親はたどれるだけにする（一覧は見せない。coldaisle.env と control-admin.yaml は読めないまま）
   sudo setfacl -m g:coldaisle-authority:--x /etc/coldaisle
   # 制御設定のディレクトリと4ファイル。default ACL で、install で置き直したファイルにも付ける
   sudo setfacl -m g:coldaisle-authority:r-x /etc/coldaisle/control-config
   sudo setfacl -d -m g:coldaisle-authority:r-- /etc/coldaisle/control-config
   # 4ファイルは名前で指す。導入する人の shell は 0750 の /etc/coldaisle をたどれないので、
   # `*.yaml` は sudo の前に展開されず、setfacl に文字のまま渡って失敗する
   for f in fan-hardware.yaml safety.yaml fan-policy.yaml air-balance.yaml; do
     sudo setfacl -m g:coldaisle-authority:r-- "/etc/coldaisle/control-config/$f"
   done
   ```

   - 書き込みは誰にも足しません。root の所有のままなので、fand も承認者も `safety.yaml` を緩められません
   - 4ファイルを `install -m 0640` で置き直すと、default ACL から読み取りが付きます。置き直した後は
     手順 8 の `getfacl` で確かめます
2. **Registry の書き手のグループ**（0104 §2.3）

   ```bash
   sudo groupadd --system coldaisle-registry
   # ログインし直すと反映される。coldaisle と AI 層のユーザー・fand は入れない
   sudo usermod -aG coldaisle-registry <Registry を書く人のユーザー名>
   ```

3. **Registry のディレクトリ**（`root:coldaisle-registry`・`2770`・承認者に ACL の読み取り）

   ```bash
   sudo install -d -o root -g coldaisle-registry -m 2770 /var/lib/coldaisle-registry
   sudo setfacl -m g:coldaisle-authority:r-x /var/lib/coldaisle-registry
   sudo setfacl -d -m g:coldaisle-authority:r-X /var/lib/coldaisle-registry
   ```

   - 所有者は root にします。書き手のグループの人がディレクトリの mode や ACL を変えられないようにするためです
   - systemd の `StateDirectory=` にはしません（所有者とグループが unit の `User=` / `Group=` へ付け替えられる）
   - Registry が作るファイルは `umask` に依らず `0640`、root の下に作るディレクトリは `2770`（親の bit を写す）に
     なります（0104 §2.3 の実装）。default ACL から承認者の読み取りが付きます
4. **lock は導入手順で作ります。** どの CLI も、共有の root（setgid 付き）では `.registry.lock` を作りません
   （0104 §2.4 / 0105 §2.5）。無ければ書き手も承認者も `RegistrySharedRootError` で止まります

   ```bash
   sudo install -o root -g coldaisle-registry -m 0660 /dev/null /var/lib/coldaisle-registry/.registry.lock
   sudo setfacl -m g:coldaisle-authority:r-- /var/lib/coldaisle-registry/.registry.lock
   ```

   - 承認者に要るのは `r` だけです。`coldaisle-authority raise` は lock を `O_RDONLY` で開いて `flock` だけを
     取ります（0104 §5 の 5）
   - lock が消えたら、同じ2行で作り直します。lock を作り直すまで Registry の書き込みも `raise` も止まります
     （壊れずに止まる）
   - Registry はローカルのファイルシステムに置きます（NFS に置かない。`flock` の前提）
   - **書き手のコマンドが返ってこないとき**は、誰かが lock を握っている可能性があります（書き手は lock を
     待ち続けます。0104 §2.4）。握っているプロセスを見つけます

     ```bash
     sudo fuser -v /var/lib/coldaisle-registry/.registry.lock
     # fuser が無い・判別できないときは、lock の inode を /proc/locks で探す
     ino=$(sudo stat -c '%i' /var/lib/coldaisle-registry/.registry.lock)
     grep ":$ino " /proc/locks    # 3列目が FLOCK、5列目が握っている pid
     ps -o pid,user,etime,cmd -p <pid>
     ```

     `coldaisle-authority raise` が承認と証拠の検証中であれば、終わるまで待ちます。止まったまま
     （`kill -STOP` された・端末で一時停止したなど）のプロセスなら、その持ち主に終わらせてもらいます。
     **制御（fand）は Registry の lock を取らない**ので、この待ちは Fan 制御を止めません（0104 §2.4）
5. **Registry の書き手の使い方。** 自分の uid のまま `--root /var/lib/coldaisle-registry` を指します
   （使い方は `docs/model-registry.md`。承認は 0062 のとおりファイルで渡す）

   ```bash
   cd /opt/coldaisle
   .venv/bin/coldaisle-registry status --root /var/lib/coldaisle-registry
   ```

6. **承認者の使い方と照合**（0104 §2.6）。`coldaisle-authority raise` の `--config-dir` と `--registry-root` には、
   **fand の unit の `ExecStart=` と同じ path** を渡します（`--config-dir /etc/coldaisle/control-config`。
   `--registry-root` は fand に Registry を渡す unit（0077 の段階 6）の値と同じ `/var/lib/coldaisle-registry`）。
   CLI は path が fand と同じかを確かめません。取り違えた・fand の起動後に差し替えた設定や artifact で上げても、
   fand は起動時に読んだ設定・artifact と照らして実効 stage の上限を Baseline にします（0089 / 0090。安全側）。
   上げた後に、上げた stage が**効いている**ことを fand のログで確かめます

   ```bash
   journalctl -u coldaisle-fand -o cat | grep -E 'authority_(config|artifact)_(matched|mismatch|unbound)' | tail -n 4
   ```

   `authority_config_matched` と `authority_artifact_matched` が出ていれば効いています。`_mismatch` /
   `_unbound` なら journal は上がっていても実効は Baseline です（trace の `authority_config_binding_ceiling` /
   `authority_artifact_ceiling` でも見られます）。設定や artifact を入れ替えるときの手順は
   `docs/authority-rollout.md` を見てください
7. **途中で止まったディレクトリを直す**（0105 §2.3）。Registry はディレクトリを作ってから mode を決めます。
   その間でプロセスが止まると、`artifacts/...` の下に `2700` のディレクトリが残り、他の書き手や読み手が入れない
   ことがあります（自動では直しません。壊れずに止まる）。該当を一覧し、親と同じ `2770` に揃えます
   （所有者＝作った書き手の uid か root で）

   ```bash
   sudo find /var/lib/coldaisle-registry/artifacts -type d -perm -2000 ! -perm -0070 -exec ls -ld {} +
   sudo chmod 2770 <該当するディレクトリ>
   ```

   詳しくは `docs/model-registry.md`「mode を決める前に止まったディレクトリを直す」
8. **形を確かめます**

   ```bash
   sudo getfacl /etc/coldaisle /etc/coldaisle/control-config \
        /etc/coldaisle/control-config/{fan-hardware,safety,fan-policy,air-balance}.yaml \
        /var/lib/coldaisle-registry /var/lib/coldaisle-registry/.registry.lock
   sudo stat -c '%A %U:%G %n' /var/lib/coldaisle-registry /var/lib/coldaisle-registry/.registry.lock
   ```

   `group:coldaisle-authority` の行が手順 1・3・4 のとおりで、`mask::` が実効の権限を削っていないこと
   （制御設定の4ファイルと Registry のデータのファイル（`registry.json`・`artifact.payload`）は `mask::r--`、
   ディレクトリは `mask::rwx` / `r-x`、**`.registry.lock` は `mask::rw-`**）。lock の mask を `r--` に下げないで
   ください。書き手は lock を `O_RDWR` で開くので、グループ `coldaisle-registry` の書き込みが削られると Registry の
   書き込みがすべて lock を開くところで失敗します（承認者の `group:coldaisle-authority:r--` は mask `rw-` の下でも `r` のまま効く）。
   Registry の root は `drwxrws---+`・`root:coldaisle-registry`、lock は `-rw-rw----+`・`root:coldaisle-registry`
   であること

9. **実機での確認**（0104 §2.9。段階 C。**人が行います**）。どれか1つでも期待と違えば `raise` は使いません

   **役割ごとの確認は、その役割のグループだけを持つアカウントで行います。** 同じ人が `coldaisle-authority` と
   `coldaisle-registry` を兼ねる（上の表の下の注記で認めている）アカウントでは、2 の「書けない」と 3 の
   「authority のディレクトリへ書けない」は成り立ちません（もう一方のグループの権限で書けるため）。兼任の
   アカウントしか無い導入先では、確認のためだけの一時アカウントを役割ごとに作り（`useradd --system
   --no-create-home --shell /usr/sbin/nologin`。それぞれ片方のグループにだけ入れる）、`sudo -u <一時アカウント>`
   で 2 と 3 を実行し、確認の後に `userdel` で消します。この `sudo -u` は権限の形を確かめるためだけで、
   本物の `raise` や Registry への書き込みには使いません（承認者は自分の uid で実行する。0086 §2.1）。
   3 の書き込み（`register`）は、一時アカウントではなく実際の書き手の uid で行います

   1. 手順 8 の `getfacl` の形が期待どおりであること
   2. **承認者の uid で**（ログインし直し、`id -nG` に `coldaisle-authority` が出てから。`sudo` を使わない）
      - 読める: `cat /etc/coldaisle/control-config/*.yaml > /dev/null`、
        `cat /var/lib/coldaisle-registry/registry.json > /dev/null`（Registry にまだ何も無ければ、
        `registry.json` の確認は手順 3 の書き込みの後に行う）
      - flock を取れる: `flock -n /var/lib/coldaisle-registry/.registry.lock true; echo $?` が `0`
      - **読めない・書けない**（すべて権限の error になること）: `cat /etc/coldaisle/coldaisle.env`、
        `cat /etc/coldaisle/control-admin.yaml`、`ls /etc/coldaisle`、`touch /etc/coldaisle/control-config/x`、
        `touch /var/lib/coldaisle-registry/x`、`sh -c ': >> /var/lib/coldaisle-registry/registry.json'`、
        `sh -c ': >> /var/lib/coldaisle-registry/.registry.lock'`
   3. **Registry の書き手の uid で**: `coldaisle-registry status --root /var/lib/coldaisle-registry` が通り、
      書く操作（開発用の artifact の `register` など）の後も 2 の「読める」が通ること。作られたファイルが `0640`・
      グループ `coldaisle-registry`・ACL 付きであることを `getfacl` で見る。書き手が authority のディレクトリへ
      書けないこと（`touch /var/lib/coldaisle-authority/x` が失敗する）
   4. **fand と同じグループで**（6.4 の手順 4 と同じ `systemd-run`）: 制御設定と `registry.json` を読めて、
      どちらにも書けないこと

      ```bash
      for f in /etc/coldaisle/control-config/safety.yaml /var/lib/coldaisle-registry/registry.json; do
        sudo systemd-run --pipe --wait --quiet \
             -p User=coldaisle-fan -p Group=coldaisle-fan \
             -p "SupplementaryGroups=coldaisle coldaisle-admin coldaisle-authority" \
             sh -c "test -r '$f' && ! test -w '$f'" || echo "期待と違う: $f"
      done
      ```

   5. **`raise` が読み取りと lock を通ること**（本物の昇格はしない。0104 §5 の 7）。承認者の uid で、
      **期限内で形の正しい、`expected_revision` だけが journal と合わない承認**を渡します。`raise` はこれを
      Registry の lock と authority の lock を取った**後**に拒みます（期限切れの承認は Registry を読む前に拒まれるので
      確認に使えません）。期待する結果は終了コード 4・`code` が `approval_rejected`（revision の不一致）で、
      journal は変わりません。終了コード 1 の `io_or_config_error`（制御設定を読めない）や `registry_error`
      （Registry を読めない。0105 §2.4）なら権限が足りていません。

      **前提: Registry に thermal_model の Production artifact があること。** `raise` は Registry の lock を
      取って Production を読んだ直後、authority の lock と `expected_revision` を見る**前**に、Production が無ければ
      拒みます。Production がまだ無い導入先（3 で candidate を登録しただけ、など）では、期待する結果は
      終了コード 4・`code` が `evidence_rejected` で、構造化ログの `error` が「Production の artifact が無い」
      ことです。これでも制御設定の読み取りと Registry の読み取り・lock は確かめられますが、authority の lock までは
      届かないので、Production を置いた後に `approval_rejected` の確認をもう一度行ってから `raise` を使います。
      どちらの場合も承認の期限内であることと、承認の `to_stage` が `fan-policy.yaml` の上限以下であることが要ります
      （それより前の検査で拒まれるため）
   6. 結果（どの確認が通ったか・`getfacl` の形）を #217 に残します。**実機のユーザー名・ホスト名・path は
      書きません**（仮の名前に置き換える）

   9 がすべて通り、#217 に結果を残したら、この節の冒頭と 6.1 の「本番での `raise` は使えません」の注記を
   外す PR を作ります（所有者の承認の後）。
