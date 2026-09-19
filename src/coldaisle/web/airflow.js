// エアフロー / ファン制御の可視化（#106 / 決定記録 0046）。**表示専用。**
//
// 原則:
//   1. **書き込まない。** 叩くのは GET だけ（api-contract §1）。モード変更の入り口は
//      coldaisle-fand のローカル Unix ソケットだけで、この画面には置かない（決定記録 0028 §2.2）
//   2. **実データと模擬データを混ぜない。** 模擬は `?mock=normal|override|throttle` のときだけ
//      別ファイル（airflow-mock.js）を読み、そのときは実データを1件も取りに行かない。
//      画面全体の帯で「模擬データ」と言う
//   3. **未接続・未取得・古い値を正常値のように見せない。** 制御の判断記録（#74 / #82）は
//      まだ API から読めないので、制御の状態はすべて「未接続」と出す
//   4. **API から来た文字列を HTML として解釈しない。** 常に textContent（要件 §7.4）
//   5. **内部のメトリクス名を画面に出さない。** 表示名はこのファイルの表が持つ
//   6. **色の区切りをコードに書かない。** `GET /api/v1/airflow/config`（config/airflow-ui.yaml）から読む
"use strict";

const MOCK_SCENARIOS = { "1": "normal", normal: "normal", override: "override", throttle: "throttle" };

// 見出しの出どころ（health.source）は空気の温度（取り込み経路）の、**いま動いている**取り込みだけのもの。
// 履歴には切り替え前の出どころの点が混ざりうる（点ごとの出どころは API に無い）ので、そう明記する
const AIR_SOURCE_PREFIX = "空気の温度（現在の取り込み元）：";

const MOCK_LABELS = {
  normal: "模擬データ（通常）",
  override: "模擬データ（異常時）",
  throttle: "模擬データ（GPU の熱による制限）",
};

const RANGES = [
  { label: "1h", window: "1h", agg: "raw" },
  { label: "6h", window: "6h", agg: "1m" },
  { label: "24h", window: "24h", agg: "1m" },
  { label: "7d", window: "7d", agg: "1h" },
];

const REFRESH_MS = 2500;
const HISTORY_REFRESH_MS = 60000;
const REQUEST_TIMEOUT_MS = 10000;
const STEP_MS = { "1m": 60000, "5m": 300000, "1h": 3600000 };
const GAP_FACTOR = 2; // この倍以上空いたら「測れていない区間」とみなす（決定記録 0011 §2.3.1）

// 空気の温度の配色（青→琥珀→赤）。**区切りの値は設定から読む。** 段数は設定の区切り＋1
const AIR_COLORS = ["#5aa7e6", "#9ea690", "#e3a63b", "#e2814b", "#e25c5c"];
const NO_DATA_COLOR = "#56606d";

const QUALITY_TAG = { suspect: "疑わしい", stale: "古い", missing: "未取得" };

// ---------------------------------------------------------------- 画面の場所 → メトリクス
// 表示名は「図の上の場所の名前」。内部のメトリクス名は出さない。
// 座標は側面図（viewBox 826×470）の上の位置。左が背面、右が前面。

const AIR_POINTS = [
  { metric: "air.room", label: "室温", x: 728, y: 396, w: 96 },
  { metric: "air.front_intake", label: "前面吸気", x: 728, y: 148, w: 96 },
  { metric: "air.gpu_intake", label: "GPU吸気（下面）", x: 334, y: 352, w: 112 },
  { metric: "air.gpu_exhaust", label: "GPU排気（上面）", x: 412, y: 224, w: 108 },
  { metric: "air.top_exhaust", label: "トップ排気", x: 140, y: 2, w: 96 },
  { metric: "air.rear_exhaust", label: "リア排気", x: 0, y: 250, w: 80 },
];

const CASE_DELTA = { derived: "d.case_delta", label: "ケース内の温度差（リア−前面）", x: 150, y: 396, w: 176 };

const ZONES = [
  { key: "front", name: "前面", role: "吸気", rpm: "fan.front.rpm", pwm: "fan.front.pwm", chip: { x: 636, y: 217 } },
  { key: "rear", name: "背面", role: "排気", rpm: "fan.rear.rpm", pwm: "fan.rear.pwm", chip: { x: 92, y: 178 } },
  { key: "top", name: "トップ", role: "排気（CPU ラジエータ）", rpm: "fan.top.rpm", pwm: "fan.top.pwm", chip: { x: 356, y: 104 } },
];

// `util: null` は「未計測」と出す（そもそも取得していない）。
// CPU 使用率（`cpu.utilization`。決定記録 0047）は収集を無効にできる（`proc_stat.enabled`）ほか、
// Linux 以外では値を持たない。そのとき collector は `missing` の行を保存しうるし、無効にする前の
// 行も残るので、**行の有無では判断しない。** `/api/v1/airflow/config` の
// `cpu_utilization.measured`（collector が保存した proc_stat の状態から決まる）が偽なら「未計測」と出す
// （`measured`。決定記録 0051）。ページを開いたときに1回読む
//
// `status` は熱源の状態（決定記録 0046 §2.6）。GPU はスロットリングの状態を返す（案1）。
// 判定は airflow-status.js（DOM に触らない関数）が持つ。CPU は状態を持たない
const HEAT_SOURCES = [
  {
    name: "CPU", util: "cpu.utilization", measured: () => page.cpuMeasured, temp: "cpu.package", power: "power.cpu.package",
    status: null,
    chip: { x: 204, y: 200, w: 104 },
  },
  {
    name: "GPU", util: "gpu.0.utilization", temp: "gpu.0.core", power: "power.gpu.0",
    status: (latest) => window.ColdaisleAirflowStatus.gpuThrottleStatus(latest),
    block: "gpu-block",
    chip: { x: 196, y: 292, w: 112 },
  },
];

const STATUS_COLOR = { bad: "#e25c5c", warn: "#e3a63b" };
const STATUS_FILL = { bad: "#3a1d1f", warn: "#33291a" };

/**
 * 熱源の状態（GPU のスロットリング。決定記録 0046 §2.6 の案1）。
 *
 * `{ text, tone, reason }`（tone は "bad" = 赤 / "warn" = 琥珀）か null。
 * 返り値があれば、側面図の GPU の枠と札が色付きになり、小さな札（text）と理由の行（reason）が出る。
 * `null` のあいだは何も出さない（**「スロットリングなし」とも言わない**。値が無い・古いときと区別できないため）。
 */
function sourceStatus(source) {
  return typeof source.status === "function" ? source.status(page.latest) : null;
}

const REFERENCE = [
  { label: "AIO ポンプ", metric: "fan.aio_pump.rpm" },
  { label: "VRM ファン", metric: "fan.vrm.rpm" },
];

// 制御の状態。**値はまだ読めない**（#74 / #82）。実データでは全部「未接続」
const CONTROL_CHIPS = [
  "運転モード",
  "安全状態",
  "制御方式",
  "学習モデルの権限",
  "運転方針",
  "負荷の傾向",
  "予測の信頼度",
  "想定外の状態",
  "故障",
];

