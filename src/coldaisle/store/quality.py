"""品質フラグの判定（要件 §5.3、spec-review C-02 / C-04）。

「値そのものから判定する」のが原則。デバイスの `err` は補助情報であり、
判定の根拠にはしない（決定記録 0003 §2.9）。
"""

from __future__ import annotations

import math

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

HUMIDITY_METRIC_SUFFIX = "_humidity"


class QualityRules(BaseModel):
    """値の妥当性判定に使うしきい値。

    既定値は要件 §5.3 と spec-review C-04 に由来する。
    実行時は設定から読む（#8）。ここに直書きしない。
    """

    model_config = ConfigDict(frozen=True)

    stale_after_ms: int = Field(default=10_000, gt=0)
    """この時間だけ更新が無ければ `stale`（要件 §5.3）。"""

    room_temp_min_c: float = 0.0
    room_temp_max_c: float = 60.0
    """室温として物理的にありえない範囲（spec-review C-04）。"""

    humidity_min_pct: float = 0.0
    humidity_max_pct: float = 100.0
    """この値に張り付いたら配線異常を疑う（spec-review C-04）。範囲外ではなく境界含みで判定する。"""

    sensor_min_c: float = -55.0
    sensor_max_c: float = 125.0
    """DS18B20 のデータシート上の測定範囲。外れた値は物理的にありえない。"""


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
    if value in (DS18B20_DISCONNECTED_C, DS18B20_POWER_ON_RESET_C):
        return Quality.SUSPECT
    if value < rules.sensor_min_c or value > rules.sensor_max_c:
        return Quality.SUSPECT
    if metric == "air.room" and not (rules.room_temp_min_c <= value <= rules.room_temp_max_c):
        return Quality.SUSPECT
    return Quality.OK
