"""テスト共通の下ごしらえ。"""

import hashlib
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from coldaisle.clock import SimulatedClock
from coldaisle.csv_export_manifest import EXPORT_FIELDS, ExportRecord, export_binding_sha256
from coldaisle.ingest import Scenario, load_scenarios
from coldaisle.store import QualityRules, SqliteStore
from coldaisle.store.calibration_history import CalibrationHistory
from coldaisle.store.export_binding import ExportBinding, read_production_records

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


# ------------------------------------------------------- export の束縛（決定記録 0100 §2.8）


def fixture_export_record() -> ExportRecord:
    """試験の専用 DB（T0 付近の小さな時刻）を覆う仮の export。値は明らかな仮の値だけ。"""
    return ExportRecord(
        export_id="export-" + "e" * 32,
        csv_name="sensors_1970-01-01.csv",
        csv_sha256="d" * 64,
        day="1970-01-01",
        timezone="UTC",
        day_start_ms=0,
        day_end_ms=86_400_000,
        timestamp_format="%Y-%m-%dT%H:%M:%S",
        row_count=0,
        row_seconds_sha256=hashlib.sha256(b"").hexdigest(),
    )


def fixture_replay_binding() -> tuple[str, str]:
    """仮の export から計算した ``(timezone, export_binding_sha256)``。専用 DB の bind に使う。"""
    record = fixture_export_record()
    return record.timezone, export_binding_sha256([record])


def add_fixture_exports(db_path: Path, records: tuple[ExportRecord, ...] | None = None) -> None:
    """本番の DB の代わりの DB の ``csv_exports`` に仮の export の行を足す（無ければ）。"""
    rows = (fixture_export_record(),) if records is None else records

    with SqliteStore(
        db_path, rules=QualityRules.from_yaml(QUALITY_RULES_PATH), clock=SimulatedClock(0)
    ) as store:
        for record in rows:
            values = record.model_dump()
            try:
                with store.transaction():
                    store.connection.execute(
                        f"INSERT INTO csv_exports ({', '.join(EXPORT_FIELDS)}, exported_ms) "
                        f"VALUES ({', '.join('?' for _ in EXPORT_FIELDS)}, 0)",
                        tuple(values[name] for name in EXPORT_FIELDS),
                    )
            except sqlite3.IntegrityError:
                pass  # 既にある（同じ export_id）


def fixture_export_binding(db_path: Path) -> tuple[CalibrationHistory, ExportBinding]:
    """``db_path`` に仮の export の行を足し、較正の記録と照合済みの export を読む。"""
    add_fixture_exports(db_path)
    return read_production_records(db_path, (fixture_export_record(),))
