"""較正オフセットの算出（#13 / FR-107）。

**求めるのはセンサー間の相対精度**であって絶対精度ではない（spec-review W-02）。
DS18B20 も AM2320 も ±0.5℃ で、ΔT には最悪 ±1.0℃ の系統誤差が乗る。
再循環の判定は数℃の話なので無視できない。

守る線は3つ。

1. **較正後のばらつきが ±0.15℃ 以内**（Issue の受入基準）
2. **同じ空気に置かれていないなら較正しない**（本物の勾配を焼き付けない）
3. **黙って書き換えない**（以降のすべての測定の解釈が変わる）
"""

import json
import math
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coldaisle.calibrate import (
    ChannelMean,
    as_calibration,
    check,
    compute,
    main,
    measure,
    write,
)
from coldaisle.channels import METRIC_TO_CHANNEL
from coldaisle.clock import SimulatedClock
from coldaisle.ingest.calibration import Calibration, CalibrationPolicy
from coldaisle.metrics import MetricCatalog
from coldaisle.store import Quality, Reading, Sample, SqliteStore
from conftest import CONFIG_DIR

NOW_MS = 1_787_616_000_000
INTERVAL_MS = 2_500
DAY_MS = 86_400_000

BIAS = {
    "air.room": 0.00,
    "air.front_intake": -0.19,
    "air.gpu_intake": 0.06,
    "air.gpu_exhaust": 0.31,
    "air.top_exhaust": -0.12,
    "air.rear_exhaust": -0.06,
}
"""各センサーの個体差。**較正はこれを打ち消せなければならない。**"""


@pytest.fixture
def policy() -> CalibrationPolicy:
    """**本番と同じ `config/calibration.yaml`** を読む。"""
    return CalibrationPolicy.from_yaml(CONFIG_DIR / "calibration.yaml")


@pytest.fixture
def catalog() -> MetricCatalog:
    return MetricCatalog.from_yaml(CONFIG_DIR / "metrics.yaml")


def fill(store, *, bias=BIAS, true_c: float = 24.0, count: int = 240, seed: int = 7, end_ms=NOW_MS):
    """同じ空気に置いた状態を模す。**個体差 + わずかな雑音。**"""
    rng = random.Random(seed)
    store.insert_samples(
        [
            Sample(
                ts_ms=end_ms - (count - index) * INTERVAL_MS,
                readings=tuple(
                    Reading(
                        metric=metric,
                        value=round(true_c + offset + rng.gauss(0, 0.02), 4),
                        quality=Quality.OK,
                    )
                    for metric, offset in bias.items()
                ),
            )
            for index in range(count)
        ]
    )


@pytest.fixture
def store(tmp_path, rules):
    with SqliteStore(tmp_path / "cal.db", rules=rules, clock=SimulatedClock(NOW_MS)) as opened:
        yield opened


def result_for(store, catalog, **kwargs):
    fill(store, **kwargs)
    return compute(measure(store, catalog, start_ms=NOW_MS - 600_000, end_ms=NOW_MS + 1))


# ---------------------------------------------------------------- 受入基準


def test_calibration_brings_the_spread_within_the_target(store, catalog, tmp_path, rules):
    """受入基準: **較正後のばらつきが ±0.15℃ 以内。**

    較正した区間で計算した値を、**別の区間**へ当てて確かめる。同じ区間で
    確かめると、定義上ゼロになるだけで何も検証したことにならない。
    """
    calibrated = result_for(store, catalog, seed=7)

    # 別の日、別の雑音、別の絶対温度で測り直す
    with SqliteStore(tmp_path / "later.db", rules=rules, clock=SimulatedClock(NOW_MS)) as later:
        fill(later, seed=99, true_c=28.5, end_ms=NOW_MS)
        after = measure(later, catalog, start_ms=NOW_MS - 600_000, end_ms=NOW_MS + 1)

    corrected = [item.mean_c + calibrated.offsets(Calibration())[item.channel] for item in after]
    assert max(corrected) - min(corrected) <= 0.15


def test_without_calibration_the_spread_exceeds_the_target(store, catalog):
    """**較正しなければ届かない。** 比較対象が無いと受入基準の意味が分からない。"""
    raw = result_for(store, catalog)
    assert raw.spread_c > 0.15


