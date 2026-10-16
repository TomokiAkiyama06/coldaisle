"""Workload Hint の単調性ゲートの証明書の**部品の digest**（レイヤ横断）。決定記録 0152 §2.3。

証明書（kind ``hint_certificate``）の ``components`` は 0066 §2.1 (c) が束ねるものを部品ごとの
SHA-256 で持つ。**digest を作る関数はここに1つだけ置き**、認証ランナー・``coldaisle-fand``・
Learned MPC worker が同じ関数を呼ぶ（0152 §2.3。``calibration_offsets`` と同じ扱い）。
``tests/`` 側で digest を計算・上書きしない。

ここにあるのは、入力が設定のスキーマに依らない部品だけ:

- ``source_tree``: 実際に import された ``coldaisle`` パッケージのディレクトリ
- ``lockfile``: ロックファイルの bytes
- ``environment``: 処理系・プラットフォーム・入った配布物の指紋
- ``calibration``: ``RuntimeCalibration`` の2状態（``available`` の実効の offset /
  ``unavailable`` の閉じた code）

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from coldaisle.calibration_offsets import effective_metric_offsets, offsets_sha256

_LENGTH_BYTES = 8
"""長さの符号化の幅（big-endian）。0066 §2.1 (c) のソースツリーの規律と同じ。"""
_EXCLUDED_DIRECTORY = "__pycache__"
_EXCLUDED_SUFFIX = ".pyc"
_SOURCE_TREE_TAG = b"coldaisle-hint-source-tree-v1"
_ENVIRONMENT_TAG = b"coldaisle-hint-environment-v1"
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


@dataclass(frozen=True, slots=True)
class InstalledDistribution:
    """入った配布物の1件。``record`` は ``RECORD`` の bytes（RECORD の無い配布物は指紋を作らない）。

    ``path_rank`` は、この配布物の見つかった ``sys.path`` の項目の**順位**（配布物を持つ項目だけを
    前から 0, 1, … と数えた値）。Python は ``sys.path`` の前の項目から import するので、同じ
    import 名を持つ配布物（同じ名前の2つ・名前の違う2つのどちらも）の項目の順を入れ替えると、
    実行時のコードが変わる。順位を指紋に入れ、項目の順の入れ替えを必ず検出する（PR #349 の P1）。
    項目の path そのものは入れない（同じホストでも起動の仕方で ``sys.path[0]`` が変わるため）。
    """

    name: str
    version: str
    record: bytes
    path_rank: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    """``environment`` の指紋の入力（0066 §2.1 (c) / 0152 §2.3）。"""

    implementation: str
    implementation_version: str
    platform: str
    machine: str
    distributions: tuple[InstalledDistribution, ...]


def _canonical_distribution_name(name: str) -> str:
    # 配布物の名前は区切りと大小を正規化してから並べる（同じ配布物の表記ゆれで順序を変えない）。
    return _DISTRIBUTION_NAME_SEPARATORS.sub("-", name).lower()


def current_environment() -> RuntimeEnvironment:
    """いま動いている処理系と、その ``sys.path`` から見える配布物を集める。"""
    version = sys.implementation.version
    distributions: list[InstalledDistribution] = []
    # sys.path の項目を前から1つずつ見て、配布物を持つ項目に順位を振る（import の優先の順）。
    rank = 0
    for entry in sys.path:
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
    return RuntimeEnvironment(
        implementation=sys.implementation.name,
        implementation_version=(
            f"{version.major}.{version.minor}.{version.micro}.{version.releaselevel}.{version.serial}"
        ),
        platform=sysconfig.get_platform(),
        machine=platform.machine(),
        distributions=tuple(distributions),
    )


def _claim_record_files(owners: dict[str, int], owner: int, record: str) -> None:
    """同じ ``sys.path`` の項目で、2つの配布物が同じファイルを入れていれば ``ValueError``。

    ``owner`` はその項目の中での配布物の通し番号で、名前では見分けない（同じ名前の古い
    ``dist-info`` が残った2つの版も別の配布物として数える）。

    重なったファイルは後から入れた配布物のものが残り、どちらが実行されるかが入れた順で決まる。
    ``RECORD`` の組は入れた順に依らないので、指紋では区別できない。区別できない環境は指紋を
    作らない（fand は照合の失敗として B0 に倒す。PR #349 の P1）。``__pycache__`` / ``*.pyc`` は
    実行時に作り直されるので比べない（0152 §2.3 のソースツリーと同じ除外）。
    """
    for row in csv.reader(io.StringIO(record)):
        if not row or not row[0]:
            continue
        path = os.path.normpath(row[0])
        if path.endswith(_EXCLUDED_SUFFIX) or _EXCLUDED_DIRECTORY in Path(path).parts:
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


def environment_bytes(environment: RuntimeEnvironment) -> bytes:
    """``environment`` の digest の元になる列。配布物は ``sys.path`` の順位で並べる。

    各配布物は「``sys.path`` の項目の順位（``path_rank``）・正規化した名前・版・``RECORD`` の
    SHA-256」。項目の順位の順、同じ項目の中では名前の順に並べる（同じ項目の中の
    列挙の順はファイルシステムに依るので効かせない）。同じ名前が2つ見えていれば2件とも入れる。

    **入ったファイルそのものは読まない。** 束ねるのは 0066 §2.1 (c) / 0152 §2.3 の定義どおり
    ``RECORD`` の bytes（各ファイルの hash の一覧）までで、入ったファイルを読み直して ``RECORD`` と
    照合はしない。``RECORD`` を変えずにファイルだけを書き換えた改変は、この指紋では検出しない
    （インストール先は root の所有。docs/ubuntu-deploy.md）。
    """
    entries = sorted(_distribution_parts(item) for item in environment.distributions)
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
