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
  // API はその経路が実機か模擬かを返さない（server-health の nvml / lm_sensors は稼働状態だけ）。
  // そのため内部テレメトリの値には「実測」とも「模擬」とも言わず、中立な「読み取り値」と書く。
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

  // 取り込み経路の値に付ける短い語。**serial のときだけ「実測」**
  const INGEST_KINDS = { serial: "実測", mock: "模擬", replay: "再生" };

  /** 取り込み経路の値の種類。undefined は health 待ち、null・未知の値は出どころ不明。 */
  function ingestKind(source) {
    if (source === undefined) return "確認中";
    return Object.prototype.hasOwnProperty.call(INGEST_KINDS, source) ? INGEST_KINDS[source] : "出どころ不明";
  }

  /**
   * そのメトリクスの値の種類（凡例・PWM の札）。`mock` は `?mock=` で表示中か。
   * 取り込み経路（air.*）は health.source で決め、内部テレメトリは中立な「読み取り値」。
   */
  function metricKind(metric, ingestSource, mock) {
    if (mock) return "模擬";
    return isIngestMetric(metric) ? ingestKind(ingestSource) : TELEMETRY_KIND;
  }

  const AIR = "空気の温度（センサー基板）";
  const AIR_NOTES = {
    serial: `${AIR}は実機の実測値です。`,
    mock: `${AIR}は模擬データ（MockSource）の値です。実機の実測値ではありません。`,
    replay: `${AIR}は過去の記録の再生です。いまの実機の値ではありません。`,
  };
  const TELEMETRY_NOTE =
    "回転数・PWM・CPU・GPU は内部テレメトリの読み取り値です（取り込みとは別の経路のため、上の区別は当てはまりません）。";

  /** 値の出どころの注記（実データ表示のとき）。経路ごとに書き分ける。 */
  function measuredNote(source) {
    let air;
    if (source === undefined) air = `${AIR}の出どころを確認中です。`;
    else if (Object.prototype.hasOwnProperty.call(AIR_NOTES, source)) air = AIR_NOTES[source];
    else air = `${AIR}の出どころは不明です（実機の実測値とは限りません）。`;
    return `${air}${TELEMETRY_NOTE}`;
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
    ingestKind,
    metricKind,
    INGEST_METRICS,
    usablePoints,
    GPU_THROTTLE_METRICS: [...THERMAL, ...POWER, ...SLOWDOWN].map(([metric]) => metric),
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ColdaisleAirflowStatus = api;
})(this);
