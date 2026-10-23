"""合成の起点: Slack のメンションに読み取り専用のツールで答える `coldaisle-slackbot`。

#374 / 決定記録 0168。

- 受け取りは Socket Mode（こちらから WebSocket でつなぐ）で `app_mention` だけ（0168 §2.1）
- 答えるのは許可リストのチャンネル**かつ**ユーザーのメンションだけ（0168 §2.2）
- LLM に渡すツールは `get_latest` / `get_stats` / `list_alerts` / `describe_system` の 4 つだけ。
  書き込み・実行の経路は無い（AGENTS.md ルール 1・2 / 0168 §2.3）
- 生の時系列・decision trace は渡さない（ルール 8）
- 質問・答えの本文と ID はログに出さない（0168 §2.5）

**`coldaisle.control` と `coldaisle.ingest` を import しない**（試験で走査する）。
取り込み・API・fand・telemetry のプロセスに依存せず、止まっても監視と通知は今のまま動く（FR-507）。
"""