// グラフ案B（温度に使用率の帯を重ねる）。**CPU を先、次に GPU。**
const GROUPS = [
  {
    name: "CPU",
    items: [
      { key: "cpu_util", label: "CPU使用率", metric: "cpu.utilization", kind: "band", color: "#c792ea", on: true },
      { key: "cpu_temp", label: "CPU温度", metric: "cpu.package", kind: "temp", color: "#c792ea", on: true },
    ],
  },
  {
    name: "GPU",
    items: [
      { key: "gpu_util", label: "GPU使用率", metric: "gpu.0.utilization", kind: "band", color: "#e8894a", on: true },
      { key: "gpu_core", label: "GPUコア温度", metric: "gpu.0.core", kind: "temp", color: "#e8894a", on: true },
      { key: "gpu_intake", label: "GPU吸気", metric: "air.gpu_intake", kind: "temp", color: "#6fd0c4", on: true },
      { key: "gpu_exhaust", label: "GPU排気", metric: "air.gpu_exhaust", kind: "temp", color: "#e3c25a", on: true },
    ],
  },
  {
    name: "ケース",
    items: [
      { key: "room", label: "室温", metric: "air.room", kind: "temp", color: "#9aa5b3", on: true },
      { key: "front_intake", label: "前面吸気", metric: "air.front_intake", kind: "temp", color: "#7fb8ec", on: false },
      { key: "top_exhaust", label: "トップ排気", metric: "air.top_exhaust", kind: "temp", color: "#b39ddb", on: false },
      { key: "rear_exhaust", label: "リア排気", metric: "air.rear_exhaust", kind: "temp", color: "#a3b8a0", on: false },
    ],
  },
];

const FAN_SERIES = [
  { key: "fan_front", label: "前面", metric: "fan.front.rpm", kind: "fan", color: "#5aa7e6" },
  { key: "fan_top", label: "トップ", metric: "fan.top.rpm", kind: "fan", color: "#f0a878" },
  { key: "fan_rear", label: "背面", metric: "fan.rear.rpm", kind: "fan", color: "#e3c25a" },
];

const ALL_SERIES = [...GROUPS.flatMap((group) => group.items), ...FAN_SERIES];

// ---------------------------------------------------------------- 状態

const page = {
  mockName: null, // null なら実データ
  mock: null, // 模擬データの提供元（airflow-mock.js）
  scale: null, // { thresholds_c, provisional }。読めなければ null（色を付けない）
  cpuMeasured: null, // airflow/config の cpu_utilization.measured。読めなければ null（分からない）
  latest: null,
  control: null, // 実データでは常に null（未接続）
  zone: "top", // 「なぜこの回転数か」で見ている系統
  ingestSource: undefined, // health.source。undefined = まだ届いていない
  pageNote: null,
  refreshing: false,
};

const graph = {
  range: RANGES[0],
  enabled: new Set(ALL_SERIES.filter((s) => s.on !== false && s.metric !== null).map((s) => s.key)),
  data: new Map(), // key -> { points, agg, downsampled }
  fromMs: null,
  toMs: null,
  cursorMs: null,
  token: 0,
  loaded: false,
};

// ---------------------------------------------------------------- 小道具

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // HTML 文字列を組み立てない（§7.4）
  return node;
}

function svgNode(name, attrs, parent) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  if (parent) parent.appendChild(node);
  return node;
}

function svgText(parent, x, y, content, attrs) {
  const node = svgNode("text", { x, y, ...attrs }, parent);
  node.textContent = content;
  return node;
}

