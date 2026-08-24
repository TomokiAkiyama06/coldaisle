"""品質フラグの判定（要件 §5.3、spec-review C-02 / C-04）。

「値そのものから判定する」のが原則。デバイスの `err` は補助情報であり、
判定の根拠にはしない（決定記録 0003 §2.9）。

判定規則は**センサーの種類ごとに違う**。DS18B20 の番兵値と測定範囲を
すべてのメトリクスへ当てると、`gpu.0.core` のちょうど 85℃ や
`power.wall` の 500W が `suspect` になり、正常値が統計から消える。
ドメインで選ぶ（決定記録 0004 §2.13）。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle.store.models import Quality

DS18B20_DISCONNECTED_C = -127.0
"""1-Wire に応答が無いときに DallasTemperature が返す値。

センサーのプロトコル上の定数であり運用で変える値ではないため、設定にしない。
"""

DS18B20_POWER_ON_RESET_C = 85.0
"""DS18B20 スクラッチパッドのパワーオンリセット値（spec-review C-02）。

`-127.00` と違い**排気温度としてありえる値**なので、見逃すと誤警報ではなく
誤った安心を生む。ちょうど 85.00 のときだけを疑う。
"""

AIR_DOMAIN_PREFIX = "air."
"""外付けセンサー（DS18B20 / AM2320）が測る空気の状態（決定記録 0002 §2.1）。

このドメインだけが v1 で判定規則を持つ。`gpu.*` / `cpu.*` / `power.*` の
規則は、実機で値の分布が分かってから #34 で決める。
"""

HUMIDITY_METRIC_SUFFIX = "_humidity"

ROOM_TEMPERATURE_METRIC = "air.room"


class QualityRules(BaseModel):
    """値の妥当性判定に使うしきい値。

    **既定値を持たない。** `config/quality.yaml` が唯一の情報源であり、
    省略した呼び出しが黙って安全側でないしきい値で動くことを防ぐ
    （AGENTS.md ルール6、決定記録 0004 §2.12）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_ms: int = Field(gt=0)
    """この時間だけ更新が無ければ `stale`（要件 §5.3）。"""

    room_temp_min_c: float
    room_temp_max_c: float
    """室温として物理的にありえない範囲（spec-review C-04）。"""

    humidity_min_pct: float
    humidity_max_pct: float
    """この値に張り付いたら配線異常を疑う（spec-review C-04）。範囲外ではなく境界含みで判定する。"""

    sensor_min_c: float
    sensor_max_c: float
    """DS18B20 のデータシート上の測定範囲。外れた値は物理的にありえない。"""

    @classmethod
    def from_yaml(cls, path: Path) -> QualityRules:
        """設定ファイルから読む。欠けたキー・余分なキーはここで落とす。

        ファイルが無ければ例外を送出する。**既定値へ黙って落ちない。**
        しきい値が読めていないことに気づかないまま運用が始まるほうが危険。
        """
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"しきい値の設定が辞書ではない: {path}")
        return cls.model_validate(loaded)


def classify(metric: str, value: float | None, rules: QualityRules) -> Quality:
    """1つの測定値の品質を決める。

    `stale` はここでは返さない。前回受信からの経過で決まるため、
    保存時ではなく読み出し時に判定する（`SqliteStore.latest`）。
    """
    if value is None or math.isnan(value):
        return Quality.MISSING
    if not math.isfinite(value):
        # 決定記録 0003 §2.8。取り込み層で弾くのが本筋だが、ここでも落とす
        return Quality.SUSPECT
    if metric.endswith(HUMIDITY_METRIC_SUFFIX):
        if value <= rules.humidity_min_pct or value >= rules.humidity_max_pct:
            return Quality.SUSPECT
        return Quality.OK
    if not metric.startswith(AIR_DOMAIN_PREFIX):
        # NVML / lm-sensors 由来のメトリクス。判定規則は #34 で決める。
        # ここで DS18B20 の規則を当てると正常値を suspect にする
        return Quality.OK
    if value in (DS18B20_DISCONNECTED_C, DS18B20_POWER_ON_RESET_C):
        return Quality.SUSPECT
    if value < rules.sensor_min_c or value > rules.sensor_max_c:
        return Quality.SUSPECT
    if metric == ROOM_TEMPERATURE_METRIC and not (
        rules.room_temp_min_c <= value <= rules.room_temp_max_c
    ):
        return Quality.SUSPECT
    return Quality.OK
