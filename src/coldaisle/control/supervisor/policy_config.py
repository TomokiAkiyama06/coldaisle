"""RL Supervisor の探索と shadow 比較の設定（`config/rl-policy.yaml`。#89 / 決定記録 0061 §2.5）。

**既定値をコードに置かない**（AGENTS.md ルール9）。実測前の値はすべて
`{value, status: provisional}` で持ち、設定が欠ければ読み込みで落とす。

**ほかの契約が持つ値をここへ写さない**（決定記録 0054 §2.6 と同じ規則）。

- action の許容範囲: `config/fan-policy.yaml` の `supervisor.output_bounds`
- episode の刻み・reward・coverage 下限: `config/rl-training.yaml`（#105 / 決定記録 0058 §2.7）
- 絶対温度上限・最低安全 demand: `config/safety.yaml`

**「反実仮想の裏づけを要求するか」は設定に出さない。** 設定で切れる安全条件は、設定次第で
破れる条件である（決定記録 0052 §4 (c) と同じ理由）。`SupervisorPolicyBinding.for_active`
がコードとして常に要求する。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from coldaisle.control.config import ConfigValue, FiniteFloat, UnitInterval
from coldaisle.control.schema import SupervisorObjectiveWeights, WorkloadRegime

RL_POLICY_CONFIG_VERSION: Literal[1] = 1
RL_POLICY_CONFIG_FILENAME = "rl-policy.yaml"

MAX_WEIGHT_CANDIDATES = 64
"""探索に並べられる weight 候補の構造上限。資源枯渇を防ぐ境界で、調整値ではない。"""

MAX_CANDIDATE_TABLES = 4_096
"""1回の探索で回せる候補表の構造上限。"""

POLICY_VERSION_MAX_LENGTH = 120
"""候補の policy version が収まらなければならない長さ。

`SupervisorOutput.version` / `EpisodeResult.policy_version` の上限と同じ値で、
候補の版は `<model_id>-<候補識別子>` になる。
"""

BASELINE_CANDIDATE_ID = "baseline"
"""Baseline の表を表す候補識別子。"""

CANDIDATE_INDEX_WIDTH = len(str(MAX_CANDIDATE_TABLES - 1))
"""候補識別子の番号の桁数。

1 regime に並ぶ候補は候補表の総数を超えないので、番号は `MAX_CANDIDATE_TABLES - 1` 以下に
収まる（総数は生成の前に上限で検査する）。
"""

MAX_CANDIDATE_ID_LENGTH = max(
    len(BASELINE_CANDIDATE_ID),
    max(len(regime.value) for regime in WorkloadRegime) + len("-a") + CANDIDATE_INDEX_WIDTH,
)
"""候補識別子の最大の長さ（`candidate_identifier()` が作る形から導く）。"""

MAX_POLICY_MODEL_ID_LENGTH = POLICY_VERSION_MAX_LENGTH - len("-") - MAX_CANDIDATE_ID_LENGTH
"""`artifact.model_id` の上限。**候補の版が上限を超えないよう、接尾辞の分を残す。**

超える model_id を受け取ると、探索の途中で候補の版が `SupervisorOutput` の検証に落ち、
学習が中断する。設定の読み込みで先に落とす。
"""


def candidate_identifier(regime: WorkloadRegime, index: int) -> str:
    """regime を1つだけ差し替えた候補の識別子。長さは `MAX_CANDIDATE_ID_LENGTH` 以下。"""
    if not 0 <= index < MAX_CANDIDATE_TABLES:
        raise ValueError(f"候補の番号が構造上限の外にある（index={index}）")
    return f"{regime.value}-a{index:0{CANDIDATE_INDEX_WIDTH}d}"


PolicyFloat = ConfigValue[FiniteFloat]
PolicyUnitInterval = ConfigValue[UnitInterval]
PolicyCount = ConfigValue[Annotated[int, Field(ge=0)]]
PolicySeed = ConfigValue[Annotated[int, Field(ge=0)]]


def _sequence_to_tuple(value: object) -> object:
    """YAML 配列を、読み込み後は不変な tuple として保持する。"""
    return tuple(value) if isinstance(value, list) else value


class _ConfigModel(BaseModel):
    """設定を欠損・余分な鍵・暗黙変換から守る共通基底。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class PolicyArtifactConfig(_ConfigModel):
    """出力する policy artifact の identity。**版は呼び出し側が明示する。**

    版を設定から取ると、同じ設定で回した2つの探索が同じ版を名乗れてしまう。
    """

    model_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=MAX_POLICY_MODEL_ID_LENGTH)
    """候補の版 `<model_id>-<候補識別子>` が `POLICY_VERSION_MAX_LENGTH` に収まる長さまで。"""


