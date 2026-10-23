"""`coldaisle-slackbot`: Slack のメンションに読み取り専用のツールで答える常駐のプロセス。

決定記録 0168。

```bash
# COLDAISLE_SLACK_APP_TOKEN / COLDAISLE_SLACK_BOT_TOKEN / 許可リスト / LLM の設定
export UV_ENV_FILE=.env
uv run coldaisle-slackbot --db var/coldaisle.db
```

終了コード:

- 0: 止める合図（SIGTERM / SIGINT）で止まった
- 1: 起動時に Slack へつなげなかった（`auth.test` の失敗。systemd が間隔をあけて起動し直す）
- 2: 設定が足りない・不正（トークンが無い・形が違う・`config/ai.yaml` が読めない・
  `slack_qa:` が無い）。
  起動し直しても直らないので、unit は `RestartPreventExitStatus=2` で止まったままにする

**トークン・ID・質問と答えの本文をログに出さない**（0168 §2.1 / §2.5）。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

import yaml

from coldaisle import logs
from coldaisle.ai.provider import AiSettings
from coldaisle.ai.provider import from_env as provider_from_env
from coldaisle.ai.tools import ToolRegistry
from coldaisle.clock import WallClock
from coldaisle.llm_exclusions import LlmExclusions, LlmExclusionsError
from coldaisle.metrics import MetricCatalog
from coldaisle.notify.notifiers import DeliveryError
from coldaisle.notify.slack_bot import SLACK_BOT_TOKEN_ENV, SlackApi
from coldaisle.report import ReportConfig
from coldaisle.rules import RuleSet
from coldaisle.slackbot.bot import MentionHandler
from coldaisle.slackbot.policy import Allowlist, DailyCallBudget, UserRateLimiter
from coldaisle.slackbot.qa import QuestionAnswerer
from coldaisle.slackbot.socket_mode import Backoff, SocketModeClient, websocket_connect
from coldaisle.store import SqliteStore
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.slackbot")

SLACK_APP_TOKEN_ENV = "COLDAISLE_SLACK_APP_TOKEN"
"""Socket Mode の App-Level Token（`xapp-`、scope は `connections:write`）。0168 §2.1。"""

EXIT_OK = 0
EXIT_UNREACHABLE = 1
EXIT_CONFIG = 2


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-slackbot` の入口。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-slackbot",
        description="Slack のメンションに読み取り専用のツールで答える（決定記録 0168）",
    )
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--ai", type=Path, default=Path("config/ai.yaml"))
    parser.add_argument("--metrics", type=Path, default=Path("config/metrics.yaml"))
    parser.add_argument("--rules", type=Path, default=Path("config/rules.yaml"))
    parser.add_argument(
        "--report-config",
        type=Path,
        default=Path("config/report.yaml"),
        help="LLM へ渡さない一覧（llm_excluded_*）。読めなければ答えない（0131 §2.4a）",
    )
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logs.configure(args.log_level)
    # websockets は DEBUG で送受信の中身（質問の本文）と接続の URL（券を含む）を出しうる
    logging.getLogger("websockets").setLevel(logging.WARNING)

    app_token, bot_token = _tokens()
    if app_token is None or bot_token is None:
        return EXIT_CONFIG
    try:
        settings = AiSettings.from_yaml(args.ai)
    except (OSError, yaml.YAMLError, ValueError) as error:
        # 無い・YAML が壊れている・検証に落ちる。pydantic と YAML の例外の文には設定の値が
        # 入るので、型の名前とファイルの場所だけ。起動し直しても直らない（終了コード 2）
        LOGGER.error(
            "config/ai.yaml を読めないので起動しない",
            extra={logs.FIELDS_KEY: {"config": str(args.ai), "error": type(error).__name__}},
        )
        return EXIT_CONFIG
    qa = settings.slack_qa
    if qa is None:
        LOGGER.error(
            "config/ai.yaml に slack_qa: が無いので起動しない（決定記録 0168 §2.5）",
            extra={logs.FIELDS_KEY: {"config": str(args.ai)}},
        )
        return EXIT_CONFIG

    allowlist = Allowlist.from_env()
    allowlist.warn_if_empty()
    bot_api = SlackApi(bot_token)
    try:
        bot_user_id = bot_api.bot_user_id()
    except DeliveryError as error:
        LOGGER.error(
            "Slack の auth.test に失敗した。起動し直して待つ",
            extra={logs.FIELDS_KEY: {"reason": str(error)}},
        )
        return EXIT_UNREACHABLE

    zone = _zone(args.report_config)
    answerer = QuestionAnswerer(
        provider=provider_from_env(settings, allow_tools=True),
        settings=qa,
        budget=DailyCallBudget(qa.daily_call_limit),
        # 時刻帯が読めなければ答えない（「昨日」を OS の時刻帯で解かない。0168 §2.3）
        open_tools=None if zone is None else _tools_opener(args),
        zone=zone or ZoneInfo("UTC"),
    )
    handler = MentionHandler(
        bot_user_id=bot_user_id,
        allowlist=allowlist,
        limiter=UserRateLimiter(qa.user_questions_per_window, qa.user_window_s),
        answerer=answerer,
        post=lambda channel, thread_ts, text: bot_api.post_message(
            channel, text, thread_ts=thread_ts, unfurl=False
        ),
        settings=qa,
    )
    stop = threading.Event()
    _on_signals(stop)
    worker = threading.Thread(target=handler.run_worker, name="slackbot-answer", daemon=True)
    worker.start()
    app_api = SlackApi(app_token)
    SocketModeClient(
        open_url=app_api.open_socket_url,
        connect=websocket_connect,
        on_event=handler.offer,
        backoff=Backoff(qa.reconnect_initial_s, qa.reconnect_max_s),
        stop=stop,
        idle_reconnect_s=qa.idle_reconnect_s,
    ).run()
    handler.stop_worker()
    worker.join(timeout=5)
    LOGGER.info("coldaisle-slackbot を止めた")
    return EXIT_OK


def _tokens() -> tuple[str | None, str | None]:
    """2 つのトークン。**値は出さない**（足りない・形の違う変数の名前だけ）。"""
    app_token = (os.environ.get(SLACK_APP_TOKEN_ENV) or "").strip()
    bot_token = (os.environ.get(SLACK_BOT_TOKEN_ENV) or "").strip()
    missing = [
        name
        for name, value in ((SLACK_APP_TOKEN_ENV, app_token), (SLACK_BOT_TOKEN_ENV, bot_token))
        if not value
    ]
    if missing:
        LOGGER.error(
            "Slack のトークンが足りないので起動しない（決定記録 0168 §2.1）",
            extra={logs.FIELDS_KEY: {"missing": missing}},
        )
        return None, None
    # 取り違え（App と Bot の逆）を起動時に止める。値は出さない
    malformed = [
        name
        for name, value, prefix in (
            (SLACK_APP_TOKEN_ENV, app_token, "xapp-"),
            (SLACK_BOT_TOKEN_ENV, bot_token, "xoxb-"),
        )
        if not value.startswith(prefix) or not value.isascii() or not value.isprintable()
    ]
    if malformed:
        LOGGER.error(
            "Slack のトークンの形が違うので起動しない（App は xapp-、Bot は xoxb- で始まる）",
            extra={logs.FIELDS_KEY: {"env": malformed}},
        )
        return None, None
    return app_token, bot_token


def _tools_opener(args: argparse.Namespace) -> Callable[[], ToolRegistry] | None:
    """ToolRegistry を作る関数。**除外の一覧が読めなければ `None`**（答えない。0168 §2.3）。

    DB は答えるスレッドの中で開く（`SqliteStore` の接続をスレッド間で共有しない。0004 §2.8）。
    """
    try:
        exclusions = LlmExclusions.from_yaml(args.report_config)
    except LlmExclusionsError as error:
        LOGGER.error(
            "LLM へ渡さない一覧が不正なので、メンションに答えない（固定の文だけを返す）",
            extra={logs.FIELDS_KEY: {"config": str(args.report_config), "reason": str(error)}},
        )
        return None
    catalog = MetricCatalog.from_yaml(args.metrics)
    rules = RuleSet.from_yaml(args.rules)
    quality = QualityRules.from_yaml(args.quality_rules)
    clock = WallClock()

    def open_tools() -> ToolRegistry:
        store = SqliteStore(args.db, rules=quality, clock=clock)
        return ToolRegistry(
            store=store, catalog=catalog, rules=rules, clock=clock, exclusions=exclusions
        )

    return open_tools


def _zone(path: Path) -> ZoneInfo | None:
    """日の境界の時刻帯。日次レポートと同じ `config/report.yaml` の `timezone`（0168 §2.3）。

    読み方は `coldaisle-report` と同じ（`ReportConfig.from_yaml`）。読めなければ `None`。
    """
    try:
        return ReportConfig.from_yaml(path).zone
    except (OSError, yaml.YAMLError, ValueError) as error:
        LOGGER.error(
            "config/report.yaml の時刻帯を読めないので、メンションに答えない（固定の文だけを返す）",
            extra={logs.FIELDS_KEY: {"config": str(path), "error": type(error).__name__}},
        )
        return None


def _on_signals(stop: threading.Event) -> None:
    def handle(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)


if __name__ == "__main__":
    raise SystemExit(main())
