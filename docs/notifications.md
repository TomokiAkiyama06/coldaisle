# 通知の到達確認（Slack）

通知の規約は決定記録 [0013](decisions/0013-notifications.md)、宛先は [0135](decisions/0135-critical-notifications-to-slack.md) にあります。
この文書は、**実物の Webhook / トークンで本当に届くか**を確かめる手順です（#51 / 0013 §2.7・§5 未決2）。
テストは宛先を差し替えて行っているため、外部サービスへの到達はここでしか確かめられません。

## 1. 宛先と変数

| 重大度 | 宛先（`config/notify.yaml` の `routing`） | 要る環境変数 |
|---|---|---|
| critical | Slack + stdout | `COLDAISLE_SLACK_WEBHOOK` |
| warning | Slack + stdout | `COLDAISLE_SLACK_WEBHOOK` |
| info | stdout | なし |

- `COLDAISLE_SLACK_WEBHOOK`: 監視専用チャンネルの Incoming Webhook の URL

critical と warning は**同じ Webhook・同じチャンネル**に届きます（決定記録 0135）。
見分けるのは本文の先頭の行の記号と `[重大度]` です（発火は critical が 🔴、warning が 🟠、
解除は ✅ に `[critical 解除]` / `[warning 解除]`。決定記録 0135 §2.2）。
夜間（22時〜7時）は critical だけが届き、warning は届きません（`night.warning_enabled: false`）。

### LINE を使う場合

LINE は今は使っていません。`LineNotifier` のコードは残してあるので、使うときは
`config/notify.yaml` の `routing` の宛先に `line` を足し、次の2つを**両方**設定します
（片方だけでは宛先が作られません。決定記録 0013 §2.7）。

- `COLDAISLE_LINE_TOKEN`: LINE Messaging API のチャネルアクセストークン（長期）
- `COLDAISLE_LINE_TO`: 送信先のユーザー ID（`U` で始まる）またはグループ ID（`C` で始まる）。
  自分宛てなら LINE Developers のチャネル設定にある「あなたのユーザー ID」。
  送信先は公式アカウントを友だち追加しておく（グループならそのグループに招く）

**値はリポジトリに書きません**（public。決定記録 0013 §2.1）。
置き場所は開発機なら `.env`（git 管理外）、導入先なら `/etc/coldaisle/coldaisle.env`
（[`ubuntu-deploy.md`](ubuntu-deploy.md) §2）。雛形は `.env.example` です。

未設定の宛先は作られず、起動時に1度だけ次の警告が出ます。

```text
"msg": "設定されていない通知先がある（その重大度は残りの宛先だけに届く）", "missing": ["slack"]
```

`missing` が空にならない限り、その重大度は stdout（構造化ログ）にしか出ていません。

### ダッシュボードのリンク

通知の本文の最後の行はダッシュボードのリンクです。既定は `config/notify.yaml` の
`dashboard_url`（`http://127.0.0.1:8000/`）で、**サーバー自身から見たアドレス**なので、
Slack を Mac・スマホで開いて押しても開けません（#307）。

- `COLDAISLE_DASHBOARD_URL`: 別の端末から開けるダッシュボードのアドレス。設定すると
  `dashboard_url` を置き換えます。アラートの通知・日次レポート（`coldaisle-report`）・
  故障疑いの案件（`coldaisle-escalate`）の通知が同じ値を使います

導入先の実際のアドレスはリポジトリに書けない（public。AGENTS.md ルール 10）ため、
導入先では `/etc/coldaisle/coldaisle.env` に置きます。例（仮の値）:

```bash
COLDAISLE_DASHBOARD_URL=https://dashboard.example.invalid/
```

- **空文字は未設定と同じ**に扱い、`config/notify.yaml` の値を使います（`.env.example` を
  写したままの空の行で既定を潰さないため）
- 値は起動時に検査します。`http` か `https` の絶対 URL で、ホストを含むこと。
  scheme が無い・相対・ホストが無い・ポートが数字でない・空白を含む値は、起動時に
  `COLDAISLE_DASHBOARD_URL: ...` で始まる `ValueError` で落ちます（壊れたリンクを最初の通知で
  知ることにしないため）。エラーの文に値そのものは入れません
- 環境変数が無いときは `config/notify.yaml` の `dashboard_url` を同じ規則で検査します

## 2. 試しに鳴らす（MockSource）

`notify_check` シナリオは、critical（`RAPID_RISE`）と warning（`INTAKE_HIGH`）を
**1回ずつ発火させて解除します。** Slack へ届くのは次の4通です（届く順）。

| 順 | 重大度 | 届くもの | 本文の先頭の行 |
|---|---|---|---|
| 1 | critical | `RAPID_RISE` の発火 | `🔴 [critical] RAPID_RISE [air.gpu_intake] — firing` |
| 2 | critical | `RAPID_RISE` の解除 | `✅ [critical 解除] RAPID_RISE [air.gpu_intake] — resolved` |
| 3 | warning | `INTAKE_HIGH` の発火 | `🟠 [warning] INTAKE_HIGH [air.gpu_intake] — firing` |
| 4 | warning | `INTAKE_HIGH` の解除 | `✅ [warning 解除] INTAKE_HIGH [air.gpu_intake] — resolved` |

正常でも、各アラートには発火と解除の **2通** が届きます。
**同じ遷移（例: `RAPID_RISE` の発火）が2通以上届いたら**、連投の抑制が効いていません（#51 の受入基準）。
LLM による発火の説明が別に1通届くのは正常で、連投には当たりません（下の注記）。
同じ流れはテスト `tests/test_rules.py::test_notify_check_scenario_reaches_each_target_once`
で宛先を差し替えて固定しています。

