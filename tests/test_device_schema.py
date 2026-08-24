"""デバイス出力 JSON スキーマ v1 の検証（決定記録 0003 / #4）。

スキーマが「通すべきものを通す」だけでなく「弾くべきものを弾く」ことも確かめる。
片方だけだと、素通しのスキーマでもテストが緑になる。
"""

import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "device_v1.schema.json"
FIXTURES = Path(__file__).parent / "fixtures"


def _reject_constant(token: str) -> float:
    raise ValueError(f"JSON として不正な定数: {token}")


def _lines(name: str) -> list[str]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("name", ["device_v1_valid.jsonl", "device_v1_missing.jsonl"])
def test_valid_fixtures_pass(validator, name):
    lines = _lines(name)
    assert lines, f"{name} が空"
    for i, line in enumerate(lines, 1):
        errors = sorted(validator.iter_errors(json.loads(line)), key=str)
        assert not errors, f"{name}:{i} が弾かれた: {errors[0].message}"


def test_schema_violations_are_rejected(validator):
    lines = _lines("device_v1_schema_violations.jsonl")
    assert len(lines) == 13
    for i, line in enumerate(lines, 1):
        obj = json.loads(line)
        assert not validator.is_valid(obj), f"{i}行目が通ってしまった: {line}"


def test_boot_log_is_discarded_and_json_lines_validate(validator):
    """ブートログが混ざっても JSON 行だけ拾えば全て契約に合う（FR-103）。"""
    parsed, discarded = [], 0
    for line in _lines("serial_with_boot_log.txt"):
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            discarded += 1
    assert discarded == 9
    assert len(parsed) == 3
    for obj in parsed:
        assert validator.is_valid(obj)


def test_invalid_json_fixture_is_really_invalid():
    for line in _lines("serial_invalid_json.txt"):
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)


def test_nonfinite_readings_slip_through_the_schema(validator):
    """メトリクス値の NaN / Infinity はスキーマでは止まらない。

    JSON 仕様では不正だが json.loads は既定で受け入れ、jsonschema からは
    number に見えるため `["number", "null"]` を通過する。
    SQLite の REAL に入ると min/max/mean を恒久的に壊すため、
    決定記録 0003 は取り込み側での拒否を必須としている。
    """
    lines = _lines("device_v1_nonfinite.jsonl")
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert validator.is_valid(obj), "この時点では通ってしまうことを記録しておく"
        assert any(isinstance(v, float) and not math.isfinite(v) for v in obj.values())


def test_nonfinite_numbers_are_rejected_with_parse_constant():
    """決定記録 0003 が定める取り込み側の規則。"""
    for line in _lines("device_v1_nonfinite.jsonl"):
        with pytest.raises(ValueError):
            json.loads(line, parse_constant=_reject_constant)
