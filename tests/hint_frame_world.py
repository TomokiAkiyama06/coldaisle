"""frame v4 のヒントの欄（#288 / 決定記録 0133 §2.5・0152 §2.6・0162）の試験で共有する値。

digest はすべて明らかな仮の値（AGENTS.md ルール10）。
"""

from __future__ import annotations

from coldaisle.control.learned_handoff import (
    LearnedFrame,
    LearnedHintCertification,
    LearnedHintComponents,
)
from coldaisle.control.schema import SupervisorOutput, SupervisorTargetBand, TemperatureTarget

HINT_CERTIFICATION = LearnedHintCertification(
    certificate_sha256="c" * 64,
    components=LearnedHintComponents(
        source_tree="1" * 64,
        lockfile="2" * 64,
        environment="3" * 64,
        hint_gate="4" * 64,
        optimizer="5" * 64,
        conditions="6" * 64,
        calibration="7" * 64,
    ),
    domain_lookback_ms=600_000,
    domain_stale_after_ms=10_000,
)
"""frame v4 の ``hint_certification`` の仮の値。"""


def lowered_band(base: SupervisorOutput, by_c: float = 5.0) -> SupervisorTargetBand:
    """基準の band を CPU / GPU とも ``by_c`` ℃ 下げた band（冷却を弱めない向き）。"""
    cpu, gpu = base.target_band.cpu_temperature, base.target_band.gpu_temperature
    return SupervisorTargetBand(
        cpu_temperature=TemperatureTarget(lower_c=cpu.lower_c - by_c, upper_c=cpu.upper_c - by_c),
        gpu_temperature=TemperatureTarget(lower_c=gpu.lower_c - by_c, upper_c=gpu.upper_c - by_c),
    )


def with_hint(frame: LearnedFrame) -> LearnedFrame:
    """基準の ``supervisor`` から band だけを下げたヒント有りの出力を入れた frame。"""
    base = frame.supervisor
    assert base is not None
    fields = {name: value for name, value in frame if name not in {"schema_version"}}
    fields["supervisor_hint"] = base.model_copy(update={"target_band": lowered_band(base)})
    fields["hint_certification"] = HINT_CERTIFICATION
    return LearnedFrame(**fields)
