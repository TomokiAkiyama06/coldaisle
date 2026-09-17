"""テスト共通の下ごしらえ。"""

from collections.abc import Generator
from pathlib import Path

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.ingest import Scenario, load_scenarios
from coldaisle.store import QualityRules

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
QUALITY_RULES_PATH = CONFIG_DIR / "quality.yaml"
SCENARIOS_PATH = CONFIG_DIR / "scenarios.yaml"
CALIBRATION_PATH = CONFIG_DIR / "calibration.json"

TEST_EPOCH_MS = 1_787_616_000_000
"""2026-08-25T00:00:00Z。テストを実時計に依存させないための固定の起点。"""

_MARKER_SELECTION_SENTINEL = (
    "tests/test_ci_test_selection.py::test_unmarked_hardware_named_case_runs_without_a_device"
)


@pytest.hookimpl(wrapper=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> Generator[None]:
    """通常 CI で test 名ではなく ``hardware`` marker だけが除外されることを検査する。"""
    if config.getoption("markexpr") != "not hardware":
        yield
        return

    collected_nodeids = {item.nodeid for item in items}
    marked_nodeids = {
        item.nodeid for item in items if item.get_closest_marker("hardware") is not None
    }

    yield

    selected_nodeids = {item.nodeid for item in items}
    if (
        _MARKER_SELECTION_SENTINEL in collected_nodeids
        and _MARKER_SELECTION_SENTINEL not in selected_nodeids
    ):
        raise pytest.UsageError("unmarked test was removed because its name contains hardware")
    unexpectedly_selected = sorted(marked_nodeids & selected_nodeids)
    if unexpectedly_selected:
        raise pytest.UsageError(
            f"hardware-marked tests were selected by the normal CI suite: {unexpectedly_selected}"
        )


@pytest.fixture(scope="session")
def rules() -> QualityRules:
    """**本番と同じ設定ファイル**を読む。

    テスト専用のしきい値を置くと、設定ファイル側が壊れていても緑になる。
    値そのものを変えたいテストは `rules.model_copy(update=...)` を使う。
    """
    return QualityRules.from_yaml(QUALITY_RULES_PATH)


@pytest.fixture(scope="session")
def scenarios() -> dict[str, Scenario]:
    """**本番と同じシナリオ定義**を読む（#6 の受入基準「テストから再現可能」）。"""
    return load_scenarios(SCENARIOS_PATH)


@pytest.fixture
def clock() -> SimulatedClock:
    """固定の起点から進む時計。

    実時計を使うと、`stale` 判定や継続時間の検証が実行速度に左右される。
    本番の `mock` / `replay` も同じ種類の時計で動く（#42）。
    """
    return SimulatedClock(TEST_EPOCH_MS)