def test_the_offsets_cancel_the_individual_bias(store, catalog):
    """個体差を打ち消す向きに出る（センサーの読みへ**足す**）。"""
    result = result_for(store, catalog)
    for metric, offset in BIAS.items():
        channel = METRIC_TO_CHANNEL[metric]
        assert result.offsets(Calibration())[channel] == pytest.approx(-offset, abs=0.02)


def test_the_reference_is_the_mean_of_all(store, catalog):
    """spec-review W-02。**絶対精度は改善しない**が、必要なのは相対値。"""
    result = result_for(store, catalog, true_c=24.0)
    assert result.reference_c == pytest.approx(24.0, abs=0.02)
    assert sum(result.offsets(Calibration()).values()) == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------- 2回目の較正（#13 のレビュー指摘）


def test_recalibration_keeps_the_previous_offsets(store, catalog):
    """**2回目の較正で前回の補正を消さない。**

    `Normalizer` は**保存する前に**オフセットを足している。したがって DB から
    読んだ平均は既に補正済みで、残差はほぼ 0 になる。そこで置き換えると
    **個体差が黙って戻る**（6ヶ月ごとのやり直しが、毎回の初期化になる）。
    """
    first = Calibration(offsets_c={"front_intake": 0.19, "gpu_exhaust": -0.31})
    # 補正済みの状態（残差ほぼ 0）を模す
    corrected = result_for(store, catalog, bias=dict.fromkeys(BIAS, 0.0))
    assert abs(corrected.residuals_c[METRIC_TO_CHANNEL["air.front_intake"]]) < 0.02

    offsets = corrected.offsets(first)
    assert offsets["front_intake"] == pytest.approx(0.19, abs=0.02)
    assert offsets["gpu_exhaust"] == pytest.approx(-0.31, abs=0.02)


def test_recalibration_adds_the_residual(store, catalog):
    """前回のあとにずれたぶんは**足される。**"""
    first = Calibration(offsets_c={"front_intake": 0.19})
    drifted = result_for(
        store, catalog, bias={**dict.fromkeys(BIAS, 0.0), "air.front_intake": -0.10}
    )
    offsets = drifted.offsets(first)
    # 残差 +0.10 前後が前回の +0.19 に乗る
    assert offsets["front_intake"] == pytest.approx(0.29, abs=0.03)


def test_an_unknown_channel_starts_from_zero(store, catalog):
    """記録に無いチャネルは 0 からで構わない（新設のセンサー）。"""
    result = result_for(store, catalog)
    assert result.offsets(Calibration())["front_intake"] == pytest.approx(0.19, abs=0.02)


def test_the_display_shows_the_change(store, catalog, policy):
    """**前後が見えること。** 置き換えか加算かを人が確かめられるように。"""
    previous = Calibration(offsets_c={"front_intake": 0.19})
    lines = result_for(store, catalog).as_lines(previous, policy)
    assert any("+0.190 →" in line for line in lines)


def test_the_cli_accumulates_across_runs(ready):
    """CLI を2回通しても前回の値が消えない（**連鎖で確かめる**）。"""
    db, target = ready
    assert main([*_argv(db, target), "--apply"]) == 0
    once = Calibration.from_json(target).offset_for("front_intake")
    assert once == pytest.approx(0.19, abs=0.02)

    # 同じ DB をもう一度（実運用では補正済みの新しいデータだが、ここでは
    # 「残差がそのまま乗る」ことだけを見る）
    assert main([*_argv(db, target), "--apply"]) == 0
    twice = Calibration.from_json(target).offset_for("front_intake")
    assert twice == pytest.approx(once * 2, abs=0.02), "置き換わってしまっている"


# ---------------------------------------------------------------- 受け付けない条件


def test_a_large_spread_is_refused(store, catalog, policy):
    """**同じ空気に置かれていないなら較正しない。**

    そのまま較正すると、本物の温度勾配をオフセットとして焼き付け、
    **以降その勾配が見えなくなる。**
    """
    gradient = {metric: index * 1.5 for index, metric in enumerate(BIAS)}
    result = result_for(store, catalog, bias=gradient)
    assert result.spread_c > policy.max_spread_c
    assert result.usable(policy) is False
    assert any("同じ空気" in problem for problem in check(result, policy))


def test_a_spread_within_the_limit_is_accepted(store, catalog, policy):
    result = result_for(store, catalog)
    assert result.usable(policy) is True
    assert check(result, policy) == []


