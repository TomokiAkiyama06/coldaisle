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
sudo git clone <このリポジトリの URL> /opt/coldaisle
sudo chown -R coldaisle:coldaisle /opt/coldaisle
sudo -u coldaisle ln -s /var/lib/coldaisle /opt/coldaisle/var
cd /opt/coldaisle && sudo -u coldaisle uv sync --no-dev
```

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
   動いたままコピーすると、WAL に残った書き込みが欠けます
2. **DB の整合を取ってからコピーする。** 止めたあと、Mac 側で

   ```bash
   sqlite3 var/coldaisle.db 'PRAGMA wal_checkpoint(TRUNCATE);'
   ```

   を実行し、`var/coldaisle.db` を1ファイルで運びます（`-wal` / `-shm` が残っていれば
   それも一緒に）
3. **Ubuntu 側に置く。** サービスを止めてから置き、所有者を揃えます

   ```bash
   sudo systemctl stop coldaisle-daemon coldaisle-api
   rsync -av <mac>:<リポジトリ>/var/coldaisle.db /tmp/coldaisle.db
   sudo install -o coldaisle -g coldaisle -m 0640 /tmp/coldaisle.db /var/lib/coldaisle/coldaisle.db
   sudo systemctl start coldaisle-daemon coldaisle-api
   ```

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
   管理しているなら、`git pull` で揃います。手元だけで変えたものがあれば Mac から
   コピーします
7. 移行後に1回 `sudo systemctl start coldaisle-rollup.service` を実行し、
   `journalctl -u coldaisle-rollup` でロールアップが通ることを確かめます