function fmt(value, digits) {
  return Number(value).toLocaleString("ja-JP", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/**
 * 1つの測定値を表示用にする。**値が無いものを 0 や「—」だけで済ませない。**
 * - metric が null → 「未計測」（そもそも取得していない）
 * - 値が無い → 「未取得」
 * - 古い / 疑わしい → 値に印を付ける
 */
function reading(metric, digits, unit) {
  if (metric === null) return { text: "未計測", quality: "missing", value: null };
  const item = page.latest && page.latest.metrics ? page.latest.metrics[metric] : undefined;
  if (!item || item.value === null || item.value === undefined || item.quality === "missing") {
    return { text: "未取得", quality: "missing", value: null };
  }
  return {
    text: `${fmt(item.value, digits)}${unit}`,
    quality: item.quality,
    value: item.value,
    tag: QUALITY_TAG[item.quality],
  };
}

/**
 * 熱源の使用率。collector が収集していない（`measured()` が false）なら、保存済みの行があっても「未計測」。
 * 設定を読めない（null）ときは決めつけず、値の有無で表示する。模擬データは設定に左右されない。
 */
function utilReading(source) {
  if (!page.mockName && typeof source.measured === "function" && source.measured() === false) {
    return reading(null, 0, "%");
  }
  return reading(source.util, 0, "%");
}

function derivedValue(name) {
  const derived = page.latest && page.latest.derived ? page.latest.derived[name] : undefined;
  return derived === undefined || derived === null ? null : derived;
}

/** 空気の温度の段。**境目ちょうどは上の段**（config/airflow-ui.yaml）。 */
function bandIndex(value, thresholds) {
  let index = 0;
  while (index < thresholds.length && value >= thresholds[index]) index += 1;
  return index;
}

/** 測定点の色。正常な値だけに色を付ける。古い・未取得は灰色（今の空気の温度ではない）。 */
function airColor(metric) {
  const item = page.latest && page.latest.metrics ? page.latest.metrics[metric] : undefined;
  if (!page.scale || !item || item.value === null || item.quality !== "ok") return NO_DATA_COLOR;
  return AIR_COLORS[bandIndex(item.value, page.scale.thresholds_c)] || NO_DATA_COLOR;
}

function valueNode(result, className) {
  const node = el("span", `${className || ""} q-${result.quality}`.trim(), result.text);
  if (result.tag && result.value !== null) node.appendChild(el("span", "qtag", result.tag));
  return node;
}

async function fetchJson(path, params) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) url.searchParams.set(key, value);
  // **応答しない API で次の更新まで止まらない。** 期限を切る
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) throw new Error(`${path}: ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------- 帯・見出し

function showBanner(id, text) {
  const banner = document.getElementById(id);
  if (text) {
    banner.textContent = text;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
}

/** データが古い・無い・未来のときは画面全体で言う（決定記録 0011 §2.5 と同じ）。 */
function renderHealth(health) {
  const age = document.getElementById("age-label");
  page.ingestSource = health.source; // serial / mock / replay / null
  if (health.last_sample_ts_ms === null) {
    showBanner("banner", "データが1件も届いていません。取り込みデーモンを確認してください。");
  } else if (health.data_age_seconds < 0) {
    showBanner(
      "banner",
      "受信時刻が未来です。時計がずれているか、時間圧縮で再生中の DB を見ています。"
    );
  } else if (health.stale) {
    const seconds = Math.round(health.data_age_seconds);
    showBanner("banner", `データが古い（最終受信から ${seconds} 秒）。取り込みが止まっている可能性があります。`);
  } else {
    showBanner("banner", null);
  }
  if (health.data_age_seconds === null) age.textContent = "";
  else if (health.data_age_seconds < 0) age.textContent = "最終受信 未来";
  else age.textContent = `最終受信 ${fmt(health.data_age_seconds, 1)} 秒前`;
}

/**
 * 表示中の値の出どころ。**`?mock=` のときは API の health に関係なく "mock"**。
 * 値の札（実測・模擬・再生）と注記はすべてここから決める（Codex P2）。
 */
function displaySource() {
  return page.mockName ? "mock" : page.ingestSource;
}

/**
 * そのメトリクスの値の札に付ける語。**取り込み経路（air.*）だけ health.source で決める**。
 * 内部テレメトリ（回転数・PWM・CPU・GPU）は「読み取り値」。`?mock=` のときは全部「模擬」。
 */
function valueKind(metric) {
  return window.ColdaisleAirflowStatus.metricKind(metric, page.ingestSource, Boolean(page.mockName));
}

function renderSource() {
  const source = document.getElementById("source-label");
  const decision = document.getElementById("decision-label");
  if (page.mockName) {
    source.textContent = MOCK_LABELS[page.mockName] || "模擬データ";
    source.classList.add("mock");
    showBanner(
      "mock-banner",
      "模擬データを表示中 — 実機の値でも実際の制御の状態でもありません（URL から ?mock= を外すと API のデータ）"
    );
  } else if (page.ingestSource === undefined) {
    source.textContent = `${AIR_SOURCE_PREFIX}出どころを確認中…`; // health が届くまで「実機」と言わない
  } else {
    // **`?mock=` が無いことを「実機」とみなさない。** health.source で決める（決定記録 0046 §2.7）
    // health.source は取り込み経路（空気の温度）だけの出どころなので、そう書く（Codex P2）
    const label = window.ColdaisleAirflowStatus.ingestSourceLabel(page.ingestSource);
    source.textContent = `${AIR_SOURCE_PREFIX}${page.pageNote ? `${label.text}（${page.pageNote}）` : label.text}`;
    source.classList.toggle("mock", !label.live);
  }
  // 内部テレメトリの出どころ。見出しに置き、グラフのタブでも見えるようにする
  const telemetry = document.getElementById("telemetry-label");
  telemetry.textContent = `回転数・PWM・CPU・GPU：${valueKind("fan.front.pwm")}`;
  telemetry.classList.toggle("mock", Boolean(page.mockName));
  document.getElementById("value-kind-key").textContent =
    `空気の温度＝${valueKind("air.room")}、回転数・PWM・CPU・GPU＝${valueKind("fan.front.pwm")}`;
  decision.textContent = page.control && page.control.decision_id ? `判断 ${page.control.decision_id}` : "";
}

// ---------------------------------------------------------------- 制御の状態

function renderControl() {
  const control = page.control;
  const chips = document.getElementById("control-chips");
  chips.replaceChildren();
  const list = control
    ? control.chips
    : CONTROL_CHIPS.map((k) => ({ k, v: "未接続", tone: "na" }));
  for (const chip of list) {
    const node = el("div", `chip ${chip.tone || ""}`.trim());
    node.appendChild(el("span", "k", chip.k));
    node.appendChild(el("span", "v", chip.v));
    chips.appendChild(node);
  }
  document.getElementById("control-note").textContent = control
    ? "制御の状態は模擬データです。実際の制御とは関係ありません。"
    : "制御の状態は未接続です。制御デーモンの判断記録（#74 / #82）を読む API がまだ無いため表示していません。" +
      window.ColdaisleAirflowStatus.measuredNote(displaySource());

  const alert = control && control.alert;
  const box = document.getElementById("control-alert");
  if (alert) {
    document.getElementById("control-alert-text").textContent = alert.text;
    document.getElementById("control-alert-note").textContent = alert.note || "";
    box.classList.remove("hidden");
  } else {
    box.classList.add("hidden");
  }
}

// ---------------------------------------------------------------- 側面図

function ensureMarkers(svg) {
  const defs = svg.querySelector("defs");
  const colors = [...AIR_COLORS, NO_DATA_COLOR];
  colors.forEach((color, index) => {
    const id = `af-arrow-${index}`;
    if (document.getElementById(id)) return;
    const marker = svgNode(
      "marker",
      { id, viewBox: "0 0 10 10", refX: 5, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto" },
      defs
    );
    svgNode("path", { d: "M0 0 10 5 0 10z", fill: color }, marker);
  });
}

function markerFor(color) {
  const index = [...AIR_COLORS, NO_DATA_COLOR].indexOf(color);
  return `url(#af-arrow-${index < 0 ? AIR_COLORS.length : index})`;
}

/** 札（枠＋2行）。枠の色＝温度。値が無い・古いときは点線の灰色。 */
function drawTag(parent, { x, y, w, h, title, lines, color, dashed, tone }) {
  const group = svgNode("g", {}, parent);
  svgNode(
    "rect",
    {
      x, y, width: w, height: h, rx: 6,
      fill: tone === "bad" ? "rgba(61, 28, 28, 0.94)" : "rgba(15, 18, 22, 0.92)",
      stroke: color,
      "stroke-width": 1.5,
      "stroke-dasharray": dashed ? "4 3" : "none",
    },
    group
  );
  svgText(group, x + w / 2, y + 13, title, {
    "text-anchor": "middle", "font-size": 10, fill: tone === "bad" ? "#f29a9a" : "#c3cad4", "font-weight": 600,
  });
  lines.forEach((line, index) => {
    svgText(group, x + w / 2, y + 29 + index * 15, line.text, {
      "text-anchor": "middle",
      "font-size": line.small ? 11 : 13,
      "font-weight": 600,
      class: "num",
      fill: line.muted ? "#7d8794" : "#e6e9ee",
    });
  });
  return group;
}

function lineFor(result, suffix) {
  const tag = result.tag && result.value !== null ? `（${result.tag}）` : "";
  return { text: `${suffix || ""}${result.text}${tag}`, muted: result.quality !== "ok" };
}

function renderDiagram() {
  const svg = document.getElementById("diagram");
  ensureMarkers(svg);

  for (const path of svg.querySelectorAll(".flow")) {
    const color = airColor(path.dataset.air);
    path.setAttribute("stroke", color);
    path.setAttribute("marker-end", markerFor(color));
  }

  const labels = document.getElementById("labels");
  labels.replaceChildren();

  for (const point of AIR_POINTS) {
    const result = reading(point.metric, 1, "℃");
    const color = airColor(point.metric);
    drawTag(labels, {
      x: point.x, y: point.y, w: point.w, h: 38,
      title: point.label,
      lines: [lineFor(result)],
      color,
      dashed: color === NO_DATA_COLOR,
    });
  }

  const delta = derivedValue(CASE_DELTA.derived);
  drawTag(labels, {
    x: CASE_DELTA.x, y: CASE_DELTA.y, w: CASE_DELTA.w, h: 38,
    title: CASE_DELTA.label,
    lines: [delta === null ? { text: "未取得", muted: true } : { text: `${delta < 0 ? "−" : "+"}${fmt(Math.abs(delta), 2)}℃` }],
    color: NO_DATA_COLOR,
    dashed: delta === null,
  });

  const zones = page.control ? page.control.zones : {};
  for (const zone of ZONES) {
    const fault = Boolean(zones[zone.key] && zones[zone.key].fault);
    const rpm = reading(zone.rpm, 0, " rpm");
    drawTag(labels, {
      x: zone.chip.x, y: zone.chip.y, w: 80, h: 38,
      title: fault ? `${zone.name} 停止` : zone.name,
      lines: [lineFor(rpm)],
      color: fault ? "#e25c5c" : "#7fb8ec",
      dashed: rpm.value === null,
      tone: fault ? "bad" : null,
    });
  }
  document.getElementById("rear-fan-fault").classList.toggle("hidden", !(zones.rear && zones.rear.fault));

  for (const source of HEAT_SOURCES) {
    const status = sourceStatus(source);
    if (source.block) paintBlock(source.block, status);
    const util = { ...lineFor(utilReading(source), "使用率 "), small: true };
    const temp = { ...lineFor(reading(source.temp, 0, "℃"), "温度 "), small: true };
    if (!status) {
      drawTag(labels, {
        x: source.chip.x, y: source.chip.y, w: source.chip.w, h: 54,
        title: source.name, lines: [util, temp], color: "#c3cad4",
      });
      continue;
    }
    drawStatusTag(labels, source, status, [util, temp]);
  }
}

/** 熱源の枠（GPU のブロック）を状態の色にする。状態が無ければ元の色に戻す。 */
function paintBlock(id, status) {
  const block = document.getElementById(id);
  if (!block) return;
  if (!block.dataset.stroke) {
    block.dataset.stroke = block.getAttribute("stroke");
    block.dataset.fill = block.getAttribute("fill");
  }
  block.setAttribute("stroke", status ? STATUS_COLOR[status.tone] : block.dataset.stroke);
  block.setAttribute("fill", status ? STATUS_FILL[status.tone] : block.dataset.fill);
  block.setAttribute("stroke-width", status ? 2.5 : 2);
}

/**
 * 状態付きの札（案1）。見出しの下に小さく理由、右に小さな札（「熱による制限」など）。
 * 札を下へ伸ばすと GPU 吸気の札に重なるので、行の間隔を詰めて高さを保つ。
 */
function drawStatusTag(parent, source, status, lines) {
  const color = STATUS_COLOR[status.tone];
  const x = source.chip.x;
  const y = source.chip.y - 4;
  // 理由の行（9px）が収まる幅にする。左端は固定（GPU 吸気の札とは高さで避けている）
  const w = Math.max(source.chip.w + 50, status.reason.length * 9 + 16);
  const group = svgNode("g", {}, parent);
  svgNode("rect", { x, y, width: w, height: 60, rx: 6, fill: "rgba(15, 18, 22, 0.94)", stroke: color, "stroke-width": 2 }, group);
  svgText(group, x + 8, y + 13, source.name, { "font-size": 11, "font-weight": 700, fill: color });
  svgText(group, x + 8, y + 26, status.reason, { "font-size": 9, fill: "#c3cad4" });
  lines.forEach((line, index) => {
    svgText(group, x + 8, y + 41 + index * 14, line.text, {
      "font-size": 11, "font-weight": 600, class: "num", fill: line.muted ? "#7d8794" : "#e6e9ee",
    });
  });
  const pillWidth = status.text.length * 10 + 14;
  svgNode("rect", { x: x + w + 6, y: y + 2, width: pillWidth, height: 16, rx: 8, fill: color }, group);
  svgText(group, x + w + 6 + pillWidth / 2, y + 14, status.text, {
    "text-anchor": "middle", "font-size": 10, "font-weight": 700, fill: "#0f1216",
  });
}

function renderLegend() {
  const legend = document.getElementById("air-legend");
  legend.replaceChildren();
  if (!page.scale) {
    legend.appendChild(el("span", null, "空気の温度の色分けを読み込めません（色は付けていません）"));
    return;
  }
  const t = page.scale.thresholds_c;
  legend.appendChild(el("span", null, page.scale.provisional ? "空気の温度（区切りは仮）" : "空気の温度"));
  const labels = [`〜${t[0]}`, ...t.slice(0, -1).map((v, i) => `${v}–${t[i + 1]}`), `${t[t.length - 1]}℃〜`];
  labels.forEach((label, index) => {
    const item = el("span");
    const sw = el("i", "sw");
    sw.style.background = AIR_COLORS[index];
    item.appendChild(sw);
    item.appendChild(document.createTextNode(label));
    legend.appendChild(item);
  });
  const none = el("span");
  const sw = el("i", "sw");
  sw.style.background = NO_DATA_COLOR;
  none.appendChild(sw);
  none.appendChild(document.createTextNode("未取得・古い"));
  legend.appendChild(none);
}

// ---------------------------------------------------------------- 系統ごとの札

function pct(value) {
  return `${Math.round(value * 100)}%`;
}

function renderZones() {
  const container = document.getElementById("zones");
  container.replaceChildren();
  for (const zone of ZONES) {
    const control = page.control ? page.control.zones[zone.key] : null;
    const panel = el("section", `panel zone${control && control.fault ? " fault" : ""}`);
    const head = el("div", "panel-head");
    head.appendChild(el("h3", "name", zone.name));
    head.appendChild(el("span", "role", zone.role));
    panel.appendChild(head);

    const big = el("div", "big");
    big.appendChild(valueNode(reading(zone.rpm, 0, ""), "rpm"));
    big.appendChild(el("span", "unit", "rpm"));
    const pwm = reading(zone.pwm, 0, "%");
    const pwmNode = el("span", "pwm", `PWM（${valueKind(zone.pwm)}） `);
    pwmNode.appendChild(valueNode(pwm));
    big.appendChild(pwmNode);
    panel.appendChild(big);

    if (!control) {
      const box = el("div", "na-box");
      box.appendChild(el("b", null, "未接続"));
      box.appendChild(
        document.createTextNode(" — 制御の出力（要求 → 実際）・決め手・推定風量は、制御の判断記録を読めるようになったら表示します。")
      );
      panel.appendChild(box);
      container.appendChild(panel);
      continue;
    }

    const bar = el("div");
    const barHead = el("div", "bar-head");
    barHead.appendChild(el("span", null, "制御の出力（0〜100%）"));
    barHead.appendChild(el("span", null, `要求 ${pct(control.requested)} → 実際 ${pct(control.effective)}`));
    bar.appendChild(barHead);
    const track = el("div", "track");
    const fill = el("div", `fill${control.forced ? " forced" : ""}`);
    fill.style.width = pct(control.effective);
    track.appendChild(fill);
    const floor = el("div", "floor");
    floor.style.left = pct(Math.min(control.floor, 0.995));
    track.appendChild(floor);
    const req = el("div", "req");
    req.style.left = pct(control.requested);
    track.appendChild(req);
    bar.appendChild(track);
    const foot = el("div", "bar-foot");
    foot.appendChild(el("span", null, "0%"));
    foot.appendChild(el("span", "fl", `安全下限 ${pct(control.floor)}`));
    foot.appendChild(el("span", null, "100%"));
    bar.appendChild(foot);
    panel.appendChild(bar);

    const bound = el("div", "bound");
    bound.appendChild(el("span", "k", "決め手"));
    bound.appendChild(el("span", `bb ${control.bound_tone || ""}`.trim(), control.bound_by));
    panel.appendChild(bound);

    // 推定値は点線の枠で囲い、「推定」と書く。**実測 CFM と誤解させない**
    const est = el("div", "est");
    est.appendChild(el("div", "tag", "推定"));
    const idx = el("div", "kv");
    idx.appendChild(el("span", null, "風量指数（このファンの最大比）"));
    idx.appendChild(el("span", "mono", fmt(control.airflow_index, 2)));
    est.appendChild(idx);
    const flow = el("div", "kv");
    flow.appendChild(el("span", null, "推定風量"));
    flow.appendChild(el("span", "mono", `${fmt(control.estimated_flow, 2)} EFU`));
    est.appendChild(flow);
    panel.appendChild(est);

    panel.appendChild(el("div", "reason", `理由：${control.reason}`));
    container.appendChild(panel);
  }
}

// ---------------------------------------------------------------- なぜこの回転数か

function renderTrace() {
  const tabs = document.getElementById("trace-tabs");
  const container = document.getElementById("trace");
  tabs.replaceChildren();
  container.replaceChildren();
  if (!page.control) {
    const box = el("div", "na-box");
    box.appendChild(el("b", null, "未接続"));
    box.appendChild(
      document.createTextNode(
        " — 1回の判断の記録（運転方針 → 学習モデルの提案 → 信頼度チェック → 急変への対応 → 安全制御 → 最終的な出力 → ファン）を読めるようになったら、ここに系統ごとに表示します。"
      )
    );
    container.appendChild(box);
    return;
  }
  // 系統の切り替えは**見る対象を変えるだけ**。制御には何も送らない
  for (const zone of ZONES) {
    const button = el("button", zone.key === page.zone ? "on" : null, zone.name);
    button.type = "button";
    button.setAttribute("aria-pressed", String(zone.key === page.zone));
    button.addEventListener("click", () => {
      page.zone = zone.key;
      renderTrace();
    });
    tabs.appendChild(button);
  }
  const steps = page.control.zones[page.zone].trace;
  steps.forEach((step, index) => {
    const row = el("div", "step");
    const rail = el("div", "rail");
    rail.appendChild(el("div", `node ${step.final ? "final" : step.tone || ""}`.trim()));
    if (index < steps.length - 1) rail.appendChild(el("div", "wire"));
    row.appendChild(rail);
    const body = el("div", `step-body${step.final ? " final" : ""}`);
    const head = el("div", "step-head");
    head.appendChild(el("span", "step-title", step.title));
    if (step.value) head.appendChild(el("span", `step-value ${step.tone || ""}`.trim(), step.value));
    body.appendChild(head);
    if (step.detail) body.appendChild(el("div", "step-detail", step.detail));
    row.appendChild(body);
    container.appendChild(row);
  });
}

// ---------------------------------------------------------------- バランス・熱源・参照

function renderBalance() {
  const state = document.getElementById("balance-state");
  const container = document.getElementById("balance");
  container.replaceChildren();
  const balance = page.control && page.control.balance;
  if (!balance) {
    state.textContent = "未接続";
    state.className = "pill";
    const box = el("div", "na-box");
    box.appendChild(el("b", null, "未接続"));
    box.appendChild(document.createTextNode(" — 吸気と排気の釣り合い（推定）は Air Balance の推定（#81）を読めるようになったら表示します。"));
    container.appendChild(box);
    return;
  }
  state.textContent = balance.state;
  state.className = `pill ${balance.tone || ""}`.trim();
  const value = el("div", "balance-value");
  value.appendChild(el("span", "v", fmt(balance.ratio, 2)));
  value.appendChild(el("span", "note", "排気 ÷ 吸気（推定）"));
  container.appendChild(value);
  const scale = el("div", "balance-scale");
  const bands = el("div", "bands");
  for (const [width, color] of [["30%", "#24405a"], ["40%", "#1f3a2a"], ["30%", "#4a3020"]]) {
    const band = el("div");
    band.style.width = width;
    band.style.background = color;
    bands.appendChild(band);
  }
  scale.appendChild(bands);
  const marker = el("div", "marker");
  marker.style.left = pct(Math.max(0, Math.min(1, balance.position)));
  scale.appendChild(marker);
  const labels = el("div", "labels");
  for (const text of ["吸気が多い", "釣り合い", "排気が多い"]) labels.appendChild(el("span", null, text));
  scale.appendChild(labels);
  container.appendChild(scale);
}

function kvRow(label, result) {
  const row = el("div", "kv");
  row.appendChild(el("span", null, label));
  row.appendChild(valueNode(result, "mono"));
  return row;
}

function renderHeat() {
  const container = document.getElementById("heat");
  container.replaceChildren();
  for (const source of [...HEAT_SOURCES].reverse()) {
    const block = el("div", "src");
    block.appendChild(el("span", "src-name", source.name));
    block.appendChild(kvRow("使用率", utilReading(source)));
    block.appendChild(kvRow("温度", reading(source.temp, 1, "℃")));
    block.appendChild(kvRow("消費電力", reading(source.power, 0, " W")));
    const status = sourceStatus(source); // GPU のスロットリング（案1）。null なら何も出さない
    if (status) {
      block.appendChild(el("span", `bb ${status.tone}`, status.text));
      block.appendChild(el("span", "note", status.reason));
    }
    container.appendChild(block);
  }
}

function renderReference() {
  const container = document.getElementById("reference");
  container.replaceChildren();
  for (const item of REFERENCE) container.appendChild(kvRow(item.label, reading(item.metric, 0, " rpm")));
}

function renderNow() {
  renderSource();
  renderControl();
  renderDiagram();
  renderLegend();
  renderZones();
  renderTrace();
  renderBalance();
  renderHeat();
  renderReference();
}

// ---------------------------------------------------------------- 更新

/**
 * 定期更新。**同時に1本だけ**（遅い応答が重なって古い結果で上書きしない）。
 * 模擬データのときは実データを取りに行かない。
 */
async function refresh() {
  if (page.mockName || page.refreshing) return;
  page.refreshing = true;
  try {
    // **片方の失敗でもう片方の応答を捨てない**（Codex P2）。それぞれを別々に反映し、失敗は別々に言う
    const [latest, health] = await Promise.allSettled([fetchJson("/api/v1/latest"), fetchJson("/api/v1/health")]);
    const errors = [];
    if (latest.status === "fulfilled") {
      page.latest = latest.value;
    } else {
      // 前回の値を「いまの値」として出し続けない。全部「未取得」にする
      page.latest = null;
      errors.push(`最新値（/api/v1/latest）を取得できません: ${latest.reason.message}`);
    }
    if (health.status === "fulfilled") {
      renderHealth(health.value);
    } else {
      // 鮮度も出どころも確かめられない。前回の health の表示（古い・未来・最終受信）を残さない
      page.ingestSource = null;
      showBanner("banner", null);
      document.getElementById("age-label").textContent = "";
      errors.push(`状態（/api/v1/health）を取得できません — データの新しさと出どころを確認できません: ${health.reason.message}`);
    }
    showBanner("api-banner", errors.join(" / ") || null);
    renderNow();
  } catch (error) {
    showBanner("api-banner", `画面を更新できません: ${error.message}`);
  } finally {
    page.refreshing = false;
  }
}

async function loadScale() {
  try {
    const body = await fetchJson("/api/v1/airflow/config");
    page.scale = body.air_temperature;
    const cpu = body.cpu_utilization;
    page.cpuMeasured = cpu && typeof cpu.measured === "boolean" ? cpu.measured : null;
  } catch (error) {
    page.scale = null; // 色を付けない。凡例で「読み込めません」と言う
    page.cpuMeasured = null;
  }
}

function loadMockScript() {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "airflow-mock.js";
    script.onload = () => resolve(window.ColdaisleAirflowMock);
    script.onerror = () => reject(new Error("airflow-mock.js を読み込めません"));
    document.head.appendChild(script);
  });
}

