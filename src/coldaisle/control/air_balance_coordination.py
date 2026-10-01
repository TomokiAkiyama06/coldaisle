"""Baseline（Fallback）の requested に Air Balance の協調を掛ける部品（#81 / 決定記録 0078）。

ここにあるのは、raw baseline と `coordinate()` の提案から、上げるだけ・zone ごとの上限つき・
下げる前の保持つきの値を作る計算と、その検証、合成の下限の見込み（``projected_floors``）を
組み立てる純粋関数だけである。loop の中の位置（Fallback の後・Gate の前・合成の前）、
掛ける条件（0078 §2.3）、失敗の翻訳（0078 §2.6 / 決定記録 0085）は ``ControlLoop`` が持つ
（0078 §2.11 の段 3）。

- **上げるだけ。** どの zone も raw baseline（``candidate``）を下回らない。破れたら例外にする
- **上限は保持中も守る。** 保持するのは値ではなく、tick ごとの上限つきの**引き上げ幅**である
- **Fallback を import しない。** Fallback は ML にも characterization にも依存しない
  最後の拠り所のまま残し、raw baseline を常に記録できるようにする（0078 §2.2）
- 合成（Guard / Critical Safety）の上下限は持たない。協調の出力は requested を作る側の値で、
  合成は協調の後にいまと同じく掛かる（AGENTS.md ルール2・5、0028 §2.3）
"""

from __future__ import annotations

from collections import deque
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from coldaisle.control.air_balance import (
    AirBalanceCoordination,
    AirBalanceModel,
    ThermalInputs,
)
from coldaisle.control.config import (
    AirBalanceCoordinationConfig,
    AirBalanceCoordinationMode,
    FanHardwareConfig,
)
from coldaisle.control.schema import (
    Demand,
    GuardZoneOutput,
    PerZone,
    ProjectedFloorBasis,
    SafetyZoneOutput,
    Zone,
)


class AirBalanceCoordinationError(RuntimeError):
    """協調の出力が不変条件（下げない・上限）を破った。**不具合として扱う**（0078 §2.6）。"""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


CoordinatorStatus = Literal["shadow", "not_needed", "applied"]
"""``coordinate()`` を呼んだ tick の結果（0078 §2.7）。

``off`` / ``skipped`` / ``failed`` は呼び出し側が決める。
"""


class CoordinatorResult(_Frozen):
    """1 tick の協調の結果（0078 §2.4 / §2.7 の欄に対応する）。

    ``output`` が Gate へ渡す値で、``mode: shadow`` では ``candidate`` と同じになる。
    ``counterfactual_output`` は保持込みの値（apply なら Gate へ渡していた値）で、
    shadow でも apply でも記録する。
    """

    mode: Literal[AirBalanceCoordinationMode.SHADOW, AirBalanceCoordinationMode.APPLY]
    status: CoordinatorStatus
    candidate: PerZone[Demand]
    """raw baseline（c）。"""
    stable_candidate: PerZone[Demand]
    """``FanHardwareConfig.stable_demands(c)``（s）。``coordinate()`` はこの値で評価する。"""
    proposed: PerZone[Demand]
    """上限を掛ける前の p。引き上げの無い zone は c のまま。"""
    output: PerZone[Demand]
    counterfactual_output: PerZone[Demand]
    max_raise: PerZone[Demand]
    """この tick に適用した ``fan-policy.yaml`` の値。

trace だけで上限を検算するために記録する（0078 §2.7）。
"""
    bounded_by_max_raise: PerZone[bool]
    held: PerZone[bool]
    """``release_hold_ms`` の保持で、この tick の引き上げ幅より大きい幅を保った zone。"""
    projected_floors: PerZone[Demand]
    coordination: AirBalanceCoordination

    @model_validator(mode="after")
    def _never_lowers_and_stays_within_max_raise(self) -> Self:
        for zone in Zone:
            candidate = self.candidate.get(zone)
            ceiling = min(1.0, candidate + self.max_raise.get(zone))
            for name, values in (
                ("output", self.output),
                ("counterfactual_output", self.counterfactual_output),
            ):
                value = values.get(zone)
                if value < candidate:
                    raise ValueError(f"{name}.{zone.value} が raw baseline を下回る")
                if value > ceiling:
                    raise ValueError(f"{name}.{zone.value} が max_raise を超えて上げている")
        raised = any(self.output.get(zone) > self.candidate.get(zone) for zone in Zone)
        if self.mode is AirBalanceCoordinationMode.SHADOW:
            if self.status != "shadow":
                raise ValueError("mode: shadow の tick は status: shadow にする")
            if self.output != self.candidate:
                raise ValueError("mode: shadow は Gate へ raw baseline を渡す")
        else:
            if self.counterfactual_output != self.output:
                raise ValueError("mode: apply では counterfactual_output と output が一致する")
            if self.status != ("applied" if raised else "not_needed"):
                raise ValueError("mode: apply の status は引き上げの有無と一致させる")
        return self


class ProjectedFloors(_Frozen):
    """合成が zone ごとに requested へ掛けると見込まれる下限 ``f_z`` と、それを決めた下限。"""

    floors: PerZone[Demand]
    basis: PerZone[ProjectedFloorBasis]


