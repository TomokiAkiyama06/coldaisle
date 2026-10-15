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

## 1a. 発火の通知にグラフを添える（Slack の Bot トークン）

重大（critical）・警告（warning）の**発生**の通知に、対象の値と関係する値（派生値ならその材料・室温）の
**直近 60 分のグラフ**を、同じ投稿のスレッドへ添えます（決定記録 [0147](decisions/0147-slack-alert-graph.md)）。
Incoming Webhook ではファイルを添付できないため、Slack アプリの **Bot トークン**が要ります。

| 環境変数 | 値 |
|---|---|
| `COLDAISLE_SLACK_BOT_TOKEN` | Bot User OAuth Token（`xoxb-` で始まる） |
| `COLDAISLE_SLACK_CHANNEL` | 送り先のチャンネルの **ID**（`C` で始まる。`#名前` ではない） |

- **両方**あると、本文も Bot（`chat.postMessage`）で送り、その投稿のスレッドへグラフを上げます。
  片方だけなら Bot を使わず、起動時に `Slack の Bot は トークンとチャンネルの両方が要る…` の警告が出ます
- 無ければ今までどおり Webhook で本文だけを送ります
- `COLDAISLE_SLACK_WEBHOOK` も残しておくと、Bot の送信が失敗したとき（トークンの失効・Bot がチャンネルから
  外れたなど）にその通知を Webhook で送り直します（グラフは付きません）
- 解除・発火の説明・日次レポートにはグラフを添えません。同じアラートのグラフは `repeat_after_s`（30 分）に 1 回まで
- グラフの設定は `config/notify-graph.yaml`。壊れていても文章の通知は止まらず、グラフだけが付かなくなります
  （起動時に `グラフの設定が読めないので…` の error が出ます）

### Bot トークンの作り方（所有者が行う）

1. ブラウザで <https://api.slack.com/apps> を開き、**Create New App** → **From scratch**。
   App Name（例 `coldaisle`）と、通知を受けるワークスペースを選んで **Create App**
   （既に Incoming Webhook 用のアプリがあれば、それを開いて 2. から進めてもかまいません）
2. 左の **OAuth & Permissions** を開き、**Scopes** の **Bot Token Scopes** で **Add an OAuth Scope** から
   次の 2 つだけを足す（**ほかの scope は足さない**）
   - `chat:write`（本文を投稿する）
   - `files:write`（グラフをスレッドへ上げる）
3. 同じ画面の上の **Install to Workspace**（既に入っていれば **Reinstall to Workspace**）→ **許可する**
4. 表示された **Bot User OAuth Token**（`xoxb-…`）を控える。**チャットや Issue に貼らない**
5. Slack で通知のチャンネルを開き、メッセージ欄で `/invite @coldaisle`（1. の App Name）を送って Bot を招く。
   招かないと `not_in_channel` で失敗します
6. チャンネル名をクリック → 詳細の一番下にある **チャンネル ID**（`C` で始まる）を控える

### 導入先へ置く

値はリポジトリにも、コマンドの引数（シェルの履歴・`ps` に残る）にも書きません。
**別のターミナル**で次を実行し、聞かれたら貼り付けて Enter を押します（画面に表示されません）。

```bash
sudo bash -c 'read -rsp "Bot User OAuth Token: " t; echo; printf "COLDAISLE_SLACK_BOT_TOKEN=%s\n" "$t" >> /etc/coldaisle/coldaisle.env'
sudo bash -c 'read -rp "チャンネル ID (C...): " c; printf "COLDAISLE_SLACK_CHANNEL=%s\n" "$c" >> /etc/coldaisle/coldaisle.env'
```

- 既に同じ名前の行（`.env.example` から写した空の `COLDAISLE_SLACK_BOT_TOKEN=` など）があれば、
  `sudoedit /etc/coldaisle/coldaisle.env` で**空の行を消して**から追記します（同じ名前が 2 行あると後の行が効きます）
