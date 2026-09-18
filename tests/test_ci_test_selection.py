"""CI の実機テスト選択を pytest marker に固定する。#127"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MARKER_EXPRESSION = '-m "not hardware"'
KEYWORD_EXPRESSION = '-k "not hardware"'


@pytest.mark.parametrize(
    "relative_path",
    (
        ".github/workflows/ci.yml",
        "AGENTS.md",
        "pyproject.toml",
        "prompts/claude-code.md",
        "issues/02-ci-pipeline.md",
        "tests/test_package.py",
    ),
    ids=("workflow", "agents", "pytest-config", "agent-prompt", "ci-issue", "test-doc"),
)
def test_ci_selection_guidance_uses_the_registered_marker(relative_path: str) -> None:
    """CI と案内が test 名を検索する ``-k`` へ戻らないようにする。"""
    text = (ROOT / relative_path).read_text(encoding="utf-8")

    assert MARKER_EXPRESSION in text
    assert KEYWORD_EXPRESSION not in text


def test_unmarked_hardware_named_case_runs_without_a_device(
    request: pytest.FixtureRequest,
) -> None:
    """名前に hardware があっても marker が無ければ通常 CI の対象になる。"""
    assert request.node.get_closest_marker("hardware") is None
