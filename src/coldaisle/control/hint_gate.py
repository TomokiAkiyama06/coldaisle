"""Workload Hint の単調性ゲートの仕様を設定から導く関数と、設定から値を選ぶ部品の digest。

決定記録 0152 §2.3 / 0156。

- :func:`derive_hint_gate_spec`: ``fan-policy.yaml`` の ``supervisor.workload_hint``
  （``monotonicity_gate`` と ``context_overrides`` の鍵）と ``safety.yaml`` の ``workload_hint``
  だけを入力にした**1つの純粋な関数**。認証ランナー・``coldaisle-fand``・Learned MPC worker が
  自分の設定に当てる（0152 §2.3）。出力の digest（``HintGateSpec.sha256``）が部品 ``hint_gate``
- :func:`hint_policy_sha256`: 部品 ``hint_policy``（基準の context の全体と ``context_overrides``）
- :func:`optimizer_sha256`: 部品 ``optimizer``（``mpc.optimizer``・``mpc.budget_ms`` と、
  ``HardConstraintSet.build`` が読む ``safety.yaml`` の ``zone_min_demand`` / ``ramp_down_per_s``。
  0156 §2.5）

**制御の値を1つも作らない。** ``requested_demand`` にも Reactive Guard / Critical Safety にも
届かない（ルール 2・3）。``coldaisle.control.safety`` / ``reactive`` / ``fallback`` /
``hardware`` を import しない（0133 §2.7 / 0152 §2.8）。呼び出し（fand の照合・ランナー・
worker）は後の段で足す。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ValidationError

from coldaisle.control.config import (
    FanPolicyConfig,
    HintGateZoneDemandsConfig,
    SafetyConfig,
    SafetyWorkloadHintConfig,
    SupervisorConfig,
    WorkloadHintConfig,
)
from coldaisle.hint_certificate import (
    HintGateAxis,
    HintGateDemandAxis,
    HintGateDomain,
    HintGateKey,
    HintGateSpec,
    HintGateWindowFeature,
    HintGateZoneDemands,
    HintGateZoneRanges,
)
from coldaisle.hint_certificate_digest import json_component_sha256
from coldaisle.measurement import validate_metric

_HINT_POLICY_TAG = b"coldaisle-hint-policy-v1"
_OPTIMIZER_TAG = b"coldaisle-hint-optimizer-v1"


class HintGateRejection(StrEnum):
    """ゲートの仕様を導けない理由（0152 §2.4 の手順 4 の閉じた語彙）。"""

    EPS_BOUNDS_INVALID = "eps_bounds_invalid"
    HINT_GATE_INVALID = "hint_gate_invalid"


class HintGateSpecError(ValueError):
    """ゲートの仕様を導けない。``reason`` で B0 に留める理由を名指す（起動は止めない）。

    メッセージには、どの検査に落ちたかだけを書き、設定の値を載せない。
    """

    def __init__(self, reason: HintGateRejection, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _normalized(values: tuple[float, ...]) -> tuple[float, ...]:
    # -0.0 と 0.0 を同じ値として符号化する（canonical JSON は "-0.0" と書き分ける）。
    return tuple(value + 0.0 for value in values)


def _row(row: HintGateZoneDemandsConfig) -> HintGateZoneDemands:
    front, rear, top = _normalized((row.front, row.rear, row.top))
    return HintGateZoneDemands(front=front, rear=rear, top=top)


def _is_finite_non_negative(value: float) -> bool:
    # NaN は比較がすべて偽なので、この形で弾ける。inf は上限で弾く。
    return 0.0 <= value < float("inf")


def _check_eps_bounds(eps: float, eps_c: float, limits: SafetyWorkloadHintConfig | None) -> None:
    """0156 §2.6 の検査 1〜3。どれか1つでも外れれば ``eps_bounds_invalid``。"""
    if limits is None:
        raise HintGateSpecError(
            HintGateRejection.EPS_BOUNDS_INVALID, "safety.yaml の workload_hint が無い"
        )
    max_eps = limits.max_eps.value
    max_eps_c = limits.max_eps_c.value
    ceiling = limits.max_eps_c_ceiling.value
    if not all(_is_finite_non_negative(value) for value in (max_eps, max_eps_c, ceiling)):
        raise HintGateSpecError(
            HintGateRejection.EPS_BOUNDS_INVALID,
            "max_eps / max_eps_c / max_eps_c_ceiling は有限・非負にする",
        )
    if not max_eps < 1.0:
        raise HintGateSpecError(HintGateRejection.EPS_BOUNDS_INVALID, "max_eps は 1.0 未満にする")
    if not max_eps_c <= ceiling:
        raise HintGateSpecError(
            HintGateRejection.EPS_BOUNDS_INVALID, "max_eps_c は max_eps_c_ceiling 以下にする"
        )
    # 上限の検査が通ってから eps を比べる（0156 §2.6）。
    if not (_is_finite_non_negative(eps) and _is_finite_non_negative(eps_c)):
        raise HintGateSpecError(
            HintGateRejection.EPS_BOUNDS_INVALID, "eps / eps_c は有限・非負にする"
        )
    if not (eps < 1.0 and eps <= max_eps):
        raise HintGateSpecError(
            HintGateRejection.EPS_BOUNDS_INVALID, "eps は 1.0 未満かつ max_eps 以下にする"
        )
    if not eps_c <= max_eps_c:
        raise HintGateSpecError(
            HintGateRejection.EPS_BOUNDS_INVALID, "eps_c は max_eps_c 以下にする"
        )


def derive_hint_gate_spec(
    workload_hint: WorkloadHintConfig,
    limits: SafetyWorkloadHintConfig | None,
) -> HintGateSpec:
    """ゲートの仕様を設定から導く（0152 §2.3 / 0156 §2.2〜§2.7）。

    検査の順: ``monotonicity_gate`` の塊がある（無ければ ``hint_gate_invalid``。0156 §2.1）→
    上限と ``eps`` / ``eps_c``（``eps_bounds_invalid``。0156 §2.6）→ 格子（鍵・各軸の ``points``・
    代表集合・点の数。``hint_gate_invalid``。0156 §2.2〜§2.5）。最初に落ちたところで
    :class:`HintGateSpecError` を投げる。``stage`` は見ない（読むかどうかは呼び出し側が決める）。

    artifact が window に要求する metric の網羅（0156 §2.4）は、artifact を読むランナーと worker が
    確かめる。この関数は設定だけから作れる値に閉じる（fand は artifact を読まない。0077 §2.6）。
    """
    gate = workload_hint.monotonicity_gate
    if gate is None:
        raise HintGateSpecError(
            HintGateRejection.HINT_GATE_INVALID,
            "supervisor.workload_hint.monotonicity_gate が無い",
        )
    _check_eps_bounds(gate.eps.value, gate.eps_c.value, limits)
    keys = sorted(
        (item.regime.value, item.workload.value) for item in workload_hint.context_overrides or ()
    )
    if not keys:
        # 鍵が無ければ格子の点は 0（0156 §2.2。0 になる仕様は設定の誤り）。
        raise HintGateSpecError(
            HintGateRejection.HINT_GATE_INVALID, "context_overrides が空で格子の点が 0"
        )
    metrics = [feature.metric for feature in gate.window_features]
    for metric in metrics:
        try:
            validate_metric(metric)
        except ValueError:
            raise HintGateSpecError(
                HintGateRejection.HINT_GATE_INVALID, "window_features の metric 名の形が不正"
            ) from None
    if len(set(metrics)) != len(metrics):
        raise HintGateSpecError(
            HintGateRejection.HINT_GATE_INVALID, "window_features の metric を重複させない"
        )
    try:
        floors = tuple(_row(row) for row in gate.safety_floors)
        baselines = tuple(_row(row) for row in gate.baselines)
        domain = HintGateDomain(
            hint_keys=tuple(HintGateKey(regime=regime, workload=work) for regime, work in keys),
            previous_demand=HintGateDemandAxis.of(_normalized(gate.previous_demand.points)),
            temperature_cpu=HintGateAxis.of(_normalized(gate.temperature.cpu.points)),
            temperature_gpu=HintGateAxis.of(_normalized(gate.temperature.gpu.points)),
            window_features=tuple(
                _feature(feature.metric, feature.points)
                for feature in sorted(gate.window_features, key=lambda item: item.metric)
            ),
            safety_floors=floors,
            safety_floor_range=HintGateZoneRanges.bounding(floors),
            baselines=baselines,
            baseline_range=HintGateZoneRanges.bounding(baselines),
        )
        return HintGateSpec(
            spec_version="hint-gate-spec-v1",
            eps=gate.eps.value + 0.0,
            eps_c=gate.eps_c.value + 0.0,
            certified_domain=domain,
            grid_points=domain.grid_points(),
        )
    except (ValidationError, ValueError) as error:
        raise HintGateSpecError(
            HintGateRejection.HINT_GATE_INVALID, f"格子の定義が不正: {_where(error)}"
        ) from None


def _feature(metric: str, points: tuple[float, ...]) -> HintGateWindowFeature:
    axis = HintGateAxis.of(_normalized(points))
    return HintGateWindowFeature(
        metric=metric, points=axis.points, minimum=axis.minimum, maximum=axis.maximum
    )


def _where(error: Exception) -> str:
    # pydantic のメッセージは入力の値を含む。欄の位置と検査の文だけを返す。
    if isinstance(error, ValidationError):
        items = error.errors(include_input=False, include_url=False)
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '-'}: {item['msg']}"
            for item in items[:5]
        )
    return str(error)


def hint_policy_sha256(supervisor: SupervisorConfig) -> str:
    """部品 ``hint_policy``: 基準の context の全体と ``context_overrides``（0152 §2.3）。

    基準の context は ``rule_policy.contexts`` の全 Regime（``status`` を持たない値の全体）。
    ``context_overrides`` は鍵 (regime, workload) の昇順に並べる（書いた順は意味を持たない）。
    """
    overrides = sorted(
        (
            {
                "regime": item.regime.value,
                "workload": item.workload.value,
                "context": item.context.value,
            }
            for item in supervisor.workload_hint.context_overrides or ()
        ),
        key=lambda item: (item["regime"], item["workload"]),
    )
    return json_component_sha256(
        _HINT_POLICY_TAG,
        {
            "contexts": supervisor.rule_policy.contexts.model_dump(mode="json"),
            "context_overrides": overrides,
        },
    )


def optimizer_sha256(policy: FanPolicyConfig, safety: SafetyConfig) -> str:
    """部品 ``optimizer``: ``solve`` の結果に効く設定（0152 §2.3 / 0156 §2.5）。

    ``mpc.optimizer`` の全体（探索格子・``max_evaluations``・``zone_bounds``・``step_ms``・
    ``max_step_up`` / ``max_step_down``・目的関数の基準量と metric）・``mpc.budget_ms``・
    ``safety.yaml`` の ``zone_min_demand`` / ``ramp_down_per_s``（``HardConstraintSet.build`` の
    floor と下げ幅を決める）。``ConfigValue`` の ``status`` / ``basis`` も含めて束ねる
    （形を選び直さない。書き直せば再認証が要る向きで、外れても B0 に留まるだけ）。
    """
    return json_component_sha256(
        _OPTIMIZER_TAG,
        {
            "mpc_optimizer": policy.mpc.optimizer.model_dump(mode="json"),
            "mpc_budget_ms": policy.mpc.budget_ms,
            "safety_zone_min_demand": safety.zone_min_demand.model_dump(mode="json"),
            "safety_ramp_down_per_s": safety.ramp_down_per_s.model_dump(mode="json"),
        },
    )
