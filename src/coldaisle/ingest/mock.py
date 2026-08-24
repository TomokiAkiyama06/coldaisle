"""MockSource: 合成データ生成器（#6）。

実機が無い期間に L1〜L4 を作り切るための土台。**シナリオは
`config/scenarios.yaml` が唯一の定義**で、ここに状況ごとの分岐を書かない。
分岐をコードへ足すと、再現したい状況が増えるたびに実装とテストの両方が
変わり、「どのシナリオが何を再現するのか」がコードを読まないと分からなくなる。

同じ `seed` なら出力は完全に一致する（受入基準）。時間圧縮は待ち時間だけに
効き、値には影響しない。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from coldaisle.ingest.protocol import RawHello, RawMessage, RawSample, RawSensor

BOOT_UP_MS = 1_200
"""起動バナーを出してから最初のサンプルまでのデバイス稼働ミリ秒。

再起動の検出（FR-106）は「`up` が巻き戻ったか」で行うため、
リセット後にここへ戻ることが試験の対象になる。
"""

MOCK_DEVICE = "xiao-esp32s3-mock"
"""実機と区別できる `dev`。DB の `devices` に紛れても取り違えないため。"""

_DUMMY_ROM_PREFIX = "28FFFFFFFFFFFF"
"""ダミーの ROM ID。**実機の ROM ID をコードへ書かない**（#41）。"""

_DS18B20_CHANNELS = ("front_intake", "gpu_intake", "gpu_exhaust", "top_exhaust", "rear_exhaust")


class Drift(BaseModel):
    """`start_s` から `end_s` にかけて `delta_c` まで直線的にずれ、以降は保つ。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["drift"]
    channel: str
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    delta_c: float

    def offset_at(self, elapsed_s: float) -> float:
        if elapsed_s <= self.start_s:
            return 0.0
        if elapsed_s >= self.end_s:
            return self.delta_c
        return self.delta_c * (elapsed_s - self.start_s) / (self.end_s - self.start_s)


class StuckValue(BaseModel):
    """`start_s` 以降、チャネルを固定値にする。`value: null` なら欠測。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stuck_value"]
    channel: str
    start_s: float = Field(ge=0)
    value: float | None
    err: str | None = None
    """`<channel>:<reason>`。決定記録 0003 §2.9 の書式。"""


class DeviceReset(BaseModel):
    """`at_s` でデバイスが再起動する。`up` が巻き戻り `seq` が 0 に戻る。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["device_reset"]
    at_s: float = Field(gt=0)


class Dropout(BaseModel):
    """`start_s` から `end_s` の間、ホストへ届かない。

    デバイスは動き続けるため `seq` は進む。受信側から見ると `seq` が飛ぶ。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["dropout"]
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)

    def covers(self, elapsed_s: float) -> bool:
        return self.start_s <= elapsed_s < self.end_s


Effect = Annotated[Drift | StuckValue | DeviceReset | Dropout, Field(discriminator="kind")]


class Baseline(BaseModel):
    """異常が無いときの値。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    room_c: float
    room_humidity_pct: float
    noise_c: float = Field(ge=0)
    noise_pct: float = Field(ge=0)
    offsets_c: dict[str, float]
    """室温からの定常オフセット。ここに無いチャネルは室温そのものになる。"""


class Scenario(BaseModel):
    """1つの状況。`config/scenarios.yaml` の1エントリに対応する。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str
    interval_ms: int = Field(gt=0)
    duration_s: float | None = Field(gt=0)
    """`None` なら終端がない。デーモンを流し続ける用途（`idle`）。"""
    seed: int
    baseline: Baseline
    effects: tuple[Effect, ...] = ()


def load_scenarios(path: Path) -> dict[str, Scenario]:
    """シナリオ定義を読む。`defaults` を各シナリオへ流し込んでから検証する。

    キーの過不足はここで落とす（`extra="forbid"`）。効果の綴り違いを
    黙って無視すると、**何も起きないシナリオが正常に見える。**
    """
    document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "scenarios" not in document:
        raise ValueError(f"シナリオ定義の形が違う（`scenarios` が無い）: {path}")

    defaults: dict[str, Any] = document.get("defaults") or {}
    scenarios: dict[str, Scenario] = {}
    for name, raw in document["scenarios"].items():
        merged = {**defaults, **(raw or {})}
        baseline = {**defaults.get("baseline", {}), **(raw or {}).get("baseline", {})}
        merged["baseline"] = baseline
        scenarios[name] = Scenario.model_validate(merged)
    return scenarios


