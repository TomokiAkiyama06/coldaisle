"""`coldaisle-control`: `coldaisle-fand` の管理ソケットへ1件送るクライアント（#74 / 0072）。

```bash
uv run coldaisle-control status
uv run coldaisle-control max --reason "GPU 負荷試験の前に全開にする"
uv run coldaisle-control manual --front 0.6 --rear 0.5 --top 0.7 --lease 30m --reason "騒音の比較"
uv run coldaisle-control auto --reason "比較の終了"
```

**人が使う入口である。** LLM のツール・読み取り API・`coldaisle-eventd` からは到達できない
（0072 §2.9）。管理ソケットは制御権を増やせない（authority の昇格の操作は存在しない）。

- `MANUAL` の値は demand（`0.0..1.0`）で渡す。PWM は受け取らない。Guard と Critical Safety の
  floor・`forced_max`・`ramp_down` はすべて掛かる（0072 §2.4）
- `MANUAL` には `--lease` が必須。期限が来たら `coldaisle-fand` が `AUTO` へ戻す
- `MAX` には期限を付けない（勝手に下がる経路を作らない）

`--socket` を渡したときは設定ファイルを読まない。待ち時間は `--timeout`、無ければ環境変数
`COLDAISLE_CONTROL_TIMEOUT_S`、どちらも無ければ設定の `apply_ack_timeout_ms` と
`limits.read_timeout_s` の和（設定を読めないときは `--timeout` を求める）。

終了コード: 0 = 受理 / 1 = 拒否 / 2 = 接続できない・応答が壊れている・設定を読めない
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from coldaisle.control_admin.config import DEFAULT_CONFIG, ControlAdminSettings
from coldaisle.control_admin.messages import PROTOCOL_VERSION, RequestError, encode_request

RESPONSE_MAX_BYTES = 4096

TIMEOUT_ENV = "COLDAISLE_CONTROL_TIMEOUT_S"
"""`--timeout` を省いたときの待ち時間（秒）を与える環境変数。"""

_DURATION_PATTERN = re.compile(r"^(\d+)([smhd]?)$")
_DURATION_UNITS_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86_400}


class AdminUnavailableError(RuntimeError):
    """管理ソケットへ接続できない、応答が壊れている、または設定を読めない。"""


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
            raise AdminUnavailableError(f"管理ソケットへ接続できない: {exc}") from exc
    finally:
        conn.close()
    try:
        decoded: Any = json.loads(bytes(buffer).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminUnavailableError("管理ソケットの応答が壊れている") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
        raise AdminUnavailableError("管理ソケットの応答が壊れている")
    return decoded


def build_parser() -> argparse.ArgumentParser:
    """CLI を組み立てる。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-control",
        description="coldaisle-fand の運転モードを切り替える（管理ソケット。決定記録 0072）",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="ソケットの場所と待ち時間を引く設定。--socket と --timeout を渡せば読まない",
    )
    parser.add_argument("--socket", type=Path, default=None, help="ソケットの場所")
    parser.add_argument("--timeout", type=float, default=None, help="応答を待つ秒数")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="いまのモードと状態を読む")
    maximum = commands.add_parser("max", help="全 zone を Max にする（期限なし）")
    maximum.add_argument("--reason", required=True, help="理由（1〜200文字）")
    auto = commands.add_parser("auto", help="自動（Fallback / Gate の選択）へ戻す")
    auto.add_argument("--reason", required=True, help="理由（1〜200文字）")
    manual = commands.add_parser("manual", help="zone ごとの demand を人が決める（期限つき）")
    for zone in ("front", "rear", "top"):
        manual.add_argument(f"--{zone}", type=float, required=True, help="demand（0.0..1.0）")
    manual.add_argument(
        "--lease", type=_duration_s, required=True, help="期限（例: 30m, 2h, 1800）"
    )
    manual.add_argument("--reason", required=True, help="理由（1〜200文字）")
    return parser


def _duration_s(text: str) -> int:
    """`30m` / `2h` / `90s` / `1d` / `1800` を秒にする。"""
    matched = _DURATION_PATTERN.match(text)
    if matched is None:
        raise argparse.ArgumentTypeError("期間は 90s / 30m / 2h / 1d か秒数で書く")
    return int(matched.group(1)) * _DURATION_UNITS_S[matched.group(2)]


def _body(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "status":
        return {"v": PROTOCOL_VERSION, "op": "status"}
    body: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "op": "set_mode",
        "mode": args.command,
        "reason": args.reason,
    }
    if args.command == "manual":
        body["requested"] = {"front": args.front, "rear": args.rear, "top": args.top}
        body["lease_s"] = args.lease
    return body


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-control` の入口。結果を1行の JSON で標準出力へ書く。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        line = encode_request(_body(args))
    except RequestError as exc:
        _fail(exc.code)
        return 1
    try:
        socket_path, timeout_s = _target(args)
        response = send(line, socket_path, timeout_s=timeout_s)
    except AdminUnavailableError as exc:
        _fail(str(exc))
        return 2
    print(json.dumps(response, ensure_ascii=False))  # noqa: T201
    return 0 if response["ok"] else 1


def _target(args: argparse.Namespace) -> tuple[Path, float]:
    """ソケットの場所と待ち時間。設定は要るときだけ読む。"""
    timeout_s = _explicit_timeout(args.timeout)
    if args.socket is not None and timeout_s is not None:
        return args.socket, timeout_s
    settings = _settings(args.config)
    socket_path = args.socket if args.socket is not None else settings.socket.path
    if timeout_s is None:
        # 受付スレッドが適用の確認を待つ上限と、送信の上限を合わせた分だけ待つ
        timeout_s = settings.apply_ack_timeout_ms / 1_000 + settings.limits.read_timeout_s
    return socket_path, timeout_s


def _explicit_timeout(flag: float | None) -> float | None:
    if flag is not None:
        value = flag
    else:
        raw = os.environ.get(TIMEOUT_ENV)
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError as exc:
            raise AdminUnavailableError(f"{TIMEOUT_ENV} が数値ではない") from exc
    if not 0 < value < float("inf"):
        raise AdminUnavailableError("待ち時間は正の秒数にする")
    return value


def _settings(config: Path) -> ControlAdminSettings:
    try:
        return ControlAdminSettings.from_yaml(config)
    except FileNotFoundError as exc:
        raise AdminUnavailableError(
            f"設定ファイルが無い: {config}（--socket と --timeout で直接指定できます）"
        ) from exc
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise AdminUnavailableError(f"設定ファイルを読めない: {config}") from exc


def _fail(reason: str) -> None:
    print(json.dumps({"ok": False, "error": reason}, ensure_ascii=False), file=sys.stderr)  # noqa: T201
