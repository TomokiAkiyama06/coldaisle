"""起動時に1回だけ読む較正（``coldaisle-fand`` と認証ランナーが共有する。決定記録 0152 §2.1）。

``coldaisle-fand`` は frame v3 の ``calibration``（0101 §2.2）を、
認証ランナー（``coldaisle-hint-certify``）は L9 に渡す ``RuntimeCalibration`` と証明書の
部品 ``calibration`` を、
**同じ読み取りの関数**から作る。読み方が2か所に分かれると、同じファイルから fand とランナーが
別の較正を見て、照合が合わない（B0）か、実行時と別の経路を認証することになる（0152 §2.1）。

既定の path を置かない（0101 §2.1 / 0096 §5 #8）。読むのは ``offsets_c`` だけ。
"""

from __future__ import annotations

import logging
from pathlib import Path

from coldaisle import logs
from coldaisle.control.learned_handoff import (
    AvailableCalibration,
    CalibrationUnavailableCode,
    UnavailableCalibration,
)
from coldaisle.control.model.calibration_digest import RuntimeCalibration
from coldaisle.ingest.calibration import Calibration

LOGGER = logging.getLogger("coldaisle.startup_calibration")


def read_runtime_calibration(path: Path | None) -> RuntimeCalibration:
    """反実仮想 artifact v2 の L9 に渡す runtime の較正を読む（0079 §2.4 / 0096 §5 #8）。"""
    return read_startup_calibration(path)[0]


def read_startup_calibration(
    path: Path | None,
) -> tuple[RuntimeCalibration, AvailableCalibration | UnavailableCalibration]:
    """起動時に1回だけ較正を読み、`RuntimeCalibration` と frame v3 の `calibration` を返す。

    frame の側（決定記録 0101 §2.2）は同じ読み込みの結果を写すだけで、`unavailable` の理由は
    閉じた code（`calibration_path_not_given` / `calibration_unreadable`）だけにする。例外の文字列と
    path は構造化ログにだけ残す（AGENTS.md ルール10）。

    **例外で起動を止めない。** 読めない・壊れている・path が無いときは
    `RuntimeCalibration.unavailable`（理由付き）を返し、構造化ログに残す。空の値で
    「読めなかった」を表さない（全チャネル 0.0 と同じ digest になるため。0096 §2.6）。
    読むのは `offsets_c` だけで、`note` / `calibrated_at` などは L9 に効かない（0096 §2.1）。
    """
    if path is None:
        reason = "較正ファイルの path が指定されていない（--calibration）"
        LOGGER.warning(
            "較正ファイルを読まないため、較正の掛かる metric を使う Thermal Model は使わない",
            extra={logs.FIELDS_KEY: {"reason": "calibration_path_not_given"}},
        )
        return RuntimeCalibration.unavailable(reason), UnavailableCalibration(
            reason=CalibrationUnavailableCode.PATH_NOT_GIVEN
        )
    try:
        loaded = Calibration.from_json(path)
        runtime = RuntimeCalibration.available(loaded.offsets_c)
    except Exception as error:
        # 何が起きても「読めなかった」として扱う（L9 が較正の掛かる artifact を拒む）。
        reason = f"{type(error).__name__}: {error}"[:500]
        LOGGER.error(
            "較正ファイルを読めないため、較正の掛かる metric を使う Thermal Model は使わない",
            extra={
                logs.FIELDS_KEY: {
                    "reason": "calibration_unreadable",
                    "calibration": str(path),
                    "error": reason,
                }
            },
        )
        return RuntimeCalibration.unavailable(reason), UnavailableCalibration(
            reason=CalibrationUnavailableCode.UNREADABLE
        )
    LOGGER.info(
        "較正ファイルを読み込んだ（再起動まで読み直さない）",
        extra={
            logs.FIELDS_KEY: {
                "calibration": str(path),
                "channels": sorted(loaded.offsets_c),
                "calibrated_at": loaded.calibrated_at,
            }
        },
    )
    # frame へは同じ値を写す（fand は artifact の `metric_binding` も digest も計算しない。
    # 0101 §2.1）
    return runtime, AvailableCalibration(offsets_c=dict(runtime.offsets_c or {}))
