"""``sys.gpu_state`` の識別（決定記録 0002 §2.6 / 0006 §2.2 / 0163 §2.6）。#63

NVML の compute process の PID ごとに ``/proc/<pid>/cgroup`` の cgroup v2 の path を読み、
**``.service`` で終わる最も内側の要素**を unit 名として ``config/gpu_state.yaml`` の
``ai_units`` と照合する。プロセス名（``comm`` / ``cmdline``）は使わない。vLLM の
worker も学習のジョブも ``python`` で、名前では分けられないため（0163 §4）。

**``compute`` は「AI 由来の CUDA process が 0」の証拠ではない。** ``compute`` が言うのは
「``ai_units`` に入る unit の process が無い」ことだけで、推論サーバの unit が一覧から
漏れていれば（改名・新しい推論サーバ）その process も ``compute`` に数えられる。
NVML に CUDA process が1つも無いことの証拠になるのは ``idle`` だけである。
``compute`` を「AI 由来 0」に使えるのは、導入時と推論サーバの変更時に陽性の対照
（推論サーバを動かして ``ai`` / ``mixed`` になる）を取れている間だけ（0163 §2.6）。

この値は観測の記録だけで、fand・Learned・Critical Safety の入力にしない（0163 §2.6）。
``/proc`` は読むだけで、書き込み・signal の送信はしない。
"""

from __future__ import annotations

import errno
import logging
import os
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple, Protocol, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from coldaisle import logs

LOGGER = logging.getLogger("coldaisle.internal_telemetry")

GPU_STATE_KEY = "sys.gpu_state"
"""``system_state`` に書く key（決定記録 0002 §2.6。``sys.`` を付ける）。"""

PROC_ROOT = Path("/proc")
"""cgroup を読む起点。kernel の ABI で運用で変える値ではない（試験だけが差し替える）。"""

SERVICE_SUFFIX = ".service"


class GpuState(StrEnum):
    """``sys.gpu_state`` の5値（0002 §2.6）。**既存の値の意味を変えない・消さない。**"""

    UNKNOWN = "unknown"
    """NVML が使えない・判定できない。最も保守的な扱い（0002 §2.6）。"""
    IDLE = "idle"
    """CUDA process が無い。**AI 由来 0 の唯一の直接の証拠。**"""
    AI = "ai"
    """すべての CUDA process が ``ai_units`` の unit に属する。"""
    COMPUTE = "compute"
    """どの CUDA process も ``ai_units`` の unit に属さない（AI 由来 0 の証拠ではない）。"""
    MIXED = "mixed"
    """両方がある。切り替えの失敗として警告する対象（0006 §2.2 / §2.4。警告は #36）。"""


