"""API のレスポンス型（L2）。#9

**時刻は2つの形で返す。** `ts_ms`（Unix ミリ秒、保存と同じ値）と
`ts`（ISO8601・UTC）。前者は計算用、後者は人間とログ用。
表示のためのローカル時刻への変換はクライアント側で行う
（api-contract の例は JST だが、サーバはタイムゾーンを持たない）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from coldaisle.store.models import AlertRecord, Quality


def iso(ts_ms: int) -> str:
    """Unix ミリ秒を ISO8601（UTC）にする。"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).isoformat()


class MetricValue(BaseModel):
    """1メトリクスの現在値（api-contract §3 の `metrics` に対応）。"""

    model_config = ConfigDict(frozen=True)

    value: float | None
    unit: str | None
    quality: Quality
    age_seconds: float
    """`ts_ms` から応答時刻までの経過。**負なら時計がずれている**（#42）。"""


class LatestResponse(BaseModel):
    """`GET /api/v1/latest`（FR-301）。"""

    model_config = ConfigDict(frozen=True)

    ts_ms: int
    ts: str
    metrics: dict[str, MetricValue]
    derived: dict[str, float | None]
    """派生値（決定記録 0002 §2.2 により保存しない）。計算できなければ `null`。"""
    stale: bool


class MetricLabelOut(BaseModel):
    """保存されるメトリクス1つ分の表示情報（`config/metrics.yaml`）。"""

    model_config = ConfigDict(frozen=True)

    unit: str
    label: str


class DerivedLabelOut(BaseModel):
    """派生値1つ分の表示情報と**式**（被減数 − 減数）。

    式を返すのは、画面が「何から何を引いた値か」を示せるようにするため
    （決定記録 0039 §2.2）。**計算はしない。** 値は `/latest` の `derived`。
    """

    model_config = ConfigDict(frozen=True)

    unit: str
    label: str
    minuend: str
    subtrahend: str


class MetricsCatalogResponse(BaseModel):
    """`GET /api/v1/metrics`（決定記録 0039 §2.1）。

    メトリクス名から人間向けの表示名を引く表。**値は含まない。**
    表示名は変えてよく（決定記録 0009 §2.1）、機械はメトリクス名で参照する。
    """

    model_config = ConfigDict(frozen=True)

    metrics: dict[str, MetricLabelOut]
    derived: dict[str, DerivedLabelOut]


class SeriesPointOut(BaseModel):
    """時系列の1点。`agg` によって埋まる項目が変わる。

    `agg=raw` では `value` と `quality`、集計では `value`（平均）と
    `min` / `max` / `missing_ratio` が入る。
    """

    model_config = ConfigDict(frozen=True)

    ts_ms: int
    value: float | None
    quality: Quality | None = None
    min: float | None = None
    max: float | None = None
    ok_count: int | None = None
    row_count: int | None = None
    missing_ratio: float | None = None


class SeriesResponse(BaseModel):
    """`GET /api/v1/series`（FR-302）。"""

    model_config = ConfigDict(frozen=True)

    metric: str
    unit: str | None
    agg: str
    """**実際に使った粒度。** 要求より粗くなることがある（`downsampled`）。"""
    downsampled: bool
    truncated: bool = False
    """上限に達して**古い側を落とした**かどうか（決定記録 0004 §2.6）。"""
    from_ms: int = Field(serialization_alias="from")
    to_ms: int = Field(serialization_alias="to")
    points: list[SeriesPointOut]


class StatsResponse(BaseModel):
    """`GET /api/v1/stats`（FR-303）。統計は `quality='ok'` の行のみ（決定記録 0002 §2.8）。"""

    model_config = ConfigDict(frozen=True)

    metric: str
    unit: str | None
    from_ms: int = Field(serialization_alias="from")
    to_ms: int = Field(serialization_alias="to")
    row_count: int
    ok_count: int
    min: float | None
    max: float | None
    mean: float | None
    p95: float | None
    """nearest-rank。**補間しない**（決定記録 0004 §2.3）。"""
    slope_per_min: float | None
    missing_ratio: float | None
    """生データ窓の下限値。通信断は含まない（決定記録 0004 §2.5）。"""


class AlertsResponse(BaseModel):
    """`GET /api/v1/alerts`（FR-304）。書き込むのはルールエンジン（#18）。"""

    model_config = ConfigDict(frozen=True)

    alerts: list[AlertRecord]


class EventOut(BaseModel):
    """記録された1件の事象（#67 / 決定記録 0045 §2.7）。

    書き込んだ接続の uid（`peer_uid`）は監査のために DB に残すが、ここには出さない。
    読み取りに要らない情報を広げない。
    """

    model_config = ConfigDict(frozen=True)

    id: int
    ts_ms: int
    ts: str
    kind: str
    payload: dict[str, Any]


