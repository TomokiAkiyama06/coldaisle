"""Shadow で観測した Rule / RL の提案を突き合わせる台帳（#89 / 決定記録 0061 §2.6）。

**Shadow は MPC にも Fan にも届かない**（決定記録 0053 §2.3）。ここで作るのは
「同じ tick で Rule と RL がどれだけ違う戦略を出したか」の集計だけで、制御へ戻る経路は無い。

この台帳が守る規則は4つある。どれも、過去の PR で実際に見つかった壊れ方に対応する。

1. **同じ tick を2度渡しても数が増えない。** 同じ内容なら畳み、食い違えば受け取らない
   （決定記録 0055 §2.1 と同じ契約）
2. **片方しか無い tick を一致として数えない。** RL の提案が無い tick は理由別に数え、
   対になった tick だけを比較の母数にする
3. **版を名指しする。** Rule / RL の policy version を束縛し、違う版の提案を同じ表に混ぜない
4. **時刻は観測した decision から取る。** 壁時計を読まない（決定記録 0054 §2.7）

下限に満たない集計は `usable=False` になり、昇格の証拠として読めない（fail closed）。
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, model_validator

from coldaisle.control.model.thermal import canonical_sha256
from coldaisle.control.schema import (
    SupervisorDecision,
    SupervisorObjectiveWeights,
    SupervisorOutput,
    SupervisorPolicyIdentity,
    SupervisorPolicyKind,
    WorkloadRegime,
)
from coldaisle.control.supervisor.policy_config import PolicyShadowConfig

SUPERVISOR_SHADOW_SCHEMA_VERSION: Literal[1] = 1

OBJECTIVE_NAMES: tuple[str, ...] = (
    "acoustic",
    "balance",
    "change",
    "cpu_temperature",
    "gpu_temperature",
)
"""比較する weight の名前。**並びを固定する**（同じ入力から同じ bytes を出すため）。"""

MAX_SHADOW_TICKS = 1_000_000
"""1つの台帳が受け取れる tick 数の構造上限。資源枯渇を防ぐ境界で、調整値ではない。"""


def shadow_summary_usable(
    *,
    observed_ticks: int,
    paired_ticks: int,
    minimum_ticks: int,
    minimum_paired_fraction: float,
) -> bool:
    """集計を shadow の証拠として読めるか。**台帳と集計の型が同じこの関数を使う。**

    対が1つも無い集計は、下限の値によらず読めない（fail closed）。別々に書くと、
    台帳が立てない `usable` を、保存した集計を読み戻す側が受け取れてしまう。
    """
    if observed_ticks == 0 or paired_ticks == 0:
        return False
    return (
        paired_ticks >= minimum_ticks and paired_ticks / observed_ticks >= minimum_paired_fraction
    )


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class SupervisorShadowConflictError(ValueError):
    """同じ tick に食い違う decision が届いた。**どちらかを選ばず受け取らない。**"""


class SupervisorShadowUsageError(ValueError):
    """台帳の使い方が間違っている（shadow slot の無い decision を渡した、など）。"""


class RegimeAgreement(_Frozen):
    """1 regime での一致の内訳。"""

    regime: WorkloadRegime
    paired_ticks: int = Field(ge=0)
    strategy_matches: int = Field(ge=0)
    target_band_matches: int = Field(ge=0)

    @model_validator(mode="after")
    def _matches_cannot_exceed_pairs(self) -> Self:
        if (
            self.strategy_matches > self.paired_ticks
            or self.target_band_matches > self.paired_ticks
        ):
            raise ValueError("一致数が対になった tick 数を超えている")
        return self


class SupervisorShadowSummary(_Frozen):
    """Shadow 比較の集計。**Registry の `shadow_evaluation_ref` に使える。**"""

    schema_version: Literal[1] = SUPERVISOR_SHADOW_SCHEMA_VERSION
    rule_policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=120)
    rl_policy_identity: SupervisorPolicyIdentity
    """比べた RL artifact の**完全な識別**（model ID・版・bytes hash）。

    版だけでは同じ版を名乗る別の artifact と区別できず、その集計を別の artifact の
    証拠として読めてしまう。
    """
    observed_ticks: int = Field(ge=0)
    paired_ticks: int = Field(ge=0)
    rule_unavailable_ticks: int = Field(ge=0)
    """Rule だけが無かった tick 数（RL の提案はあった）。"""
    rl_unavailable_ticks: int = Field(ge=0)
    """RL だけが無かった tick 数（Rule の提案はあった）。"""
    both_unavailable_ticks: int = Field(default=0, ge=0)
    """Rule と RL の**両方**が無かった tick 数。

    Rule 側の欠落へ畳むと、同じ tick の RL の欠落が内訳から消え、Rule も落ちていた間の
    RL worker の停止が見えなくなる。どちらの事実も残すため別の欄にする。
    """
    rl_errors: dict[str, NonNegativeInt] = Field(default_factory=dict)
    """RL の提案が無かった理由の内訳（`Reason.code` ごと）。

    `rl_unavailable_ticks` と `both_unavailable_ticks` の**両方**を数える。
    """
    first_ts_ms: int | None = Field(default=None, ge=0)
    last_ts_ms: int | None = Field(default=None, ge=0)
    strategy_matches: int = Field(ge=0)
    target_band_matches: int = Field(ge=0)
    weight_mean_abs_delta: dict[str, float] = Field(default_factory=dict)
    """対になった tick での、目的関数 weight ごとの差の絶対値の平均。

    **`paired_ticks` と一緒に読む。** 対が1つも無ければ 0.0 になるが、それは
    「差が無かった」ではなく**「比べていない」**である。`usable` が同じことを明示する。
    """
    weight_max_abs_delta: dict[str, float] = Field(default_factory=dict)
    """同じく、差の絶対値の最大。対が無ければ 0.0。"""
    regimes: tuple[RegimeAgreement, ...] = ()
    minimum_ticks: int = Field(ge=1)
    """対で観測できた tick 数の下限。**1 以上**（`PolicyShadowConfig.minimum_ticks` と同じ）。"""
    minimum_paired_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    usable: bool
    """下限を満たしたか。**満たさない集計を「差が無かった」と読まない**（fail closed）。

    `shadow_summary_usable()` から導く値で、食い違う集計は作れない（読み戻しでも同じ）。
    """

    @model_validator(mode="after")
    def _counts_add_up(self) -> Self:
        accounted = (
            self.paired_ticks
            + self.rule_unavailable_ticks
            + self.rl_unavailable_ticks
            + self.both_unavailable_ticks
        )
        if accounted != self.observed_ticks:
            raise ValueError("対と欠落の合計が観測 tick 数と一致しない")
        if sum(self.rl_errors.values()) != self.rl_unavailable_ticks + self.both_unavailable_ticks:
            raise ValueError("RL 欠落の理由の内訳が RL の欠落 tick 数と一致しない")
        if (
            self.strategy_matches > self.paired_ticks
            or self.target_band_matches > self.paired_ticks
        ):
            raise ValueError("一致数が対になった tick 数を超えている")
        if sum(agreement.paired_ticks for agreement in self.regimes) != self.paired_ticks:
            raise ValueError("regime ごとの対の合計が全体と一致しない")
        derived = shadow_summary_usable(
            observed_ticks=self.observed_ticks,
            paired_ticks=self.paired_ticks,
            minimum_ticks=self.minimum_ticks,
            minimum_paired_fraction=self.minimum_paired_fraction,
        )
        if self.usable != derived:
            # **導いた値と違う `usable` を受け取らない。** 保存した集計や手で作った集計が、
            # 台帳なら立てない `usable=True` を名乗れないようにする。
            raise ValueError("usable が観測数・対の数・下限から導いた値と一致しない")
        if (self.first_ts_ms is None) != (self.last_ts_ms is None):
            raise ValueError("観測区間の両端はそろえて持つ")
        if (
            self.first_ts_ms is not None
            and self.last_ts_ms is not None
            and self.first_ts_ms > self.last_ts_ms
        ):
            raise ValueError("観測区間の開始を終了より後にしない")
        if self.observed_ticks == 0 and self.first_ts_ms is not None:
            raise ValueError("観測していない集計に時刻を書かない")
        return self

    @property
    def paired_fraction(self) -> float | None:
        """観測した tick のうち対になった割合。1つも観測していなければ `None`。"""
        if self.observed_ticks == 0:
            return None
        return self.paired_ticks / self.observed_ticks

    @property
    def strategy_agreement_fraction(self) -> float | None:
        """戦略が一致した割合。対が無ければ `None`（**0 とは区別する**）。"""
        if self.paired_ticks == 0:
            return None
        return self.strategy_matches / self.paired_ticks

    def digest(self) -> str:
        """この集計そのものを表す SHA-256。"""
        return canonical_sha256(self)

    def evaluation_ref(self) -> str:
        """#104 の `shadow_evaluation_ref` へ渡す、path を含まない参照。

        **`usable` でない集計には参照を出さない**（fail closed）。#104 の `promote()` は参照が
        空でないことしか見ないので、下限に満たない集計の参照を渡せば、足りない shadow の
        証拠が昇格の根拠として記録されてしまう。
        """
        if not self.usable:
            raise SupervisorShadowUsageError(
                "下限を満たさない shadow 集計は昇格の証拠にしない"
                "（shadow_evaluation_ref を出さない）"
            )
        return f"supervisor-shadow:{self.digest()}"


class SupervisorShadowLedger:
    """同じ tick の Rule / RL 提案を集計する。**制御へ戻る経路を持たない。**"""

    __slots__ = (
        "_both_unavailable",
        "_config",
        "_first_ts_ms",
        "_last_ts_ms",
        "_regimes",
        "_rl_errors",
        "_rl_identity",
        "_rl_unavailable",
        "_rule_unavailable",
        "_rule_version",
        "_seen",
        "_strategy_matches",
        "_target_band_matches",
        "_weight_max",
        "_weight_total",
    )

    def __init__(
        self,
        config: PolicyShadowConfig,
        *,
        rule_policy_version: str,
        rl_identity: SupervisorPolicyIdentity,
    ) -> None:
        """Rule の版と RL artifact の完全な識別を束縛する。**あとから混ぜられない。**

        Rule と RL が同じ版文字列（例 `1.0.0`）を名乗ってもよい。版は policy kind ごとに
        別々に照合し、RL 側はさらに model ID・bytes hash まで束縛するので、取り違えない。
        """
        if not rule_policy_version:
            raise SupervisorShadowUsageError("比較する Rule policy version を名指しする")
        self._config = config
        self._rule_version = rule_policy_version
        self._rl_identity = rl_identity
        self._seen: dict[int, str] = {}
        self._rule_unavailable = 0
        self._rl_unavailable = 0
        self._both_unavailable = 0
        self._strategy_matches = 0
        self._target_band_matches = 0
        self._rl_errors: dict[str, int] = {}
        self._regimes: dict[WorkloadRegime, list[int]] = {}
        self._weight_total: dict[str, float] = dict.fromkeys(OBJECTIVE_NAMES, 0.0)
        self._weight_max: dict[str, float] = dict.fromkeys(OBJECTIVE_NAMES, 0.0)
        self._first_ts_ms: int | None = None
        self._last_ts_ms: int | None = None

    def observe(self, decision: SupervisorDecision) -> None:
        """1 tick の decision を取り込む。**同じ内容の重複は畳み、食い違いは拒む。**"""
        shadow = decision.shadow
        if shadow is None:
            # shadow slot の無い tick を「観測した」と数えると、対になる見込みの無い tick で
            # 母数だけが増え、coverage の下限が意味を失う。
            raise SupervisorShadowUsageError("shadow slot の無い decision を shadow 比較に渡さない")
        if decision.active.policy is not SupervisorPolicyKind.RULE:
            raise SupervisorShadowUsageError("Rule が active の tick だけを RL と比べる")
        if shadow.policy is not SupervisorPolicyKind.RL:
            raise SupervisorShadowUsageError("shadow slot に RLPolicy 以外を渡さない")
        self._check_versions(decision.active.output, shadow.output)
        if shadow.output is not None and shadow.policy_identity != self._rl_identity:
            # **版だけでなく完全な識別を照合する。** 同じ版を名乗る別の model ID・別の bytes の
            # 提案を、束縛した artifact の証拠として数えない。identity の無い提案も拒む。
            raise SupervisorShadowUsageError(
                "RL 提案の artifact 識別が台帳の束縛と一致しない（model ID・版・bytes hash）"
            )

        digest = canonical_sha256(decision)
        previous = self._seen.get(decision.tick_id)
        if previous is not None:
            if previous != digest:
                # どちらを採っても片方の事実が消える。選ぶのは照合器の仕事ではない（0055 §2.1）。
                raise SupervisorShadowConflictError(
                    "同じ tick に食い違う Supervisor decision が届いた"
                    f"（tick_id={decision.tick_id}）"
                )
            # **冪等な取り込みで数が増えないようにする。** 同じ行が2度届くのは入力の誤りではない。
            return
        if len(self._seen) >= MAX_SHADOW_TICKS:
            raise SupervisorShadowUsageError("shadow 台帳の tick 数が構造上限を超えている")
        self._seen[decision.tick_id] = digest
        self._first_ts_ms = (
            decision.ts_ms if self._first_ts_ms is None else min(self._first_ts_ms, decision.ts_ms)
        )
        self._last_ts_ms = (
            decision.ts_ms if self._last_ts_ms is None else max(self._last_ts_ms, decision.ts_ms)
        )

        rule_output = decision.active.output
        rl_output = shadow.output
        if rl_output is None:
            # Rule も落ちている tick でも RL の欠落理由を数える。Rule 側へ畳むと
            # RL worker の停止が内訳から消える。
            code = shadow.error.code if shadow.error is not None else "supervisor_unavailable"
            self._rl_errors[code] = self._rl_errors.get(code, 0) + 1
            if rule_output is None:
                self._both_unavailable += 1
            else:
                self._rl_unavailable += 1
            return
        if rule_output is None:
            self._rule_unavailable += 1
            return
        # 違う regime を前提にした提案どうしは、`SupervisorDecision` の段階で作れない
        # （同じ tick の active / shadow は同じ regime を使う）。ここで読み替えもしない。
        self._record_pair(rule_output, rl_output)

    def _check_versions(
        self, rule_output: SupervisorOutput | None, rl_output: SupervisorOutput | None
    ) -> None:
        """**版を名指しする。** 違う版の提案を同じ集計に混ぜない。"""
        if rule_output is not None and rule_output.version != self._rule_version:
            raise SupervisorShadowUsageError(
                f"Rule policy の版が台帳と違う（expected={self._rule_version};"
                f" actual={rule_output.version}）"
            )
        if rl_output is not None and rl_output.version != self._rl_identity.version:
            raise SupervisorShadowUsageError(
                f"RL policy の版が台帳と違う（expected={self._rl_identity.version};"
                f" actual={rl_output.version}）"
            )

    def _record_pair(self, rule_output: SupervisorOutput, rl_output: SupervisorOutput) -> None:
        regime = rule_output.regime
        counters = self._regimes.setdefault(regime, [0, 0, 0])
        counters[0] += 1
        if rule_output.strategy == rl_output.strategy:
            self._strategy_matches += 1
            counters[1] += 1
        if rule_output.target_band == rl_output.target_band:
            self._target_band_matches += 1
            counters[2] += 1
        for name in OBJECTIVE_NAMES:
            delta = abs(_weight(rl_output.weights, name) - _weight(rule_output.weights, name))
            self._weight_total[name] += delta
            self._weight_max[name] = max(self._weight_max[name], delta)

    def summary(self) -> SupervisorShadowSummary:
        """いままでの観測から集計を作る。**同じ観測列からは同じ bytes が出る。**"""
        observed = len(self._seen)
        paired = observed - (self._rule_unavailable + self._rl_unavailable + self._both_unavailable)
        mean = {
            name: (self._weight_total[name] / paired if paired else 0.0) for name in OBJECTIVE_NAMES
        }
        regimes = tuple(
            RegimeAgreement(
                regime=regime,
                paired_ticks=counters[0],
                strategy_matches=counters[1],
                target_band_matches=counters[2],
            )
            for regime, counters in sorted(self._regimes.items(), key=lambda item: item[0].value)
        )
        # 集計の型と同じ関数で導く（対が1つも無い集計は下限によらず読めない）。
        usable = shadow_summary_usable(
            observed_ticks=observed,
            paired_ticks=paired,
            minimum_ticks=self._config.minimum_ticks.value,
            minimum_paired_fraction=self._config.minimum_paired_fraction.value,
        )
        return SupervisorShadowSummary(
            rule_policy_version=self._rule_version,
            rl_policy_identity=self._rl_identity,
            observed_ticks=observed,
            paired_ticks=paired,
            rule_unavailable_ticks=self._rule_unavailable,
            rl_unavailable_ticks=self._rl_unavailable,
            both_unavailable_ticks=self._both_unavailable,
            rl_errors=dict(sorted(self._rl_errors.items())),
            first_ts_ms=self._first_ts_ms,
            last_ts_ms=self._last_ts_ms,
            strategy_matches=self._strategy_matches,
            target_band_matches=self._target_band_matches,
            weight_mean_abs_delta=mean,
            weight_max_abs_delta=dict(self._weight_max),
            regimes=regimes,
            minimum_ticks=self._config.minimum_ticks.value,
            minimum_paired_fraction=self._config.minimum_paired_fraction.value,
            usable=usable,
        )


def _weight(weights: SupervisorObjectiveWeights, name: str) -> float:
    """固定した並びの weight を1つ読む。"""
    value = getattr(weights, name)
    assert isinstance(value, float)
    return value