// ---------------------------------------------------------------- グラフ

function renderRanges() {
  const container = document.getElementById("ranges");
  container.replaceChildren();
  for (const range of RANGES) {
    const button = el("button", range === graph.range ? "on" : null, range.label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(range === graph.range));
    button.addEventListener("click", () => {
      graph.range = range;
      renderRanges();
      // **新しい期間の応答が届くまで、前の期間の線・軸・値を出さない**（Codex P2）
      clearGraph();
      document.getElementById("graph-note").textContent = "読み込み中…";
      loadHistory();
    });
    container.appendChild(button);
  }
}

function hasData(series) {
  const entry = graph.data.get(series.key);
  return Boolean(entry && entry.points.some((p) => p.value !== null));
}

function renderToggles() {
  const container = document.getElementById("toggles");
  container.replaceChildren();
  for (const group of GROUPS) {
    container.appendChild(el("span", "group", group.name));
    for (const series of group.items) {
      const button = el("button", "tg");
      button.type = "button";
      const sw = el("span", `sw${series.kind === "band" ? " band" : ""}`);
      sw.style.background = series.color;
      button.appendChild(sw);
      let label = series.label;
      if (series.metric === null) label += "（未計測）";
      else if (graph.loaded && !hasData(series)) label += "（データなし）";
      button.appendChild(document.createTextNode(label));
      if (series.metric === null) {
        button.disabled = true;
        button.setAttribute("aria-pressed", "false");
        button.title = "この値はまだ計測していません";
      } else {
        button.setAttribute("aria-pressed", String(graph.enabled.has(series.key)));
        button.addEventListener("click", () => {
          if (graph.enabled.has(series.key)) graph.enabled.delete(series.key);
          else graph.enabled.add(series.key);
          renderToggles();
          drawGraph();
        });
      }
      container.appendChild(button);
    }
  }
}

