"""反実仮想 artifact v2 の較正の digest（``calibration-digest-v1``。決定記録 0096）。

artifact v2 の ``calibration_binding.sha256`` の意味をここで1つに決める。trainer（学習時）と
loader の L9（読み込み時）が**同じ関数**で計算し、呼び出し側が digest を手で作る経路を持たない
（0096 §2.4 / §2.6）。

- 入力は ``metric_binding`` の entry の列と、チャネル名 → offset（``Calibration.offsets_c``）だけ
- 派生値は minuend / subtrahend へ1段だけ展開する。展開した先がまた派生値なら作らない
- 較正を当てるかの判定と実効の offset の写像はレイヤ横断の :mod:`coldaisle.calibration_offsets`
  と共有する（``Normalizer`` と同じ述語。0096 §5 #3）
- ``ingest`` を import しない。``Calibration`` 型を読むのは合成の起点

規則を変えるときは artifact の ``schema_version`` を上げる（0096 §2.5）。規則の名前は bytes にも
manifest にも入れない。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from coldaisle.calibration_offsets import (
    calibration_applies,
    canonical_offsets_bytes,
    effective_metric_offsets,
    offsets_sha256,
)
from coldaisle.channels import METRIC_TO_CHANNEL
from coldaisle.measurement import DERIVED_PREFIX


class _Derived(Protocol):
    @property
    def minuend(self) -> str: ...

    @property
    def subtrahend(self) -> str: ...


class _Entry(Protocol):
    @property
    def metric(self) -> str: ...

    @property
    def derived(self) -> _Derived | None: ...


def calibrated_metrics(entries: Iterable[_Entry]) -> frozenset[str]:
    """``metric_binding`` の entry から、較正の掛かる metric の集合を導く（0096 §2.2 の1〜3）。

    1. 派生値は minuend / subtrahend の2つへ展開し、派生値の名前そのものは残さない
    2. ``METRIC_TO_CHANNEL`` にあるものだけを残す
    3. ``Normalizer`` が較正を当てない metric（湿度）を除く

    展開した先が派生値（``d.*``）なら ``ValueError``（黙って1段だけ展開して進めない）。
    """
    names: set[str] = set()
    for entry in entries:
        if entry.derived is None:
            names.add(entry.metric)
            continue
        for part in (entry.derived.minuend, entry.derived.subtrahend):
            if part.startswith(DERIVED_PREFIX):
                raise ValueError(
                    f"派生値 {entry.metric} の展開した先がまた派生値である: {part}"
                    "（較正の digest は1段の展開だけを扱う）"
                )
            names.add(part)
    return frozenset(
        name for name in names if name in METRIC_TO_CHANNEL and calibration_applies(name)
    )


def _projected(
    entries: Iterable[_Entry], offsets_c: Mapping[str, float]
) -> dict[str, float] | None:
    metrics = calibrated_metrics(entries)
    if not metrics:
        return None
    effective = effective_metric_offsets(offsets_c)
    return {metric: effective[metric] for metric in metrics}


def calibration_digest_bytes(
    entries: Iterable[_Entry], offsets_c: Mapping[str, float]
) -> bytes | None:
    """digest の元になる canonical bytes（0096 §2.3）。較正の掛かる metric が無ければ ``None``。"""
    projected = _projected(entries, offsets_c)
    return None if projected is None else canonical_offsets_bytes(projected)


def calibration_digest(entries: Iterable[_Entry], offsets_c: Mapping[str, float]) -> str | None:
    """``calibration-digest-v1``（0096 §2）。較正の掛かる metric が無ければ ``None``。

    ``offsets_c`` は ``Calibration`` 型で読んだ後のチャネル名 → offset（℃）。無いチャネルは 0.0。
    ``note`` / ``calibrated_at`` / ``reference`` / ``samples`` は入力に取らない（0096 §2.1）。
    """
    projected = _projected(entries, offsets_c)
    return None if projected is None else offsets_sha256(projected)


@dataclass(frozen=True, slots=True)
class RuntimeCalibration:
    """runtime の較正（0096 §2.6）。**読めた値**と**読めなかった**の2状態。

    空の ``Mapping`` で「読めなかった」を表さない。空の値は全チャネル 0.0 と同じ digest になり、
    0.0 で学習した ``null`` でない artifact が L9 を通ってしまうため。:meth:`available` と
    :meth:`unavailable` からだけ作る。
    """

    offsets_c: Mapping[str, float] | None
    """読めた較正の値（チャネル名 → ℃）。読めなかったときは ``None``。"""
    unavailable_reason: str | None
    """読めなかった理由（構造化ログと拒否の詳細に出す）。読めたときは ``None``。"""

    def __post_init__(self) -> None:
        if (self.offsets_c is None) == (self.unavailable_reason is None):
            raise ValueError("RuntimeCalibration は available / unavailable のどちらか1つで作る")

    @classmethod
    def available(cls, offsets_c: Mapping[str, float]) -> RuntimeCalibration:
        """読めた較正（``Calibration.offsets_c``）。非有限値は作らない。"""
        copied: dict[str, float] = {}
        for channel, value in offsets_c.items():
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"較正の offset が有限でない: {channel}={value!r}")
            copied[str(channel)] = number
        return cls(offsets_c=MappingProxyType(copied), unavailable_reason=None)

    @classmethod
    def unavailable(cls, reason: str) -> RuntimeCalibration:
        """較正ファイルを読めなかった。``reason`` は空にしない。"""
        if not reason:
            raise ValueError("読めなかった理由を空にしない")
        return cls(offsets_c=None, unavailable_reason=reason)

    @property
    def is_available(self) -> bool:
        """較正の値を読めたか。"""
        return self.offsets_c is not None
