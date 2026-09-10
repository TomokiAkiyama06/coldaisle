"""public リポジトリの衛生（#41）。

**一度 push した情報は履歴に残り、force push でも完全には消えない。**

`detect-secrets`（pre-commit / CI）は**認証情報**を見張る。ここが見張るのは
Issue が別に挙げている**環境固有の情報**である。

- IPアドレス・MACアドレス・ホスト名
- シリアル番号（DS18B20 の ROM は**ハードウェアの個体識別子**である）
- 個人を特定できる記述、実行環境の絶対パス

**目視の確認は続かない。** 受入基準「リポジトリ単体を読んで、稼働環境や運用主体が
特定できない」を、ファイルが増えても保ち続けるために試験にする。
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ico", ".woff", ".woff2"}
"""読まないもの。**それ以外はすべて読む。**

許可リスト（`.py` や `.md` だけ見る）にすると、**新しい種類のファイルが黙って
検査の外に出る。** 実際に `.jsonl` の試験データが漏れていた（#41 のレビュー前の自己点検）。
"""

SKIP = {"LICENSE", ".secrets.baseline"}
"""見張らないファイル。

`LICENSE` は**一字も変えない**（Apache 財団の文面）。`.secrets.baseline` は
検出器が見つけた箇所のハッシュで、値そのものは入っていない。
"""


def tracked_files() -> list[Path]:
    """**git が追跡しているものだけ**を見る。`var/` や `.venv` は対象外。"""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    return [
        ROOT / name
        for name in listed.split("\0")
        if name and Path(name).name not in SKIP and Path(name).suffix not in BINARY_SUFFIXES
    ]


def contents() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for path in tracked_files():
        if not path.exists():
            continue
        try:
            out.append((path, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:  # pragma: no cover - いまは該当なし
            continue
    return out


def hits(pattern: re.Pattern[str], *, allow: re.Pattern[str] | None = None) -> list[str]:
    """当たった箇所を `path:line: text` で返す。**件数ではなく場所を出す。**"""
    found: list[str] = []
    for path, text in contents():
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                if allow is not None and allow.search(match.group(0)):
                    continue
                found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}")
    return found


# ---------------------------------------------------------------- 環境固有の情報


IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ALLOWED_IPV4 = re.compile(
    r"^(?:127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.0|192\.0\.2\.\d{1,3}|198\.51\.100\.\d{1,3}"
    r"|203\.0\.113\.\d{1,3})$"
)
"""許すのは loopback・全アドレス・文書用の予約範囲（RFC 5737）だけ。

**実環境のアドレスを例に使わない。** 例が要るなら予約範囲を使う。
"""


def test_no_real_ip_addresses():
    assert hits(IPV4, allow=ALLOWED_IPV4) == []


def test_no_mac_addresses():
    """MACアドレスは**機器の個体識別子**である。"""
    assert hits(re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")) == []


def test_no_absolute_home_paths():
    """**実行環境の絶対パスを残さない。** 利用者名がそのまま入る。"""
    assert hits(re.compile(r"/Users/[A-Za-z0-9._-]+|/home/(?!runner\b)[A-Za-z0-9._-]+")) == []


EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ALLOWED_EMAIL = re.compile(r"@(?:example\.com|example\.org|anthropic\.com|users\.noreply\.github)")


def test_no_personal_email_addresses():
    assert hits(EMAIL, allow=ALLOWED_EMAIL) == []


def test_no_wifi_credentials():
    """ファームウェアの SSID / パスフレーズを**値ごと**コミットしない。"""
    assert (
        hits(re.compile(r"""(?i)\b(?:ssid|psk|passphrase|wifi_?pass)\b\s*[:=]\s*["'][^"']+["']"""))
        == []
    )


# ---------------------------------------------------------------- ハードウェアの個体識別子


ROM = re.compile(r"\b28[0-9A-Fa-f]{14}\b")
PLACEHOLDER_ROM = re.compile(r"^28FF", re.IGNORECASE)
"""DS18B20 の ROM は**個体を一意に指す**。

試験や文書では `28FF…` で始まる値だけを使う。実物の ROM の2バイト目が `FF` に
なることはまずないので、**ここに当たるものは実機の値だと分かる。**
"""


def test_rom_ids_are_placeholders():
    """受入基準「シリアル番号を含めない」。**実機の ROM を貼らない。**"""
    assert hits(ROM, allow=PLACEHOLDER_ROM) == []


def test_short_rom_like_strings_are_not_used():
    """桁の足りない ROM を書くと、上の検査をすり抜ける。"""
    suspicious = hits(
        re.compile(r"""rom["']?\s*[:=]\s*["']28(?!FF)[0-9A-Fa-f]*["']""", re.IGNORECASE)
    )
    assert suspicious == []


# ---------------------------------------------------------------- 秘匿情報の置き場所


def test_no_env_or_database_is_tracked():
    """受入基準: **`.env` と実データベースがコミットされていない。**"""
    listed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    bad = [
        name
        for name in listed
        if ((name == ".env" or name.startswith(".env.")) and name != ".env.example")
        or name.endswith((".db", ".sqlite3", ".db-wal", ".db-shm"))
        or name.startswith("config/secrets.")
    ]
    assert bad == []


def test_env_example_has_names_without_values():
    """**変数名だけを示す。** 値を書くと、雛形が秘匿情報になる。"""
    assigned = [
        line
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    assert assigned, ".env.example に変数が1つも無い"
    for line in assigned:
        assert line.split("=", 1)[1] == "", f"値が書かれている: {line}"


@pytest.mark.parametrize(
    "pattern",
    [".env", ".env.*", "*.db", "*.sqlite3", "config/secrets.*", "logs/", "*.log", "var/"],
)
def test_gitignore_covers_the_required_patterns(pattern):
    """**入れない仕組みを残す。** 1行消えただけで次の事故が起きる。"""
    assert pattern in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_secret_detection_runs_in_ci():
    """受入基準: **pre-commit と CI の両方**で検出される。"""
    assert "detect-secrets" in (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    workflows = (ROOT / ".github" / "workflows").glob("*.yml")
    assert any("detect-secrets" in path.read_text(encoding="utf-8") for path in workflows)


# ---------------------------------------------------------------- ライセンス


def test_license_file_exists_and_matches_the_declaration():
    """受入基準: **LICENSE が配置されている。**

    `pyproject.toml` と README が Apache-2.0 を宣言しているのに本文が無いと、
    **条件が相手に届かない。**
    """
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert 'license = "Apache-2.0"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "Apache License 2.0" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_license_placeholder_is_filled_in():
    """附則の雛形を埋める。**誰の著作物か分からない LICENSE を置かない。**"""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "[yyyy]" not in text
    assert "[name of copyright owner]" not in text
    assert "Copyright 2026" in text


def test_the_copyright_notice_is_in_the_readme():
    assert "Copyright 2026" in (ROOT / "README.md").read_text(encoding="utf-8")
