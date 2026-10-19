"""Workload Hint の単調性ゲートの証明書の**部品の digest**（レイヤ横断）。決定記録 0152 §2.3。

証明書（kind ``hint_certificate``）の ``components`` は 0066 §2.1 (c) が束ねるものを部品ごとの
SHA-256 で持つ。**digest を作る関数はここに1つだけ置き**、認証ランナー・``coldaisle-fand``・
Learned MPC worker が同じ関数を呼ぶ（0152 §2.3。``calibration_offsets`` と同じ扱い）。
``tests/`` 側で digest を計算・上書きしない。

ここにあるのは、入力が設定のスキーマに依らない部品だけ:

- ``source_tree``: 実際に import された ``coldaisle`` パッケージのディレクトリ
- ``lockfile``: ロックファイルの bytes
- ``environment``: 処理系・プラットフォーム・入った配布物・interpreter のフラグと実体・
  ``sys.path`` の全項目（位置・path・中身）の指紋（0157 §2.2）
- ``calibration``: ``RuntimeCalibration`` の2状態（``available`` の実効の offset /
  ``unavailable`` の閉じた code）

起動の形の判定 :func:`launch_form`（``isolated_module`` / ``other``。0157 §2.3）もここに置く。
``other`` のプロセスは ``environment`` を計算せず、照合を ``environment_mismatch`` に倒す
（呼び出し側の fand・worker・認証ランナーは後の段で足す）。

``hint_policy`` / ``hint_gate`` / ``optimizer`` / ``conditions`` と ``certificate_sha256`` は、
ゲートの仕様（格子の定義と ``eps`` の設定）と合わせて後の段で足す。

**例外を握りつぶさない。** 読めない・symlink を含む・非有限の offset は ``ValueError`` /
``OSError`` のまま呼び出し側へ返す。fand はそれを照合の失敗（B0）として扱う（0152 §2.4。
起動は止めない）。ここは ``coldaisle.control`` を import しない（Critical Safety・Reactive Guard
とも無関係で、制御の値を1つも作らない）。
"""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import io
import os
import platform
import re
import stat
import sys
import sysconfig
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from coldaisle.calibration_offsets import effective_metric_offsets, offsets_sha256

_LENGTH_BYTES = 8
"""長さの符号化の幅（big-endian）。0066 §2.1 (c) のソースツリーの規律と同じ。"""
_EXCLUDED_DIRECTORY = "__pycache__"
_EXCLUDED_SUFFIX = ".pyc"
_SOURCE_TREE_TAG = b"coldaisle-hint-source-tree-v1"
_ENVIRONMENT_TAG = b"coldaisle-hint-environment-v2"
"""v2 で 0157 §2.2 の ``interpreter_flags`` / ``executable`` / ``sys_path`` を足した。"""
_SYS_PATH_TREE_TAG = b"coldaisle-hint-sys-path-tree-v1"
_TREE_FILE = b"file"
_TREE_LINK = b"link"
_LINK_TO_FILE = b"to-file"
_LINK_TO_DIRECTORY = b"to-directory"
_LINK_BROKEN = b"broken"
_CALIBRATION_TAG = b"coldaisle-hint-calibration-v1"
_CALIBRATION_AVAILABLE = b"available"
_CALIBRATION_UNAVAILABLE = b"unavailable"
_DISTRIBUTION_NAME_SEPARATORS = re.compile(r"[-_.]+")


def length_prefixed(parts: Iterable[bytes]) -> bytes:
    """各要素を「長さ（8 バイト big-endian）・内容」で符号化して連結する（0066 §2.1 (c)）。

    区切りの無い連結は、境界をずらした別の列と同じ bytes を作れるため使わない。
    """
    encoded = bytearray()
    for part in parts:
        encoded += len(part).to_bytes(_LENGTH_BYTES, "big")
        encoded += part
    return bytes(encoded)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- source_tree


def imported_package_dir() -> Path:
    """**実際に import された** ``coldaisle`` パッケージのディレクトリ（0152 §2.3）。

    作業ツリーのパスや設定から探さない。fand・worker・ランナーが自分の動かしているコードを束ねる。
    """
    import coldaisle

    init = coldaisle.__file__
    if init is None:
        raise ValueError("coldaisle パッケージの場所を決められない")
    return Path(init).resolve().parent


