"""`coldaisle-event`: 書き込み入口へ1件送るクライアント（#67 / 決定記録 0045）。

```bash
uv run coldaisle-event gpu-mode compute --source workspace-gpu-manager
uv run coldaisle-event gpu-mode ai --note "推論サーバを再開"
```

Workspace の GPU Manager やスクリプトから呼ぶ。送る前にサーバと同じ検証を通すので、
形の誤りは接続する前に分かる。**受理したかはサーバの応答だけで判断する。**

終了コード: 0 = 受理 / 1 = 拒否 / 2 = 接続できない・応答が壊れている
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from coldaisle.event_entry.config import DEFAULT_CONFIG, EventEntrySettings
from coldaisle.event_entry.messages import MessageError, encode_gpu_mode

RESPONSE_MAX_BYTES = 4096


class EntryUnavailableError(RuntimeError):
    """入口へ接続できない、または応答が壊れている。"""


def send(line: bytes, socket_path: Path, *, timeout_s: float) -> dict[str, Any]:
    """1行を送り、1行の応答を JSON object として返す。"""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(timeout_s)
        try:
            conn.connect(str(socket_path))
            conn.sendall(line)
            buffer = bytearray()
            while b"\n" not in buffer and len(buffer) <= RESPONSE_MAX_BYTES:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                buffer += chunk
        except OSError as exc:
            raise EntryUnavailableError(f"書き込み入口へ接続できない: {exc}") from exc
    finally:
        conn.close()
    try:
        decoded: Any = json.loads(bytes(buffer).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EntryUnavailableError("書き込み入口の応答が壊れている") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
        raise EntryUnavailableError("書き込み入口の応答が壊れている")
    return decoded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coldaisle-event", description="書き込み入口（coldaisle-eventd）へイベントを送る"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--socket", type=Path, default=None, help="設定のソケットを上書きする")
    commands = parser.add_subparsers(dest="command", required=True)
    gpu_mode = commands.add_parser("gpu-mode", help="GPU Mode の切り替えを記録する")
    gpu_mode.add_argument("mode", choices=["ai", "compute"])
    gpu_mode.add_argument(
        "--source", default=None, help="書き手の名前（例: workspace-gpu-manager）"
    )
    gpu_mode.add_argument("--note", default=None, help="人間向けの補足（200文字以内）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-event` の入口。結果を1行の JSON で標準出力へ書く。"""
    args = build_parser().parse_args(argv)
    settings = EventEntrySettings.from_yaml(args.config)
    socket_path: Path = args.socket or settings.socket.path
    try:
        line = encode_gpu_mode(args.mode, source=args.source, note=args.note)
    except MessageError as exc:
        print(json.dumps({"ok": False, "error": exc.reason}), file=sys.stderr)  # noqa: T201
        return 1
    try:
        response = send(line, socket_path, timeout_s=settings.limits.read_timeout_s)
    except EntryUnavailableError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)  # noqa: T201
        return 2
    print(json.dumps(response, ensure_ascii=False))  # noqa: T201
    return 0 if response["ok"] else 1
