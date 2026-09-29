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

## この PR に含めないもの

- **`coldaisle-fand`（3系統 Fan 制御）の unit は含めません。** `Type=notify` /
  `WatchdogSec` / `Restart` / `ExecStopPost`（Max への引き継ぎ）の中身は
  Critical Safety の deadman そのものであり、決定記録 0060 の未決 7 として
  **人の判断が必要な安全系の論点**です。別の Issue で決めます。
- `coldaisle-telemetry`（Internal Telemetry）と `coldaisle-eventd`（書き込みソケット。
  決定記録 0045 未決 4）の unit も含めません。
- 日次 CSV（`coldaisle-rollup --export-day`）の自動実行は配線していません。
  対象日を毎回渡す必要があり、自動化の方法はまだ決めていません（手で実行はできます）。

---

## 1. 置き場所

| もの | 場所 |
|---|---|
| コード（`git clone` したもの）と `.venv` | `/opt/coldaisle` |
| DB・レポート・案件資料など実行時のデータ | `/var/lib/coldaisle`（`/opt/coldaisle/var` からリンクする） |
| 日次 CSV | `/var/lib/coldaisle/server_sensor_logs`（下の注記） |
| 秘匿情報（`.env.example` の変数） | `/etc/coldaisle/coldaisle.env`（任意） |
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

sudo install -d -o coldaisle -g coldaisle -m 0750 /var/lib/coldaisle
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
sudo cp /opt/coldaisle/deploy/systemd/coldaisle-*.service \
        /opt/coldaisle/deploy/systemd/coldaisle-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coldaisle-daemon.service coldaisle-api.service
sudo systemctl enable --now coldaisle-rollup.timer coldaisle-report.timer
```

タイマーの時刻は OS の時刻帯で解釈されます（`timedatectl` で確認）。
日境界は `Asia/Tokyo`（`coldaisle-rollup --timezone` / `config/report.yaml`）なので、
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

## 5. Mac からのデータ移行

1. **Mac 側の取り込みを止める。** launchd などで常駐させているなら止めます。
   動いたままコピーすると、コピーのあとに書かれたサンプルが移行先に入りません
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
   sudo install -o coldaisle -g coldaisle -m 0640 /tmp/coldaisle.db /var/lib/coldaisle/coldaisle.db
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
