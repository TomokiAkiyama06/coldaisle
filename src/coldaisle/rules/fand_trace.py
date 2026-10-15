"""fand の decision trace から状態と fault だけを読む（L2）。決定記録 0148 §2.4

**`ControlTick` で検証しない。** ルールのパッケージは制御の型（`coldaisle.control`）に依存せず、
v1 から形の変わっていない 2 つの欄（`state.safety_state` と `faults[].code` /
`faults[].zone`）だけを読む。
版（v14 以前・v15・v16 …）は読み分けない。2 つの欄の形が想定と違えば「読めない」（`None`）とし、
黙って「異常なし」にしない（0148 §2.4）。

**trace をそのまま外へ出さない**（ルール 8・0071 §2.8）。ここから先へ渡すのは閉じた語彙
（状態・fault code・zone）だけで、fault の自由記述（`faults[].detail`）は読まない（0148 §2.6）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from coldaisle.store import SequencedControlTrace

SAFETY_STATES = frozenset({"startup", "normal", "degraded", "emergency"})
"""Critical Safety の状態の語彙（0028 §2.5 (d)）。閾値ではなく trace の形の一部。"""

_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_ZONE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class FandFault:
    """1 つの fault の code と zone（zone に紐づかない fault は `None`）。"""

    code: str
    zone: str | None


@dataclass(frozen=True)
class FandStatus:
    """最新の trace から読んだ fand の状態。"""

    safety_state: str
    faults: tuple[FandFault, ...]


def read_fand_status(trace: SequencedControlTrace) -> FandStatus | None:
    """trace の 2 つの欄を読む。**形が想定と違えば `None`**（読めない trace。0148 §2.4）。"""
    try:
        body = json.loads(trace.trace_json)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    state = body.get("state")
    if not isinstance(state, dict):
        return None
    safety_state = state.get("safety_state")
    if not isinstance(safety_state, str) or safety_state not in SAFETY_STATES:
        return None
    raw_faults = body.get("faults", [])
    if not isinstance(raw_faults, list):
        return None
    faults: list[FandFault] = []
    for raw in raw_faults:
        if not isinstance(raw, dict):
            return None
        code = raw.get("code")
        zone = raw.get("zone")
        if not isinstance(code, str) or _CODE.match(code) is None:
            return None
        if zone is not None and (not isinstance(zone, str) or _ZONE.match(zone) is None):
            return None
        fault = FandFault(code=code, zone=zone)
        if fault not in faults:
            faults.append(fault)
    return FandStatus(safety_state=safety_state, faults=tuple(faults))


def describe_faults(faults: tuple[FandFault, ...], labels: Mapping[str, str]) -> str:
    """本文と `alerts.detail` に載せる原因の 1 行（0148 §2.5）。**閉じた語彙だけで作る。**

    `原因: <名前>（<code>[ / <zone>]）、…`。名前の無い code は code のまま出す。
    """
    if not faults:
        return "原因: 記録なし"
    parts = []
    for fault in faults:
        where = f" / {fault.zone}" if fault.zone else ""
        label = labels.get(fault.code)
        parts.append(f"{label}（{fault.code}{where}）" if label else f"{fault.code}{where}")
    return "原因: " + "、".join(parts)