/** 想定される点の間隔。**線を切る判断に使う**（決定記録 0011 §2.3.1）。 */
function expectedStep(entry) {
  if (STEP_MS[entry.agg]) return STEP_MS[entry.agg];
  let smallest = Infinity;
  for (let i = 1; i < entry.points.length; i += 1) {
    const delta = entry.points[i].ts_ms - entry.points[i - 1].ts_ms;
    if (delta > 0 && delta < smallest) smallest = delta;
  }
  return Number.isFinite(smallest) ? smallest : 0;
}

/** その時刻の値。**点が遠いときは値が無いとみなす**（測れていない区間を埋めない）。 */
function valueAt(entry, ts) {
  if (!entry || entry.points.length === 0) return null;
  const points = entry.points;
  let lo = 0;
  let hi = points.length - 1;
  if (ts < points[0].ts_ms) return null;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (points[mid].ts_ms <= ts) lo = mid;
    else hi = mid - 1;
  }
  const step = expectedStep(entry);
  if (step > 0 && ts - points[lo].ts_ms > step * GAP_FACTOR) return null;
  return points[lo].value;
}

function fmtTime(ms) {
  const date = new Date(ms);
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  if (graph.range.window === "7d") return `${date.getMonth() + 1}/${date.getDate()} ${hh}:${mm}`;
  return `${hh}:${mm}`;
}