class MockSource:
    """`Source` の合成データ実装。

    `speed` は待ち時間だけを縮める。`--speed 60` なら1分を1秒で流すが、
    生成される値と `up` / `seq` は実時間で動かした場合と同一になる。
    値まで変わると、時間圧縮したテストと実運用で挙動が違うことになる。
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        seed: int | None = None,
        speed: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if speed <= 0:
            raise ValueError(f"speed は正の数（1分を1秒にするなら 60）: {speed}")
        self._scenario = scenario
        self._seed = scenario.seed if seed is None else seed
        self._speed = speed
        self._sleep = sleep

    @property
    def hello(self) -> RawHello:
        """起動バナー。`interval_ms` は `expected_count`（決定記録 0002 §2.8）の元になる。"""
        sensors: dict[str, RawSensor] = {
            channel: RawSensor(
                kind="ds18b20",
                gpio=index + 1,
                rom=f"{_DUMMY_ROM_PREFIX}{index + 1:02X}",
                res=11,  # spec-review C-01
            )
            for index, channel in enumerate(_DS18B20_CHANNELS)
        }
        sensors["room"] = RawSensor(kind="am2320")
        return RawHello(
            fw="0.0.0-mock",
            dev=MOCK_DEVICE,
            interval_ms=self._scenario.interval_ms,
            sensors=sensors,
        )

    def stream(self) -> Iterator[RawMessage]:
        scenario = self._scenario
        rng = random.Random(self._seed)
        interval_s = scenario.interval_ms / 1000
        resets = sorted(
            (effect for effect in scenario.effects if isinstance(effect, DeviceReset)),
            key=lambda effect: effect.at_s,
        )

        yield self.hello
        seq = 0
        up_ms = BOOT_UP_MS
        step = 0
        while scenario.duration_s is None or step * interval_s < scenario.duration_s:
            elapsed_s = step * interval_s
            if step:
                self._sleep(interval_s / self._speed)

            if resets and elapsed_s >= resets[0].at_s:
                resets.pop(0)
                seq = 0
                up_ms = BOOT_UP_MS
                yield self.hello

            # 乱数はドロップアウト中も引く。デバイスは測り続けており、
            # 「届かなかっただけ」で以降の値が変わってはいけない
            sample = self._sample(elapsed_s, seq, up_ms, rng)
            if not any(
                effect.covers(elapsed_s)
                for effect in scenario.effects
                if isinstance(effect, Dropout)
            ):
                yield sample

            seq += 1
            up_ms += scenario.interval_ms
            step += 1

    def _sample(self, elapsed_s: float, seq: int, up_ms: int, rng: random.Random) -> RawSample:
        baseline = self._scenario.baseline
        room_c = baseline.room_c + rng.gauss(0.0, baseline.noise_c)
        channels: dict[str, float | None] = {
            "room_temp": round(room_c, 2),
            "room_humidity": round(
                baseline.room_humidity_pct + rng.gauss(0.0, baseline.noise_pct), 2
            ),
        }
        for channel in _DS18B20_CHANNELS:
            value = (
                room_c
                + baseline.offsets_c.get(channel, 0.0)
                + self._drift(channel, elapsed_s)
                + rng.gauss(0.0, baseline.noise_c)
            )
            channels[channel] = round(value, 2)

        errors: list[str] = []
        for effect in self._scenario.effects:
            if isinstance(effect, StuckValue) and elapsed_s >= effect.start_s:
                channels[effect.channel] = effect.value
                if effect.err is not None:
                    errors.append(effect.err)
        return RawSample(seq=seq, up=up_ms, channels=channels, err=tuple(errors))

    def _drift(self, channel: str, elapsed_s: float) -> float:
        return sum(
            effect.offset_at(elapsed_s)
            for effect in self._scenario.effects
            if isinstance(effect, Drift) and effect.channel == channel
        )