class EventsResponse(BaseModel):
    """`GET /api/v1/events`。タイムライン注釈の元（#67）。

    **書き込みは読み取り API では受けない。** 入口は別プロセスの Unix ソケット
    （`coldaisle-eventd`。決定記録 0045）。
    """

    model_config = ConfigDict(frozen=True)

    from_ms: int = Field(serialization_alias="from")
    to_ms: int = Field(serialization_alias="to")
    events: list[EventOut]
    truncated: bool


class HealthResponse(BaseModel):
    """`GET /api/v1/health`（FR-305）。

    **データが古いときに `ok` を返さない。** 無音で古い値を出すのが
    監視システムの最悪の失敗である（api-contract §3）。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    source: str | None
    """取り込みソース種別。デーモンが記録する（`sys.ingest_source`）。"""
    telemetry_source: str | None
    """内部テレメトリの出どころの種類（`hardware` / `mock`）。

    テレメトリデーモンが記録する（`sys.telemetry_kind`。決定記録 0049 §2.3）。
    **これは「いまの種類」であって、保存済みの値ごとの出どころではない。**
    記録の無い DB（古いデーモン）では `null` で、画面は中立な「読み取り値」のままにする。
    取り込み（`source`）とは別の経路なので、意味を混ぜない。
    """
    last_sample_at: str | None
    last_sample_ts_ms: int | None
    data_age_seconds: float | None
    stale: bool
    metrics: int
    missing_ratio_1h: float | None
    """直近1時間の欠測率。1分ロールアップから出す（決定記録 0002 §2.8）。"""
    queue_drops_1h: int = 0
    """直近1時間に取り込みの待ち行列が捨てたメッセージ数（決定記録 0012 §2.3）。

    **0 でない日は原因を追う。** 保存が取り込みに追いつけていない。
    """


class ServerSignal(StrEnum):
    """Workspace がそのまま描画できる決定論的な信号色。"""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class HealthSourceStatus(StrEnum):
    """Server Health が公開する情報源の状態。"""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    STOPPED = "stopped"


class HealthSource(BaseModel):
    """1つの情報源の生死と最後に見えた測定時刻。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: HealthSourceStatus
    detail: str
    last_sample_ts_ms: int | None
    last_sample_at: str | None


class HealthSources(BaseModel):
    """Issue #66 が固定する4つの情報源。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_unit: HealthSource
    nvml: HealthSource
    lm_sensors: HealthSource
    ai_layer: HealthSource


class ServerHealthMetric(BaseModel):
    """Server Health 内の現在値。未取得でもキーを残す。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float | None
    unit: str | None
    quality: Quality
    age_seconds: float | None


class ServerGpuHealth(BaseModel):
    """Workspace が ``nvidia-smi`` 無しで描画する GPU 状態。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: str
    metrics: dict[str, ServerHealthMetric]


class ServerEnvironmentHealth(BaseModel):
    """外気・ケース・CPU/Board の現在値。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: dict[str, ServerHealthMetric]


class AdvisoryCondition(BaseModel):
    """切替直前に提示する環境条件1件（#68 / 決定記録 0063 §2.2）。

    **欠けている条件も必ず1件として載せる。** キーごと省略すると、
    受け手は「条件が無い」と「条件が正常」を区別できない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str
    value: float | None
    unit: str | None
    quality: Quality
    age_seconds: float | None
    usable: bool
    """判断材料として使えるか（`quality=ok` かつ設定の `max_age_s` 以内）。"""
    advisory_max: float | None
    """助言用の上限（`config/compute-mode-advisory.yaml`）。制御には使わない。"""
    exceeded: bool
    """使える値が `advisory_max` を超えているか。使えない値では常に false。"""
    reference_value: float | None
    """直近の実測フルロード期間中の同じ metric の値。比較できなければ null。"""
    delta: float | None
    """`value - reference_value`。どちらかが無ければ null。"""


class FullLoadReference(BaseModel):
    """直近に**観測された**フルロード期間（#68 / 決定記録 0063 §2.3）。

    GPU Mode の申告（`events` / `sys.gpu_mode`）ではなく、記録された電力から導く。
    時刻・継続時間は根拠となったバケットの時刻から決め、現在時刻から作らない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    started_at_ms: int
    started_at: str
    ended_at_ms: int
    ended_at: str
    covered_s: float
    """根拠として数えたバケットの合計時間。欠落した時間は含まない。"""
    bucket_count: int
    load_metric: str
    load_peak: float | None
    load_mean: float | None
    conditions: dict[str, float]
    """当時の環境条件。証拠の無い metric はキーごと入らない。"""
    peaks: dict[str, float]
    """当時の最高温度。証拠の無い metric はキーごと入らない。"""