function formatSeries(series, value) {
  if (value === null || value === undefined) return "—";
  if (series.kind === "fan") return `${fmt(value, 0)} rpm`;
  if (series.kind === "band") return `${fmt(value, 0)}%`;
  return `${fmt(value, 1)}℃`;
}

const PAD = { left: 52, right: 12, top: 10, bottom: 22 };

function frame(svg, padLeft = PAD.left) {
  const width = Math.max(320, Math.round(svg.getBoundingClientRect().width || 800));
  const height = Number(svg.getAttribute("height"));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.replaceChildren();
  const x0 = padLeft;
  const x1 = width - PAD.right;
  const span = Math.max(1, graph.toMs - graph.fromMs);
  return {
    width,
    height,
    x0,
    x1,
    sx: (ts) => x0 + ((ts - graph.fromMs) / span) * (x1 - x0),
    ts: (x) => graph.fromMs + ((x - x0) / (x1 - x0)) * span,
  };
}

function drawLines(svg, box, sy, series) {
  for (const item of series) {
    const entry = graph.data.get(item.key);
    if (!entry) continue;
    const step = expectedStep(entry);
    let run = [];
    let previous = null;
    const flush = () => {
      if (run.length > 1) {
        svgNode("polyline", { points: run.join(" "), fill: "none", stroke: item.color, "stroke-width": 1.8 }, svg);
      }
      run = [];
    };
    for (const point of entry.points) {
      const jumped = previous !== null && step > 0 && point.ts_ms - previous > step * GAP_FACTOR;
      if (point.value === null || jumped) flush();
      previous = point.ts_ms;
      if (point.value !== null) run.push(`${box.sx(point.ts_ms).toFixed(1)},${sy(point.value).toFixed(1)}`);
    }
    flush();
  }
}

