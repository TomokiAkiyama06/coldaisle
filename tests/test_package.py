"""雛形が最小構成で機能することの確認。

実装は #3 以降で入るため、ここでは「パッケージが読める」ことと
「hardware マーカーが登録されている」ことだけを確かめる。
"""

import pytest

import coldaisle


def test_package_is_importable():
    assert coldaisle.__version__ == "0.1.0"


@pytest.mark.hardware
def test_hardware_marker_placeholder():
    """実機テストの置き場所。CI は -k "not hardware" でこれを除外する。

    中身は #12 SerialSource / #15 連続運転テストで実装する。
    """
    assert True
