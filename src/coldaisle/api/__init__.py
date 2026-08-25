"""L2 API: FastAPI、WebSocket。

`uvicorn coldaisle.api:app` で起動する（AGENTS.md のコマンド）。
設定は環境変数から読む（`COLDAISLE_DB` ほか。`app.Config` を参照）。

**書き込み系のエンドポイントを持たない**（FR-307）。
"""

from coldaisle.api.app import Config, create_app

app = create_app()
"""uvicorn が読む ASGI アプリ。"""

__all__ = ["Config", "app", "create_app"]