/** 使用率を濃さで描く（背景の帯・使用率の段で共用）。 */
function drawBands(svg, box, entry, color, top, height, maxOpacity) {
  if (!entry) return;
  const step = expectedStep(entry);
  entry.points.forEach((point, index) => {
    if (point.value === null) return;
    const next = entry.points[index + 1];
    const end = next && (!step || next.ts_ms - point.ts_ms <= step * GAP_FACTOR) ? next.ts_ms : point.ts_ms + step;
    const x = box.sx(point.ts_ms);
    const width = Math.max(0.5, box.sx(end) - x);
    const opacity = Math.max(0, Math.min(1, point.value / 100)) * maxOpacity;
    svgNode("rect", { x: x.toFixed(1), y: top, width: width.toFixed(1), height, fill: color, "fill-opacity": opacity.toFixed(3) }, svg);
  });
}

function axisX(svg, box, y) {
  for (let i = 0; i <= 4; i += 1) {
    const ts = graph.fromMs + ((graph.toMs - graph.fromMs) * i) / 4;
    const anchor = i === 0 ? "start" : i === 4 ? "end" : "middle";
    svgText(svg, box.sx(ts), y, fmtTime(ts), { "text-anchor": anchor, fill: "#7d8794", "font-size": 11 });
  }
}

function niceRange(values, step, minSpan) {
  let lo = Math.floor(Math.min(...values) / step) * step;
  let hi = Math.ceil(Math.max(...values) / step) * step;
  while (hi - lo < minSpan) {
    lo -= step;
    hi += step;
  }
  return [lo, hi];
}

function cursorLayer(svg, box) {
  const line = svgNode("line", { class: "cursor", x1: 0, x2: 0, y1: PAD.top, y2: box.height - PAD.bottom, stroke: "#e6e9ee", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" }, svg);
  const hit = svgNode("rect", { x: box.x0, y: 0, width: box.x1 - box.x0, height: box.height, fill: "transparent" }, svg);
  hit.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * box.width;
    graph.cursorMs = Math.max(graph.fromMs, Math.min(graph.toMs, box.ts(x)));
    updateCursor();
  });
  hit.addEventListener("mouseleave", () => {
    graph.cursorMs = null;
    updateCursor();
  });
  return line;
}

const cursors = [];

function updateCursor() {
  for (const { line, box } of cursors) {
    if (graph.cursorMs === null) {
      line.setAttribute("visibility", "hidden");
    } else {
      const x = box.sx(graph.cursorMs).toFixed(1);
      line.setAttribute("x1", x);
      line.setAttribute("x2", x);
      line.setAttribute("visibility", "visible");
    }
  }
  renderReadout();
}

function drawTempChart() {
  const svg = document.getElementById("chart-temp");
  const box = frame(svg);
  const top = PAD.top;
  const bottom = box.height - PAD.bottom;
  const temps = GROUPS.flatMap((g) => g.items).filter((s) => s.kind === "temp" && graph.enabled.has(s.key));
  const bands = GROUPS.flatMap((g) => g.items).filter((s) => s.kind === "band" && graph.enabled.has(s.key));
  for (const band of bands) drawBands(svg, box, graph.data.get(band.key), band.color, top, bottom - top, 0.3);

  const values = temps.flatMap((s) => (graph.data.get(s.key) || { points: [] }).points.map((p) => p.value)).filter((v) => v !== null);
  if (values.length === 0) {
    svgText(svg, box.width / 2, box.height / 2, "表示できる温度のデータがありません", { "text-anchor": "middle", fill: "#9aa5b3", "font-size": 12 });
  } else {
    const [lo, hi] = niceRange(values, 5, 10);
    const sy = (v) => bottom - ((v - lo) / (hi - lo)) * (bottom - top);
    for (let v = lo; v <= hi; v += 5) {
      svgNode("line", { x1: box.x0, x2: box.x1, y1: sy(v), y2: sy(v), stroke: "#232a33" }, svg);
      if ((v - lo) % 10 === 0 || hi - lo <= 20) svgText(svg, box.x0 - 6, sy(v) + 4, `${v}℃`, { "text-anchor": "end", fill: "#7d8794", "font-size": 11 });
    }
    drawLines(svg, box, sy, temps);
  }
  axisX(svg, box, box.height - 5);
  cursors.push({ line: cursorLayer(svg, box), box });
}

function drawUtilRows() {
  const container = document.getElementById("util-rows");
  container.replaceChildren();
  for (const series of GROUPS.flatMap((g) => g.items).filter((s) => s.kind === "band")) {
    const row = el("div", "util-row");
    row.appendChild(el("div", "name", series.label.replace("使用率", "")));
    const svg = svgNode("svg", { height: 26 });
    row.appendChild(svg);
    container.appendChild(row);
    // 名前の列が温度グラフの軸ラベルの幅ぶんあるので、左の余白は取らない（時刻の位置が揃う）
    const box = frame(svg, 0);
    svgNode("rect", { x: 0, y: 0, width: box.width, height: 26, fill: "#1b2027" }, svg);
    if (series.metric === null) {
      svgText(svg, box.width / 2, 17, "計測していません", { "text-anchor": "middle", fill: "#7d8794", "font-size": 11 });
    } else if (!hasData(series)) {
      svgText(svg, box.width / 2, 17, "データがありません", { "text-anchor": "middle", fill: "#7d8794", "font-size": 11 });
    } else {
      drawBands(svg, box, graph.data.get(series.key), series.color, 0, 26, 1);
    }
  }
}

function drawFanChart() {
  const svg = document.getElementById("chart-fan");
  const box = frame(svg);
  const top = PAD.top;
  const bottom = box.height - PAD.bottom;
  const visible = FAN_SERIES.filter((s) => graph.enabled.has(s.key));
  const values = visible.flatMap((s) => (graph.data.get(s.key) || { points: [] }).points.map((p) => p.value)).filter((v) => v !== null);
  const hi = values.length ? Math.max(500, Math.ceil(Math.max(...values) / 500) * 500) : 2000;
  const sy = (v) => bottom - (v / hi) * (bottom - top);
  for (const v of [0, hi / 2, hi]) {
    svgNode("line", { x1: box.x0, x2: box.x1, y1: sy(v), y2: sy(v), stroke: "#232a33" }, svg);
    svgText(svg, box.x0 - 6, sy(v) + 4, v === 0 ? "0 rpm" : fmt(v, 0), { "text-anchor": "end", fill: "#7d8794", "font-size": 11 });
  }
  if (values.length === 0) {
    svgText(svg, box.width / 2, box.height / 2, "表示できる回転数のデータがありません", { "text-anchor": "middle", fill: "#9aa5b3", "font-size": 12 });
  }
  drawLines(svg, box, sy, visible);
  axisX(svg, box, box.height - 5);
  cursors.push({ line: cursorLayer(svg, box), box });

  const legend = document.getElementById("fan-legend");
  legend.replaceChildren();
  for (const series of FAN_SERIES) {
    const button = el("button", "tg");
    button.type = "button";
    button.setAttribute("aria-pressed", String(graph.enabled.has(series.key)));
    const sw = el("span", "key-line");
    sw.style.background = series.color;
    button.appendChild(sw);
    button.appendChild(document.createTextNode(hasData(series) || !graph.loaded ? series.label : `${series.label}（データなし）`));
    button.addEventListener("click", () => {
      if (graph.enabled.has(series.key)) graph.enabled.delete(series.key);
      else graph.enabled.add(series.key);
      drawGraph();
    });
    legend.appendChild(button);
  }
}

