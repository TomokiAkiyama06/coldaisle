"""合成の起点: 較正オフセットを求める（#13 / FR-107）。

DS18B20 も AM2320 も確度は ±0.5℃。**最悪ケースで ΔT に ±1.0℃ の系統誤差が
乗る**（spec-review W-02）。再循環の判定（`d.intake_rise`）は数℃の話なので、
これは無視できない。

求めるのは**センサー間の相対精度**であって絶対精度ではない。全センサーを
同じ空気に置き、その平均を基準に各センサーのずれを出す。

## 読み取り元は DB

**シリアルポートを開かない**（AGENTS.md ルール3。開いてよいのは取り込みデーモン
だけ）。デーモンが動いている状態で、保存済みの区間から計算する。

## 黙って書き換えない

較正値は**以降のすべての測定の解釈を変える。** 既定では計算結果を見せるだけで、
`--apply` を付けたときだけ書く（`coldaisle-memory` と同じ作法）。
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from coldaisle import logs
from coldaisle.channels import METRIC_TO_CHANNEL
from coldaisle.clock import Clock, WallClock
from coldaisle.ingest.calibration import Calibration
from coldaisle.metrics import MetricCatalog
from coldaisle.store.db import SqliteStore
from coldaisle.store.quality import QualityRules

LOGGER = logging.getLogger("coldaisle.calibrate")

TEMPERATURE_UNIT = "C"
"""この単位のメトリクスだけを較正する。**%RH に ℃ を足さない**（#13 / 要件 §5.1）。"""

REFERENCE = "mean_of_all"
"""基準の取り方（spec-review W-02）。全センサーの平均を「正しい」とみなす。"""

MIN_SAMPLES = 120
"""1チャネルあたりの最低件数。2.5秒周期で5分ぶん。

**少ない標本で較正しない。** ノイズをオフセットとして焼き付けることになる。
"""

MAX_SPREAD_C = 2.0
"""較正を受け付けるセンサー間のばらつきの上限。

**これを超えるなら、同じ空気に置かれていない。** そのまま較正すると、
**本物の温度勾配をオフセットとして焼き付ける**（以降その勾配が見えなくなる）。
spec-review W-02 は30分以上の熱平衡を求めている。
"""


@dataclass(frozen=True)
class ChannelMean:
    """1チャネルの測定結果。"""

    channel: str
    metric: str
    mean_c: float
    count: int


@dataclass(frozen=True)
class Result:
    """較正の計算結果。**書き込みはしない。**"""

    means: tuple[ChannelMean, ...]
    reference_c: float
    offsets_c: dict[str, float]
    spread_c: float
    """センサー間のばらつき（最大 − 最小）。**同じ空気に置けているかの指標。**"""

    @property
    def usable(self) -> bool:
        return self.spread_c <= MAX_SPREAD_C

    def as_lines(self) -> list[str]:
        lines = [
            f"基準 {self.reference_c:.3f} C（{REFERENCE}）",
            f"ばらつき {self.spread_c:.3f} C（上限 {MAX_SPREAD_C:.2f}）",
            "",
        ]
        for item in sorted(self.means, key=lambda m: m.channel):
            offset = self.offsets_c[item.channel]
            lines.append(
                f"  {item.channel:<14} 平均 {item.mean_c:8.3f} C"
                f"  オフセット {offset:+.3f} C  ({item.count} 件)"
            )
        return lines


def measure(
    store: SqliteStore, catalog: MetricCatalog, *, start_ms: int, end_ms: int
) -> list[ChannelMean]:
    """区間の平均を取る。**`quality='ok'` の測定だけ**（`stats()` と同じ規約）。"""
    means: list[ChannelMean] = []
    for metric, meta in catalog.metrics.items():
        if meta.unit != TEMPERATURE_UNIT or metric not in METRIC_TO_CHANNEL:
            continue
        stats = store.stats(metric, start_ms, end_ms)
        if stats.mean_value is None:
            continue
        means.append(
            ChannelMean(
                channel=METRIC_TO_CHANNEL[metric],
                metric=metric,
                mean_c=stats.mean_value,
                count=stats.ok_value_count,
            )
        )
    return means


def compute(means: Sequence[ChannelMean]) -> Result:
    """平均から基準とオフセットを出す。

    **基準は全センサーの平均**（spec-review W-02）。絶対精度は改善しないが、
    本システムが必要としているのは相対値なので問題にならない。
    """
    if not means:
        raise ValueError("較正に使える測定が無い")
    values = [item.mean_c for item in means]
    reference = sum(values) / len(values)
    return Result(
        means=tuple(means),
        reference_c=reference,
        # センサーの読みに足して基準へ寄せる向き。`Normalizer` が値へ加算する
        offsets_c={item.channel: reference - item.mean_c for item in means},
        spread_c=max(values) - min(values),
    )


