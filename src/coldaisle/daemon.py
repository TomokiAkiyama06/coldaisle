"""取り込みデーモン（合成の起点）。#8 / #18

`Source → Normalizer → Store → Rules` を1つに組み立てて回す。
**シリアルポートを開く唯一のプロセス**（AGENTS.md ルール3、spec-review C-03）。
元の仕様ではモニタとダッシュボードがそれぞれポートを開くため同時起動できなかった。
所有者を1つに集約する。

**`ingest/`（L0）の中に置かない。** ここは L0・L1・L2 を束ねる場所であり、
L0 の部品ではない。中に置くと取り込み層がルールエンジン（L2）を import することに
なり、レイヤの依存が逆向きになる（AGENTS.md「レイヤ間の依存は一方向」）。
`clock` / `channels` / `metrics` と同じく、層をまたぐものはパッケージ直下に置く。

**1サンプルの失敗でプロセスを落とさない。** ここだけは例外を捕まえて継続する
（AGENTS.md コード規約の唯一の例外）。ただし握りつぶさず、件数とスタックを残す。
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from coldaisle import logs
from coldaisle.channels import QUEUE_DROPS_METRIC
from coldaisle.clock import Clock
from coldaisle.ingest import Calibration, MockSource, Normalizer, ReplaySource, load_scenarios
from coldaisle.ingest.protocol import RawHello, RawMessage, RawSample, Source
from coldaisle.metrics import MetricCatalog
from coldaisle.rules import Engine, RuleSet, Transition
from coldaisle.store import (
    DeviceRecord,
    Quality,
    QualityRules,
    Reading,
    Sample,
    SensorRecord,
    SqliteStore,
)

LOGGER = logging.getLogger("coldaisle.ingest")

MAX_LOGGED_DUPLICATES = 10
"""重複を個別に記録する上限。総数は終了時にまとめて出す。"""

QUEUE_SIZE = 256
"""読み取りスレッドとの待ち行列。**有界にする**（常駐メモリ。NFR-05）。"""

INGEST_SOURCE_KEY = "sys.ingest_source"
"""`system_state` のキー。API の `/health` がソース種別として返す（FR-305）。"""

DEFAULT_DB = Path("var/coldaisle.db")
DEFAULT_SCENARIOS = Path("config/scenarios.yaml")
DEFAULT_QUALITY_RULES = Path("config/quality.yaml")
DEFAULT_CALIBRATION = Path("config/calibration.json")
DEFAULT_RULES = Path("config/rules.yaml")
DEFAULT_METRICS = Path("config/metrics.yaml")


@dataclass
class Stats:
    """1回の実行で起きたこと。終了時にまとめてログへ出す。"""

    samples: int = 0
    readings: int = 0
    hellos: int = 0
    discarded: int = 0
    """正規化・保存に失敗して捨てたサンプル数。**0 でない日は原因を追う。**"""
    dropped_samples: int = 0
    """`seq` の飛びの合計（FR-105）。"""
    restarts: int = 0
    """`up` の巻き戻りの回数（FR-106）。"""
    alerts_fired: int = 0
    """発火したアラートの数（FR-401〜409）。"""
    duplicates: int = 0
    """既にある `(metric, ts_ms)` として無視された行数（決定記録 0012 §2.4）。"""
    queue_drops: int = 0
    """待ち行列が溢れて捨てたメッセージ数（決定記録 0012 §2.3）。"""
    unknown_channels: set[str] = field(default_factory=set)

    def as_fields(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "readings": self.readings,
            "hellos": self.hellos,
            "discarded": self.discarded,
            "dropped_samples": self.dropped_samples,
            "restarts": self.restarts,
            "alerts_fired": self.alerts_fired,
            "duplicates": self.duplicates,
            "queue_drops": self.queue_drops,
            "unknown_channels": sorted(self.unknown_channels),
        }


class Daemon:
    """取り込みループ。`run()` は `stream()` が尽きるか停止要求まで戻らない。"""

    def __init__(
        self,
        *,
        source: Source,
        store: SqliteStore,
        normalizer: Normalizer,
        source_name: str = "unknown",
        engine: Engine | None = None,
        tick_s: float = 1.0,
    ) -> None:
        self._source = source
        self._store = store
        self._normalizer = normalizer
        self._source_name = source_name
        self._engine = engine
        self._tick_s = tick_s
        self._stop = False
        self._device_id: str | None = None
        self._reported_drops = 0
        self.stats = Stats()

    @property
    def source(self) -> Source:
        return self._source

    @property
    def store(self) -> SqliteStore:
        return self._store

    @property
    def normalizer(self) -> Normalizer:
        return self._normalizer

    def request_stop(self) -> None:
        """次のメッセージの手前で止める。SIGTERM から呼ぶ。

        **今処理しているサンプルは書き切る。** 途中で落とすと、
        1サンプルの一部だけが保存された時刻ができる（決定記録 0002 §2.3）。
        ソースが待機中の場合、実際に止まるのは次のサンプルが来たときになる。
        """
        self._stop = True

    def run(self, *, max_samples: int | None = None) -> Stats:
        """取り込みループ。

        ソースは**別スレッド**で読む。同じスレッドで読むと `stream()` の待機中は
        何もできず、**サンプルが来ないこと自体を検出できない**（FR-401 の無音は
        30秒サンプルが無いことで判定する）。待ち受けに時間切れを設け、
        空振りのたびに時刻ベースのルールを評価する。

        DB を触るのはこのスレッドだけ。読み取りスレッドはソースを回すだけにする
        （接続はスレッド間で共有しない。決定記録 0004 §2.8）。
        """
        LOGGER.info(
            "取り込みを開始する",
            extra={logs.FIELDS_KEY: {"max_samples": max_samples, "source": self._source_name}},
        )
        # API がソース種別を答えられるようにする（FR-305）。状態は変化時だけ書く
        self._store.set_system_state(
            INGEST_SOURCE_KEY, self._source_name, at_ms=self._normalizer.clock.now_ms()
        )

        # 受信時刻を**受け取った瞬間に**確定して一緒に運ぶ（決定 D-05）。
        # 処理時刻で付けると、追いつくまでの間に進んだぶんだけずれる
        if self._engine is not None:
            # **いつから見ているか**を基準にする。起動時からデバイスが無い場合、
            # これが無いと `SENSOR_FAULT` が永久に鳴らない
            self._engine.begin(self._normalizer.clock.now_ms())

        inbox: queue.Queue[tuple[int, RawMessage] | BaseException | None] = queue.Queue(
            maxsize=QUEUE_SIZE
        )
        reader = threading.Thread(target=self._read_into, args=(inbox,), daemon=True)
        reader.start()
        while True:
            if self._stop:
                LOGGER.info("停止要求を受けた")
                break
            try:
                received = inbox.get(timeout=self._tick_s)
            except queue.Empty:
                self._tick()
                continue
            if received is None:
                break  # ソースが尽きた
            if isinstance(received, BaseException):
                # **ソースが落ちたことを正常終了と区別する。** 同じに扱うと、
                # 途中で死んだ監視をサービス管理（systemd）が再起動できない。
                # 1サンプルの失敗で落とさないこと（AGENTS.md）とは別の話
                LOGGER.error("ソースが落ちた", exc_info=received)
                raise received
            self._handle(*received)
            if max_samples is not None and self.stats.samples >= max_samples:
                break
        LOGGER.info("取り込みを終了する", extra={logs.FIELDS_KEY: self.stats.as_fields()})
        return self.stats

    def _read_into(self, inbox: queue.Queue[tuple[int, RawMessage] | BaseException | None]) -> None:
        """ソースを回して詰めるだけのスレッド。**DB には触らない。**

        ソースが落ちたら例外そのものを渡す。正常終了と同じ印にすると、
        **途中で死んだ監視を完走と区別できない**（サービス管理が再起動できない）。
        """
        try:
            for message in self._source.stream():
                if self._stop:
                    break
                received = (self._source.clock.now_ms(), message)
                try:
                    inbox.put_nowait(received)
                except queue.Full:
                    # **捨てるのは古い側**（決定記録 0004 §2.6 と同じ理由。
                    # 監視で欠けてはならないのは直近）。黙って捨てない
                    with suppress(queue.Empty):
                        inbox.get_nowait()
                    self.stats.queue_drops += 1
                    inbox.put(received)
        except Exception as error:
            inbox.put(error)
        else:
            inbox.put(None)

    def _with_queue_drops(self, sample: Sample) -> Sample:
        """待ち行列の取りこぼしを、次のサンプルに乗せて記録する。

        捨てるのは読み取りスレッドだが、**DB を触るのは取り込みスレッドだけ**
        （決定記録 0004 §2.8）。API は別プロセスなので、`/api/v1/health` から
        見えるようにするには残すしかない（決定記録 0012 §2.3）。
        """
        pending = self.stats.queue_drops - self._reported_drops
        if pending <= 0:
            return sample
        self._reported_drops = self.stats.queue_drops
        LOGGER.warning("待ち行列が溢れた", extra={logs.FIELDS_KEY: {"dropped": pending}})
        return sample.model_copy(
            update={
                "readings": (
                    *sample.readings,
                    Reading(metric=QUEUE_DROPS_METRIC, value=float(pending), quality=Quality.OK),
                )
            }
        )

    def _tick(self) -> None:
        """サンプルが来ないときの評価。無音の検出（FR-401）はここで動く。"""
        if self._engine is None:
            return
        try:
            self._record(self._engine.on_tick())
        except Exception:
            LOGGER.warning("ルールの評価に失敗した", exc_info=True)

    def _record(self, transitions: list[Transition]) -> None:
        self.stats.alerts_fired += sum(1 for t in transitions if t.state == "firing")

    def _handle(self, received_ms: int, message: RawMessage) -> None:
        try:
            if isinstance(message, RawHello):
                self._on_hello(message, received_ms)
            else:
                self._on_sample(message, received_ms)
        except Exception:
            self.stats.discarded += 1
            LOGGER.warning(
                "サンプルを破棄して継続する",
                exc_info=True,
                extra={logs.FIELDS_KEY: {"discarded": self.stats.discarded}},
            )

    def _on_hello(self, hello: RawHello, received_ms: int) -> None:
        # **受け取った瞬間の時刻を使う。** 待ち行列に積まれている間に時計は進む
        at_ms = received_ms
        self._device_id = hello.dev
        recorded = {sensor.channel: sensor.rom for sensor in self._store.sensors_for(hello.dev)}
        observed = {channel: sensor.rom for channel, sensor in hello.sensors.items()}
        mismatched = sorted(
            channel
            for channel, rom in observed.items()
            if channel in recorded and recorded[channel] != rom
        )
        sensors = [
            SensorRecord(
                channel=channel,
                kind=sensor.kind,
                gpio=sensor.gpio,
                rom=sensor.rom,
                resolution=sensor.res,
            )
            for channel, sensor in hello.sensors.items()
        ]
        # **不一致のあいだは記録側を上書きしない。** 記録された ROM は「較正が
        # 対応している構成」であり、人が較正をやり直すまでの基準になる
        # （FR-403 / 決定記録 0012 §2.6）
        self._store.record_hello(
            DeviceRecord(
                device_id=hello.dev,
                fw=hello.fw,
                schema_v=hello.v,
                interval_ms=hello.interval_ms,
            ),
            sensors,
            at_ms=at_ms,
            replace_sensors=not mismatched,
        )
        self.stats.hellos += 1
        LOGGER.info(
            "起動バナーを受け取った",
            extra={
                logs.FIELDS_KEY: {
                    "device": hello.dev,
                    "fw": hello.fw,
                    "interval_ms": hello.interval_ms,
                    "ts_ms": at_ms,
                }
            },
        )
        if mismatched:
            LOGGER.warning(
                "プローブの ROM が記録と違う（較正のやり直しが要る）",
                extra={logs.FIELDS_KEY: {"device": hello.dev, "channels": mismatched}},
            )
        if self._engine is not None:
            self._record(self._engine.on_hello(observed, recorded, at_ms=at_ms))

    def _on_sample(self, raw: RawSample, received_ms: int) -> None:
        normalized = self._normalizer.normalize(raw, ts_ms=received_ms)
        stored_sample = self._with_queue_drops(normalized.sample)
        expected = len(stored_sample.readings)
        written = self._store.insert_sample(stored_sample)
        self.stats.samples += 1
        self.stats.readings += written
        if written < expected:
            # 同じ `(metric, ts_ms)` は無視される（決定記録 0012 §2.4）。
            # **黙って捨てない。** 同じ CSV の二重再生などで起こる
            self.stats.duplicates += expected - written
            if self.stats.duplicates <= MAX_LOGGED_DUPLICATES:
                LOGGER.warning(
                    "既にある時刻の行を書かなかった",
                    extra={
                        logs.FIELDS_KEY: {
                            "ts_ms": normalized.sample.ts_ms,
                            "ignored": expected - written,
                        }
                    },
                )

        if normalized.dropped_samples:
            self.stats.dropped_samples += normalized.dropped_samples
            LOGGER.warning(
                "サンプルの取りこぼしを検出した",
                extra={
                    logs.FIELDS_KEY: {
                        "dropped": normalized.dropped_samples,
                        "seq": raw.seq,
                        "ts_ms": normalized.sample.ts_ms,
                    }
                },
            )
        if normalized.device_restarted:
            self.stats.restarts += 1
            LOGGER.warning(
                "デバイスの再起動を検出した",
                extra={logs.FIELDS_KEY: {"seq": raw.seq, "up": raw.up}},
            )
        if normalized.out_of_order:
            LOGGER.warning("seq が進んでいない", extra={logs.FIELDS_KEY: {"seq": raw.seq}})
        if self._engine is not None:
            self._record(self._engine.on_sample(normalized.sample))
        if normalized.unknown_channels:
            new = set(normalized.unknown_channels) - self.stats.unknown_channels
            self.stats.unknown_channels |= set(normalized.unknown_channels)
            if new:
                # 未知フィールドは捨てて継続する（決定記録 0003 §2.7）。初出だけ記録する
                LOGGER.info(
                    "未知のチャネルを無視した", extra={logs.FIELDS_KEY: {"channels": sorted(new)}}
                )


@dataclass(frozen=True)
class Config:
    """デーモン1回ぶんの設定。CLI 引数から作る。"""

    source: str = "mock"
    scenario: str = "idle"
    speed: float = 1.0
    db: Path = DEFAULT_DB
    scenarios: Path = DEFAULT_SCENARIOS
    quality_rules: Path = DEFAULT_QUALITY_RULES
    calibration: Path = DEFAULT_CALIBRATION
    csv: Path | None = None
    """`--source replay` の入力。ファイルかディレクトリ。"""
    bulk: bool = False
    """一括投入。待たずに流す（`--speed` は無視される）。"""
    timezone: str = "Asia/Tokyo"
    """CSV の時刻の解釈に使う。ファイルにオフセットが無いため（決定記録 0008 §2.8）。"""
    rules: Path = DEFAULT_RULES
    metrics: Path = DEFAULT_METRICS
    tick_s: float = 1.0
    """サンプルが来ないときに時刻ベースのルールを評価する間隔。"""


def build(config: Config) -> Daemon:
    """**合成の起点。** 時計を1つ選び、取り込みと保存へ同じものを配る（#42）。

    ここが唯一の組み立て場所であることが、`Clock` を型で縛れないぶんの担保になる。
    別々に作ると、取り込みはシナリオ時間・保存は実時計という組み合わせが成立する。
    """
    source = _build_source(config)
    rules = QualityRules.from_yaml(config.quality_rules)
    calibration = _calibration_for(config)
    config.db.parent.mkdir(parents=True, exist_ok=True)
    clock: Clock = source.clock
    store = SqliteStore(config.db, rules=rules, clock=clock)
    return Daemon(
        source=source,
        store=store,
        normalizer=Normalizer(rules=rules, calibration=calibration, clock=clock),
        source_name=config.source,
        # ルールエンジンは**取り込みと同じプロセス・同じ時計**で動かす。
        # 継続時間の判定が実時間に依存すると、圧縮再生で検証できない
        # （決定記録 0007 §2.11 / §5 未決3）
        engine=Engine(
            rules=RuleSet.from_yaml(config.rules),
            catalog=MetricCatalog.from_yaml(config.metrics),
            store=store,
            clock=clock,
        ),
        tick_s=config.tick_s,
    )


def _calibration_for(config: Config) -> Calibration:
    """再生では較正を当てない（決定記録 0010 §2.9）。

    CSV に入っているのは**保存済みの値**である（日次CSV は `readings` の値を
    書き出す。決定記録 0008 §2.8）。そこへ較正オフセットを当てると**二重になり、
    再生した温度が全部ずれる。** 較正は取り込み時点の変換であって、
    記録済みの値へ後から重ねるものではない。
    """
    if config.source == "replay":
        return Calibration(note="再生では較正を当てない（決定記録 0010 §2.9）")
    return Calibration.from_json(config.calibration)


def _build_source(config: Config) -> Source:
    if config.source == "replay":
        if config.csv is None:
            raise SystemExit("--source replay には --csv が要る（ファイルかディレクトリ）")
        try:
            return ReplaySource(
                config.csv,
                tz=ZoneInfo(config.timezone),
                speed=config.speed,
                bulk=config.bulk,
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
    if config.source != "mock":
        raise SystemExit(f"--source {config.source} は未実装（serial は #12）")
    scenarios = load_scenarios(config.scenarios)
    if config.scenario not in scenarios:
        raise SystemExit(
            f"シナリオが無い: {config.scenario}（候補: {', '.join(sorted(scenarios))}）"
        )
    return MockSource(scenarios[config.scenario], speed=config.speed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coldaisle-daemon", description="センサー取り込みデーモン"
    )
    parser.add_argument("--source", choices=["mock", "replay", "serial"], default="mock")
    parser.add_argument("--scenario", default="idle", help="mock のシナリオ名")
    parser.add_argument("--speed", type=float, default=1.0, help="時間圧縮。60 なら1分を1秒で流す")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--quality-rules", type=Path, default=DEFAULT_QUALITY_RULES)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument(
        "--csv", type=Path, default=None, help="replay の入力（ファイル/ディレクトリ）"
    )
    parser.add_argument("--bulk", action="store_true", help="replay を待たずに流す（一括投入）")
    parser.add_argument("--timezone", default="Asia/Tokyo", help="CSV の時刻の解釈")
    parser.add_argument("--max-samples", type=int, default=None, help="試験用。件数で止める")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.configure(args.log_level)
    daemon = build(
        Config(
            source=args.source,
            scenario=args.scenario,
            speed=args.speed,
            db=args.db,
            scenarios=args.scenarios,
            quality_rules=args.quality_rules,
            calibration=args.calibration,
            rules=args.rules,
            metrics=args.metrics,
            csv=args.csv,
            bulk=args.bulk,
            timezone=args.timezone,
        )
    )
    if args.source == "replay":
        LOGGER.info("再生では較正を当てない（決定記録 0010 §2.9）")
    if args.speed != 1.0 or args.bulk:
        # 決定記録 0007 §2.11: 圧縮再生はこのプロセスの中だけで意味を持つ
        LOGGER.warning(
            "時間圧縮中はホスト時刻がシナリオ時間で進む。"
            "別プロセスの API / ダッシュボードを同時に使わないこと（決定記録 0007 §2.11）",
            extra={logs.FIELDS_KEY: {"speed": args.speed}},
        )

    def _stop(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("シグナルを受けた", extra={logs.FIELDS_KEY: {"signal": signum}})
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        daemon.run(max_samples=args.max_samples)
    finally:
        daemon.store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - `python -m coldaisle.daemon`
    raise SystemExit(main())