def _raise(error: OSError) -> None:
    # os.walk は既定で読めないディレクトリを黙って飛ばす。飛ばすとそこのコードが束ねる範囲から
    # 漏れるので、例外のまま返す（fand は照合の失敗として B0 に倒す）。
    raise error


def _source_files(package_dir: Path) -> list[tuple[bytes, Path]]:
    files: list[tuple[bytes, Path]] = []
    for current, directories, names in os.walk(package_dir, onerror=_raise, followlinks=False):
        here = Path(current)
        kept: list[str] = []
        for directory in directories:
            path = here / directory
            if path.is_symlink():
                # 追わないと束ねる範囲が外のディレクトリに依る。黙って飛ばすとそのコードが漏れる。
                raise ValueError(f"ソースツリーに symlink のディレクトリがある: {directory}")
            if directory != _EXCLUDED_DIRECTORY:
                kept.append(directory)
        directories[:] = kept
        for name in names:
            if name.endswith(_EXCLUDED_SUFFIX):
                continue
            path = here / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"ソースツリーに通常のファイルでないものがある: {name}")
            relative = path.relative_to(package_dir).as_posix().encode("utf-8")
            files.append((relative, path))
    files.sort(key=lambda item: item[0])
    return files


def source_tree_bytes(package_dir: Path) -> bytes:
    """``source_tree`` の digest の元になる列（0152 §2.3 / 0066 §2.1 (c)）。

    ``__pycache__`` のディレクトリと ``*.pyc`` を除く全ファイルを、相対パス（POSIX 区切りの
    UTF-8）の bytes の昇順に「相対パスの長さ・相対パス・内容の長さ・内容」と符号化して連結する。
    symlink・通常のファイルでないものがあれば ``ValueError``（束ねる範囲を外に依らせない）。
    """
    parts: list[bytes] = [_SOURCE_TREE_TAG]
    for relative, path in _source_files(package_dir):
        parts.append(relative)
        parts.append(path.read_bytes())
    return length_prefixed(parts)


def source_tree_sha256(package_dir: Path) -> str:
    """``source_tree`` の部品の SHA-256（小文字16進64文字）。"""
    return _sha256_hex(source_tree_bytes(package_dir))


# ---------------------------------------------------------------- lockfile


def lockfile_sha256(path: Path) -> str:
    """``lockfile`` の部品: ロックファイルの bytes の SHA-256（0152 §2.3）。

    path は起動時の引数で渡す（0152 §2.4 / §5 の 4）。読めなければ ``OSError`` のまま返す。
    """
    return _sha256_hex(path.read_bytes())


# ---------------------------------------------------------------- environment


class LaunchForm(StrEnum):
    """起動の形の閉じた語彙（0157 §2.3）。``other`` のプロセスは指紋を計算しない。"""

    ISOLATED_MODULE = "isolated_module"
    OTHER = "other"


class SysPathKind(StrEnum):
    """``sys.path`` の項目の種類（0157 §2.2）。"""

    DIRECTORY = "directory"
    FILE = "file"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class InstalledDistribution:
    """入った配布物の1件。``record`` は ``RECORD`` の bytes（RECORD の無い配布物は指紋を作らない）。

    ``path_rank`` は、この配布物の見つかった ``sys.path`` の項目の**順位**（配布物を持つ項目だけを
    前から 0, 1, … と数えた値）。Python は ``sys.path`` の前の項目から import するので、同じ
    import 名を持つ配布物（同じ名前の2つ・名前の違う2つのどちらも）の項目の順を入れ替えると、
    実行時のコードが変わる。順位を指紋に入れ、項目の順の入れ替えを必ず検出する（PR #349 の P1）。
    配布物の名前・版・``RECORD`` は、照合の理由で何が変わったかを名指すために残す（0157 §2.2）。
    項目の位置・path・中身は :class:`SysPathEntry` が別に束ねる。
    """

    name: str
    version: str
    record: bytes
    path_rank: int = 0