def project_floors(
    *,
    safety: PerZone[SafetyZoneOutput],
    guard: PerZone[GuardZoneOutput],
    ramp: PerZone[float | None],
) -> ProjectedFloors:
    """合成（0028 §2.4）が requested に掛ける下限の見込み（決定記録 0078 §2.2 / §2.4）。

    ```text
    f_z = 1.0                                              … safety.z.forced_max
        = max(guard.z.floor, safety.z.floor, ramp_down の下限)  … それ以外（どれも無ければ 0.0）
    ```

    Front / Rear / Top に同じ式を掛ける。Guard の ceiling は下限の後には掛からないので入れない。
    **裁定を作り直さない・書き換えない**（発行済みの値を読むだけ）。``ramp`` は
    ``DemandComposer.ramp_floor()`` が合成と同じ関数で求めた値を渡す。basis は同値なら
    ``forced_max`` → ``safety_floor`` → ``guard_floor`` → ``ramp_down`` の順で1つ選ぶ（0078 §2.7）。
    """
    floors: dict[Zone, float] = {}
    basis: dict[Zone, ProjectedFloorBasis] = {}
    for zone in Zone:
        if safety.get(zone).forced_max:
            floors[zone] = 1.0
            basis[zone] = ProjectedFloorBasis.FORCED_MAX
            continue
        candidates: tuple[tuple[ProjectedFloorBasis, float | None], ...] = (
            (ProjectedFloorBasis.SAFETY_FLOOR, safety.get(zone).floor),
            (ProjectedFloorBasis.GUARD_FLOOR, guard.get(zone).floor),
            (ProjectedFloorBasis.RAMP_DOWN, ramp.get(zone)),
        )
        present = [(name, value) for name, value in candidates if value is not None]
        if not present:
            floors[zone] = 0.0
            basis[zone] = ProjectedFloorBasis.NONE
            continue
        value = max(item for _, item in present)
        floors[zone] = value
        basis[zone] = next(name for name, item in present if item == value)
    return ProjectedFloors(
        floors=PerZone[Demand](
            front=floors[Zone.FRONT], rear=floors[Zone.REAR], top=floors[Zone.TOP]
        ),
        basis=PerZone[ProjectedFloorBasis](
            front=basis[Zone.FRONT], rear=basis[Zone.REAR], top=basis[Zone.TOP]
        ),
    )


