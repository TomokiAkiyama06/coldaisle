"""GPU の Fan を GPU コアの温度の曲線で書く root の小さなサービス（#285）。

決定記録 0128 に従い、このモジュールは標準ライブラリだけを使い、``coldaisle``
パッケージを import しない（0122 の ``cpu_power_publisher`` と同じ形）。systemd から
``/usr/bin/python3 -I -S <このファイル>`` で動かし、venv に依存しない。NVML は
``ctypes`` で ``libnvidia-ml.so.1`` を開いて呼ぶ。

GPU の Fan は fand の Control Pipeline（Front / Rear / Top）の外に置く（0128 §2.1）。
曲線は決定論的で、GPU 自身の既定の制御より、どの温度でも同じか高い回転にする。
止めたとき・NVML のエラーのときは GPU 自身の既定の制御へ戻す。落ちたとき・hang の
ときは unit の ``ExecStopPost=`` が ``--restore-default`` で戻す（0128 §2.4）。

曲線・周期・起動時の待ち・ヒステリシス・下げる速さは引数で受け取り、コードに既定値を
持たない（AGENTS.md ルール9。値は unit が 0128 のとおりに渡す）。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import math
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import FrameType
from typing import Any, Protocol

EXIT_FAILED = 1
"""運転中に NVML が読めない・書けない、または起動時の待ちを過ぎても device が 0 個。

systemd に再起動させる。device が 0 個なのは driver の読み込みが遅い・読み込み直しの
途中のことがある。再起動して待ち直せば、GPU が現れたときに人の操作なしで曲線へ戻る（0128 §2.2）。
"""
EXIT_CONFIG = 2
"""引数が不正・起動時の待ちを過ぎても device が 2 個以上・曲線が Fan の範囲の外・
NVML の library が無い。

unit の ``RestartPreventExitStatus=2`` と対になる。人が直すまで結果が変わらない。
"""
DEVICE_RETRY_INTERVAL_S = 1.0
"""起動時に device を探し直す間隔（0128 §2.2 の 1 s）。driver の読み込みを待つための手順の値。"""
MAX_PERCENT = 100
"""曲線の最後の点の回転（0128 §2.3）。600 W の定常で GPU の Fan を全開にするため。"""
NVML_LIBRARY = "libnvidia-ml.so.1"
NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0
FAN_POLICY_MANUAL = 1
"""``nvmlDeviceGetFanControlPolicy_v2`` の手動（``NVML_FAN_POLICY_MANUAL``）。

