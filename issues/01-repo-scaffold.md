---
title: "リポジトリ雛形とツールチェーン整備"
labels: infra, priority:must
milestone: "M0 基盤"
---

## 背景
Claude Code / Codex の双方が同じ規約で作業できる土台を先に作る。
ここが固まっていないとエージェント併用時に差分が荒れる。

## やること
- [ ] `uv init` でプロジェクト作成、`src/coldaisle/` レイアウト
- [ ] 依存追加: `fastapi` `uvicorn` `pyserial` `pydantic` `pyyaml` `httpx`
- [ ] dev依存: `pytest` `pytest-asyncio` `ruff` `mypy`
- [ ] `pyproject.toml` に ruff / mypy(strict) / pytest 設定
- [ ] `pytest.ini_options` に `markers = ["hardware: 実機が必要"]` を登録
- [ ] ディレクトリ骨格: `ingest/ store/ api/ rules/ ai/ web/ firmware/ config/ docs/ tests/`
- [ ] `.gitignore`（`.venv`, `*.db`, `.env`, `~/server_sensor_logs` 相当）
- [ ] `CLAUDE.md` を作成し `AGENTS.md` を参照させる（内容は複製しない）

## 受入基準
- `uv sync && uv run pytest && uv run ruff check . && uv run mypy src` がすべて成功する
- リポジトリをcloneした別のエージェントが `AGENTS.md` だけ読めば作業を開始できる