def check(result: Result, *, min_samples: int = MIN_SAMPLES) -> list[str]:
    """受け付けられない理由。**空なら較正してよい。**"""
    problems: list[str] = []
    if not result.usable:
        problems.append(
            f"センサー間のばらつきが {result.spread_c:.3f} C ある（上限 {MAX_SPREAD_C:.2f}）。"
            "同じ空気に置かれていない可能性がある。**本物の温度勾配を"
            "オフセットとして焼き付けることになる**"
        )
    thin = sorted(item.channel for item in result.means if item.count < min_samples)
    if thin:
        problems.append(f"測定が少ないチャネルがある（{min_samples} 件未満）: {', '.join(thin)}")
    missing = sorted(set(METRIC_TO_CHANNEL.values()) - {item.channel for item in result.means})
    temperature_missing = [
        channel for channel in missing if channel != METRIC_TO_CHANNEL["air.room_humidity"]
    ]
    if temperature_missing:
        problems.append(f"測定が無いチャネルがある: {', '.join(temperature_missing)}")
    return problems


NOTE = (
    "較正オフセット（FR-107 / #13）。`coldaisle-calibrate` が算出。"
    "手順は docs/calibration.md。値を手で変えたら決定記録に残すこと"
    "（以降のデータの解釈が変わる）。"
)


def as_calibration(result: Result, previous: Calibration, *, at: datetime) -> Calibration:
    """書き込む中身。**有効期限は引き継ぐ。覚書は書き換える。**

    引き継いだ覚書は較正**前**の状態について書かれている（「実測前の暫定値」など）。
    そのまま残すと、ファイルが自分自身と食い違う。
    """
    return previous.model_copy(
        update={
            # `-0.0` を残さない。差分で読むときに紛らわしいだけで、意味は同じ
            "offsets_c": {
                channel: round(value, 3) or 0.0 for channel, value in result.offsets_c.items()
            },
            "calibrated_at": at.isoformat(),
            "reference": REFERENCE,
            "samples": {item.channel: item.count for item in result.means},
            "note": NOTE,
        }
    )


def write(calibration: Calibration, path: Path) -> None:
    """**人が読める形で書く。** git の差分で何が変わったか分かるように。"""
    payload = calibration.model_dump()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    """`coldaisle-calibrate`。**既定は見せるだけ**（`--apply` で書く）。"""
    parser = argparse.ArgumentParser(
        prog="coldaisle-calibrate", description="較正オフセットを求める（既定では書かない）"
    )
    parser.add_argument("--db", type=Path, default=Path("var/coldaisle.db"))
    parser.add_argument("--calibration", type=Path, default=Path("config/calibration.json"))
    parser.add_argument("--metrics", type=Path, default=Path("config/metrics.yaml"))
    parser.add_argument("--quality-rules", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--minutes", type=float, default=10.0, help="直近この分数を使う")
    parser.add_argument("--timezone", default="Asia/Tokyo")
    parser.add_argument("--apply", action="store_true", help="書き込む。**付けなければ見せるだけ**")
    parser.add_argument(
        "--force", action="store_true", help="受け付けられない理由があっても書く（**非推奨**）"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logs.configure(args.log_level)
    clock: Clock = WallClock()
    catalog = MetricCatalog.from_yaml(args.metrics)
    store = SqliteStore(args.db, rules=QualityRules.from_yaml(args.quality_rules), clock=clock)
    try:
        end_ms = clock.now_ms()
        start_ms = end_ms - int(args.minutes * 60_000)
        means = measure(store, catalog, start_ms=start_ms, end_ms=end_ms)
    finally:
        store.close()

    if not means:
        print(f"直近 {args.minutes:g} 分に測定がありません（デーモンは動いていますか）")  # noqa: T201
        return 1

    result = compute(means)
    for line in result.as_lines():
        print(line)  # noqa: T201

    problems = check(result)
    for problem in problems:
        print(f"\n**{problem}**")  # noqa: T201

    if not args.apply:
        print("\n書き込んでいません。内容を確認して `--apply` を付けてください。")  # noqa: T201
        return 0
    if problems and not args.force:
        print("\n上の理由により書き込みません（`--force` で上書きできますが、勧めません）")  # noqa: T201
        return 1

    previous = Calibration.from_json(args.calibration)
    at = datetime.fromtimestamp(clock.now_ms() / 1000, tz=UTC).astimezone(ZoneInfo(args.timezone))
    write(as_calibration(result, previous, at=at), args.calibration)
    LOGGER.info(
        "較正値を書き込んだ",
        extra={
            logs.FIELDS_KEY: {
                "path": str(args.calibration),
                "spread_c": round(result.spread_c, 3),
                "forced": bool(problems),
            }
        },
    )
    print(f"\n{args.calibration} を更新しました。**取り込みを再起動してください。**")  # noqa: T201
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