@dataclass(frozen=True, slots=True)
class SysPathEntry:
    """``sys.path`` の1項目（0157 §2.2）。

    ``path`` は項目の絶対 path、``real_path`` はその ``realpath``（項目そのものが symlink のときに
    両方を束ねる）。中身は ``real_path`` の先で読む。``content_sha256`` はディレクトリなら木の
    digest、ファイルなら bytes の SHA-256、無い項目なら空。path は指紋の**入力**にだけ使い、
    証明書・trace・ログには digest しか出さない（ルール 10）。
    """

    position: int
    path: str
    real_path: str
    kind: SysPathKind
    content_sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """``environment`` の指紋の入力（0066 §2.1 (c) / 0152 §2.3 / 0157 §2.2）。

    ``interpreter_flags`` は ``sys.flags`` の全欄（``flags.<名前>``）と ``sys._xoptions``
    （``xoptions.<名前>``）の組。``executable`` は ``sys.executable`` の path、
    ``executable_sha256`` はその実体（``realpath``）の bytes の SHA-256。
    """

    implementation: str
    implementation_version: str
    platform: str
    machine: str
    distributions: tuple[InstalledDistribution, ...]
    interpreter_flags: tuple[tuple[str, str], ...]
    executable: str
    executable_sha256: str
    sys_path: tuple[SysPathEntry, ...]


def _canonical_distribution_name(name: str) -> str:
    # 配布物の名前は区切りと大小を正規化してから並べる（同じ配布物の表記ゆれで順序を変えない）。
    return _DISTRIBUTION_NAME_SEPARATORS.sub("-", name).lower()


def launch_form(
    entry_module: str,
    *,
    isolated: bool,
    safe_path: bool,
    main_spec_name: str | None,
    orig_argv: Sequence[str],
    executable: str,
) -> LaunchForm:
    """起動の形を判定する（0157 §2.3）。次の**すべて**を満たすときだけ ``isolated_module``。

    1. ``isolated`` と ``safe_path`` が真（``-I``）
    2. ``__main__.__spec__.name`` が ``entry_module``（そのプロセスの入口の module）と一致する
    3. ``orig_argv`` が ``[executable, "-I", "-m", entry_module, ...]`` の形で、``-m`` の前に
       ``-I`` 以外の interpreter の引数が無い（``-m`` で起動した別の module から入口を呼ぶ形を
       数えない）

    どれか1つでも外れれば ``other``。``entry_module`` は呼び出し側のコードが定数で持つ（設定から
    読まない）。
    """
    if not entry_module:
        raise ValueError("入口の module を空にしない")
    if not (isolated and safe_path):
        return LaunchForm.OTHER
    if main_spec_name != entry_module:
        return LaunchForm.OTHER
    if not executable or list(orig_argv[:4]) != [executable, "-I", "-m", entry_module]:
        return LaunchForm.OTHER
    return LaunchForm.ISOLATED_MODULE


def current_launch_form(entry_module: str) -> LaunchForm:
    """いま動いているプロセスの起動の形（:func:`launch_form` に実際の値を渡す）。"""
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    name = getattr(spec, "name", None)
    return launch_form(
        entry_module,
        isolated=bool(sys.flags.isolated),
        safe_path=bool(sys.flags.safe_path),
        main_spec_name=name if isinstance(name, str) else None,
        orig_argv=tuple(sys.orig_argv),
        executable=sys.executable,
    )


def _interpreter_flags() -> tuple[tuple[str, str], ...]:
    # sys.flags の全欄を名前つきで（__match_args__ は名前のある欄の全部）。
    # 値は repr で型ごと束ねる。
    items = [
        (f"flags.{name}", repr(getattr(sys.flags, name))) for name in type(sys.flags).__match_args__
    ]
    items.extend((f"xoptions.{key}", repr(value)) for key, value in sys._xoptions.items())
    return tuple(sorted(items))


