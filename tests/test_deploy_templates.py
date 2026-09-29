"""Ubuntu 常駐化のテンプレート（#57 / 決定記録 0069）。

`systemd-analyze verify` は CI に無いので、**壊れやすいところだけ**を静的に見る。

1. unit の構文（節と `キー=値`）と、常駐・ログ・データの置き場所の約束
2. `ExecStart` が実在する入口を指していること（名前を変えたら気づく）
3. udev ルールが**仮の値のまま**であること（実機の値をコミットしない。0021）
4. `coldaisle-fand` の unit を置いていないこと（0060 未決7。安全系の判断が要る）
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
UDEV = ROOT / "deploy" / "udev"

SERVICES = ("coldaisle-daemon", "coldaisle-api")
"""常駐するもの。**落ちたら戻す**（NFR-01）。"""

JOBS = ("coldaisle-rollup", "coldaisle-report")
"""タイマーから1回走って終わるもの。"""

SECTION = re.compile(r"^\[([A-Za-z]+)\]$")
ASSIGNMENT = re.compile(r"^([A-Za-z][A-Za-z0-9]*)=(.*)$")


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """systemd の unit を読む。**同じキーの繰り返しを許す**（configparser は許さない）。

    節の外の代入や、`キー=値` の形でない行があれば落とす。
    """
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = SECTION.match(line)
        if header:
            assert header.group(1) not in sections, f"{path.name}:{number}: 節の重複"
            current = sections.setdefault(header.group(1), {})
            continue
        assigned = ASSIGNMENT.match(line)
        assert assigned, f"{path.name}:{number}: 読めない行: {line}"
        assert current is not None, f"{path.name}:{number}: 節の外の代入"
        current.setdefault(assigned.group(1), []).append(assigned.group(2))
    return sections


def one(unit: dict[str, dict[str, list[str]]], section: str, key: str) -> str:
    values = unit[section][key]
    assert len(values) == 1, f"{section}.{key} が複数ある"
    return values[0]


def scripts() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(data["project"]["scripts"])


def unit_files() -> list[Path]:
    return sorted(SYSTEMD.glob("*.service")) + sorted(SYSTEMD.glob("*.timer"))


def test_the_expected_units_exist():
    names = {path.name for path in unit_files()}
    expected = {f"{name}.service" for name in (*SERVICES, *JOBS)}
    expected |= {f"{name}.timer" for name in JOBS}
    assert names == expected


def test_fand_is_not_templated_here():
    """**Fan 制御の unit は置かない**（0060 未決7 / 0069 §2.4）。"""
    assert not list(SYSTEMD.glob("coldaisle-fand*"))
    assert not list(SYSTEMD.glob("coldaisle-safety-handoff*"))


@pytest.mark.parametrize("path", unit_files(), ids=lambda path: path.name)
def test_every_unit_parses(path):
    unit = parse_unit(path)
    assert "Unit" in unit
    assert one(unit, "Unit", "Description")


@pytest.mark.parametrize("name", SERVICES)
def test_resident_services_always_restart(name):
    unit = parse_unit(SYSTEMD / f"{name}.service")
    assert one(unit, "Service", "Restart") == "always"
    # NFR-01: 10秒以内に復帰する
    assert float(one(unit, "Service", "RestartSec")) < 10
    # 既定の StartLimit で再起動を諦めない
    assert one(unit, "Unit", "StartLimitIntervalSec") == "0"
    assert one(unit, "Install", "WantedBy") == "multi-user.target"


@pytest.mark.parametrize("name", JOBS)
def test_jobs_are_oneshot_and_have_a_timer(name):
    service = parse_unit(SYSTEMD / f"{name}.service")
    assert one(service, "Service", "Type") == "oneshot"
    timer = parse_unit(SYSTEMD / f"{name}.timer")
    assert one(timer, "Timer", "OnCalendar")
    assert one(timer, "Timer", "Persistent") == "true"
    assert one(timer, "Install", "WantedBy") == "timers.target"


@pytest.mark.parametrize("name", (*SERVICES, *JOBS))
def test_services_log_to_journald_and_keep_data_in_var_lib(name):
    unit = parse_unit(SYSTEMD / f"{name}.service")
    assert one(unit, "Service", "StandardOutput") == "journal"
    assert one(unit, "Service", "StandardError") == "journal"
    assert one(unit, "Service", "StateDirectory") == "coldaisle"
    exec_start = one(unit, "Service", "ExecStart")
    environment = " ".join(unit["Service"].get("Environment", []))
    assert "/var/lib/coldaisle/coldaisle.db" in exec_start + environment


@pytest.mark.parametrize("name", (*SERVICES, *JOBS))
def test_services_do_not_run_as_root(name):
    unit = parse_unit(SYSTEMD / f"{name}.service")
    assert one(unit, "Service", "User") == "coldaisle"  # 仮の値（0069 §2.2）
    assert one(unit, "Service", "Group") == "coldaisle"


@pytest.mark.parametrize("name", ("coldaisle-daemon", *JOBS))
def test_exec_start_points_at_a_real_entry_point(name):
    """**入口の名前を変えたら、テンプレートも落ちる。**"""
    unit = parse_unit(SYSTEMD / f"{name}.service")
    binary = one(unit, "Service", "ExecStart").split()[0]
    assert binary == f"/opt/coldaisle/.venv/bin/{name}"
    assert name in scripts()


def test_the_report_waits_for_the_rollup():
    """停止をまたいで両タイマーが同時に追いついても、report は rollup のあと（0017 §2.1）。"""
    unit = parse_unit(SYSTEMD / "coldaisle-report.service")
    assert "coldaisle-rollup.service" in " ".join(unit["Unit"].get("After", []))


def test_the_ingest_daemon_reads_serial_with_dialout():
    unit = parse_unit(SYSTEMD / "coldaisle-daemon.service")
    assert "--source serial" in one(unit, "Service", "ExecStart")
    assert one(unit, "Service", "SupplementaryGroups") == "dialout"


def test_the_api_listens_on_loopback_only():
    unit = parse_unit(SYSTEMD / "coldaisle-api.service")
    exec_start = one(unit, "Service", "ExecStart")
    assert "coldaisle.api:app" in exec_start
    assert "--host 127.0.0.1" in exec_start


# ---------------------------------------------------------------- udev


def udev_rules() -> list[str]:
    lines: list[str] = []
    for path in sorted(UDEV.glob("*.rules")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def test_the_udev_rule_names_the_fixed_device():
    rules = udev_rules()
    assert len(rules) == 1
    rule = rules[0]
    assert 'SUBSYSTEM=="tty"' in rule
    assert 'SYMLINK+="server-sensors"' in rule
    assert 'GROUP="dialout"' in rule


def test_the_udev_rule_carries_placeholders_only():
    """**実機の VID / PID / シリアルをコミットしない**（AGENTS.md ルール10）。"""
    rule = udev_rules()[0]
    matched = {key: value for key, value in re.findall(r'ATTRS\{(\w+)\}=="([^"]*)"', rule)}
    assert matched == {
        "idVendor": "0000",
        "idProduct": "0000",
        "serial": "REPLACE-WITH-DEVICE-SERIAL",
    }


def test_the_udev_symlink_is_the_first_serial_candidate():
    """udev が作る名前と、取り込みが最優先で探す名前を**ずらさない**。"""
    from coldaisle.ingest.serial_source import FIXED_PORTS, PORT_PATTERNS

    assert PORT_PATTERNS[0] == FIXED_PORTS[0] == "/dev/server-sensors"