function renderReadout() {
  const container = document.getElementById("readout");
  const title = document.getElementById("readout-title");
  const time = document.getElementById("readout-time");
  container.replaceChildren();
  if (graph.fromMs === null) return;
  const at = graph.cursorMs === null ? graph.toMs : graph.cursorMs;
  title.textContent = graph.cursorMs === null ? "期間の終わりの値" : "カーソル位置の値";
  time.textContent = fmtTime(at);
  const groups = [...GROUPS, { name: "ファン", items: FAN_SERIES }];
  for (const group of groups) {
    container.appendChild(el("div", "group", group.name));
    for (const series of group.items) {
      const row = el("div", "row");
      const name = el("span", "name");
      const sw = el("span", `sw${series.kind === "band" ? " band" : ""}`);
      sw.style.background = series.color;
      name.appendChild(sw);
      name.appendChild(document.createTextNode(series.label));
      row.appendChild(name);
      const value = series.metric === null ? "未計測" : formatSeries(series, valueAt(graph.data.get(series.key), at));
      row.appendChild(el("span", "mono", value));
      container.appendChild(row);
    }
  }
  const regime = el("div", "row");
  regime.style.borderTop = "1px solid #2a323d";
  regime.style.paddingTop = "8px";
  regime.style.marginTop = "6px";
  regime.appendChild(el("span", "name", "負荷の傾向"));
  regime.appendChild(el("span", null, page.control ? page.control.regime : "未接続"));
  container.appendChild(regime);
}

function drawGraph() {
  if (graph.fromMs === null) return;
  cursors.length = 0;
  drawTempChart();
  drawUtilRows();
  drawFanChart();
  renderToggles();
  renderReadout();
}

/**
 * 履歴を捨て、グラフ・軸・読み取り値を空にする。**取得に失敗した期間の下に、
 * 前の期間の線・軸・値を残さない**（Codex P2）。注記（graph-note）は呼び出し側が書く。
 */
function clearGraph() {
  graph.data = new Map();
  graph.fromMs = null;
  graph.toMs = null;
  graph.cursorMs = null;
  graph.loaded = false;
  cursors.length = 0;
  for (const id of ["chart-temp", "chart-fan", "util-rows", "readout", "fan-legend"]) {
    document.getElementById(id).replaceChildren();
  }
  document.getElementById("readout-time").textContent = "";
  document.getElementById("graph-agg").textContent = "";
  renderToggles();
}

/** 履歴。**最後に頼んだ期間の応答だけを使う**（切り替え直後に古い期間の応答で上書きしない）。 */
async function loadHistory() {
  const token = ++graph.token;
  const range = graph.range;
  const note = document.getElementById("graph-note");
  const agg = document.getElementById("graph-agg");
  const wanted = ALL_SERIES.filter((s) => s.metric !== null);
  try {
    let results;
    let fromMs;
    let toMs;
    if (page.mockName) {
      toMs = Date.now();
      fromMs = toMs - page.mock.windowMs(range.window);
      results = wanted.map((s) => ({ key: s.key, body: page.mock.series(page.mockName, s.metric, fromMs, toMs, range.window) }));
    } else {
      results = await Promise.all(
        wanted.map((s) =>
          fetchJson("/api/v1/series", { metric: s.metric, window: range.window, agg: range.agg }).then((body) => ({ key: s.key, body }))
        )
      );
      fromMs = results[0].body.from;
      toMs = results[0].body.to;
    }
    if (token !== graph.token) return;
    // 生データの suspect / missing は値なしにしてから持つ（線・帯・読み取り値の全部で欠けとして扱う）
    const usable = window.ColdaisleAirflowStatus.usablePoints;
    graph.data = new Map(
      results.map(({ key, body }) => [key, { points: usable(body.points, body.agg), agg: body.agg, downsampled: body.downsampled }])
    );
    graph.fromMs = fromMs;
    graph.toMs = toMs;
    graph.loaded = true;
    const first = results[0] && results[0].body;
    agg.textContent = page.mockName
      ? "粒度: 模擬"
      : `粒度: ${first.agg === "raw" ? "生データ" : first.agg}${first.downsampled ? "（点数の上限に合わせて粗くしました）" : ""}`;
    note.textContent = "線が途切れている区間は測れていません。";
    drawGraph();
  } catch (error) {
    if (token !== graph.token) return; // 新しい期間を頼んだあとに届いた古い失敗は無視する
    clearGraph();
    note.textContent = `履歴を取得できません: ${error.message}`;
  }
}

// ---------------------------------------------------------------- タブ

function currentTab() {
  return window.location.hash === "#graph" ? "graph" : "now";
}

function applyTab() {
  const tab = currentTab();
  document.getElementById("view-now").classList.toggle("hidden", tab !== "now");
  document.getElementById("view-graph").classList.toggle("hidden", tab !== "graph");
  for (const [id, name] of [["tab-now", "now"], ["tab-graph", "graph"]]) {
    const link = document.getElementById(id);
    link.classList.toggle("on", tab === name);
    if (tab === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (tab === "graph") {
    if (!graph.loaded) loadHistory();
    else drawGraph(); // 非表示の間は幅が 0 なので描き直す
  }
}

// ---------------------------------------------------------------- 起動

/**
 * 起動。**最初の取得に失敗しても復帰の仕組みを止めない**（決定記録 0011 §2.7.1）。
 * タイマーを先に立ててから読みに行く。
 */
async function start() {
  const requested = new URLSearchParams(window.location.search).get("mock");
  renderRanges();
  renderToggles();
  window.addEventListener("hashchange", applyTab);
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => currentTab() === "graph" && drawGraph(), 150);
  });

  if (requested !== null && MOCK_SCENARIOS[requested]) {
    page.mockName = MOCK_SCENARIOS[requested];
    await loadScale();
    try {
      page.mock = await loadMockScript();
      const scenario = page.mock.scenario(page.mockName);
      page.latest = scenario.latest;
      page.control = scenario.control;
      document.getElementById("age-label").textContent = scenario.control ? `${fmt(scenario.control.age_seconds, 1)} 秒前` : "";
    } catch (error) {
      showBanner("banner", `模擬データを読み込めません: ${error.message}`);
    }
    renderNow();
    applyTab();
    return;
  }
  if (requested !== null) {
    // 帯（#banner）は健全性の表示で上書きされるので、出どころの表示に添える
    page.pageNote = `模擬シナリオ「${requested}」は無いため API のデータを表示。normal / override / throttle`;
  }

  setInterval(refresh, REFRESH_MS);
  setInterval(() => currentTab() === "graph" && loadHistory(), HISTORY_REFRESH_MS);
  renderNow(); // 取得前でも「未取得」「未接続」の枠を出す
  applyTab();
  await loadScale();
  await refresh();
}

start();