def _file_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _tree_records(
    root: str, relative: str, records: list[tuple[bytes, list[bytes]]], follow_directory_links: bool
) -> None:
    # os.scandir は読めないディレクトリで例外を投げる。そのまま返す（束ねる範囲から漏らさない）。
    with os.scandir(os.path.join(root, relative) if relative else root) as listing:
        children = list(listing)
    for child in children:
        name = child.name
        rel = f"{relative}/{name}" if relative else name
        rel_bytes = os.fsencode(rel)
        mode = os.lstat(child.path).st_mode
        if stat.S_ISDIR(mode):
            if name == _EXCLUDED_DIRECTORY:
                continue
            _tree_records(root, rel, records, follow_directory_links)
        elif stat.S_ISREG(mode):
            records.append((rel_bytes, [_TREE_FILE, rel_bytes, _file_sha256(child.path).encode()]))
        elif stat.S_ISLNK(mode):
            records.append((rel_bytes, _link_parts(child.path, rel_bytes, follow_directory_links)))
        else:
            raise ValueError(f"sys.path の項目に通常のファイルでないものがある: {rel}")


def _link_parts(path: str, rel_bytes: bytes, follow_directory_links: bool) -> list[bytes]:
    """木の中の symlink を「印・相対パス・リンクの文字列・先の状態」で符号化する（0157 §2.2）。"""
    parts = [_TREE_LINK, rel_bytes, os.fsencode(os.readlink(path))]
    try:
        target = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        # 壊れたリンクは import されない。中身は無いが、リンクの文字列は束ねる。
        return [*parts, _LINK_BROKEN]
    if stat.S_ISREG(target.st_mode):
        # 先が項目の外でも中身を束ねる（import されうるのは先の中身）。
        return [*parts, _LINK_TO_FILE, _file_sha256(path).encode()]
    if stat.S_ISDIR(target.st_mode):
        if not follow_directory_links:
            # 辿った先の木の中の symlink のディレクトリは辿らない。循環と深さの規則を作らない。
            raise ValueError("辿った symlink の先にさらに symlink のディレクトリがある")
        return [
            *parts,
            _LINK_TO_DIRECTORY,
            _tree_sha256(path, follow_directory_links=False).encode(),
        ]
    raise ValueError("symlink の先が通常のファイルでもディレクトリでもない")


def _tree_sha256(root: str, *, follow_directory_links: bool) -> str:
    """``sys.path`` の項目のディレクトリの木の digest（0157 §2.2）。

    ``source_tree`` と同じ規律（相対パスの bytes の昇順・長さ付き・``__pycache__`` のディレクトリを
    除く）で、ファイルは中身の SHA-256 を束ねる（site-packages を丸ごと連結して持たない）。
    ``__pycache__`` の外の ``*.pyc`` は、ソースの無い module として import されうるので除かない。
    """
    records: list[tuple[bytes, list[bytes]]] = []
    _tree_records(root, "", records, follow_directory_links)
    records.sort(key=lambda item: item[0])
    parts: list[bytes] = [_SYS_PATH_TREE_TAG, str(len(records)).encode("ascii")]
    for _, encoded in records:
        parts.append(length_prefixed(encoded))
    return _sha256_hex(length_prefixed(parts))