class PolicySearchConfig(_ConfigModel):
    """候補の並べ方と、Baseline を上回ったと見なす条件。"""

    family: Literal["per_regime_coordinate_v1"]
    """いまの唯一の探索 family。

    Baseline（Rule policy）の表を起点に、**regime を1つだけ候補 action へ差し替えた表**を
    すべて並べる。複雑な探索にしないのは、反実仮想モデルが無い間は候補間の差を
    そもそも測れないからである（決定記録 0058 §3）。
    """
    seed: PolicySeed
    """候補の生成・評価に使う種。**同じ種からは同じ並びが出る。**"""
    max_candidate_tables: Annotated[int, Field(ge=1, le=MAX_CANDIDATE_TABLES)]
    """並べてよい候補表の上限。

    **超えたら切り詰めず落とす。** 黙って切り詰めると、報告に出ない候補が生まれ、
    「全候補を比べた」と読めてしまう。
    """
    minimum_reward_improvement: PolicyFloat
    """Baseline より良いと見なすために要る、揃えた長さの reward 平均の差。

    0 でも「同点では勝たせない」規則は変わらない（比較は厳密な優越で判定する）。
    """
    weight_candidates: Annotated[
        tuple[SupervisorObjectiveWeights, ...],
        BeforeValidator(_sequence_to_tuple),
        Field(min_length=1, max_length=MAX_WEIGHT_CANDIDATES),
    ]
    """試す目的関数 weight。**strategy と target band は `output_bounds` から取る**（写さない）。

    weight は連続値なので候補を設定で明示する。格子の刻みを code に置くと、実測前の
    刻みが既定値として固定される（AGENTS.md ルール9）。
    """

    @model_validator(mode="after")
    def _candidates_are_unique(self) -> Self:
        serialized = [item.model_dump_json() for item in self.weight_candidates]
        if len(set(serialized)) != len(serialized):
            # 同じ候補を2度並べると、候補数だけが増えて探索の内容が変わらない。
            raise ValueError("search.weight_candidates を重複させない")
        return self


class PolicyShadowConfig(_ConfigModel):
    """shadow 比較を「読めた」と見なす下限（fail closed）。"""

    minimum_ticks: PolicyCount
    """対で観測できた tick 数の下限。"""
    minimum_paired_fraction: PolicyUnitInterval
    """観測した tick のうち、Rule と RL の両方の提案が揃っていた割合の下限。"""


class RlPolicyConfig(_ConfigModel):
    """RL Supervisor の探索・shadow 比較の設定一式。"""

    schema_version: Literal[1]
    artifact: PolicyArtifactConfig
    search: PolicySearchConfig
    shadow: PolicyShadowConfig

    @classmethod
    def from_file(cls, path: Path) -> tuple[RlPolicyConfig, str]:
        """設定を読み、検証済みの設定と**その bytes の** SHA-256 を返す。

        報告に残すのは hash だけで、絶対 path は残さない（決定記録 0021）。
        """
        text = path.read_text(encoding="utf-8")
        loaded: Any = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"RL policy の設定が辞書ではない: {path.name}")
        return cls.model_validate(loaded), sha256(text.encode("utf-8")).hexdigest()
