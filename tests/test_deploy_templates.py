"""Ubuntu 常駐化のテンプレート（#57 / 決定記録 0069）。

`systemd-analyze verify` は CI に無いので、**壊れやすいところだけ**を静的に見る。

1. unit の構文（節と `キー=値`）と、常駐・ログ・データの置き場所の約束
2. `ExecStart` が実在する入口を指していること（名前を変えたら気づく）
3. udev ルールが**仮の値のまま**であること（実機の値をコミットしない。0021）
4. `coldaisle-fand` の unit が決定記録 0080 §2.2〜§2.9 の約束を守ること（段階 1 / #57）
5. DB を共有する unit の `StateDirectoryMode` と `UMask` がそろっていること（0080 §2.1）
6. authority の journal を承認者のグループと共有する専用のディレクトリ
   （決定記録 0086 §2.2。段階 3c）
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

from coldaisle.safety_handoff import HANDOFF_RECORD_PATH

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
UDEV = ROOT / "deploy" / "udev"

SERVICES = ("coldaisle-daemon", "coldaisle-api")
"""常駐するもの。**落ちたら戻す**（NFR-01）。"""

JOBS = ("coldaisle-rollup", "coldaisle-report")
"""タイマーから1回走って終わるもの。"""

FAND = "coldaisle-fand"
"""3系統 Fan 制御デーモン（決定記録 0080）。`/var/lib/coldaisle` を StateDirectory に持たない。"""

AUTHORITY_DIR = "/var/lib/coldaisle-authority"
"""authority.json の置き場所（決定記録 0086 §2.2。**仮の値**）。

