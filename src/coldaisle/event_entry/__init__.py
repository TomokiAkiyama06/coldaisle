"""書き込み専用のローカル Unix ソケット入口（#67 / 決定記録 0045）。

読み取り API（`coldaisle.api` / `coldaisle.server`）とは**別の入口**である。
読み取り API は GET だけを持ち続け（決定記録 0009 §3）、外からの書き込みは
すべてこのパッケージのソケットを通る。

- `coldaisle-eventd`（`server.main`）: ソケットで待ち受け、検証して `events` へ追記する
- `coldaisle-event`（`client.main`）: 人とスクリプトのためのクライアント

**AI 層（`coldaisle.ai`）・読み取り API・`coldaisle.server` はこのパッケージを import しない。**
LLM から書き込みの経路へ到達させないため（AGENTS.md ルール1 / 決定記録 0045 §2.7）。
ここから Fan Demand・制御・設定へ至る経路も作らない（ルール2 / 0045 §2.6）。
"""