def test_too_few_samples_are_refused(store, catalog, policy):
    """**少ない標本で較正しない。** 雑音をオフセットとして焼き付ける。"""
    result = result_for(store, catalog, count=10)
    assert any("測定が少ない" in problem for problem in check(result, policy))


def test_a_missing_channel_is_refused(store, catalog, policy):
    """1本でも測れていなければ較正しない（**残りだけの平均は基準にならない**）。"""
    partial = {metric: offset for metric, offset in BIAS.items() if metric != "air.rear_exhaust"}
    result = result_for(store, catalog, bias=partial)
    assert any("測定が無い" in problem for problem in check(result, policy))


def test_humidity_is_not_calibrated(store, catalog):
    """**%RH に ℃ のオフセットを足さない**（要件 §5.1）。"""
    result = result_for(store, catalog)
    assert "room_humidity" not in result.offsets(Calibration())


def test_no_measurements_raises(store, catalog):
    with pytest.raises(ValueError, match="較正に使える測定が無い"):
        compute(measure(store, catalog, start_ms=NOW_MS - 600_000, end_ms=NOW_MS + 1))


# ---------------------------------------------------------------- 書き込む中身


def test_the_record_carries_its_evidence(store, catalog, tmp_path):
    """**根拠を残す。** いつ・何を基準に・何件で測ったか。"""
    result = result_for(store, catalog)
    at = datetime.fromtimestamp(NOW_MS / 1000, tz=UTC)
    written = as_calibration(result, Calibration(), at=at)
    assert written.calibrated_at == at.isoformat()
    assert written.reference == "mean_of_all"
    assert written.samples["room_temp"] >= 120


def test_the_stale_note_is_replaced(store, catalog):
    """**ファイルが自分自身と食い違わない。**

    引き継いだ覚書は較正**前**について書かれている（「実測前の暫定値」）。
    """
    previous = Calibration(note="実測前の暫定値で、全チャネル 0.0")
    written = as_calibration(result_for(store, catalog), previous, at=datetime.now(tz=UTC))
    assert "実測前の暫定値" not in written.note
    assert "docs/calibration.md" in written.note


def test_negative_zero_is_not_written(store, catalog, tmp_path):
    """`-0.0` を残さない。差分で読むときに紛らわしいだけで、意味は同じ。"""
    written = as_calibration(result_for(store, catalog), Calibration(), at=datetime.now(tz=UTC))
    path = tmp_path / "calibration.json"
    write(written, path)
    values = json.loads(path.read_text(encoding="utf-8"))["offsets_c"].values()
    negative_zeros = [v for v in values if v == 0.0 and math.copysign(1.0, v) < 0]
    assert negative_zeros == []
    assert any(v == 0.0 for v in values), "0 になるチャネルが無いと、この試験に意味が無い"


def test_the_written_file_can_be_read_back(store, catalog, tmp_path):
    written = as_calibration(result_for(store, catalog), Calibration(), at=datetime.now(tz=UTC))
    path = tmp_path / "calibration.json"
    write(written, path)
    assert Calibration.from_json(path) == written
    assert json.loads(path.read_text(encoding="utf-8"))["reference"] == "mean_of_all"


# ---------------------------------------------------------------- 有効期限


def test_an_uncalibrated_file_is_treated_as_expired():
    """**未較正も「やり直しが要る」に含める。** 出荷時の全ゼロがこれ。"""
    assert Calibration().is_expired(NOW_MS, after_days=183) is True
    assert Calibration().age_days(NOW_MS) is None


def test_a_fresh_calibration_is_not_expired():
    at = datetime.fromtimestamp((NOW_MS - 10 * DAY_MS) / 1000, tz=UTC)
    calibration = Calibration(calibrated_at=at.isoformat())
    assert calibration.is_expired(NOW_MS, after_days=183) is False
    assert calibration.age_days(NOW_MS) == pytest.approx(10.0, abs=0.01)


def test_an_old_calibration_expires():
    """センサーは経年でずれる。**ずれたオフセットは測っていないより悪い。**"""
    at = datetime.fromtimestamp((NOW_MS - 200 * DAY_MS) / 1000, tz=UTC)
    assert Calibration(calibrated_at=at.isoformat()).is_expired(NOW_MS, after_days=183) is True


