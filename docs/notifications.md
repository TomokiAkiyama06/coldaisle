# 通知の到達確認（Slack / LINE）

通知の規約は決定記録 [0013](decisions/0013-notifications.md) にあります。
この文書は、**実物の Webhook / トークンで本当に届くか**を確かめる手順です（#51 / 0013 §2.7・§5 未決2）。
テストは宛先を差し替えて行っているため、外部サービスへの到達はここでしか確かめられません。

## 1. 宛先と変数

| 重大度 | 宛先（`config/notify.yaml` の `routing`） | 要る環境変数 |
|---|---|---|
| critical | LINE + stdout | `COLDAISLE_LINE_TOKEN` と `COLDAISLE_LINE_TO`（**両方**） |
| warning | Slack + stdout | `COLDAISLE_SLACK_WEBHOOK` |
| info | stdout | なし |

- `COLDAISLE_SLACK_WEBHOOK`: 監視専用チャンネルの Incoming Webhook の URL
- `COLDAISLE_LINE_TOKEN`: LINE Messaging API のチャネルアクセストークン（長期）
- `COLDAISLE_LINE_TO`: 送信先のユーザー ID（`U` で始まる）またはグループ ID（`C` で始まる）。
  自分宛てなら LINE Developers のチャネル設定にある「あなたのユーザー ID」。
  送信先は公式アカウントを友だち追加しておく（グループならそのグループに招く）

**値はリポジトリに書きません**（public。決定記録 0013 §2.1）。
置き場所は開発機なら `.env`（git 管理外）、導入先なら `/etc/coldaisle/coldaisle.env`
（[`ubuntu-deploy.md`](ubuntu-deploy.md) §2）。雛形は `.env.example` です。

未設定の宛先は作られず、起動時に1度だけ次の警告が出ます。

```text
"msg": "設定されていない通知先がある（その重大度は残りの宛先だけに届く）", "missing": ["line", "slack"]
```

`missing` が空にならない限り、その重大度は stdout（構造化ログ）にしか出ていません。

## 2. 試しに鳴らす（MockSource）

`notify_check` シナリオは、critical（`RAPID_RISE`）と warning（`INTAKE_HIGH`）を
**1回ずつ発火させて解除します。** 外部へ届くのは次の4通です。

| 宛先 | 届くもの |
|---|---|
| LINE | 🔴 `RAPID_RISE` の発火 → ✅ 解除 |
| Slack | 🔴 `INTAKE_HIGH` の発火 → ✅ 解除 |

同じアラートが2通以上届いたら連投の抑制が効いていません（#51 の受入基準）。
同じ流れはテスト `tests/test_rules.py::test_notify_check_scenario_reaches_each_target_once`
で宛先を差し替えて固定しています。

```bash
# 本番の DB を汚さない。使い捨ての DB を使う
TMP_DB="$(mktemp -d)/notify-check.db"
uv run --env-file .env coldaisle-daemon --source mock --scenario notify_check --speed 60 --db "$TMP_DB"
```

- `--speed 60` で12分のシナリオが約12秒で終わります
- **昼（7時〜22時。`config/notify.yaml` の `night`）に行ってください。**
  夜間は warning を送らない設定（`night.warning_enabled: false`）なので、Slack 側が届きません。
  シナリオ時間で12分進むため、21時45分以降も避けます
- 鳴る値は暫定の閾値（`config/rules.yaml`）によるもので、「この値が危険」という意味ではありません。
  受け取る側のチャンネル・グループには、試験であることを先に書いておくと取り違えません
- `config/ai.yaml` で説明を有効にしていて LLM に繋がる場合は、発火の説明が追加で届くことがあります

導入先の `/etc/coldaisle/coldaisle.env` で確かめる場合は、そのファイルを読めるユーザーで
同じコマンドを `--env-file /etc/coldaisle/coldaisle.env` にして実行します。
シリアルを開かない（mock）・DB も別なので、稼働中の取り込みとは干渉しません。

## 3. 結果の見方

ログ（JSON Lines）で次を確かめます。

| 見るもの | 期待 |
|---|---|
| 起動時の `missing` | 警告が出ない（または確かめたい宛先が含まれない） |
| 終了時の `"取り込みを終了する"` の `notifications` | 4（発火2 + 解除2。夜間に走らせると warning の2件が抑えられて 2 になる） |
| `"通知の送信に失敗した"` | 出ない。出たら `target` が失敗した宛先 |

送信に失敗しても取り込みは止まりません（決定記録 0013 §2.4）。よくある原因:

- Slack が `404` / `403`: Webhook の URL の写し間違い、またはチャンネル・アプリの削除
- LINE が `401`: トークンの誤り・失効
- LINE が `400`: `COLDAISLE_LINE_TO` の誤り、または送信先が公式アカウントを友だち追加していない

## 4. 導入先へ反映する

`/etc/coldaisle/coldaisle.env` を書き換えたら、`coldaisle-daemon` を再起動するまで反映されません
（環境変数は起動時にだけ読みます）。再起動の後、`journalctl -u coldaisle-daemon` で
起動時の `missing` の警告が消えたことを確かめます。

## 5. まだ決まっていないこと

- **夜間の warning の有効化**（`night.warning_enabled`）は、ベースライン測定（#50）で
  閾値を確定してから判断します（決定記録 0013 §2.5）。この手順では変えません
