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

  // 回転数・PWM・温度・使用率の出どころの注記。見出し（ingestSourceLabel）と同じ判断で書く。
  // **health.source が serial のときだけ「実測」と言う**（Codex P2 / 決定記録 0046 §2.7）
  const VALUES = "回転数・PWM・温度・使用率";
  const MEASURED_NOTES = {
    serial: `${VALUES}は実機の実測値です。`,
    mock: `${VALUES}は模擬データ（MockSource）の値です。実機の実測値ではありません。`,
    replay: `${VALUES}は過去の記録の再生です。いまの実機の値ではありません。`,
  };

  /** `health.source` に合わせた値の注記。undefined は health 待ち、null・未知の値は出どころ不明。 */
  function measuredNote(source) {
    if (source === undefined) return `${VALUES}の出どころを確認中です。`;
    return Object.prototype.hasOwnProperty.call(MEASURED_NOTES, source)
      ? MEASURED_NOTES[source]
      : `${VALUES}の出どころは不明です（実機の実測値とは限りません）。`;
  }

  // 値の札（凡例・各ゾーンの PWM）に付ける短い語。**serial のときだけ「実測」**（Codex P2）
  const VALUE_KINDS = { serial: "実測", mock: "模擬", replay: "再生" };

  /** `health.source`（`?mock=` のときは "mock"）に合わせた値の種類。undefined は health 待ち。 */
  function valueKind(source) {
    if (source === undefined) return "確認中";
    return Object.prototype.hasOwnProperty.call(VALUE_KINDS, source) ? VALUE_KINDS[source] : "出どころ不明";
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
    valueKind,
    usablePoints,
    GPU_THROTTLE_METRICS: [...THERMAL, ...POWER, ...SLOWDOWN].map(([metric]) => metric),
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ColdaisleAirflowStatus = api;
})(this);