既定の制御（``NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW`` = 0）に戻されていたら書き直す。
"""

_LOGGER = logging.getLogger("coldaisle.gpu_fan_curve")


class NvmlError(Exception):
    """NVML の呼び出しが ``NVML_SUCCESS`` 以外を返した。"""

    def __init__(self, function: str, code: int, message: str) -> None:
        super().__init__(f"{function}: {code} {message}")
        self.function = function
        self.code = code


class Nvml(Protocol):
    """このサービスが使う NVML の範囲。

    device は index 0 だけを扱う（ちょうど1つのときだけ制御する）。
    """

    def init(self) -> None:
        """NVML を初期化する。"""

    def shutdown(self) -> None:
        """NVML を閉じる。"""

    def device_count(self) -> int:
        """device の数。"""

    def fan_count(self) -> int:
        """device 0 の Fan の数。"""

    def fan_speed_range(self) -> tuple[int, int]:
        """device 0 に書ける回転（%）の最小と最大。"""

    def temperature_c(self) -> int:
        """device 0 の GPU コアの温度（°C）。"""

    def target_fan_speed(self, fan: int) -> int:
        """Fan の目標の回転（%）。"""

    def fan_policy(self, fan: int) -> int:
        """Fan の制御の方式（手動か既定の制御か）。"""

    def set_fan_speed(self, fan: int, percent: int) -> None:
        """Fan を手動で ``percent`` に固定する。"""

    def set_default_fan_speed(self, fan: int) -> None:
        """Fan を GPU 自身の既定の制御へ戻す。"""


class CtypesNvml:
    """``libnvidia-ml.so.1`` を ``ctypes`` で呼ぶ。

    標準ライブラリだけで済ませるため（0128 §2.2）。
    """

    def __init__(self, library: str = NVML_LIBRARY) -> None:
        self._lib = ctypes.CDLL(library)
        self._lib.nvmlErrorString.restype = ctypes.c_char_p
        self._handle = ctypes.c_void_p()

    def _call(self, function: str, *args: object) -> None:
        code = int(getattr(self._lib, function)(*args))
        if code != NVML_SUCCESS:
            raw = self._lib.nvmlErrorString(code)
            message = raw.decode("ascii", "replace") if isinstance(raw, bytes) else ""
            raise NvmlError(function, code, message)

    def _uint(self, function: str, *args: object) -> int:
        value = ctypes.c_uint()
        self._call(function, *args, ctypes.byref(value))
        return int(value.value)

    def init(self) -> None:
        """NVML を初期化し、device 0 の handle を取る（数は ``device_count`` で確かめる）。"""
        self._call("nvmlInit_v2")

    def shutdown(self) -> None:
        """NVML を閉じる。"""
        self._call("nvmlShutdown")

    def device_count(self) -> int:
        """device の数。ちょうど1つなら device 0 の handle を取っておく。"""
        count = self._uint("nvmlDeviceGetCount_v2")
        if count == 1:
            self._call(
                "nvmlDeviceGetHandleByIndex_v2", ctypes.c_uint(0), ctypes.byref(self._handle)
            )
        return count

    def fan_count(self) -> int:
        """device 0 の Fan の数。"""
        return self._uint("nvmlDeviceGetNumFans", self._handle)

    def fan_speed_range(self) -> tuple[int, int]:
        """device 0 に書ける回転（%）の最小と最大。"""
        low, high = ctypes.c_uint(), ctypes.c_uint()
        self._call(
            "nvmlDeviceGetMinMaxFanSpeed", self._handle, ctypes.byref(low), ctypes.byref(high)
        )
        return int(low.value), int(high.value)

    def temperature_c(self) -> int:
        """device 0 の GPU コアの温度（°C）。"""
        return self._uint(
            "nvmlDeviceGetTemperature", self._handle, ctypes.c_uint(NVML_TEMPERATURE_GPU)
        )

    def target_fan_speed(self, fan: int) -> int:
        """Fan の目標の回転（%）。"""
        return self._uint("nvmlDeviceGetTargetFanSpeed", self._handle, ctypes.c_uint(fan))

    def fan_policy(self, fan: int) -> int:
        """Fan の制御の方式。"""
        return self._uint("nvmlDeviceGetFanControlPolicy_v2", self._handle, ctypes.c_uint(fan))

    def set_fan_speed(self, fan: int, percent: int) -> None:
        """Fan を手動で ``percent`` に固定する（``CAP_SYS_ADMIN`` が要る。0128 §2.5）。"""
        self._call(
            "nvmlDeviceSetFanSpeed_v2", self._handle, ctypes.c_uint(fan), ctypes.c_uint(percent)
        )

    def set_default_fan_speed(self, fan: int) -> None:
        """Fan を GPU 自身の既定の制御へ戻す。"""
        self._call("nvmlDeviceSetDefaultFanSpeed_v2", self._handle, ctypes.c_uint(fan))


@dataclass(frozen=True, slots=True)
class Curve:
    """GPU コアの温度（°C）→ 回転（%）の折れ線（0128 §2.3）。点の間は直線で補う。"""

    points: tuple[tuple[float, int], ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("曲線の点は2つ以上にする")
        temperatures = [point[0] for point in self.points]
        percents = [point[1] for point in self.points]
        if any(b <= a for a, b in pairwise(temperatures)):
            raise ValueError("曲線の温度は真に増加させる")
        if any(b < a for a, b in pairwise(percents)):
            raise ValueError("曲線の回転は減らさない")
        if percents[-1] != MAX_PERCENT:
            raise ValueError(f"曲線の最後の点は {MAX_PERCENT} % にする")
        if percents[0] < 0:
            raise ValueError("曲線の回転は 0 以上にする")

    @classmethod
    def parse(cls, text: str) -> Curve:
        """``45:30,55:50,65:80,70:100`` の形（温度:回転 をカンマで区切る）を読む。"""
        points: list[tuple[float, int]] = []
        for item in text.split(","):
            temperature, separator, percent = item.strip().partition(":")
            if not separator:
                raise ValueError(f"曲線の点の形が違う: {item!r}")
            value = float(temperature)
            if not math.isfinite(value):
                raise ValueError(f"曲線の温度が有限でない: {item!r}")
            points.append((value, int(percent)))
        return cls(tuple(points))

    def percent_at(self, temperature_c: float) -> float:
        """``temperature_c`` での回転（%）。

        最初の点より下は最初の回転、最後の点より上は最後の回転。
        """
        first, last = self.points[0], self.points[-1]
        if temperature_c <= first[0]:
            return float(first[1])
        if temperature_c >= last[0]:
            return float(last[1])
        for (t0, p0), (t1, p1) in pairwise(self.points):
            if t0 <= temperature_c <= t1:
                return p0 + (p1 - p0) * (temperature_c - t0) / (t1 - t0)
        raise AssertionError("unreachable")

    @property
    def minimum(self) -> int:
        """曲線の最小の回転。"""
        return self.points[0][1]


@dataclass(frozen=True, slots=True)
class Settings:
    """引数で受け取る設定。既定値は持たない。"""

    curve: Curve
    interval_ms: int
    startup_wait_ms: int
    hysteresis_c: float
    down_rate_percent_per_s: float

    def __post_init__(self) -> None:
        if self.interval_ms <= 0:
            raise ValueError("interval_ms は正にする")
        if self.startup_wait_ms < 0:
            raise ValueError("startup_wait_ms は 0 以上にする")
        if not math.isfinite(self.hysteresis_c) or self.hysteresis_c < 0:
            raise ValueError("hysteresis_c は 0 以上にする")
        if not math.isfinite(self.down_rate_percent_per_s) or self.down_rate_percent_per_s <= 0:
            raise ValueError("down_rate_percent_per_s は正にする")


class FanLevel:
    """上げはすぐ、下げはヒステリシスと速さの上限を付けて回転を決める（0128 §2.2）。

    下げるときも、いまの温度の曲線の値（``percent_at(temperature)``）を下回らない。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._level: float | None = None
        self._updated_s: float | None = None

    def update(self, temperature_c: float, now_s: float) -> int:
        """``now_s``（単調時計の秒）に読んだ温度から、書く回転（%、整数に切り上げ）を返す。"""
        curve = self._settings.curve
        wanted = curve.percent_at(temperature_c)
        previous, previous_s = self._level, self._updated_s
        if previous is None or previous_s is None or wanted >= previous:
            level = wanted
        else:
            held = curve.percent_at(temperature_c + self._settings.hysteresis_c)
            elapsed_s = max(0.0, now_s - previous_s)
            limited = previous - self._settings.down_rate_percent_per_s * elapsed_s
            level = min(previous, max(held, limited))
        self._level, self._updated_s = level, now_s
        # 書くのは整数の %。切り上げて安全側に寄せる（曲線の値を下回らない）
        return math.ceil(level - 1e-9)


