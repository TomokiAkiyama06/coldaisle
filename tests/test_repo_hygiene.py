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

import os
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

SKIP = {"LICENSE", ".secrets.baseline", Path(__file__).name}
"""見張らないファイル。

`LICENSE` は**一字も変えない**（Apache 財団の文面）。`.secrets.baseline` は
検出器が見つけた箇所のハッシュで、値そのものは入っていない。

**このファイル自身も外す。** 式そのものと「当たるべき例」を書いてあるので、
自分の式に自分が引っかかる。代わりに、**各式が例を拾えることを下の自己試験で
確かめる**（`tests/test_ai_tools.py` の安全性検査と同じ作り）。
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
    """**読めないバイトがあっても読み飛ばさない。**

    UTF-8 として壊れた1バイトでファイルごと検査から外すと、その中の ASCII の
    アドレスやパスが全部すり抜ける。置換して読み、**中身は必ず見る。**
    """
    return [
        (path, path.read_bytes().decode("utf-8", errors="replace"))
        for path in tracked_files()
        if path.exists()
    ]


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

MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")

EXEC_PATH = re.compile(
    r"/Users/[A-Za-z0-9._-]+|/home/(?!runner\b)[A-Za-z0-9._-]+|/root/|/workspace/"
)
"""**実行環境の絶対パスを残さない。**

`/Users/<名前>` と `/home/<名前>` は利用者名がそのまま入る。`/root/` は root で
動かしていること、`/workspace/` は CI やサンドボックスの作業場所を表す。