fand と承認者のグループが共有する。
"""

AUTHORITY_GROUP = "coldaisle-authority"
"""昇格・rollback を行う人のグループ（決定記録 0086 §2.2。**仮の値**）。"""

DB_UNITS = (*SERVICES, *JOBS)
"""`StateDirectory=coldaisle` を持ち、`/var/lib/coldaisle` の mode を決める unit（0080 §2.1）。"""

SENSORS_RULES = UDEV / "99-coldaisle-sensors.rules"
HWMON_RULES = UDEV / "99-coldaisle-hwmon.rules"

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
    expected = {f"{name}.service" for name in (*SERVICES, *JOBS, FAND)}
    expected |= {f"{name}.timer" for name in JOBS}
    assert names == expected


def test_the_handoff_is_not_a_unit_of_its_own():
    """引き継ぎ実行部は fand の `ExecStopPost` で走る（0080 §2.5）。別の unit にしない。"""
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


@pytest.mark.parametrize("name", DB_UNITS)
def test_db_units_share_the_state_directory_with_the_group(name):
    """**4つとも同じ値。** どれかの起動で `/var/lib/coldaisle` の mode が戻される（0080 §2.1）。

    2770（setgid）でないと、fand が作った `-wal` / `-shm` の gid が fand の主グループになり、
    取り込みが開けなくなる。`UMask=0007` でないと、`-wal` / `-journal` がグループで書けない。
    """
    unit = parse_unit(SYSTEMD / f"{name}.service")
    assert one(unit, "Service", "StateDirectoryMode") == "2770"
    assert one(unit, "Service", "UMask") == "0007"


@pytest.mark.parametrize("name", (*SERVICES, *JOBS))
def test_services_do_not_run_as_root(name):
    unit = parse_unit(SYSTEMD / f"{name}.service")
    assert one(unit, "Service", "User") == "coldaisle"  # 仮の値（0069 §2.2）
    assert one(unit, "Service", "Group") == "coldaisle"


@pytest.mark.parametrize("name", ("coldaisle-daemon", *JOBS, FAND))
def test_exec_start_points_at_a_real_entry_point(name):
    """**入口の名前を変えたら、テンプレートも落ちる。**"""
    unit = parse_unit(SYSTEMD / f"{name}.service")
    binary = one(unit, "Service", "ExecStart").split()[0]
    assert binary == f"/opt/coldaisle/.venv/bin/{name}"
    assert name in scripts()


def test_the_report_waits_for_the_rollup():
    """停止をまたいで両タイマーが同時に追いついても、report は rollup のあと（0017 §2.1）。"""
    unit = parse_unit(SYSTEMD / "coldaisle-report.service")
    # After= は順序だけ。Wants= が無いと、別々に queue された report が先に走りうる
    assert "coldaisle-rollup.service" in " ".join(unit["Unit"].get("After", []))
    assert "coldaisle-rollup.service" in " ".join(unit["Unit"].get("Wants", []))


def test_the_ingest_daemon_reads_serial_with_dialout():
    unit = parse_unit(SYSTEMD / "coldaisle-daemon.service")
    assert "--source serial" in one(unit, "Service", "ExecStart")
    assert one(unit, "Service", "SupplementaryGroups") == "dialout"


def test_the_api_listens_on_loopback_only():
    unit = parse_unit(SYSTEMD / "coldaisle-api.service")
    exec_start = one(unit, "Service", "ExecStart")
    assert "coldaisle.api:app" in exec_start
    assert "--host 127.0.0.1" in exec_start


def test_the_api_reads_the_same_db_the_writers_write():
    """env ファイルの COLDAISLE_DB が Environment= を上書きしても、API は書き手と同じ DB を読む。"""
    unit = parse_unit(SYSTEMD / "coldaisle-api.service")
    exec_start = one(unit, "Service", "ExecStart")
    assert exec_start.startswith("/usr/bin/env COLDAISLE_DB=/var/lib/coldaisle/coldaisle.db ")
    assert not any("COLDAISLE_DB" in v for v in unit["Service"].get("Environment", []))


# ---------------------------------------------------------------- udev


def udev_rules(path: Path = SENSORS_RULES) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def test_the_expected_udev_rules_exist():
    assert {path.name for path in UDEV.glob("*.rules")} == {SENSORS_RULES.name, HWMON_RULES.name}


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


# ---------------------------------------------------------------- coldaisle-fand（決定記録 0080）


def fand() -> dict[str, dict[str, list[str]]]:
    return parse_unit(SYSTEMD / f"{FAND}.service")


def words(unit: dict[str, dict[str, list[str]]], section: str, key: str) -> list[str]:
    """空白区切りで複数回書けるキー（`After=` など）を1つの並びにする。"""
    return " ".join(unit[section].get(key, [])).split()


def test_fand_is_a_notify_service_and_only_main_may_feed_the_deadman():
    """子や将来の worker が `WATCHDOG=1` を送れると、main loop の hang を隠す（0080 §2.2）。"""
    unit = fand()
    assert one(unit, "Service", "Type") == "notify"
    assert one(unit, "Service", "NotifyAccess") == "main"
    assert one(unit, "Service", "WatchdogSec")
    assert one(unit, "Service", "WatchdogSignal") == "SIGABRT"
    assert one(unit, "Service", "LimitCORE") == "0"


def test_fand_refuses_to_run_without_the_deadman_and_uses_the_shared_db():
    exec_start = one(fand(), "Service", "ExecStart").split()
    assert "--require-watchdog" in exec_start
    assert exec_start[exec_start.index("--db") + 1] == "/var/lib/coldaisle/coldaisle.db"
    # authority.json は承認者のグループと共有する専用のディレクトリに置く（決定記録 0086 §2.2）。
    # API のグループが書ける /var/lib/coldaisle にも、fand 専用（0700）の StateDirectory にも
    # 置かない
    authority = exec_start[exec_start.index("--authority-root") + 1]
    assert authority == AUTHORITY_DIR
    # 制御の設定と管理ソケットの設定は fand が書けない場所（0080 §2.2）
    for option in ("--config-dir", "--admin-config"):
        assert not exec_start[exec_start.index(option) + 1].startswith("/var/lib/")


def test_fand_stops_every_process_before_the_handoff():
    """`ExecStopPost` の前に cgroup の全プロセスを止め、書き手を1つにする（0028 §2.7）。"""
    assert one(fand(), "Service", "KillMode") == "control-group"


def test_fand_never_gives_up_restarting():
    unit = fand()
    assert one(unit, "Service", "Restart") == "always"
    assert one(unit, "Unit", "StartLimitIntervalSec") == "0"
    assert one(unit, "Install", "WantedBy") == "multi-user.target"


def test_fand_prevents_restart_only_for_the_permanent_exit_codes():
    """**ちょうど {3, 4}。** 1 を入れると未捕捉の例外の後に再起動しない（0080 §2.4）。"""
    assert set(words(fand(), "Service", "RestartPreventExitStatus")) == {"3", "4"}
    assert len(words(fand(), "Service", "RestartPreventExitStatus")) == 2


def test_the_handoff_runs_as_root_from_the_source_file_without_arguments():
    """venv と `coldaisle` パッケージに依存せず、fand の権限の喪失に巻き込まれない（0080 §2.5）。"""
    command = one(fand(), "Service", "ExecStopPost").split()
    assert command[0] == "+/usr/bin/python3"
    assert command[1:3] == ["-I", "-S"]
    assert len(command) == 4, "引数を渡さない"
    source = command[3]
    assert source.startswith("/opt/coldaisle/")
    # 実在するファイルを指す（モジュールの位置を変えたら落ちる）
    assert (ROOT / source.removeprefix("/opt/coldaisle/")).is_file()
    assert source.endswith("/coldaisle/safety_handoff.py")


def test_the_deploy_guide_runs_the_handoff_exactly_as_the_unit_does():
    """導入先のシステムの Python で実行部が動くことを、unit と同じ形で確かめさせる（0080 §2.5）。

    uv の管理する Python の導入先（docs/ubuntu-deploy.md 2 節）でも `/usr/bin/python3` が
    使われるため、unit を変えたら手順も変える。
    """
    command = one(fand(), "Service", "ExecStopPost").removeprefix("+")
    guide = (ROOT / "docs" / "ubuntu-deploy.md").read_text(encoding="utf-8")
    assert f"sudo {command};" in guide


def test_fand_keeps_the_kernel_tunables_writable():
    """`ProtectKernelTunables=yes` は `/sys` を読み取り専用にする（0080 §2.6）。

    hwmon へ書けなくなり、takeover も引き継ぎも失敗する。
    """
    assert one(fand(), "Service", "ProtectKernelTunables") == "no"


def test_fand_is_sandboxed_away_from_serial_and_tcp():
    """シリアルを開くのは取り込みだけ（ルール6）。TCP を持たない（ルール1）。"""
    service = fand()["Service"]
    assert service["ProtectSystem"] == ["strict"]
    assert service["PrivateDevices"] == ["yes"]
    assert service["RestrictAddressFamilies"] == ["AF_UNIX"]
    assert service["IPAddressDeny"] == ["any"]
    assert service["CapabilityBoundingSet"] == [""]
    assert service["AmbientCapabilities"] == [""]
    assert service["NoNewPrivileges"] == ["yes"]
    assert words(fand(), "Service", "ReadWritePaths") == ["/var/lib/coldaisle", AUTHORITY_DIR]


def test_fand_does_not_take_over_the_shared_state_directory():
    """`StateDirectory=coldaisle` を書かない（0080 §2.1）。

    書くと `/var/lib/coldaisle` の所有者が fand に付け替えられ、取り込み・API が DB を書けなくなる。
    """
    unit = fand()
    assert words(unit, "Service", "StateDirectory") == ["coldaisle-fand"]
    # 書かないと既定の 0755 になり、他のローカルユーザーが authority の journal を読める
    assert one(unit, "Service", "StateDirectoryMode") == "0700"
    assert one(unit, "Service", "UMask") == "0007"


def test_only_fand_owns_the_handoff_runtime_directory():
    """ほかの unit の停止で `/run/coldaisle` が消えると、引き継ぎ記録ごと消える（0080 §2.7）。"""
    assert HANDOFF_RECORD_PATH.parent == Path("/run/coldaisle")
    owners = [
        path.name
        for path in SYSTEMD.glob("*.service")
        if HANDOFF_RECORD_PATH.parent.name in words(parse_unit(path), "Service", "RuntimeDirectory")
    ]
    assert owners == [f"{FAND}.service"]
    unit = fand()
    assert one(unit, "Service", "RuntimeDirectoryMode") == "0711"
    assert one(unit, "Service", "RuntimeDirectoryPreserve") == "yes"


def test_fand_starts_after_the_ingest_without_depending_on_it():
    """取り込みの停止・再起動が冷却の制御の停止に波及しない（0080 §2.9）。"""
    unit = fand()
    for key in ("Requires", "BindsTo", "PartOf", "Requisite"):
        assert key not in unit["Unit"], f"{key}= を使わない"
    assert "coldaisle-daemon.service" in words(unit, "Unit", "Wants")
    after = words(unit, "Unit", "After")
    assert "coldaisle-daemon.service" in after
    assert "systemd-modules-load.service" in after


def test_fand_runs_as_its_own_user_with_the_db_and_admin_groups():
    """API / 取り込みと同じ uid にも root にもしない（0080 §2.1 / 0072 §2.5）。"""
    unit = fand()
    user = one(unit, "Service", "User")
    assert user not in {"coldaisle", "root", "0"}
    assert one(unit, "Service", "Group") not in {"coldaisle", "root", "0"}
    admin = yaml.safe_load((ROOT / "config" / "control-admin.yaml").read_text(encoding="utf-8"))
    groups = words(unit, "Service", "SupplementaryGroups")
    assert sorted(groups) == sorted({"coldaisle", admin["socket"]["group"], AUTHORITY_GROUP})


def test_the_authority_journal_lives_in_its_own_shared_directory():
    """journal は承認者のグループと共有する専用のディレクトリ（決定記録 0086 §2.2）。

    - `--authority-root` と `ReadWritePaths=` が同じディレクトリを指す
      （`ProtectSystem=strict` の下で開ける）
    - そのディレクトリを `StateDirectory=` にしない（systemd がグループを `Group=` へ付け替え、
      承認者が書けなくなる）
    - DB の `/var/lib/coldaisle`（API のグループが書ける）とも、fand 専用の `0700` の
      状態ディレクトリとも分ける
    """
    unit = fand()
    exec_start = one(unit, "Service", "ExecStart").split()
    root = exec_start[exec_start.index("--authority-root") + 1]
    assert root in words(unit, "Service", "ReadWritePaths")
    state = [f"/var/lib/{name}" for name in words(unit, "Service", "StateDirectory")]
    for other in (*state, "/var/lib/coldaisle"):
        assert root != other
        assert not root.startswith(f"{other}/")
    # 承認者のグループは fand の主グループとも、DB・管理ソケットのグループとも別
    admin = yaml.safe_load((ROOT / "config" / "control-admin.yaml").read_text(encoding="utf-8"))
    assert AUTHORITY_GROUP not in {
        one(unit, "Service", "Group"),
        "coldaisle",
        admin["socket"]["group"],
    }


def test_the_deploy_guide_creates_the_authority_directory_the_unit_uses():
    """unit の値を変えたら導入手順も変える（決定記録 0086 §2.2 / §2.7）。

    所有者は fand のユーザー、グループは承認者のグループ、`2770`（setgid）。
    """
    unit = fand()
    guide = (ROOT / "docs" / "ubuntu-deploy.md").read_text(encoding="utf-8")
    user = one(unit, "Service", "User")
    assert f"install -d -o {user} -g {AUTHORITY_GROUP} -m 2770 {AUTHORITY_DIR}" in guide
    # 一時的な unit で書き込み権を確かめる手順は、unit と同じ補助グループを渡す（0080 §2.1）
    supplementary = " ".join(words(unit, "Service", "SupplementaryGroups"))
    assert f'"SupplementaryGroups={supplementary}"' in guide


def test_fand_states_every_timeout():
    """既定（90 秒）のままだと、止まらない fand を待つあいだ PWM が最後の値に残る（0080 §2.2）。"""
    service = fand()["Service"]
    for key in ("TimeoutStartSec", "TimeoutStopSec", "TimeoutAbortSec"):
        assert len(service.get(key, [])) == 1, f"{key} を明示する"
        assert service[key][0] not in {"", "infinity"}


def test_fand_logs_to_journald():
    unit = fand()
    assert one(unit, "Service", "StandardOutput") == "journal"
    assert one(unit, "Service", "SyslogIdentifier") == FAND


# ---------------------------------------------------------------- udev（hwmon）


def test_the_hwmon_rule_changes_sysfs_attributes_with_run():
    """hwmon の属性は `RUN+=` で変える（0080 §2.6）。

    `GROUP=` / `MODE=` は /dev のノードにしか効かず、hwmon には /dev のノードが無い。
    """
    rules = udev_rules(HWMON_RULES)
    assert rules
    group = one(fand(), "Service", "Group")
    for rule in rules:
        assert 'SUBSYSTEM=="hwmon"' in rule
        assert "GROUP=" not in rule
        assert "MODE=" not in rule
        runs = re.findall(r'RUN\+="([^"]*)"', rule)
        chgrp = [run for run in runs if run.startswith("/bin/chgrp ")]
        chmod = [run for run in runs if run.startswith("/bin/chmod ")]
        assert len(chgrp) == len(chmod) == 1
        assert chgrp[0].split()[1] == group
        assert chmod[0].split()[1] == "0664"
        # 対象は pwmN と pwmN_enable だけ
        for run in (chgrp[0], chmod[0]):
            targets = run.split()[2:]
            assert targets
            assert all(re.fullmatch(r"/sys%p/pwm\w+?(_enable)?", target) for target in targets)


def test_the_hwmon_rule_carries_placeholders_only():
    """**実機の driver 名と channel 番号をコミットしない**（AGENTS.md ルール10）。"""
    for rule in udev_rules(HWMON_RULES):
        assert re.findall(r'ATTR\{name\}=="([^"]*)"', rule) == ["REPLACE-WITH-DRIVER-NAME"]
        assert set(re.findall(r"/sys%p/(pwm\w+)", rule)) == {"pwmN", "pwmN_enable"}
