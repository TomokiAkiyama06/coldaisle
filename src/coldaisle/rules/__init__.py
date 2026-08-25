"""L2 ルールエンジン（決定論的。AI非依存）。

**AI は一切関与しない。** 同じ入力からは必ず同じ判定が出る。
閾値と継続時間は `config/rules.yaml`（AGENTS.md ルール6）。
"""

from coldaisle.rules.engine import Engine, Transition
from coldaisle.rules.models import RuleSet

__all__ = ["Engine", "RuleSet", "Transition"]