class GpuStateConfig(BaseModel):
    """``config/gpu_state.yaml`` の形（0163 §2.6）。既定値をコードに持たない。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ai_units: tuple[str, ...] = Field(min_length=1)
    """推論サーバの systemd unit 名（完全一致）。ここに無い unit・scope の process は compute。"""

    @field_validator("ai_units")
    @classmethod
    def _units_are_plain_service_names(cls, units: tuple[str, ...]) -> tuple[str, ...]:
        for unit in units:
            if not unit.endswith(SERVICE_SUFFIX) or unit == SERVICE_SUFFIX:
                raise ValueError(f"ai_units は .service で終わる unit 名にする: {unit!r}")
            if "/" in unit or any(character.isspace() for character in unit):
                raise ValueError(f"ai_units に path や空白を含めない: {unit!r}")
            if unit.startswith("user@"):
                # user のサービスマネージャ自身。これを入れると、その利用者の
                # scope（端末・アプリ）の process まで推論サーバに数えられる（0163 §2.6）
                raise ValueError(f"ai_units に user@ のサービスマネージャを入れない: {unit!r}")
        if len(set(units)) != len(units):
            raise ValueError("ai_units は重複させない")
        return units

    @classmethod
    def from_yaml(cls, path: Path) -> Self:
        """厳格に読む。形が不正なら例外（呼び出し側が ``unknown`` に倒す）。"""
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"gpu_state の設定が辞書ではない: {path}")
        return cls.model_validate(loaded)


class CgroupUnreadableError(Exception):
    """PID は存在するが cgroup が読めない（権限・``hidepid``・cgroup v2 でない・形が違う）。"""


class ProcessGoneError(Exception):
    """NVML の一覧を得てから cgroup を読むまでの間に process が終わった。"""


def unit_from_cgroup(text: str) -> str | None:
    """``/proc/<pid>/cgroup`` の中身から、最も内側の ``.service`` の要素を返す。

    cgroup v2 の行（``0::<path>``）だけを見る。``.service`` の要素が無い
    （``session-N.scope``・``docker-<id>.scope`` など）なら ``None``。
    cgroup v2 の行が無い・複数ある・path が絶対でないなら判定できないので例外にする。

    user の unit（``.../user@<uid>.service/app.slice/<unit>.service``）は
    ``<unit>.service`` を返す。
    user のサービスマネージャの下の scope（``.../user@<uid>.service/app.slice/x.scope``）は
    ``user@<uid>.service`` を返す。``ai_units`` は ``user@`` を受けないので compute になる。
    """
    paths = [line[3:] for line in text.splitlines() if line.startswith("0::")]
    if len(paths) != 1:
        raise CgroupUnreadableError("cgroup v2 の行（0::）がちょうど1つではない")
    path = paths[0]
    if not path.startswith("/"):
        raise CgroupUnreadableError("cgroup v2 の path が絶対 path ではない")
    for element in reversed(path.split("/")):
        if element.endswith(SERVICE_SUFFIX) and element != SERVICE_SUFFIX:
            return element
    return None


class CgroupReader(Protocol):
    """PID から unit 名を引く境界。試験では偽の ``/proc`` を渡す。"""

    def unit_of(self, pid: int) -> str | None:
        """unit 名。``.service`` の要素が無ければ ``None``。

        終わった process は ``ProcessGoneError``、読めなければ ``CgroupUnreadableError``。
        """
        ...


def _process_exists(pid: int) -> bool:
    """``pidfd_open`` で PID の存在だけを確かめる（signal を送らない・権限が要らない）。

    ``hidepid=invisible`` や ``ProtectProc=invisible`` では、存在する process の
    ``/proc/<pid>`` も ``ENOENT`` になる。それを「終わった」と数えると、推論サーバが
    動いていても ``idle`` と記録してしまう。``pidfd_open`` は ``/proc`` の見え方に
    左右されない。確かめられないときは「存在する」側（判定不能）に倒す。
    """
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    os.close(descriptor)
    return True


class ProcCgroupReader:
    """``<root>/<pid>/cgroup`` を読む実装。"""

    def __init__(
        self,
        root: Path = PROC_ROOT,
        *,
        process_exists: Callable[[int], bool] = _process_exists,
    ) -> None:
        self._root = root
        self._process_exists = process_exists

    def unit_of(self, pid: int) -> str | None:
        """PID の unit 名を返す。PID の再利用の競合は避けられない（間隔は数 ms）。"""
        try:
            text = (self._root / str(pid) / "cgroup").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError) as error:
            if self._process_exists(pid):
                raise CgroupUnreadableError("process は存在するが /proc から見えない") from error
            raise ProcessGoneError from error
        except OSError as error:
            if error.errno == errno.ESRCH:
                raise ProcessGoneError from error
            raise CgroupUnreadableError(
                f"cgroup を読めない: {errno.errorcode.get(error.errno or 0, type(error).__name__)}"
            ) from error
        except UnicodeDecodeError as error:
            raise CgroupUnreadableError("cgroup が UTF-8 ではない") from error
        return unit_from_cgroup(text)


class GpuStateObservation(NamedTuple):
    """1回の判定。``reason`` は ``unknown`` の理由（ログ用）。"""

    state: GpuState
    reason: str | None = None
    non_ai_units: tuple[str, ...] = ()


NO_SERVICE = "(no .service)"
"""``.service`` の要素が無い process をログに出すときの表記。"""


def classify(units: Iterable[str | None], ai_units: frozenset[str]) -> GpuStateObservation:
    """各 process の unit 名から値を決める（0163 §2.6 の表）。

    ``None`` は ``.service`` の要素が無い process（scope など）で、compute に数える。
    """
    ai = 0
    other: set[str] = set()
    other_count = 0
    for unit in units:
        if unit is not None and unit in ai_units:
            ai += 1
        else:
            other_count += 1
            other.add(unit if unit is not None else NO_SERVICE)
    non_ai = tuple(sorted(other))
    if ai == 0 and other_count == 0:
        return GpuStateObservation(GpuState.IDLE)
    if other_count == 0:
        return GpuStateObservation(GpuState.AI)
    if ai == 0:
        return GpuStateObservation(GpuState.COMPUTE, non_ai_units=non_ai)
    return GpuStateObservation(GpuState.MIXED, non_ai_units=non_ai)


class GpuStateClassifier:
    """NVML の PID の一覧から ``sys.gpu_state`` を決める。

    ``config`` が ``None``（``config/gpu_state.yaml`` が無い・形が不正）なら常に ``unknown``。
    温度などの収集は止めない（0163 §2.6。#63 の「Compute Mode 中も温度欠測なし」）。
    """

    def __init__(
        self,
        config: GpuStateConfig | None,
        reader: CgroupReader | None = None,
        *,
        config_error: str | None = None,
    ) -> None:
        self._ai_units = frozenset(config.ai_units) if config is not None else None
        self._config_error = config_error or "gpu_state の設定が無い"
        self._reader = reader or ProcCgroupReader()
        self._last: GpuStateObservation | None = None

    def unknown(self, reason: str) -> GpuState:
        """NVML 側の理由で判定できないとき。値を返し、変わったときだけログに残す。"""
        return self._record(GpuStateObservation(GpuState.UNKNOWN, reason))

    def classify(self, pids: Iterable[int]) -> GpuState:
        """全 GPU の compute process の PID から値を決める。

        読む間に終わった PID は数えない。1つでも読めない PID があれば全体を
        ``unknown`` にする（0163 §5 の 5。読めない PID を compute に数えない）。
        """
        if self._ai_units is None:
            return self.unknown(self._config_error)
        units: list[str | None] = []
        for pid in sorted(set(pids)):
            try:
                units.append(self._reader.unit_of(pid))
            except ProcessGoneError:
                continue
            except CgroupUnreadableError as error:
                return self.unknown(f"cgroup_unreadable: {error}")
        return self._record(classify(units, self._ai_units))

    def _record(self, observation: GpuStateObservation) -> GpuState:
        # 毎周期ログに出すと 2.5 秒ごとに同じ行が並ぶ。値・理由・unit が変わったときだけ出す
        if observation != self._last:
            level = logging.WARNING if observation.state is GpuState.UNKNOWN else logging.INFO
            LOGGER.log(
                level,
                "sys.gpu_state の判定が変わった",
                extra={
                    logs.FIELDS_KEY: {
                        "state": observation.state.value,
                        "reason": observation.reason,
                        "non_ai_units": list(observation.non_ai_units),
                    }
                },
            )
            self._last = observation
        return observation.state