class Notifier:
    """``sd_notify`` を ``NOTIFY_SOCKET`` へ送る（Type=notify・WatchdogSec。0128 §2.4）。

    ``NOTIFY_SOCKET`` が無ければ（unit の外で動かしたとき）何もしない。
    """

    def __init__(self, address: str | None) -> None:
        self._address = address

    @classmethod
    def from_environment(cls) -> Notifier:
        """systemd が渡した ``NOTIFY_SOCKET`` を使う。"""
        return cls(os.environ.get("NOTIFY_SOCKET"))

    def send(self, message: str) -> None:
        """1 つの datagram を送る。抽象名前空間（先頭の ``@``）にも対応する。"""
        if not self._address:
            return
        address = "\0" + self._address[1:] if self._address.startswith("@") else self._address
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("ascii"), address)


class FanCurveService:
    """1 周期ぶんの読み取りと書き込み。NVML と時計は差し替えられる（試験用）。"""

    def __init__(
        self,
        nvml: Nvml,
        settings: Settings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._nvml = nvml
        self._level = FanLevel(settings)
        self._monotonic = monotonic
        self._fans = range(nvml.fan_count())
        if len(self._fans) == 0:
            raise ValueError("Fan が無い")
        low, high = nvml.fan_speed_range()
        if settings.curve.minimum < low or high < MAX_PERCENT:
            raise ValueError(
                f"曲線（{settings.curve.minimum}〜{MAX_PERCENT} %）が"
                f" Fan の範囲 {low}〜{high} % の外"
            )
        self._written: int | None = None

    def step(self) -> int:
        """温度を1回読み、目標の回転を求め、違う Fan だけに書く。書いた回転を返す。

        NVML のエラーは ``NvmlError`` のまま上げる（呼び手が既定の制御へ戻して終わる）。
        """
        temperature = self._nvml.temperature_c()
        percent = self._level.update(float(temperature), self._monotonic())
        rewritten: list[int] = []
        for fan in self._fans:
            if (
                self._nvml.fan_policy(fan) != FAN_POLICY_MANUAL
                or self._nvml.target_fan_speed(fan) != percent
            ):
                self._nvml.set_fan_speed(fan, percent)
                rewritten.append(fan)
        if percent != self._written:
            _LOGGER.info(
                _json_event(
                    {"event": "gpu_fan_target", "percent": percent, "temperature_c": temperature}
                )
            )
        elif rewritten:
            # 目標は変わっていないのに書き直した: driver が既定の制御へ戻した・外から変えられた
            _LOGGER.warning(
                _json_event({"event": "gpu_fan_rewritten", "fans": rewritten, "percent": percent})
            )
        self._written = percent
        return percent

    def restore_default(self) -> None:
        """すべての Fan を既定の制御へ戻す。"""
        restore_default_fans(self._nvml, self._fans)


def restore_default_fans(nvml: Nvml, fans: Sequence[int]) -> None:
    """各 Fan を既定の制御へ戻す。1 つが失敗しても残りを試み、最後に最初のエラーを上げる。"""
    first: NvmlError | None = None
    for fan in fans:
        try:
            nvml.set_default_fan_speed(fan)
        except NvmlError as error:
            first = first or error
    if first is not None:
        raise first


def wait_for_single_device(
    nvml: Nvml,
    startup_wait_ms: int,
    stop: threading.Event,
    notifier: Notifier,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> int | None:
    """NVML を初期化し、device がちょうど1つになるまで探し直す（0128 §2.2）。

    最後に見た device の数を返す（見つかれば 1。NVML が初期化できないままなら 0）。
    待ちの間に ``stop`` が立てば ``None``。待つ間も watchdog へ通知する
    （Type=notify の watchdog は起動から数える）。
    """
    deadline = monotonic() + startup_wait_ms / 1_000
    initialized = False
    count = 0
    while True:
        try:
            if not initialized:
                nvml.init()
                initialized = True
            count = nvml.device_count()
            if count == 1:
                return count
        except NvmlError as error:
            _LOGGER.warning(_json_event({"event": "gpu_fan_waiting", "detail": str(error)}))
        notifier.send("WATCHDOG=1")
        remaining = deadline - monotonic()
        if remaining <= 0:
            _LOGGER.error(_json_event({"event": "gpu_fan_device_not_unique", "count": count}))
            return count
        if stop.wait(min(DEVICE_RETRY_INTERVAL_S, remaining)):
            return None


def run(
    service: FanCurveService,
    interval_ms: int,
    stop: threading.Event,
    notifier: Notifier,
) -> int:
    """``stop`` が立つまで周期ごとに ``step`` を呼ぶ。戻り値は終了コード。

    どの終わり方でも、終わる前に既定の制御へ戻す
    （戻せなくても unit の ``ExecStopPost=`` がもう一度試みる）。
    """
    interval_s = interval_ms / 1_000
    deadline = time.monotonic()
    code = 0
    ready = False
    while not stop.is_set():
        try:
            service.step()
        except NvmlError as error:
            _LOGGER.error(_json_event({"event": "gpu_fan_failed", "detail": str(error)}))
            code = EXIT_FAILED
            break
        if not ready:
            notifier.send("READY=1")
            ready = True
        notifier.send("WATCHDOG=1")
        deadline += interval_s
        now = time.monotonic()
        if deadline < now:
            deadline = now + interval_s
        stop.wait(deadline - now)
    try:
        service.restore_default()
    except NvmlError as error:
        _LOGGER.error(_json_event({"event": "gpu_fan_restore_failed", "detail": str(error)}))
        code = code or EXIT_FAILED
    return code


def restore_default_main(nvml: Nvml) -> int:
    """``--restore-default``: 既定の制御へ戻すだけ（unit の ``ExecStopPost=``。0128 §2.4）。

    device がちょうど1つでなければ戻す先が無いので 0 で終わる。
    """
    try:
        nvml.init()
        count = nvml.device_count()
        if count != 1:
            _LOGGER.warning(_json_event({"event": "gpu_fan_restore_skipped", "count": count}))
            return 0
        restore_default_fans(nvml, range(nvml.fan_count()))
    except NvmlError as error:
        _LOGGER.error(_json_event({"event": "gpu_fan_restore_failed", "detail": str(error)}))
        return EXIT_FAILED
    _LOGGER.info(_json_event({"event": "gpu_fan_restored"}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """引数は ``--restore-default`` のとき以外すべて必須。

    既定値をコードに持たない（AGENTS.md ルール9）。
    """
    parser = argparse.ArgumentParser(description="GPU fan curve service (NVML)")
    parser.add_argument("--restore-default", action="store_true")
    parser.add_argument("--curve")
    parser.add_argument("--interval-ms", type=int)
    parser.add_argument("--startup-wait-ms", type=int)
    parser.add_argument("--hysteresis-c", type=float)
    parser.add_argument("--down-rate-percent-per-s", type=float)
    return parser


def main(
    argv: Sequence[str] | None = None, *, nvml_factory: Callable[[], Nvml] = CtypesNvml
) -> int:
    """systemd の ``coldaisle-gpu-fan.service`` の入口。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.restore_default:
        try:
            nvml = nvml_factory()
        except OSError as error:
            _LOGGER.error(_json_event({"event": "gpu_fan_restore_failed", "detail": str(error)}))
            return EXIT_FAILED
        return restore_default_main(nvml)
    required = (
        "curve",
        "interval_ms",
        "startup_wait_ms",
        "hysteresis_c",
        "down_rate_percent_per_s",
    )
    missing = [name for name in required if getattr(args, name) is None]
    try:
        if missing:
            raise ValueError(f"必須の引数が無い: {', '.join(missing)}")
        settings = Settings(
            curve=Curve.parse(args.curve),
            interval_ms=args.interval_ms,
            startup_wait_ms=args.startup_wait_ms,
            hysteresis_c=args.hysteresis_c,
            down_rate_percent_per_s=args.down_rate_percent_per_s,
        )
    except ValueError as error:
        _LOGGER.error(_json_event({"event": "gpu_fan_invalid_arguments", "detail": str(error)}))
        return EXIT_CONFIG
    # device を待つ間も SIGTERM で止まれるよう、探す前に入れる
    stop = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    notifier = Notifier.from_environment()
    try:
        nvml = nvml_factory()
    except OSError as error:
        # driver が入っていない導入先。待っても変わらない
        _LOGGER.error(_json_event({"event": "gpu_fan_library_missing", "detail": str(error)}))
        return EXIT_CONFIG
    count = wait_for_single_device(nvml, settings.startup_wait_ms, stop, notifier)
    if count is None:
        _LOGGER.info(_json_event({"event": "gpu_fan_stopped", "exit_code": 0}))
        return 0
    if count == 0:
        return EXIT_FAILED
    if count != 1:
        return EXIT_CONFIG
    try:
        service = FanCurveService(nvml, settings)
    except ValueError as error:
        _LOGGER.error(_json_event({"event": "gpu_fan_invalid_arguments", "detail": str(error)}))
        return EXIT_CONFIG
    except NvmlError as error:
        _LOGGER.error(_json_event({"event": "gpu_fan_failed", "detail": str(error)}))
        return EXIT_FAILED
    _LOGGER.info(
        _json_event(
            {
                "event": "gpu_fan_started",
                "curve": [list(point) for point in settings.curve.points],
                "interval_ms": settings.interval_ms,
            }
        )
    )
    code = run(service, settings.interval_ms, stop, notifier)
    _LOGGER.info(_json_event({"event": "gpu_fan_stopped", "exit_code": code}))
    return code


def _json_event(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
