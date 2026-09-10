"""較正オフセット（FR-107）。#8

センサー個体差を補正する値。`config/calibration.json` が唯一の情報源で、
コードに既定値を持たせない（AGENTS.md ルール6）。

**湿度には適用しない。** v1 で較正するのは温度チャネルだけであり
（要件 §5.1 / #13）、%RH に ℃ のオフセットを足す事故を型の側で防ぐ。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class Calibration(BaseModel):
    """チャネルごとの温度オフセット（℃）。

    ここに無いチャネルは補正なし（0.0）として扱う。**欠けていても失敗させない**のは、
    センサーが増えたときに較正値が未測定でも取り込みを続けられるようにするため。
    未測定であることは値が生のままであることとして現れる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    note: str = ""
    """人間向けの覚書。値ではないので判定には使わない。"""

    calibrated_at: str | None = None
    """較正を行った時刻（ISO8601）。**`None` は「まだ測っていない」。**

    黙って古い較正を使い続けないために持つ。`is_expired()` が見る。
    """

    reference: str = ""
    """基準の取り方。`mean_of_all`（spec-review W-02）。**何と比べたかを残す。**"""

    revalidate_after_days: int = Field(default=183, gt=0)
    """この日数を過ぎたら較正をやり直す（#13。既定は約6ヶ月）。

    値の置き場所をこのファイルにしたのは、**「いつまで有効か」が較正の記録の
    一部**だからである。運用の調整つまみではない。
    """

    samples: dict[str, int] = Field(default_factory=dict)
    """チャネルごとの、較正に使った測定の件数。**根拠を残す。**"""

    offsets_c: dict[str, Annotated[float, Field(allow_inf_nan=False)]] = Field(default_factory=dict)
    """非有限値は**読み込み時に**弾く。

    JSON の `1e309` は `json.loads` が `inf` にする。通してしまうと、
    較正後の値が非有限になって `Reading` に拒否され、**そのチャネルを含む
    全サンプルが1件ずつ破棄され続ける。** 設定の誤りは設定を読む時点で言う。
    """

    @classmethod
    def from_json(cls, path: Path) -> Calibration:
        """設定ファイルから読む。ファイルが無ければ例外。

        黙って「補正なし」へ落ちない。較正済みのつもりで生値を保存していると、
        あとから見分ける手段が無い。
        """
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"較正ファイルが辞書ではない: {path}")
        return cls.model_validate(loaded)

    def offset_for(self, channel: str) -> float:
        return self.offsets_c.get(channel, 0.0)

    def age_days(self, now_ms: int) -> float | None:
        """較正からの経過日数。**測っていなければ `None`。**"""
        if self.calibrated_at is None:
            return None
        try:
            at = datetime.fromisoformat(self.calibrated_at)
        except ValueError:
            return None
        if at.tzinfo is None:
            # オフセットの無い時刻は解釈が割れる。**推測しない**
            return None
        return (now_ms / 1000 - at.timestamp()) / 86_400

    def is_expired(self, now_ms: int) -> bool:
        """やり直しが要るか。**未較正も「要る」に含める。**

        センサーは経年でずれる。ずれたオフセットは、**測っていないより悪い**
        （補正済みのつもりで誤った値を見ることになる）。
        """
        age = self.age_days(now_ms)
        return age is None or age > self.revalidate_after_days