```bash
# 本番の DB を汚さない。使い捨ての DB を使う
TMP_DB="$(mktemp -d)/notify-check.db"
uv run --env-file .env coldaisle-daemon --source mock --scenario notify_check --speed 60 --db "$TMP_DB"
```

- `--speed 60` で12分のシナリオが約12秒で終わります
- **昼（7時〜22時。`config/notify.yaml` の `night`）に行ってください。**
  夜間は warning を送らない設定（`night.warning_enabled: false`）なので、`INTAKE_HIGH` の2通が届きません。
  シナリオ時間で12分進むため、21時45分以降も避けます。夜に確かめるときは下の「夜に確かめたいとき」
- 鳴る値は暫定の閾値（`config/rules.yaml`）によるもので、「この値が危険」という意味ではありません。
  受け取る側のチャンネル・グループには、試験であることを先に書いておくと取り違えません
- `config/ai.yaml` で説明を有効にしていて LLM に繋がる場合は、発火の説明が追加で届くことがあります。
  これは正常で、発火1回につき説明は1通までです（終了時の `notifications` の数には含まれません）

導入先の `/etc/coldaisle/coldaisle.env` で確かめる場合は、そのファイルを読めるユーザーで
同じコマンドを `--env-file /etc/coldaisle/coldaisle.env` にして実行します。
シリアルを開かない（mock）・DB も別なので、稼働中の取り込みとは干渉しません。

### 夜に確かめたいとき

夜間は warning が抑えられるため、そのままでは4通が揃いません。
**本番の `config/notify.yaml` は変えず**、一時的な場所へ写した設定で `night.warning_enabled: true` にし、
`--notify` でその写しを渡します。写しは確かめた後に消します。

```bash
WORK="$(mktemp -d)"
cp config/notify.yaml "$WORK/notify.yaml"
sed -i 's/^  warning_enabled: false$/  warning_enabled: true/' "$WORK/notify.yaml"
grep -n 'warning_enabled' "$WORK/notify.yaml"   # true になったことを確かめる

uv run --env-file .env coldaisle-daemon --source mock --scenario notify_check --speed 60 \
  --db "$WORK/notify-check.db" --notify "$WORK/notify.yaml"
rm -r "$WORK"
```

導入先で `/etc/coldaisle/coldaisle.env` を使う場合、そのファイルが root にしか読めないなら、
root で環境変数を読み込んでから運用者のユーザーへ切り替えて実行します
（`operator` と path は仮の値。導入先のものに置き換える）。写しは先に運用者のユーザーで、
そのユーザーが読み書きできる場所に作っておきます（使い捨ての DB も同じ場所に作られる）。

```bash
# 運用者のユーザーで（写しの作り方は上と同じ）
mkdir -p /tmp/notify-check
cp /opt/coldaisle/config/notify.yaml /tmp/notify-check/notify.yaml
sed -i 's/^  warning_enabled: false$/  warning_enabled: true/' /tmp/notify-check/notify.yaml

# 環境変数だけ root で読み、実行は運用者のユーザーで
sudo bash -c 'set -a; . /etc/coldaisle/coldaisle.env; set +a; cd /opt/coldaisle && runuser -u operator -p -- .venv/bin/coldaisle-daemon --source mock --scenario notify_check --speed 60 --db /tmp/notify-check/notify-check.db --notify /tmp/notify-check/notify.yaml'
```

- `runuser -p` で読み込んだ環境変数を引き継ぎます。daemon 自体は root で動かしません
- 終わったら `rm -r /tmp/notify-check` で写しと DB を消します
- 稼働中の `coldaisle-daemon` の設定（`config/notify.yaml`）は変わりません。夜間の warning の
  本番での有効化は下の「まだ決まっていないこと」のとおり、この手順では行いません

## 3. 結果の見方

ログ（JSON Lines）で次を確かめます。

| 見るもの | 期待 |
|---|---|
| 起動時の `missing` | 警告が出ない（または確かめたい宛先が含まれない） |
| 終了時の `"取り込みを終了する"` の `notifications` | 4（発火2 + 解除2。夜間にそのまま走らせると warning の2件が抑えられて 2 になる） |
| `"通知の送信に失敗した"` | 出ない。出たら `target` が失敗した宛先 |

送信に失敗しても取り込みは止まりません（決定記録 0013 §2.4）。よくある原因:

- Slack が `404` / `403`: Webhook の URL の写し間違い、またはチャンネル・アプリの削除
- （LINE を使う場合）LINE が `401`: トークンの誤り・失効
- （LINE を使う場合）LINE が `400`: `COLDAISLE_LINE_TO` の誤り、または送信先が公式アカウントを友だち追加していない

## 4. 導入先へ反映する

`/etc/coldaisle/coldaisle.env` を書き換えたら、`coldaisle-daemon` を再起動するまで反映されません
（環境変数は起動時にだけ読みます）。再起動の後、`journalctl -u coldaisle-daemon` で
起動時の `missing` の警告が消えたことを確かめます。
`COLDAISLE_DASHBOARD_URL` も同じで、再起動するまで通知のリンクは変わりません
（日次レポートは次に `coldaisle-report.service` が走ったときから）。

## 5. まだ決まっていないこと

- **夜間の warning の有効化**（`night.warning_enabled`）は、ベースライン測定（#50）で
  閾値を確定してから判断します（決定記録 0013 §2.5）。この手順では変えません