def sys_path_entry(position: int, entry: str) -> SysPathEntry:
    """``sys.path`` の1項目を、位置・絶対 path・``realpath``・種類・中身の digest にする。

    0157 §2.2。

    読めないディレクトリ・通常のファイルでもディレクトリでもない項目は例外（fand は照合の失敗
    として B0 に倒す）。
    """
    path = os.path.abspath(entry)
    real_path = os.path.realpath(path)
    try:
        mode = os.stat(real_path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return SysPathEntry(position, path, real_path, SysPathKind.MISSING, "")
    if stat.S_ISDIR(mode):
        content = _tree_sha256(real_path, follow_directory_links=True)
        return SysPathEntry(position, path, real_path, SysPathKind.DIRECTORY, content)
    if stat.S_ISREG(mode):
        return SysPathEntry(position, path, real_path, SysPathKind.FILE, _file_sha256(real_path))
    raise ValueError(f"sys.path の項目が通常のファイルでもディレクトリでもない: 位置 {position}")


def current_environment(sys_path: Sequence[str] | None = None) -> RuntimeEnvironment:
    """いま動いている処理系と、その ``sys.path`` から見える配布物・項目の中身を集める。

    ``sys_path`` を省けば ``sys.path`` を使う（試験は偽の列を渡す。0157 §2.4）。
    呼び出し側は、:func:`current_launch_form` が ``other`` のプロセスでは呼ばない（0157 §2.3）。
    標準ライブラリと site-packages の全ファイルを読むので、起動時に1回だけ呼ぶ。
    """
    entries = list(sys.path if sys_path is None else sys_path)
    version = sys.implementation.version
    distributions: list[InstalledDistribution] = []
    # sys.path の項目を前から1つずつ見て、配布物を持つ項目に順位を振る（import の優先の順）。
    rank = 0
    for entry in entries:
        found = False
        owners: dict[str, int] = {}
        for owner, distribution in enumerate(importlib.metadata.distributions(path=[entry])):
            name = distribution.metadata["Name"]
            if not name:
                raise ValueError("名前の無い配布物がある")
            record = distribution.read_text("RECORD")
            if record is None:
                # RECORD の無い配布物は、入ったファイルを変えても指紋が変わらない。指紋を作らない
                # （fand は照合の失敗として B0 に倒す。PR #349 の P1）。
                raise ValueError(f"RECORD の無い配布物がある: {name}")
            _claim_record_files(owners, owner, record)
            distributions.append(
                InstalledDistribution(
                    name=str(name),
                    version=distribution.version,
                    record=record.encode("utf-8"),
                    path_rank=rank,
                )
            )
            found = True
        if found:
            rank += 1
    executable = sys.executable
    if not executable:
        raise ValueError("sys.executable を決められない")
    return RuntimeEnvironment(
        implementation=sys.implementation.name,
        implementation_version=(
            f"{version.major}.{version.minor}.{version.micro}.{version.releaselevel}.{version.serial}"
        ),
        platform=sysconfig.get_platform(),
        machine=platform.machine(),
        distributions=tuple(distributions),
        interpreter_flags=_interpreter_flags(),
        executable=executable,
        executable_sha256=_file_sha256(os.path.realpath(executable)),
        sys_path=tuple(sys_path_entry(position, entry) for position, entry in enumerate(entries)),
    )


def _claim_record_files(owners: dict[str, int], owner: int, record: str) -> None:
    """同じ ``sys.path`` の項目で、2つの配布物が同じファイルを入れていれば ``ValueError``。

    ``owner`` はその項目の中での配布物の通し番号で、名前では見分けない（同じ名前の古い
    ``dist-info`` が残った2つの版も別の配布物として数える）。

    重なったファイルは後から入れた配布物のものが残り、どちらが実行されるかが入れた順で決まる。
    ``RECORD`` の組は入れた順に依らないので、指紋では区別できない。区別できない環境は指紋を
    作らない（fand は照合の失敗として B0 に倒す。PR #349 の P1）。比べないのは ``__pycache__`` の
    下（ソースから作り直される cache）だけ。それ以外の ``*.pyc`` はソースの無い module として
    import されうるので、重なれば拒む。
    """
    for row in csv.reader(io.StringIO(record)):
        if not row or not row[0]:
            continue
        path = os.path.normpath(row[0])
        if _EXCLUDED_DIRECTORY in Path(path).parts:
            continue
        if owners.setdefault(path, owner) != owner:
            raise ValueError(f"同じ sys.path の項目で2つの配布物が同じファイルを入れている: {path}")


def _distribution_parts(
    distribution: InstalledDistribution,
) -> tuple[bytes, bytes, bytes, bytes]:
    if distribution.path_rank < 0:
        raise ValueError("path_rank は 0 以上")
    return (
        # 10 進の固定幅にして、並べ替えの順を path_rank の大小と一致させる。
        f"{distribution.path_rank:010d}".encode("ascii"),
        _canonical_distribution_name(distribution.name).encode("utf-8"),
        distribution.version.encode("utf-8"),
        _sha256_hex(distribution.record).encode("ascii"),
    )


def _sys_path_parts(entry: SysPathEntry) -> list[bytes]:
    if entry.position < 0:
        raise ValueError("sys.path の位置は 0 以上")
    if (entry.kind is SysPathKind.MISSING) != (not entry.content_sha256):
        raise ValueError("無い項目だけが中身の digest を持たない")
    return [
        f"{entry.position:010d}".encode("ascii"),
        os.fsencode(entry.path),
        os.fsencode(entry.real_path),
        entry.kind.value.encode("ascii"),
        entry.content_sha256.encode("ascii"),
    ]


def environment_bytes(environment: RuntimeEnvironment) -> bytes:
    """``environment`` の digest の元になる列（0152 §2.3 / 0157 §2.2）。

    1. 処理系の実装と版・プラットフォーム・``platform.machine()``
    2. 配布物: 「``sys.path`` の項目の順位（``path_rank``）・正規化した名前・版・``RECORD`` の
       SHA-256」を、項目の順位の順、同じ項目の中では名前の順に（列挙の順はファイルシステムに
       依るので効かせない）。同じ名前が2つ見えていれば2件とも入れる
    3. ``interpreter_flags``（鍵の昇順）
    4. ``executable`` の path と実体の SHA-256
    5. ``sys.path`` の**全項目を位置の順に**: 位置・絶対 path・``realpath``・種類・中身の digest。
       site-packages も中身を読むので、``RECORD`` に載らないファイル（``.pth`` など）と
       ``RECORD`` を変えずに書き換えたファイルも入る
    """
    entries = sorted(_distribution_parts(item) for item in environment.distributions)
    flags = sorted(environment.interpreter_flags)
    path_entries = sorted(environment.sys_path, key=lambda item: item.position)
    if [item.position for item in path_entries] != list(range(len(path_entries))):
        raise ValueError("sys.path の位置は 0 から欠けずに並ぶ")
    if not environment.executable or not environment.executable_sha256:
        raise ValueError("executable とその digest を空にしない")
    parts: list[bytes] = [
        _ENVIRONMENT_TAG,
        environment.implementation.encode("utf-8"),
        environment.implementation_version.encode("utf-8"),
        environment.platform.encode("utf-8"),
        environment.machine.encode("utf-8"),
        str(len(entries)).encode("ascii"),
    ]
    for entry in entries:
        parts.extend(entry)
    parts.append(str(len(flags)).encode("ascii"))
    for key, value in flags:
        parts.extend((key.encode("utf-8"), value.encode("utf-8")))
    parts.extend((os.fsencode(environment.executable), environment.executable_sha256.encode()))
    parts.append(str(len(path_entries)).encode("ascii"))
    for path_entry in path_entries:
        parts.append(length_prefixed(_sys_path_parts(path_entry)))
    return length_prefixed(parts)


def environment_sha256(environment: RuntimeEnvironment | None = None) -> str:
    """``environment`` の部品の SHA-256。省けば :func:`current_environment` を使う。"""
    return _sha256_hex(environment_bytes(environment or current_environment()))


# ---------------------------------------------------------------- calibration


def calibration_available_sha256(offsets_c: Mapping[str, float]) -> str:
    """``calibration`` の部品（``available``）。0152 §2.3。

    較正を当てる全 metric の実効の offset（``effective_metric_offsets``）の ``offsets_sha256`` を、
    状態の印と組にして符号化した列の SHA-256。artifact の
    ``metric_binding`` に依らない較正の全体で束ねる（L9 の digest とは別。0152 §2.3）。
    空の ``offsets_c`` も正当な較正で、``unavailable`` とは違う値になる（0096 §2.6）。
    """
    digest = offsets_sha256(effective_metric_offsets(offsets_c))
    return _sha256_hex(
        length_prefixed((_CALIBRATION_TAG, _CALIBRATION_AVAILABLE, digest.encode("ascii")))
    )


def calibration_unavailable_sha256(code: str) -> str:
    """``calibration`` の部品（``unavailable``）。``code`` は frame の閉じた code（0101 §2.2）。

    例外の文字列（path を含みうる）を渡さない。呼び出し側は ``CalibrationUnavailableCode`` の
    値を渡す。
    """
    if not code:
        raise ValueError("unavailable の code を空にしない")
    return _sha256_hex(
        length_prefixed((_CALIBRATION_TAG, _CALIBRATION_UNAVAILABLE, code.encode("utf-8")))
    )
