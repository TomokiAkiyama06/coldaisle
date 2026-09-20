// 熱源の状態（GPU のスロットリング）と、データの出どころの表示。#106 / 決定記録 0046 §2.6 / §2.7。
//
// **DOM に触らない関数だけを置く**（node からそのまま試せるようにするため）。
// 描き方は airflow.js が持つ。
//
// 規則:
//   - 熱による制限（hw_thermal / sw_thermal）→ 赤。どちらかが 1 なら最優先
//   - 電力による制限（hw_power_brake / sw_power_cap）→ 琥珀
//   - ハードウェアによる減速（hw_slowdown だけ）→ 琥珀
//   - **値が無い・古い（quality が ok 以外）ものは「分からない」として読まない。**
//     どれも立っていなければ null を返し、画面には何も出さない。
//     「スロットリングなし」とは言わない（分からない場合と区別できないため）
//   - T.Limit までの余裕（gpu.0.tlimit_margin）は出さない（案3。採用されていない）
"use strict";

(function (root) {
  const THERMAL = [
    ["gpu.0.throttle.hw_thermal", "ハードウェア"],
    ["gpu.0.throttle.sw_thermal", "ソフトウェア"],
  ];
  const POWER = [
    ["gpu.0.throttle.hw_power_brake", "ハードウェア"],
    ["gpu.0.throttle.sw_power_cap", "ソフトウェア"],
  ];
  const SLOWDOWN = [["gpu.0.throttle.hw_slowdown", "ハードウェア"]];

  /** フラグが立っているか。**quality が ok で値が 1 のときだけ**真。 */
  function raised(metrics, metric) {
    const item = metrics[metric];
    return Boolean(item && item.quality === "ok" && item.value !== null && Number(item.value) >= 0.5);
  }

  function which(metrics, flags) {
    return flags.filter(([metric]) => raised(metrics, metric)).map(([, source]) => source);
  }

  /**
   * GPU のスロットリング状態。`latest` は `/api/v1/latest` の応答（または同じ形の模擬データ）。
   * 返り値は `{ text, tone, reason }`（tone は "bad" = 赤 / "warn" = 琥珀）か null。
   */
  function gpuThrottleStatus(latest) {
    const metrics = latest && latest.metrics;
    if (!metrics) return null;
    const thermal = which(metrics, THERMAL);
    if (thermal.length > 0) {
      return { text: "熱による制限", tone: "bad", reason: `理由：熱による制限（${thermal.join("・")}）` };
    }
    const power = which(metrics, POWER);
    if (power.length > 0) {
      return { text: "電力による制限", tone: "warn", reason: `理由：電力による制限（${power.join("・")}）` };
    }
    if (which(metrics, SLOWDOWN).length > 0) {
      return { text: "ハードウェアによる減速", tone: "warn", reason: "理由：ハードウェアによる減速" };
    }
    return null;
  }

  // 取り込みの種類（`/api/v1/health` の `source`）→ 見出しの出どころの表示。
  // **分からないときに「実機」と言わない**（再生や模擬の DB を見ていることがある）
  const SOURCE_LABELS = {
    serial: { text: "実機データ", live: true },
    mock: { text: "模擬データ（MockSource）", live: false },
    replay: { text: "再生データ（過去の記録）", live: false },
  };
  const UNKNOWN_SOURCE = { text: "出どころ不明（実機のライブ値とは限りません）", live: false };

  /** `health.source` を見出しの表示にする。null・未知の値は「出どころ不明」。 */
  function ingestSourceLabel(source) {
    return Object.prototype.hasOwnProperty.call(SOURCE_LABELS, source) ? SOURCE_LABELS[source] : UNKNOWN_SOURCE;
  }

  // **値の出どころは経路ごとに違う**（Codex P2）。`health.source`（serial / mock / replay）が
  // 表すのは取り込みデーモン（センサー基板）の経路だけで、その経路が書くのは
  // channels.py の CHANNEL_TO_METRIC にある空気の温度・湿度（air.*）だけ。
  // 回転数・PWM・CPU・GPU は内部テレメトリ（coldaisle-telemetry）が別の経路で書き、
  // その経路の種類は `health.telemetry_source`（hardware / mock）が返す（決定記録 0049）。
  // `?mock=` のときだけは、どの値も模擬。
  const INGEST_METRICS = [
    "air.room",
    "air.room_humidity",
    "air.front_intake",
    "air.gpu_intake",
    "air.gpu_exhaust",
    "air.top_exhaust",
    "air.rear_exhaust",
  ];
  const TELEMETRY_KIND = "読み取り値";

  /** 取り込みデーモン（health.source の経路）が書くメトリクスか。 */
  function isIngestMetric(metric) {
    return INGEST_METRICS.includes(metric);
  }

  // 内部テレメトリの**いま届いている値**に付ける短い語（決定記録 0049 §2.5）。
  // 知らない種類・記録の無い DB（null）は「読み取り値」のままにする（分からないときに実測と言わない）
  const TELEMETRY_KINDS = { hardware: "実測", mock: "模擬" };

  /**
   * その値が**いま届いている**か。`/api/v1/latest` の1件（`{ value, quality }`）を渡す。
   *
   * `value` が非 null で、`quality` が `ok` / `suspect` のときだけ真。
   * - 鮮度は `latest()` が既に判定している（`config/quality.yaml` の `stale_after_ms`。
   *   決定記録 0004 §2.2）。画面はしきい値を持たない
   * - 「ok / suspect は届いている、stale / missing / 未保存は届いていない」の切り方は
   *   Server Health（決定記録 0042 §2.4）と同じ
   * - **quality だけでは足りない。** hwmon は有限でない読み値を `value: null` /
   *   `quality: suspect` で保存する。画面は値が無いので「未取得」と出すため、
   *   そこに出どころの札を付けると表示の無い値に「実測」が付く
   */
  function delivered(item) {
    if (!item || item.value === null || item.value === undefined) return false;
    return item.quality === "ok" || item.quality === "suspect";
  }

  /** `/api/v1/latest`（または同じ形の模擬データ）から1件取り出す。無ければ undefined。 */
  function latestItem(latest, metric) {
    const metrics = latest && latest.metrics;
    if (!metrics || !Object.prototype.hasOwnProperty.call(metrics, metric)) return undefined;
    return metrics[metric];
  }

  /**
   * 内部テレメトリの値1つに付ける札（決定記録 0049 §2.5）。
   * 届いていない値（古い・未取得・キーが無い）は「読み取り値」のまま。
   * `kind` が undefined（health 待ち）なら「確認中」。実測とは言わない。
   */
  function telemetryKind(kind, item) {
    if (!delivered(item)) return TELEMETRY_KIND;
    if (kind === undefined) return "確認中";
    return Object.prototype.hasOwnProperty.call(TELEMETRY_KINDS, kind) ? TELEMETRY_KINDS[kind] : TELEMETRY_KIND;
  }

  /**
   * 見出し・凡例に出す内部テレメトリの札。**いま届いている値の出どころ**として書く。
   * 届いている内部テレメトリの値が1つも無ければ「読み取り値」、health 未着なら「確認中」。
   */
  function telemetrySummaryKind(telemetrySource, latest, mock) {
    if (mock) return "模擬";
    if (telemetrySource === undefined) return "確認中";
    const metrics = (latest && latest.metrics) || {};
    for (const metric of Object.keys(metrics)) {
      if (isIngestMetric(metric)) continue;
      if (delivered(metrics[metric])) return telemetryKind(telemetrySource, metrics[metric]);
    }
    return TELEMETRY_KIND;
  }

  // 取り込み経路の値に付ける短い語。**serial のときだけ「実測」**
  const INGEST_KINDS = { serial: "実測", mock: "模擬", replay: "再生" };

  /** 取り込み経路の値の種類。undefined は health 待ち、null・未知の値は出どころ不明。 */
  function ingestKind(source) {
    if (source === undefined) return "確認中";
    return Object.prototype.hasOwnProperty.call(INGEST_KINDS, source) ? INGEST_KINDS[source] : "出どころ不明";
  }

  /**
   * そのメトリクスの**いまの値**に付ける札（凡例・PWM の札）。`mock` は `?mock=` で表示中か。
   * 取り込み経路（air.*）は health.source で決め、内部テレメトリは
   * health.telemetry_source と**その値が届いているか**で決める（決定記録 0049 §2.5）。
   * `latest` は `/api/v1/latest` の応答（または同じ形の模擬データ）。
   */
  function metricKind(metric, ingestSource, mock, telemetrySource, latest) {
    if (mock) return "模擬";
    if (isIngestMetric(metric)) return ingestKind(ingestSource);
    return telemetryKind(telemetrySource, latestItem(latest, metric));
  }

  const AIR = "空気の温度（センサー基板）";
  const AIR_NOTES = {
    serial: `${AIR}は実機の実測値です。`,
    mock: `${AIR}は模擬データ（MockSource）の値です。実機の実測値ではありません。`,
    replay: `${AIR}は過去の記録の再生です。いまの実機の値ではありません。`,
  };
  const TELEMETRY = "回転数・PWM・CPU・GPU";
  // **過去の値の出どころは出さない**（決定記録 0049 §2.5）。模擬の adapter で書いた区間が
  // 履歴に混ざりうるため、グラフの点が実機の記録かどうかは主張しない
  const TELEMETRY_HISTORY_NOTE = "古い値とグラフの過去の値の出どころは表示しません。";

  /** 内部テレメトリの注記。**いま届いている値**についてだけ出どころを言う。 */
  function telemetryNote(telemetrySource, latest) {
    const kind = telemetrySummaryKind(telemetrySource, latest, false);
    if (kind === "実測") {
      return `${TELEMETRY}のいま届いている値は実機の読み取り値です。${TELEMETRY_HISTORY_NOTE}`;
    }
    if (kind === "模擬") {
      return `${TELEMETRY}のいま届いている値は模擬の adapter の値です。実機の値ではありません。${TELEMETRY_HISTORY_NOTE}`;
    }
    if (kind === "確認中") {
      return `${TELEMETRY}の出どころを確認中です。${TELEMETRY_HISTORY_NOTE}`;
    }
    return `${TELEMETRY}は内部テレメトリの読み取り値です（取り込みとは別の経路のため、上の区別は当てはまりません）。${TELEMETRY_HISTORY_NOTE}`;
  }

  /** 値の出どころの注記（実データ表示のとき）。経路ごとに書き分ける。 */
  function measuredNote(source, telemetrySource, latest) {
    let air;
    if (source === undefined) air = `${AIR}の出どころを確認中です。`;
    else if (Object.prototype.hasOwnProperty.call(AIR_NOTES, source)) air = AIR_NOTES[source];
    else air = `${AIR}の出どころは不明です（実機の実測値とは限りません）。`;
    return `${air}${telemetryNote(telemetrySource, latest)}`;
  }

  /**
   * グラフに使える点へ直す。**生データ（agg=raw）では quality が ok 以外の点を値なし（null）にする**。
   * `/api/v1/series` の raw は suspect の値（DS18B20 の -127 など）をそのまま返すため、
   * 線・帯・読み取り値のどれにも出さず、欠けた区間として扱う（ダッシュボードのカードと同じく
   * ok 以外を正常な値として見せない）。集計済みの点は ok の行だけで作られているのでそのまま使う。
   */
  function usablePoints(points, agg) {
    if (!Array.isArray(points)) return [];
    if (agg !== "raw") return points;
    return points.map((point) => (point.quality === "ok" ? point : { ...point, value: null }));
  }

  const api = {
    gpuThrottleStatus,
    ingestSourceLabel,
    measuredNote,
    telemetryNote,
    ingestKind,
    telemetryKind,
    telemetrySummaryKind,
    metricKind,
    INGEST_METRICS,
    usablePoints,
    GPU_THROTTLE_METRICS: [...THERMAL, ...POWER, ...SLOWDOWN].map(([metric]) => metric),
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ColdaisleAirflowStatus = api;
})(this);
