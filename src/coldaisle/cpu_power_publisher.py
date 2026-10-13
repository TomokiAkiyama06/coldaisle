"""CPU Package の消費電力を RAPL から粗い平均だけで渡す root の小さなサービス（#278）。

決定記録 0122 §2.1 に従い、このモジュールは標準ライブラリだけを使い、``coldaisle``
パッケージを import しない（引き継ぎ実行部と同じ形。0080 §2.5）。systemd から
``/usr/bin/python3 -I -S <このファイル>`` で動かし、venv に依存しない。

powercap の ``energy_uj`` は PLATYPUS（CVE-2020-8694 / 8695）への対策で root だけが読める。
ここで 1 s 程度の窓の平均を 1 W に丸めた値だけを JSON で書き、一般のユーザーで動く
``coldaisle-telemetry`` に渡す。ms 単位の変化は渡さない。

読む先・書く先・周期・妥当範囲は引数で受け取り、コードに既定値を持たない
（AGENTS.md ルール9。値は unit が 0122 のとおりに渡す）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

SCHEMA_VERSION = 1
"""出力の JSON の版（0122 §2.1）。読み手（``CpuPowerAdapter``）と対になる。"""
PACKAGE_ZONE_NAME = "package-0"
"""powercap の zone の ``name``。番号（``intel-rapl:0``）ではなく名前で探す（0122 §2.1）。"""
EXIT_ZONE_NOT_UNIQUE = 2
"""起動時の待ちを過ぎても zone がちょうど1つでない・引数が不正。