`/var/lib/` `/etc/` `/opt/` `/srv/` は**入れていない。** これらは FHS の標準的な
置き場所で、誰の環境かを示さない（#26 が `/var/lib/coldaisle` を配置先として
書いている）。**意図して書く配置先と、手元から貼り付いた痕跡は別物である。**
"""

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ALLOWED_EMAIL = re.compile(r"@(?:example\.com|example\.org|anthropic\.com|users\.noreply\.github)")

HOSTNAME = re.compile(
    r"""(?ix)
    (?: \b(?: hostname | host | server_name | broker | mqtt_host ) \s* [:=]     # 設定の代入
      | --host(?:name)? [=\s]+                                                  # CLI の指定
    )
    \s* ["']? ( [A-Za-z0-9][A-Za-z0-9._-]* )
    """
)
"""ホスト名の代入と `--host` の指定を見る。

**散文には当たらないようにする。** `[:=]` か `--host` を必須にしているので、
「the host is unreachable」のような文は拾わない。
"""

ALLOWED_HOSTNAME = re.compile(
    r"""(?ix) (?: localhost | 127\.0\.0\.1 | 0\.0\.0\.0
              | \b(?:example|invalid|test|local)\b
              | your[-_] | \$ | \{ | < )"""
)
"""`localhost` と文書用の名前だけ。**実機の名前を例に使わない。**"""

WIFI = re.compile(
    r"""(?ix)
    \b(?: ssid | psk | passphrase | wifi_?pass(?:word)? ) \b
    \s* [:=] \s*
    ["']? ( [^\s"'#,}\]]+ )        # 引用の有無を問わない
    """
)
"""**引用符の有無で見逃さない。**

`ssid: production-net` や `WIFI_PASS=hunter2` は YAML / シェルでよくある形で、
引用符を必須にすると通ってしまう。空の代入（雛形）は値が取れないので当たらない。
"""


def test_no_real_ip_addresses():
    assert hits(IPV4, allow=ALLOWED_IPV4) == []


def test_no_mac_addresses():
    """MACアドレスは**機器の個体識別子**である。"""
    assert hits(MAC) == []


def test_no_absolute_execution_paths():
    assert hits(EXEC_PATH) == []


def test_no_personal_email_addresses():
    assert hits(EMAIL, allow=ALLOWED_EMAIL) == []


def test_no_real_hostnames():
    """受入基準の「ホスト名を含めない」。**主張するなら検査する。**"""
    assert hits(HOSTNAME, allow=ALLOWED_HOSTNAME) == []


def test_no_wifi_credentials():
    """ファームウェアの SSID / パスフレーズを**値ごと**コミットしない。"""
    assert hits(WIFI) == []


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


# ---------------------------------------------------------------- 押し上げた履歴


CHECKS: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = [
    ("実IPアドレス", IPV4, ALLOWED_IPV4),
    ("MACアドレス", MAC, None),
    ("実行環境の絶対パス", EXEC_PATH, None),
    ("個人のメールアドレス", EMAIL, ALLOWED_EMAIL),
    ("実機のホスト名", HOSTNAME, ALLOWED_HOSTNAME),
    ("Wi-Fi の設定値", WIFI, None),
    ("実機の ROM", ROM, PLACEHOLDER_ROM),
]
"""作業ツリーと**履歴の両方**に同じ式を当てる。"""

BASE_REFS = ("origin/main", "main")


def _merge_base() -> str | None:
    for ref in BASE_REFS:
        done = subprocess.run(
            ["git", "merge-base", ref, "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return None


def added_lines(base: str) -> list[tuple[str, str]]:
    """`base..HEAD` の**各コミットが追加した行**を `(path, line)` で返す。

    `git diff base..HEAD` ではいけない。**両端の木を比べるだけ**なので、
    途中で入れて消した行は差分に出ない。それはまさにこの試験が捕まえたい形である。
    `git log -p` で1コミットずつ見る。

    マージコミットの差分は既定で出ない（衝突解決で入った行は見えない）。
    このリポジトリの運用は1 Issue = 1ブランチで、ブランチ内でマージしない。
    """
    diff = subprocess.run(
        ["git", "log", "-p", "--unified=0", "--no-color", "--format=", f"{base}..HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    out: list[tuple[str, str]] = []
    path = "?"
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[len("+++ b/") :]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, line[1:]))
    return out


def test_pushed_commits_carry_no_identifiers():
    """**push した履歴には残る。** 途中のコミットで入れて消しても消えない。

    作業ツリーだけを見ると、1つの PR の中で「入れて → 消した」場合に通ってしまう。
    Issue #41 の前提は「一度 push した情報は履歴に残り、force push でも完全には
    消えない」なので、**そのブランチが足した行すべて**を見る。

    既存の履歴は対象にしない（もう公開されていて、試験を緑にする手段が無い）。
    止められるのは**これから足す分**だけである。
    """
    base = _merge_base()
    if base is None:
        # CI では浅いクローンにしない（`fetch-depth: 0`）。黙って飛ばさない
        assert not os.environ.get("CI"), "CI で履歴が取れていない（fetch-depth を確認）"
        pytest.skip("main が無いため履歴を比較できない（浅いクローン）")

    found: list[str] = []
    for path, line in added_lines(base):
        if Path(path).name in SKIP or Path(path).suffix in BINARY_SUFFIXES:
            continue
        for name, pattern, allow in CHECKS:
            for match in pattern.finditer(line):
                if allow is not None and allow.search(match.group(0)):
                    continue
                found.append(f"{path}: {name}: {line.strip()[:100]}")
    assert found == []


# ---------------------------------------------------------------- 検査自身の試験


@pytest.mark.parametrize(
    ("pattern", "allow", "text"),
    [
        (IPV4, ALLOWED_IPV4, "接続先 192.168.10.42"),
        (IPV4, ALLOWED_IPV4, "10.0.0.5 へ向ける"),
        (MAC, None, "MAC a4:cf:12:34:56:78"),
        (MAC, None, "MAC a4-cf-12-34-56-78"),
        (EXEC_PATH, None, "/Users/someone/work"),
        (EXEC_PATH, None, "/home/alice/coldaisle"),
        (EXEC_PATH, None, "/root/coldaisle/var"),
        (EXEC_PATH, None, "/workspace/coldaisle/.env"),
        (EMAIL, ALLOWED_EMAIL, "連絡先 someone@gmail.com"),
        (HOSTNAME, ALLOWED_HOSTNAME, "hostname: gpu-rack-07"),
        (HOSTNAME, ALLOWED_HOSTNAME, "host=monitor.internal"),
        (HOSTNAME, ALLOWED_HOSTNAME, "--host gpu-rack-07"),
        (WIFI, None, 'ssid = "MyHomeWifi"'),
        (WIFI, None, "ssid: production-net"),
        (WIFI, None, "WIFI_PASS=hunter2"),
        (WIFI, None, "passphrase: s3cret"),
        (ROM, PLACEHOLDER_ROM, "rom 28A1B2C3D4E5F601"),
    ],
)
def test_the_patterns_catch_what_they_claim(pattern, allow, text):
    """**式が例を拾えることを確かめる。**

    例をファイルに置いて走査させると自分の式に当たるので、ここで assert する。
    """
    found = [
        match.group(0)
        for match in pattern.finditer(text)
        if allow is None or not allow.search(match.group(0))
    ]
    assert found, f"拾えていない: {text!r}"


@pytest.mark.parametrize(
    ("pattern", "allow", "text"),
    [
        (IPV4, ALLOWED_IPV4, "http://127.0.0.1:8000/"),
        (IPV4, ALLOWED_IPV4, "例として 192.0.2.10 を使う"),  # RFC 5737
        (IPV4, ALLOWED_IPV4, "netmask 255.255.255.0"),
        (EXEC_PATH, None, "/var/lib/coldaisle/coldaisle.db"),  # #26 の配置先
        (EXEC_PATH, None, "/etc/systemd/system/coldaisle.service"),
        (EXEC_PATH, None, "/home/runner/work"),  # GitHub Actions
        (EMAIL, ALLOWED_EMAIL, "noreply@anthropic.com"),
        (EMAIL, ALLOWED_EMAIL, "t@example.com"),
        (HOSTNAME, ALLOWED_HOSTNAME, "--host 127.0.0.1 --port 8000"),
        (HOSTNAME, ALLOWED_HOSTNAME, "host: localhost"),
        (HOSTNAME, ALLOWED_HOSTNAME, "the host is unreachable"),  # 散文
        (HOSTNAME, ALLOWED_HOSTNAME, "hostname: ${COLDAISLE_HOST}"),
        (WIFI, None, "WIFI_PASS="),  # 雛形（値が無い）
        (ROM, PLACEHOLDER_ROM, 'rom="28FFFFFFFFFFFF01"'),
        (ROM, PLACEHOLDER_ROM, 'rom="28ffffffffffff01"'),
    ],
)
def test_the_patterns_do_not_catch_legitimate_text(pattern, allow, text):
    """**正当な書き方を止めない。** 止めると、検査を外す圧力になる。"""
    found = [
        match.group(0)
        for match in pattern.finditer(text)
        if allow is None or not allow.search(match.group(0))
    ]
    assert found == [], f"誤検出: {text!r} → {found}"
