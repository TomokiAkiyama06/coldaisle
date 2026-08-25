"""合成の起点: 読み取り専用 API に AI 向けツールの窓口を足す（#23）。

```bash
COLDAISLE_DB=var/coldaisle.db uv run uvicorn coldaisle.server:app --host 127.0.0.1 --port 8000
```

**なぜ `coldaisle.api` に直接足さないのか。**

`api/`（L2）が `ai/`（L3）を import すると、レイヤ間の依存が逆流する
（AGENTS.md「下位レイヤが上位レイヤを import しない」）。L2 は `Tools`
プロトコルだけを知り、実体を渡すのはここ。`daemon.py` / `report.py` と同じ、
合成のためだけの入口である。

`coldaisle.api:app` はそのまま使える（ツールの窓口が無いだけ）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from coldaisle.ai.tools import ToolRegistry
from coldaisle.api.app import Config, Tools, create_app
from coldaisle.clock import Clock, WallClock
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import RuleSet
from coldaisle.store import SqliteStore

DEFAULT_RULES = Path("config/rules.yaml")


def create_server(config: Config | None = None, *, clock: Clock | None = None) -> FastAPI:
    """API にツールの窓口を足したアプリを作る。"""
    settings = config or Config.from_env()
    catalog = MetricCatalog.from_yaml(settings.metrics)
    rules = RuleSet.from_yaml(Path(os.environ.get("COLDAISLE_RULES", str(DEFAULT_RULES))))
    ticking = clock or WallClock()

    def tools(store: SqliteStore) -> Tools:
        # **接続はスレッドごと**（決定記録 0004 §2.8）。渡された接続をそのまま使う
        return ToolRegistry(store=store, catalog=catalog, rules=rules, clock=ticking)

    return create_app(settings, clock=ticking, tools=tools)


app = create_server()
"""uvicorn が読む ASGI アプリ。"""