- 権限は変わりません（`0640`・`root:coldaisle`。[`ubuntu-deploy.md`](ubuntu-deploy.md) §2）。
  `sudo stat -c '%a %U:%G' /etc/coldaisle/coldaisle.env` で確かめます
- 反映には `coldaisle-daemon` の再起動が要ります（§4）。起動のログに Bot の警告が出ていないことを確かめます

開発機で試すときは `.env`（git 管理外）に同じ 2 行を書き、§2 の `notify_check` を流します。
`RAPID_RISE` と `INTAKE_HIGH` の発火の投稿のスレッドに、グラフが 1 枚ずつ付けば届いています
（mock の DB には直近の値しか無いので、グラフは短い線になります）。

| ログ | 意味 |
|---|---|
| `グラフを添えられなかった` の `slack: files.completeUploadExternal: not_in_channel` | Bot をチャンネルに招いていない（5.） |
| `… missing_scope` | `files:write` / `chat:write` が足りない（2.）。足したら **Reinstall** が要る |
| `… invalid_auth` / `not_authed` | トークンの写し間違い・失効 |
| `Slack の Bot で送れなかったので Webhook で送り直す` | Bot の本文の送信が失敗し、Webhook で届けた（理由は `reason`） |
| `描く値が無いのでグラフを添えない` | 直近 60 分に値が無い（センサーの欠けなど）。本文は届いている |

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

### 発火が遅れるとき（センサーの欠け・無音）

しきい値・範囲の 5 つのルール（`recirculation`・`intake_high`・`airflow_degraded`・`room_high`・
`humidity_out_of_range`）は、発火待ちの間に入力の欠け・サンプルの無音で次のアラートが**発火すると**、
発火待ちを取り消し、回復の後に条件が成立した時刻から数え直します（決定記録 0139）。

| 先に届くアラート | 条件（`config/rules.yaml`） | 数え直すルール |
|---|---|---|
| `SENSOR_MISSING`（warning） | 入力のメトリクスが `sensor_missing.consecutive`（5）サンプル連続で ok でない | その入力に依るルール（派生値は 2 つの入力のどちらでも） |
| `SENSOR_FAULT`（critical） | サンプルが `sensor_fault.silence_s`（30 s）届かない（一括再生ではサンプルの時刻の空白） | 5 つすべて |

- **発火が遅れるときは、必ずその原因のアラートが先に届いています。** 1〜4 サンプルの短い欠けでは
  数え直さず、今までどおりの時刻に鳴ります
- 遅れは最大で `fire_after_s` + 欠けの長さです（例: AM2320 が 5 回失敗して約 30 s 休むと、
  `room_high` は約 20.7 分。決定記録 0139 §2.2 (3)）
- 欠けが `fire_after_s` より短い間隔で繰り返すと、そのルールは発火しないまま続きます。そのときは
  `SENSOR_MISSING` が欠けのたびに届くので、センサーの欠けを先に直します
- 発火中のアラートは欠けで解除しません。回復の後に解除の閾値で解除します
- `sensor_missing` / `sensor_fault` を無効にしている・欄が省かれた・デーモンの再起動の空白では
  数え直しません（黙って遅らせないため。空白をまたいで発火しうる。決定記録 0139 §5）
- 一括再生（`--bulk`）でも、CSV の 30 s 以上の空白は空白の後の最初のサンプルの時刻で `SENSOR_FAULT` になります

## 4. 導入先へ反映する

`/etc/coldaisle/coldaisle.env` を書き換えたら、`coldaisle-daemon` を再起動するまで反映されません
（環境変数は起動時にだけ読みます）。再起動の後、`journalctl -u coldaisle-daemon` で
起動時の `missing` の警告が消えたことを確かめます。
`COLDAISLE_DASHBOARD_URL` も同じで、再起動するまで通知のリンクは変わりません
（日次レポートは次に `coldaisle-report.service` が走ったときから）。

## 5. まだ決まっていないこと

- **夜間の warning の有効化**（`night.warning_enabled`）は、ベースライン測定（#50）で
  閾値を確定してから判断します（決定記録 0013 §2.5）。この手順では変えません