def test_a_timestamp_without_an_offset_is_not_guessed():
    """オフセットの無い時刻は解釈が割れる。**推測しない**（やり直し扱い）。"""
    calibration = Calibration(calibrated_at="2026-09-10T11:00:00")
    assert calibration.age_days(NOW_MS) is None
    assert calibration.is_expired(NOW_MS, after_days=183) is True


def test_the_shipped_file_is_marked_uncalibrated():
    """**出荷時は未較正。** 較正済みのつもりで生値を見ることにならないように。"""
    shipped = Calibration.from_json(CONFIG_DIR / "calibration.json")
    assert shipped.calibrated_at is None
    assert set(shipped.offsets_c.values()) == {0.0}


# ---------------------------------------------------------------- CLI


def _argv(db: Path, calibration: Path) -> list[str]:
    return [
        "--db",
        str(db),
        "--calibration",
        str(calibration),
        "--metrics",
        str(CONFIG_DIR / "metrics.yaml"),
        "--quality-rules",
        str(CONFIG_DIR / "quality.yaml"),
        "--minutes",
        "10",
    ]


@pytest.fixture
def ready(tmp_path, rules, monkeypatch):
    """直近10分に測定があるDBと、書き込み先の較正ファイル。"""
    monkeypatch.setattr("coldaisle.calibrate.WallClock", lambda: SimulatedClock(NOW_MS))
    db = tmp_path / "cli.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        fill(store)
    target = tmp_path / "calibration.json"
    target.write_text(
        (CONFIG_DIR / "calibration.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return db, target


def test_nothing_is_written_without_apply(ready, capsys):
    """受入基準の外だが、**以降のすべての測定の解釈が変わる**ので確認を挟む。"""
    db, target = ready
    before = target.read_text(encoding="utf-8")
    assert main(_argv(db, target)) == 0
    assert target.read_text(encoding="utf-8") == before
    assert "書き込んでいません" in capsys.readouterr().out


def test_apply_writes_the_offsets(ready):
    db, target = ready
    assert main([*_argv(db, target), "--apply"]) == 0
    written = Calibration.from_json(target)
    assert written.calibrated_at is not None
    assert written.offset_for("front_intake") == pytest.approx(0.19, abs=0.02)


def test_a_refused_calibration_is_not_written(tmp_path, rules, monkeypatch, capsys):
    """**受け付けられない理由があれば `--apply` でも書かない。**"""
    monkeypatch.setattr("coldaisle.calibrate.WallClock", lambda: SimulatedClock(NOW_MS))
    db = tmp_path / "gradient.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        fill(store, bias={metric: index * 1.5 for index, metric in enumerate(BIAS)})
    target = tmp_path / "calibration.json"
    target.write_text(
        (CONFIG_DIR / "calibration.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    before = target.read_text(encoding="utf-8")
    assert main([*_argv(db, target), "--apply"]) == 1
    assert target.read_text(encoding="utf-8") == before
    assert "書き込みません" in capsys.readouterr().out


def test_force_overrides_the_refusal(tmp_path, rules, monkeypatch):
    """**逃げ道は残すが、既定にしない。**"""
    monkeypatch.setattr("coldaisle.calibrate.WallClock", lambda: SimulatedClock(NOW_MS))
    db = tmp_path / "gradient.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(NOW_MS)) as store:
        fill(store, bias={metric: index * 1.5 for index, metric in enumerate(BIAS)})
    target = tmp_path / "calibration.json"
    target.write_text(
        (CONFIG_DIR / "calibration.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert main([*_argv(db, target), "--apply", "--force"]) == 0
    assert Calibration.from_json(target).calibrated_at is not None


def test_an_empty_window_says_so(tmp_path, rules, monkeypatch, capsys):
    monkeypatch.setattr("coldaisle.calibrate.WallClock", lambda: SimulatedClock(NOW_MS))
    db = tmp_path / "empty.db"
    with SqliteStore(db, rules=rules, clock=SimulatedClock(NOW_MS)):
        pass
    target = tmp_path / "calibration.json"
    target.write_text('{"offsets_c":{}}', encoding="utf-8")
    assert main(_argv(db, target)) == 1
    assert "測定がありません" in capsys.readouterr().out


def test_channel_means_carry_the_metric_name():
    item = ChannelMean(channel="room_temp", metric="air.room", mean_c=24.0, count=240)
    assert METRIC_TO_CHANNEL[item.metric] == item.channel
