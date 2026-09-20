"""保存済み decision trace から Shadow 実績を書き出す（#90 → #91）。

決定記録 0030 §5 が「SQLite 外への export は #90 / #91 で決める」としていた部分である。
書き出すのは **counterfactual を持つ tick だけ**で、1行に

- 実際に適用した制御器と effective demand
- 適用しなかった提案（requested / optimizer の結果 / 裏づけ済み confidence / 予測 future）
- その予測と実測の突き合わせ

を、同じ ``tick_id`` / ``ts_ms`` と推論の識別子で結び付けて置く。

**読むだけで、書かない。** trace も観測も入力として受け取り、Store も hardware も触らない。
同じ入力からは必ず同じ bytes を出す（#91 の比較が再現できるように）。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Literal, Protocol, TextIO

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.control.schema import ControllerKind, ControlTick, ShadowRecord
from coldaisle.control.shadow.outcome import (
    ObservationIndex,
    OutcomeObservation,
    ShadowOutcome,
    ShadowOutcomeMatcher,
)

SHADOW_EXPORT_SCHEMA_VERSION: Literal[1] = 1
"""export 1行の形の版。**列の意味を変えたら上げる。**"""


class ControlTraceRow(Protocol):
    """``coldaisle.store`` が返す decision trace の行（#82 / 決定記録 0030）。

    Protocol にして Store を import しない。offline 評価（#91）は CSV でも SQLite でも、
    この形さえ満たせば同じ export を使える。
    """

    @property
    def ts_ms(self) -> int: ...

    @property
    def tick_id(self) -> int: ...

    @property
    def schema_version(self) -> int: ...

    @property
    def trace_json(self) -> str: ...


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ShadowExportRow(_Frozen):
    """1 tick 分の Shadow 実績。"""

    schema_version: Literal[1] = SHADOW_EXPORT_SCHEMA_VERSION
    control_schema_version: int = Field(ge=1)
    tick_id: int = Field(ge=0)
    ts_ms: int = Field(ge=0)
    shadow: ShadowRecord
    outcomes: tuple[ShadowOutcome, ...] = ()
    """counterfactual の予測と実測の突き合わせ。照合できなければ理由が残る。"""


def shadow_rows(
    traces: Iterable[ControlTraceRow],
    *,
    observations: Sequence[OutcomeObservation] = (),
    matcher: ShadowOutcomeMatcher | None = None,
) -> Iterator[ShadowExportRow]:
    """保存済み trace から、counterfactual を持つ tick の行を順に返す。

    ``matcher`` を渡すと予測と実測を突き合わせる。渡さなければ ``outcomes`` は空になる。
    **突き合わせの有無で counterfactual の記録内容は変わらない。**
    """
    # 実測の索引は**全行で1つ**。行ごとに並べ直すと、走査が行数に比例して伸びる。
    index = ObservationIndex(observations)
    for row in traces:
        tick = ControlTick.model_validate_json(row.trace_json)
        # **counterfactual の有無に関わらず先に照らす。** shadow を持たない行を素通しすると、
        # 索引の壊れた trace が「この期間には Shadow 実績が無い」に化ける。
        if (tick.tick_id, tick.ts_ms, tick.schema_version) != (
            row.tick_id,
            row.ts_ms,
            row.schema_version,
        ):
            # 保存した索引と中身が食い違う trace を、そのまま評価へ流さない。
            # **版も照らす**（`coldaisle.dataset` の trace 検証と同じ）。索引の版だけを見て
            # 読み分ける側が、中身と違う意味で解釈してしまう。
            raise ValueError(
                f"decision trace の索引と中身が一致しない"
                f"（row={row.tick_id}/{row.ts_ms}/v{row.schema_version}; "
                f"trace={tick.tick_id}/{tick.ts_ms}/v{tick.schema_version}）"
            )
        if tick.shadow is None:
            continue
        yield ShadowExportRow(
            control_schema_version=tick.schema_version,
            tick_id=tick.tick_id,
            ts_ms=tick.ts_ms,
            shadow=tick.shadow,
            outcomes=_outcomes(tick.shadow, index, matcher),
        )


def _outcomes(
    shadow: ShadowRecord,
    index: ObservationIndex,
    matcher: ShadowOutcomeMatcher | None,
) -> tuple[ShadowOutcome, ...]:
    if matcher is None:
        return ()
    return tuple(
        matcher.match(counterfactual.prediction, index)
        for counterfactual in shadow.counterfactuals
        if counterfactual.prediction is not None
    )


def write_shadow_jsonl(rows: Iterable[ShadowExportRow], stream: TextIO) -> int:
    """JSON Lines として書き出し、行数を返す。同じ入力からは同じ bytes になる。"""
    written = 0
    for row in rows:
        stream.write(row.model_dump_json())
        stream.write("\n")
        written += 1
    return written


def counterfactual_controllers(rows: Iterable[ShadowExportRow]) -> frozenset[ControllerKind]:
    """export に現れた counterfactual の制御器。#91 が比較対象を決めるのに使う。"""
    return frozenset(
        counterfactual.controller for row in rows for counterfactual in row.shadow.counterfactuals
    )