unit の ``RestartPreventExitStatus=2`` と対になる。
"""
ZONE_RETRY_INTERVAL_S = 1.0
"""起動時に zone を探し直す間隔（0122 §2.1 の 1 s）。driver の読み込みを待つための手順の値。"""
EXIT_READ_OR_WRITE_FAILED = 1
"""運転中に読めない・書けない。systemd に再起動させ、zone を探し直させる。"""
OUTPUT_MODE = 0o640
"""出力ファイルの mode。グループ（unit の ``Group=``）だけが読める（0122 §2.1）。"""
_MAX_ATTRIBUTE_BYTES = 4 * 1024
_NS_PER_MS = 1_000_000
_NS_PER_US = 1_000

_LOGGER = logging.getLogger("coldaisle.cpu_power_publisher")


class ZoneNotUniqueError(Exception):
    """``name`` が ``package-0`` の zone が 0 個、または 2 個以上ある。"""

    def __init__(self, count: int) -> None:
        super().__init__(f"package zone count: {count}")
        self.count = count


@dataclass(frozen=True, slots=True)
class PublisherSettings:
    """引数で受け取る設定。既定値は持たない。"""

    output: Path
    interval_ms: int
    startup_wait_ms: int
    min_watts: int
    max_watts: int

    def __post_init__(self) -> None:
        if self.interval_ms <= 0:
            raise ValueError("interval_ms は正にする")
        if self.startup_wait_ms < 0:
            raise ValueError("startup_wait_ms は 0 以上にする")
        if self.min_watts < 0 or self.min_watts >= self.max_watts:
            raise ValueError("min_watts は 0 以上で max_watts より小さくする")

    @property
    def min_elapsed_ns(self) -> int:
        """これより短い窓の平均は書かない（周期の半分。0122 §2.1）。"""
        return self.interval_ms * _NS_PER_MS // 2

    @property
    def max_elapsed_ns(self) -> int:
        """これより長い窓の平均は書かない（周期の2倍。停止や遅れで窓が崩れたため）。"""
        return self.interval_ms * _NS_PER_MS * 2


def find_package_zone(powercap_root: Path) -> Path:
    """``powercap_root`` の直下で ``name`` が ``package-0`` の zone をちょうど1つ探す。

    ``/sys/class/powercap`` の下は symlink の一覧なので、たどった先の ``name`` を読む。
    読めない entry は候補にしない（``name`` を持たない ``intel-rapl`` の control type など）。
    """
    matches: list[Path] = []
    for entry in sorted(powercap_root.iterdir()):
        try:
            name = _read_attribute(entry / "name")
        except (OSError, ValueError):
            continue
        if name == PACKAGE_ZONE_NAME:
            matches.append(entry)
    if len(matches) != 1:
        raise ZoneNotUniqueError(len(matches))
    return matches[0]


def wait_for_package_zone(
    powercap_root: Path,
    startup_wait_ms: int,
    stop: threading.Event,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> Path | None:
    """zone がちょうど1つ見つかるまで探し直す（0122 §2.1）。

    ``startup_wait_ms`` を過ぎても見つからなければ ``ZoneNotUniqueError`` を上げる。
    ``powercap_root`` が無い・読めない間も、driver の読み込み前とみなして待つ。
    待ちの間に ``stop`` が立てば ``None`` を返す。
    """
    deadline = monotonic() + startup_wait_ms / 1_000
    while True:
        try:
            return find_package_zone(powercap_root)
        except (ZoneNotUniqueError, OSError) as error:
            last = error if isinstance(error, ZoneNotUniqueError) else ZoneNotUniqueError(0)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise last
        if stop.wait(min(ZONE_RETRY_INTERVAL_S, remaining)):
            return None


def watts_from_energy(
    previous_uj: int, current_uj: int, max_energy_range_uj: int, elapsed_ns: int
) -> float:
    """積算エネルギーの差と経過時間から平均の W を出す。差が負なら一周とみなす。"""
    delta_uj = current_uj - previous_uj
    if delta_uj < 0:
        delta_uj += max_energy_range_uj
    if delta_uj < 0:
        raise ValueError("一周を足しても energy の差が負")
    if elapsed_ns <= 0:
        raise ValueError("経過時間が正でない")
    # μJ / μs = W
    return delta_uj / (elapsed_ns / _NS_PER_US)


@dataclass(frozen=True, slots=True)
class _EnergySample:
    energy_uj: int
    monotonic_ns: int


class Publisher:
    """1周期ぶんの読み取り・計算・書き込みを行う。時計は差し替えられる（試験用）。"""

    def __init__(
        self,
        zone: Path,
        settings: PublisherSettings,
        *,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        wall_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._zone = zone
        self._settings = settings
        self._monotonic_ns = monotonic_ns
        self._wall_ns = wall_ns
        self._max_energy_range_uj = _read_int_attribute(zone / "max_energy_range_uj")
        if self._max_energy_range_uj <= 0:
            raise ValueError("max_energy_range_uj が正でない")
        self._previous: _EnergySample | None = None

    def step(self) -> int | None:
        """``energy_uj`` を1回読み、書けたら書いた W を返す。書かなかった回は ``None``。

        読めない・書けないときは ``OSError`` / ``ValueError`` をそのまま上げる。
        """
        current = _EnergySample(
            energy_uj=_read_int_attribute(self._zone / "energy_uj"),
            monotonic_ns=self._monotonic_ns(),
        )
        previous, self._previous = self._previous, current
        if previous is None:
            return None
        elapsed_ns = current.monotonic_ns - previous.monotonic_ns
        if not self._settings.min_elapsed_ns <= elapsed_ns <= self._settings.max_elapsed_ns:
            _LOGGER.warning(
                _json_event(
                    {"event": "cpu_power_window_skipped", "elapsed_ms": elapsed_ns // _NS_PER_MS}
                )
            )
            return None
        watts = round(
            watts_from_energy(
                previous.energy_uj, current.energy_uj, self._max_energy_range_uj, elapsed_ns
            )
        )
        if not self._settings.min_watts <= watts <= self._settings.max_watts:
            _LOGGER.warning(_json_event({"event": "cpu_power_out_of_range", "watts": watts}))
            return None
        write_atomically(
            self._settings.output,
            {
                "schema_version": SCHEMA_VERSION,
                "ts_ms": self._wall_ns() // _NS_PER_MS,
                "watts": watts,
                "window_ms": round(elapsed_ns / _NS_PER_MS),
            },
        )
        return watts


def write_atomically(path: Path, payload: dict[str, Any]) -> None:
    """同じディレクトリの一時ファイルに書き、``os.replace`` で置き換える。

    読み手（``coldaisle-telemetry``）に書きかけのファイルを見せない。
    """
    encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            # mkstemp は 0600 で作る。グループ（unit の Group=）にだけ読ませる
            os.fchmod(fd, OUTPUT_MODE)
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def run(publisher: Publisher, interval_ms: int, stop: threading.Event) -> int:
    """``stop`` が立つまで周期ごとに ``step`` を呼ぶ。戻り値は終了コード。"""
    interval_s = interval_ms / 1_000
    deadline = time.monotonic()
    while not stop.is_set():
        try:
            publisher.step()
        except (OSError, ValueError) as error:
            _LOGGER.error(
                _json_event({"event": "cpu_power_failed", "detail": type(error).__name__})
            )
            return EXIT_READ_OR_WRITE_FAILED
        deadline += interval_s
        now = time.monotonic()
        if deadline < now:
            # 遅れを取り戻そうと連続で読むと窓が短くなる。次の周期から数え直す
            deadline = now + interval_s
        stop.wait(deadline - now)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """引数はすべて必須（既定値をコードに持たない。AGENTS.md ルール9）。"""
    parser = argparse.ArgumentParser(description="CPU Package power publisher (RAPL)")
    parser.add_argument("--powercap-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ms", type=int, required=True)
    parser.add_argument("--startup-wait-ms", type=int, required=True)
    parser.add_argument("--min-watts", type=int, required=True)
    parser.add_argument("--max-watts", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """systemd の ``coldaisle-cpu-power.service`` の入口。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    try:
        settings = PublisherSettings(
            output=args.output,
            interval_ms=args.interval_ms,
            startup_wait_ms=args.startup_wait_ms,
            min_watts=args.min_watts,
            max_watts=args.max_watts,
        )
    except ValueError as error:
        _LOGGER.error(_json_event({"event": "cpu_power_invalid_arguments", "detail": str(error)}))
        return EXIT_ZONE_NOT_UNIQUE
    # zone を待つ間も SIGTERM で止まれるよう、探す前に入れる
    stop = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        zone = wait_for_package_zone(args.powercap_root, settings.startup_wait_ms, stop)
    except ZoneNotUniqueError as error:
        _LOGGER.error(_json_event({"event": "cpu_power_zone_not_unique", "count": error.count}))
        return EXIT_ZONE_NOT_UNIQUE
    if zone is None:
        _LOGGER.info(_json_event({"event": "cpu_power_stopped", "exit_code": 0}))
        return 0
    try:
        publisher = Publisher(zone, settings)
    except (OSError, ValueError) as error:
        # 見つかった後に読めない。unit に再起動させ、zone を探し直させる（0122 §2.1）
        _LOGGER.error(_json_event({"event": "cpu_power_failed", "detail": type(error).__name__}))
        return EXIT_READ_OR_WRITE_FAILED
    _LOGGER.info(
        _json_event(
            {"event": "cpu_power_started", "zone": zone.name, "interval_ms": settings.interval_ms}
        )
    )
    code = run(publisher, settings.interval_ms, stop)
    _LOGGER.info(_json_event({"event": "cpu_power_stopped", "exit_code": code}))
    return code


def _read_attribute(path: Path) -> str:
    with path.open("rb") as handle:
        payload = handle.read(_MAX_ATTRIBUTE_BYTES + 1)
    if len(payload) > _MAX_ATTRIBUTE_BYTES:
        raise OSError("powercap attribute が大きすぎる")
    return payload.decode("ascii").strip()


def _read_int_attribute(path: Path) -> int:
    text = _read_attribute(path)
    if not text.isdigit():
        raise ValueError("powercap attribute が非負の整数でない")
    return int(text)


def _json_event(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
