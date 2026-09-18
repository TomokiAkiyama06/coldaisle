"""`coldaisle-rollup` の入口。Store のロールアップへ周期メトリクスを渡す。#10 / #65

Store（L1）は Internal Telemetry の設定を import しない。周期の分かる上位の設定を
ここで読み、``periodic_intervals_ms`` として渡す。これが無いと、Internal Telemetry
daemon の停止区間が1分ロールアップに残らず、生データの保持期間を過ぎると消える。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from coldaisle.clock import Clock
from coldaisle.internal_telemetry import InternalTelemetryConfig
from coldaisle.store import rollup
from coldaisle.telemetry_daemon import DEFAULT_CONFIG, periodic_metric_intervals


def main(argv: Sequence[str] | None = None, *, clock: Clock | None = None) -> int:
    """``--internal-telemetry`` だけをここで読み、残りの引数は Store の CLI へ渡す。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--internal-telemetry", type=Path, default=DEFAULT_CONFIG)
    known, rest = parser.parse_known_args(argv)
    intervals = periodic_metric_intervals(
        InternalTelemetryConfig.from_yaml(known.internal_telemetry)
    )
    return rollup.main(rest, periodic_intervals_ms=intervals, clock=clock)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
