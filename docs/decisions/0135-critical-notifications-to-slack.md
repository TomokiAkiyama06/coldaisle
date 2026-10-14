# 決定記録 0135: 通知の critical の宛先を Slack にする（LINE を使わない）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-15、リポジトリ所有者の承認）
- **Date**: 2026-10-15
- **Supersedes**: [0001](0001-initial-project-decisions.md) D-03（通知は重大度で分岐。critical / warning で別経路）
- **関連**: [0001](0001-initial-project-decisions.md) D-04 /
  [0013](0013-notifications.md) §2.3.1 / §2.5 / §2.6 / §2.7 /
  `config/notify.yaml`、`docs/notifications.md` /
  `src/coldaisle/notify/`（コードは変えない）
- **対象 Issue**: #303

---

## 1. Context

決定記録 0001 D-03 は「通知は重大度で分岐（critical / warning で別経路）」とし、
`config/notify.yaml` は `routing.critical: [line, stdout]`・`routing.warning: [slack, stdout]` だった。

所有者は LINE を使わないと判断した（2026-10-15）。LINE の宛先（`COLDAISLE_LINE_TOKEN` /
`COLDAISLE_LINE_TO`）が無いため宛先は作られず（0013 §2.1）、**critical のアラートが
stdout（構造化ログ）にしか出ていない。** 起動時の `missing` の警告に `line` が残り続け、
最も重い重大度だけが人へ届かない状態になっている。

---

## 2. Decision

### 2.1 critical の宛先を `[slack, stdout]` にする

`config/notify.yaml` の `routing.critical` を `[slack, stdout]` にする。
warning と**同じ Webhook（`COLDAISLE_SLACK_WEBHOOK`）・同じチャンネル**へ届く。
`warning` / `info` の宛先は変えない。

### 2.2 critical と warning の区別は本文の重大度の表示で行う

宛先が同じになるため、経路では区別できない。本文に載せている重大度と記号
（0013 §2.6。発火は 🔴、解除は ✅）で区別する。本文の形は変えない。

### 2.3 夜間の方針は変えない

0001 D-04 / 0013 §2.5 のまま。夜間（既定 22時〜7時）も **critical は届き**、
warning は `night.warning_enabled` が true になるまで送らない。
critical と warning が同じチャンネルに来ても、夜間に鳴るのは critical だけである。

0013 §2.3.1（重大度は解除する前に控える）も変えない。宛先が同じになっても、
解除の重大度が warning に落ちれば本文の表示が誤り、夜間の判定（発火を送ったか）も
重大度ごとの方針に依存するため、重大度を保つ理由は残る。

### 2.4 LINE の Notifier のコードは残す

`LineNotifier`・環境変数の読み込み・その試験は残す。
LINE を使うときは `routing` に `line` を戻すだけでよい（コードの変更は要らない）。

---

## 3. Consequences

### 良くなること

- critical のアラートが人へ届く（今は stdout にしか出ていない）
- 宛先の環境変数が `COLDAISLE_SLACK_WEBHOOK` 1つで揃い、起動時の `missing` の警告が消える

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| critical と warning が同じチャンネルに来て、経路では見分けられない | 本文の重大度と記号で区別する（2.2）。夜間は critical だけが鳴る（2.3） |
| Slack が落ちると critical も warning も届かない（別経路の冗長が無くなる） | stdout（構造化ログ）には必ず残る（0013 §3）。宛先の失敗は記録される（0013 §2.4）。別経路が要るなら `routing` に宛先を足す |
| 0013 の記述（§1 の D-03 への言及・§2.3.1 の「critical の解除が Slack へ流れる」の例）は別経路を前提にしている | 0013 の決定（遷移だけを送る・連投の抑制・解除の重大度を保つ・夜間の方針・本文）はどれも変わらないため、0013 は置き換えない。読み替えは本記録の 2.3 |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| critical 用に別のチャンネル（別の Webhook）を用意する | Notifier は宛先の名前ごとに1つで、Slack は `COLDAISLE_SLACK_WEBHOOK` 1つしか読まない。別チャンネルにはコードの変更（宛先の追加・環境変数の追加）が要る。所有者は同じチャンネルでよいと判断した |
| 今のまま（critical は LINE） | LINE を使わないため、critical がログにしか出ない（1） |
| LINE の Notifier のコードを消す | 将来使うときに戻す手間が増える。残しても宛先を作らないだけで害は無い（2.4） |
