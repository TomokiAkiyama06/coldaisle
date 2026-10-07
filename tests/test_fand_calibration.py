"""`coldaisle-fand` が起動時に読む runtime の較正（決定記録 0079 §2.4 / 0096 §5 #8）。

- 較正ファイルは**起動時に1回だけ**読み、artifact v2 の L9 に渡す `RuntimeCalibration` にする
- 読めない・壊れている・path が無いときは**起動を止めず** `unavailable`（理由付き）にし、
  構造化ログに残す
- `unavailable` では較正の掛かる metric を使う artifact が L9 で拒まれ、Learned MPC は
  Fallback になる
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from coldaisle.control.fallback import LearnedFailure
from coldaisle.control_daemon import build, build_parser, read_runtime_calibration
from test_fand_registry import daemon_config
from test_learned_mpc import (
    CALIBRATION_OFFSETS,
    MpcArtifact,
    load_runtime,
    make_artifact,
    propose,
)


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> MpcArtifact:
    """較正の掛かる metric（`air.front_intake`）を使う反実仮想 artifact v2。"""
    return make_artifact(tmp_path_factory.mktemp("pr86-fand-calibration") / "registry")


def write_calibration(path: Path, offsets: dict[str, float], **extra: Any) -> Path:
    """`coldaisle-calibrate` と同じ形の較正ファイル。"""
    document = {"note": "試験", "calibrated_at": None, "offsets_c": offsets} | extra
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def reasons(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [getattr(record, "fields", {}).get("reason") for record in caplog.records]


def test_a_readable_calibration_becomes_the_runtime_values(tmp_path: Path) -> None:
    """読めた較正の `offsets_c` だけが L9 の入力になる（`note` などは効かない。0096 §2.1）。"""
    path = write_calibration(
        tmp_path / "calibration.json",
        CALIBRATION_OFFSETS,
        calibrated_at="2026-10-07T10:00:00+09:00",
        reference="mean_of_all",
    )

    calibration = read_runtime_calibration(path)

    assert calibration.is_available
    assert dict(calibration.offsets_c or {}) == CALIBRATION_OFFSETS


def test_no_path_means_unavailable_not_an_empty_calibration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**既定の path を制御側に置かない**（0079 §2.4）。空の値で「読めなかった」を表さない。"""
    caplog.set_level(logging.INFO)

    calibration = read_runtime_calibration(None)

    assert not calibration.is_available
    assert calibration.offsets_c is None
    assert "calibration_path_not_given" in reasons(caplog)


@pytest.mark.parametrize(
    "content",
    [None, "{ not json", '["offsets"]', '{"offsets_c": {"front_intake": 1e309}}', '{"x": 1}'],
    ids=["missing", "broken-json", "not-a-dict", "non-finite", "unknown-field"],
)
def test_an_unreadable_calibration_is_unavailable_and_logged(
    tmp_path: Path, content: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """**読めなくても例外で起動を止めない**（0096 §5 #8）。理由を構造化ログに残す。"""
    path = tmp_path / "calibration.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    calibration = read_runtime_calibration(path)

    assert not calibration.is_available
    assert calibration.unavailable_reason
    assert "calibration_unreadable" in reasons(caplog)


def test_fand_reads_the_calibration_once_at_startup(tmp_path: Path) -> None:
    """fand は起動時に読み、走行中は読み直さない（較正を変えたら fand を先に再起動する）。"""
    path = write_calibration(tmp_path / "calibration.json", CALIBRATION_OFFSETS)
    daemon = build(daemon_config(tmp_path, calibration=path))
    try:
        assert daemon.runtime_calibration is not None
        assert daemon.runtime_calibration.is_available
        write_calibration(path, {"front_intake": 9.9})
        assert daemon.run(max_ticks=2).ticks == 2
        assert dict(daemon.runtime_calibration.offsets_c or {}) == CALIBRATION_OFFSETS
    finally:
        daemon.close()


@pytest.mark.parametrize("given", [False, True], ids=["not-given", "broken"])
def test_fand_starts_without_a_usable_calibration(tmp_path: Path, given: bool) -> None:
    """**較正を読めなくても Fan の制御は始まる**（Learned だけが使えなくなる。0096 §5 #8）。"""
    broken = tmp_path / "calibration.json"
    broken.write_text("{ broken", encoding="utf-8")
    daemon = build(daemon_config(tmp_path, calibration=broken if given else None))
    try:
        assert daemon.runtime_calibration is not None
        assert not daemon.runtime_calibration.is_available
        assert daemon.run(max_ticks=2).ticks == 2
    finally:
        daemon.close()


def test_the_cli_takes_the_calibration_and_has_no_default() -> None:
    parser = build_parser()

    assert parser.parse_args([]).calibration is None
    assert parser.parse_args(["--calibration", "config/calibration.json"]).calibration == Path(
        "config/calibration.json"
    )


def test_the_value_fand_read_decides_l9(trained: MpcArtifact, tmp_path: Path) -> None:
    """fand が読んだ値を loader へ渡すと、同じ較正なら通り、読めなければ L9 で拒まれる。"""
    good = read_runtime_calibration(write_calibration(tmp_path / "good.json", CALIBRATION_OFFSETS))
    changed = read_runtime_calibration(
        write_calibration(tmp_path / "changed.json", {**CALIBRATION_OFFSETS, "front_intake": 0.5})
    )
    missing = read_runtime_calibration(tmp_path / "absent.json")

    assert load_runtime(trained.verified, calibration=good).controller is not None
    for calibration in (changed, missing):
        runtime = load_runtime(trained.verified, calibration=calibration)
        assert runtime.controller is None
        result = propose(runtime)
        assert result.failure is LearnedFailure.MODEL_LOAD_FAILURE
        assert result.failure_reason is not None and "L9" in result.failure_reason.detail