class ComputeModeAdvisory(BaseModel):
    """Compute Mode 切替の判断材料。制御や拒否には使わない。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    safe: bool
    warnings: tuple[str, ...]
    blocking: Literal[False] = False
    conditions: tuple[AdvisoryCondition, ...]
    """設定に並べた順の環境条件。件数は設定と常に一致する。"""
    reference: FullLoadReference | None
    """比較に使った実測フルロード期間。無ければ null（`limitations` に理由が入る）。"""
    reference_count: int
    """さかのぼり期間に見つかったフルロード期間の件数。重複した観測では増えない。"""
    reference_window_days: int
    evaluated_at_ms: int
    """履歴部分を評価した時刻。`generated_at_ms` より古いことがある。"""
    evaluated_at: str
    limitations: tuple[str, ...]
    """**比較できなかったこと。** 空でないなら判断材料が欠けている。"""


class ServerHealthResponse(BaseModel):
    """``GET /api/v1/server-health`` と対応 WebSocket の共通 payload。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    generated_at_ms: int
    generated_at: str
    signal: ServerSignal
    summary: str
    summary_source: Literal["ai", "template"]
    gpu: ServerGpuHealth
    environment: ServerEnvironmentHealth
    active_alerts: list[AlertRecord]
    sources: HealthSources
    compute_mode_advisory: ComputeModeAdvisory


class SensorOut(BaseModel):
    """センサー1本。**どの物理プローブがどのメトリクスか**を示す（#14 / FR-403）。"""

    model_config = ConfigDict(frozen=True)

    channel: str
    """デバイスが送る名前（`front_intake`）。"""
    metric: str | None
    """ホストの名前（`air.front_intake`）。対応表に無ければ `null`。"""
    kind: str
    gpio: int | None = None
    rom: str | None = None
    """記録された ROM。**較正のオフセットが対応している個体**（spec-review W-03）。"""
    observed_rom: str | None = None
    """**いま繋がっている ROM**（食い違っているときだけ入る）。

    これが無いと、差し替えたあとに「何に変わったのか」を知る手段が無い。
    """
    changed: bool = False
    """記録と食い違っているか。**ダッシュボードの印はこれで決める。**

    アラートの文面から読み取らない。文面は最初の不一致のまま更新されない場合が
    あり（`Engine.on_hello` は発火中なら何も返さない）、**あとから別のチャネルが
    ずれても印が動かない。**
    """
    resolution: int | None = None


class DeviceOut(BaseModel):
    """起動バナーで申告された構成1台ぶん。"""

    model_config = ConfigDict(frozen=True)

    device_id: str
    fw: str | None = None
    interval_ms: int | None = None
    last_hello_at: str | None = None
    sensors: list[SensorOut]


class DevicesResponse(BaseModel):
    """`GET /api/v1/devices`（#14）。

    **記録された構成**であって、いま繋がっている構成ではない。この2つが
    食い違っている状態が `PROBE_CHANGED`（FR-403）であり、
    **記録の側は人が較正をやり直すまで動かさない**（決定記録 0012 §2.6）。
    """

    model_config = ConfigDict(frozen=True)

    devices: list[DeviceOut]


class ToolListResponse(BaseModel):
    """`GET /api/v1/tools`（#23）。

    `tools` は OpenAI function calling 形式の定義そのもの。**中身は型付けしない**
    （形を決めているのは呼び出し先のモデルの作法であって、この契約ではない）。
    決まっている外側だけを型にする。
    """

    model_config = ConfigDict(frozen=True)

    read_only: bool
    advisory: bool
    """**回答は提案であり、実行される操作ではない。** この層に実行の手段は無い。"""
    guidance: str
    """呼び出し側の system prompt に入れる注意書き。"""
    tools: list[dict[str, Any]]


class ToolCallMeta(BaseModel):
    """何を呼んだか（#23「どのツールを呼んだかを可視化」）。"""

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, str]
    """届いたクエリ文字列そのもの。**型の変換は各ツールの引数モデルが行う。**"""
    ok: bool
    """`false` なら `result.error` に理由が入る（HTTP は 200 のまま）。"""
    ts_ms: int
    ts: str
    elapsed_ms: int
    read_only: bool
    advisory: bool


class ToolCallResponse(BaseModel):
    """`GET /api/v1/tools/{name}`（#23）。

    `result` はツールごとに形が違うので型付けしない。**外側の封筒は固定**で、
    Workspace 側は `meta` を見て呼び出しを表示できる。
    """

    model_config = ConfigDict(frozen=True)

    meta: ToolCallMeta
    result: dict[str, Any]


class StreamMessage(BaseModel):
    """`WS /api/v1/stream`（FR-306）が押し出す1件。"""

    model_config = ConfigDict(frozen=True)

    type: str = "latest"
    latest: LatestResponse
