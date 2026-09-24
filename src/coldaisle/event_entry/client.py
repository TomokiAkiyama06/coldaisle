"""`coldaisle-event`: 書き込み入口へ1件送るクライアント（#67 / 決定記録 0045）。

```bash
uv run coldaisle-event gpu-mode compute --source workspace-gpu-manager
uv run coldaisle-event gpu-mode ai --note "推論サーバを再開"
uv run coldaisle-event workload-hint training --expected-duration 4h --source job-launcher
uv run coldaisle-event workload-hint end
```

`workload-hint` は決定記録 0064 の Workload Hint を送る（#107）。Stage A では**記録するだけ**で、
制御はこれを読まない。`--expected-duration` は `90s` / `30m` / `4h` / `2d` か秒数で書く。

Workspace の GPU Manager やスクリプトから呼ぶ。送る前にサーバと同じ検証を通すので、
形の誤りは接続する前に分かる。**受理したかはサーバの応答だけで判断する。**
設定で決まる上限（受理する `hint_v`・`expected_duration_s` の上限）はサーバだけが確かめる。

`--socket` を渡したときは**設定ファイルを読まない**。Workspace などリポジトリの外から
呼ぶ側は、作業ディレクトリに `config/event-entry.yaml` が無いのが普通である。
`--socket` が無いときだけ `--config`（既定 `config/event-entry.yaml`）からソケットを引く。

待ち時間は `--timeout`、無ければ環境変数 `COLDAISLE_EVENT_TIMEOUT_S`、
どちらも無ければ 5 秒。サーバ側の読み取り上限（`limits.read_timeout_s`）とは別の値である。

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

from coldaisle.event_entry.config import DEFAULT_CONFIG, EventEntrySettings
from coldaisle.event_entry.messages import MessageError, encode_gpu_mode, encode_workload_hint

RESPONSE_MAX_BYTES = 4096

TIMEOUT_ENV = "COLDAISLE_EVENT_TIMEOUT_S"
"""`--timeout` を省いたときの待ち時間（秒）を与える環境変数。"""

DEFAULT_TIMEOUT_S = 5.0
"""`--timeout` も環境変数も無いときの待ち時間（秒）。"""

HINT_WORKLOADS = ("training", "benchmark", "inference_service")
"""`workload-hint` の第1引数に書ける負荷の種類（決定記録 0064 §2.3 の閉じた語彙）。"""

HINT_END = "end"
"""`workload-hint end`: いま有効なヒントを取り消す。"""

_DURATION_PATTERN = re.compile(r"^(\d+)([smhd]?)$")
_DURATION_UNITS_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86_400}


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
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="ソケットの場所を引く設定。--socket を渡したときは読まない",
    )
    parser.add_argument(
        "--socket", type=Path, default=None, help="ソケットの場所。渡せば設定ファイルを読まない"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=f"応答を待つ秒数（既定: 環境変数 {TIMEOUT_ENV}、無ければ {DEFAULT_TIMEOUT_S:g}）",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    gpu_mode = commands.add_parser("gpu-mode", help="GPU Mode の切り替えを記録する")
    gpu_mode.add_argument("mode", choices=["ai", "compute"])
    gpu_mode.add_argument(
        "--source", default=None, help="書き手の名前（例: workspace-gpu-manager）"
    )
    gpu_mode.add_argument("--note", default=None, help="人間向けの補足（200文字以内）")
    hint = commands.add_parser(
        "workload-hint", help="これから流す負荷を記録する（記録のみ。制御は読まない）"
    )
    hint.add_argument("workload", choices=[*HINT_WORKLOADS, HINT_END])
    hint.add_argument(
        "--expected-duration",
        type=_duration_s,
        default=None,
        help="見込みの長さ（例: 4h, 30m, 14400）。end には付けない",
    )
    hint.add_argument("--source", default=None, help="書き手の名前（例: job-launcher）")
    hint.add_argument("--note", default=None, help="人間向けの補足（200文字以内）")
    return parser


def _duration_s(text: str) -> int:
    """`4h` / `30m` / `90s` / `2d` / `14400` を秒にする。"""
    matched = _DURATION_PATTERN.match(text)
    if matched is None:
        raise argparse.ArgumentTypeError("期間は 90s / 30m / 4h / 2d か秒数で書く")
    return int(matched.group(1)) * _DURATION_UNITS_S[matched.group(2)]


def _encode(args: argparse.Namespace) -> bytes:
    """副コマンドの引数を1行にする。形の誤りは `MessageError`。"""
    if args.command == "gpu-mode":
        return encode_gpu_mode(args.mode, source=args.source, note=args.note)
    if args.workload == HINT_END:
        # 期間の付いた取り消しは送る前に形の検証で落ちる（0064 §2.3）
        return encode_workload_hint(
            "end",
            expected_duration_s=args.expected_duration,
            source=args.source,
            note=args.note,
        )
    return encode_workload_hint(
        "start",
        workload=args.workload,
        expected_duration_s=args.expected_duration,
        source=args.source,
        note=args.note,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-event` の入口。結果を1行の JSON で標準出力へ書く。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        timeout_s = _timeout(args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        line = _encode(args)
    except MessageError as exc:
        _fail(exc.reason)
        return 1
    try:
        socket_path = args.socket if args.socket is not None else _configured_socket(args.config)
        response = send(line, socket_path, timeout_s=timeout_s)
    except EntryUnavailableError as exc:
        _fail(str(exc))
        return 2
    print(json.dumps(response, ensure_ascii=False))  # noqa: T201
    return 0 if response["ok"] else 1


def _fail(reason: str) -> None:
    print(json.dumps({"ok": False, "error": reason}, ensure_ascii=False), file=sys.stderr)  # noqa: T201


def _timeout(flag: float | None) -> float:
    """`--timeout` → 環境変数 → 既定 の順に決める。正の有限値だけを受ける。"""
    if flag is not None:
        value = flag
    else:
        raw = os.environ.get(TIMEOUT_ENV)
        try:
            value = DEFAULT_TIMEOUT_S if raw is None else float(raw)
        except ValueError as exc:
            raise ValueError(f"{TIMEOUT_ENV} が数値ではない") from exc
    if not 0 < value < float("inf"):
        raise ValueError("待ち時間は正の秒数にする")
    return value


def _configured_socket(config: Path) -> Path:
    """設定ファイルからソケットの場所を引く。読めなければ理由の分かる失敗にする。"""
    try:
        return EventEntrySettings.from_yaml(config).socket.path
    except FileNotFoundError as exc:
        raise EntryUnavailableError(
            f"設定ファイルが無い: {config}（--socket でソケットを直接指定できます）"
        ) from exc
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise EntryUnavailableError(f"設定ファイルを読めない: {config}") from exc
