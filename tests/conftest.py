"""テスト共通の下ごしらえ。"""

from pathlib import Path

import pytest

from coldaisle.store import QualityRules

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
QUALITY_RULES_PATH = CONFIG_DIR / "quality.yaml"


@pytest.fixture(scope="session")
def rules() -> QualityRules:
    """**本番と同じ設定ファイル**を読む。

    テスト専用のしきい値を置くと、設定ファイル側が壊れていても緑になる。
    値そのものを変えたいテストは `rules.model_copy(update=...)` を使う。
    """
    return QualityRules.from_yaml(QUALITY_RULES_PATH)