class AirBalanceCoordinator:
    """raw baseline に協調を掛ける（0078 §2.4）。保持の窓だけを状態として持つ。

    ```text
    s     = stable_demands(c)
    coord = coordinate(s, thermal, projected floors)
    p_z   = coord.requested.z if coord.requested.z > s_z else c_z
    r_z   = max(0, min(p_z, c_z + max_raise_z) - c_z)
    h_z   = 直近 release_hold_ms の r_z の最大値（この tick を含む）
    held  = min(1.0, c_z + h_z)
    ```

    保持の状態はプロセスのメモリにだけ持つ（再起動で消える）。条件が崩れた tick（``skipped``）と
    失敗の tick（``failed``）では、呼び出し側が ``release()`` で即座に解く（0078 §2.4）。
    ``apply()`` 自身が例外で終わるときは、ここで解いてから例外を投げ直す。
    """

    __slots__ = ("_fan_hardware", "_hold", "_last_mono_ms", "_model", "_settings")

    def __init__(
        self,
        *,
        settings: AirBalanceCoordinationConfig,
        model: AirBalanceModel,
        fan_hardware: FanHardwareConfig,
    ) -> None:
        if settings.mode is AirBalanceCoordinationMode.OFF:
            # off の tick は coordinate() を呼ばない（0078 §2.3）。部品を作らないことで、
            # off なのに協調の値が出る経路を持たない。
            raise ValueError("mode: off では AirBalanceCoordinator を作らない")
        if model.metadata.source.status != "calibrated":
            # ControlConfig も同じ組み合わせを拒むが、部品の側でも
            # 未校正の曲線を requested へ入れない。
            raise ValueError("未校正の Air Balance では協調しない（決定記録 0078 §2.4）")
        self._settings = settings
        self._model = model
        self._fan_hardware = fan_hardware
        self._hold: dict[Zone, deque[tuple[int, float]]] = {zone: deque() for zone in Zone}
        self._last_mono_ms: int | None = None

    @property
    def mode(self) -> AirBalanceCoordinationMode:
        """設定の ``mode``（``shadow`` か ``apply``）。"""
        return self._settings.mode

    def max_raise(self) -> PerZone[Demand]:
        """``fan-policy.yaml`` の zone ごとの上限。"""
        limits = self._settings.max_raise
        return PerZone[Demand](
            front=limits.front.value, rear=limits.rear.value, top=limits.top.value
        )

    def release(self) -> None:
        """保持を即座に解く（次の tick は raw baseline から評価し直す）。"""
        for window in self._hold.values():
            window.clear()

    def apply(
        self,
        *,
        candidate: PerZone[Demand],
        thermal: ThermalInputs,
        projected_floors: PerZone[Demand],
        now_mono_ms: int,
    ) -> CoordinatorResult:
        """raw baseline ``candidate`` に協調を掛けた結果を返す。

        ``projected_floors`` は合成が requested に掛ける zone ごとの下限の見込み（0078 §2.2。
        ``project_floors()`` で作る）。``coordinate()`` は風量を zone ごとに
        ``max(requested_z, projected_floors.z)`` の demand で見積もる。

        **例外を握りつぶさない。** ``coordinate()`` の例外も、出力の不変条件の破れ
        （``AirBalanceCoordinationError``）も、保持を解いてから呼び出し側へ投げる。
        ``failed`` として記録し、``mode: apply`` なら ``fallback_exception`` へ翻訳するのは
        呼び出し側である（0078 §2.6 / 決定記録 0085）。
        """
        try:
            return self._apply(
                candidate=candidate,
                thermal=thermal,
                projected_floors=projected_floors,
                now_mono_ms=now_mono_ms,
            )
        except Exception:
            self.release()
            raise

    def _apply(
        self,
        *,
        candidate: PerZone[Demand],
        thermal: ThermalInputs,
        projected_floors: PerZone[Demand],
        now_mono_ms: int,
    ) -> CoordinatorResult:
        if self._last_mono_ms is not None and now_mono_ms < self._last_mono_ms:
            raise AirBalanceCoordinationError("単調時計が戻った")
        self._last_mono_ms = now_mono_ms

        stable = self._fan_hardware.stable_demands(candidate)
        coordination = self._model.coordinate(stable, thermal, projected_floors=projected_floors)
        if coordination.candidate != stable:
            raise AirBalanceCoordinationError(
                "coordinate() が渡した demand と違う candidate を返した"
            )
        max_raise = self.max_raise()

        proposed: dict[Zone, float] = {}
        held_values: dict[Zone, float] = {}
        bounded: dict[Zone, bool] = {}
        held_flags: dict[Zone, bool] = {}
        for zone in Zone:
            c = candidate.get(zone)
            s = stable.get(zone)
            requested = coordination.requested.get(zone)
            if requested < s:
                # AirBalanceCoordination の検証が既に拒むはずの値。差し替えた model の不具合でも
                # 下げる値を通さない（0078 §2.4）。
                raise AirBalanceCoordinationError(f"coordinate() が {zone.value} を下げた")
            p = requested if requested > s else c
            limit = max_raise.get(zone)
            raise_now = max(0.0, min(p, c + limit) - c)
            hold = self._held_raise(zone, raise_now, now_mono_ms)
            # 幅は上限以下だが、浮動小数の足し引きで c + limit をわずかに
            # 超えないよう、上限でも切る。
            proposed[zone] = p
            held_values[zone] = min(1.0, c + hold, c + limit)
            bounded[zone] = p > c + limit
            held_flags[zone] = hold > raise_now

        counterfactual = PerZone[Demand](
            front=held_values[Zone.FRONT], rear=held_values[Zone.REAR], top=held_values[Zone.TOP]
        )
        apply_mode = self._settings.mode is AirBalanceCoordinationMode.APPLY
        output = counterfactual if apply_mode else candidate
        status: CoordinatorStatus
        if not apply_mode:
            status = "shadow"
        elif any(output.get(zone) > candidate.get(zone) for zone in Zone):
            status = "applied"
        else:
            status = "not_needed"
        mode: Literal[AirBalanceCoordinationMode.SHADOW, AirBalanceCoordinationMode.APPLY] = (
            AirBalanceCoordinationMode.APPLY if apply_mode else AirBalanceCoordinationMode.SHADOW
        )
        try:
            return CoordinatorResult(
                mode=mode,
                status=status,
                candidate=candidate,
                stable_candidate=stable,
                proposed=PerZone[Demand](
                    front=proposed[Zone.FRONT], rear=proposed[Zone.REAR], top=proposed[Zone.TOP]
                ),
                output=output,
                counterfactual_output=counterfactual,
                max_raise=max_raise,
                bounded_by_max_raise=PerZone[bool](
                    front=bounded[Zone.FRONT], rear=bounded[Zone.REAR], top=bounded[Zone.TOP]
                ),
                held=PerZone[bool](
                    front=held_flags[Zone.FRONT],
                    rear=held_flags[Zone.REAR],
                    top=held_flags[Zone.TOP],
                ),
                projected_floors=projected_floors,
                coordination=coordination,
            )
        except ValueError as error:
            raise AirBalanceCoordinationError(str(error)) from error

    def _held_raise(self, zone: Zone, raise_now: float, now_mono_ms: int) -> float:
        """直近 ``release_hold_ms`` の引き上げ幅の最大値（この tick を含む）。"""
        hold_ms = self._settings.release_hold_ms.value
        window = self._hold[zone]
        while window and now_mono_ms - window[0][0] >= hold_ms:
            window.popleft()
        held = max((value for _, value in window), default=0.0)
        if raise_now > 0.0 and hold_ms > 0:
            window.append((now_mono_ms, raise_now))
        return max(held, raise_now)
