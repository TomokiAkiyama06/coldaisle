// 開発用・フォールバック用ダッシュボード（#17）。本番UIは Workspace 側なので作り込まない。
//
// 原則が2つある。
//   1. **無音で古い値を出さない。** 古ければ画面全体で言う（api-contract §3）
//   2. **API から来た文字列を HTML として解釈しない。** 常に textContent（要件 §7.4）
"use strict";

const RANGES = [
  { label: "1h", window: "1h", agg: "raw" },
  { label: "6h", window: "6h", agg: "1m" },
  { label: "24h", window: "24h", agg: "1m" },
  { label: "7d", window: "7d", agg: "1h" },
];

const QUALITY_LABEL = { ok: "正常", missing: "欠測", suspect: "疑わしい", stale: "古い" };

const SERIES_COLORS = [
  "#7fb3ff", "#7fd6a5", "#f0b86e", "#e08283", "#b39ddb", "#6fd0d8", "#c9d17a",
];

let currentRange = RANGES[0];
let socket = null;
let retryDelayMs = 1000;

/** 数値を桁をそろえて出す。null は「—」。 */
function fmt(value, digits = 2) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(digits);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // HTML 文字列を組み立てない（§7.4）
  return node;
}

/** 現在値カード。品質は色と文言の両方で示す（色だけだと見分けにくい）。 */
function renderCards(latest) {
  const container = document.getElementById("cards");
  container.replaceChildren();
  for (const [metric, item] of Object.entries(latest.metrics)) {
    const card = el("div", `card q-${item.quality}`);
    card.appendChild(el("div", "label", metric));
    const value = el("div", "value", fmt(item.value));
    if (item.unit) value.appendChild(el("span", "unit", item.unit));
    card.appendChild(value);
    card.appendChild(el("span", "badge", QUALITY_LABEL[item.quality] || item.quality));
    container.appendChild(card);
  }
}

function renderDerived(latest) {
  const container = document.getElementById("derived");
  container.replaceChildren();
  for (const [name, value] of Object.entries(latest.derived)) {
    const card = el("div", "card");
    card.appendChild(el("div", "label", name));
    const shown = el("div", "value", fmt(value));
    shown.appendChild(el("span", "unit", "C"));
    card.appendChild(shown);
    container.appendChild(card);
  }
}

/**
 * 画面全体の警告。**データが古いときに黙らない。**
 * デーモンが止まっていることが一目で分かる状態にする（受入基準）。
 */
function renderBanner(health) {
  const banner = document.getElementById("banner");
  const age = document.getElementById("age");
  if (health.last_sample_ts_ms === null) {
    banner.textContent = "データが1件も届いていません。取り込みデーモンを確認してください。";
    banner.classList.remove("hidden");
  } else if (health.data_age_seconds < 0) {
    // 受信時刻が未来。時計のずれか、圧縮再生中の DB を見ている（決定記録 0007 §2.11）
    banner.textContent =
      "受信時刻が未来です。時計がずれているか、時間圧縮で再生中の DB を見ています。";
    banner.classList.remove("hidden");
  } else if (health.stale) {
    const seconds = Math.round(health.data_age_seconds);
    banner.textContent = `データが古い（最終受信から ${seconds} 秒）。取り込みが止まっている可能性があります。`;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
  const source = health.source ? ` / ${health.source}` : "";
  if (health.data_age_seconds === null) age.textContent = "";
  else if (health.data_age_seconds < 0) age.textContent = `最終受信 未来${source}`;
  else age.textContent = `最終受信 ${fmt(health.data_age_seconds, 1)} 秒前${source}`;
}

function renderAlerts(alerts) {
  const container = document.getElementById("alerts");
  container.replaceChildren();
  if (alerts.length === 0) {
    container.appendChild(el("p", "empty", "発生中のアラートはありません。"));
    return;
  }
  for (const alert of alerts) {
    const node = el("div", `alert sev-${alert.severity} state-${alert.state}`);
    node.appendChild(el("div", "title", `${alert.rule_id}${alert.metric ? ` — ${alert.metric}` : ""}`));
    const started = new Date(alert.started_ms).toLocaleString();
    const detail = alert.detail ? ` / ${alert.detail}` : "";
    node.appendChild(el("div", "meta", `${alert.state} · ${started}${detail}`));
    container.appendChild(node);
  }
}

/** 依存の無い折れ線。Chart.js を読み込まない（オフラインでも見えるようにするため）。 */
function drawChart(svgId, legendId, series) {
  const svg = document.getElementById(svgId);
  const legend = document.getElementById(legendId);
  svg.replaceChildren();
  legend.replaceChildren();

  const box = svg.viewBox.baseVal;
  const pad = { left: 44, right: 8, top: 10, bottom: 20 };
  const points = series.flatMap((line) => line.points.filter((p) => p.value !== null));
  if (points.length === 0) {
    svg.appendChild(text(box.width / 2, box.height / 2, "データがありません", "middle"));
    return;
  }

  const xs = points.map((p) => p.ts_ms);
  const ys = points.map((p) => p.value);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  let minY = Math.min(...ys);
  let maxY = Math.max(...ys);
  if (maxY - minY < 1) { minY -= 0.5; maxY += 0.5; } // 平坦な系列でも潰れないように

  const sx = (v) => pad.left + ((v - minX) / (maxX - minX || 1)) * (box.width - pad.left - pad.right);
  const sy = (v) => box.height - pad.bottom - ((v - minY) / (maxY - minY)) * (box.height - pad.top - pad.bottom);

  for (const value of [minY, (minY + maxY) / 2, maxY]) {
    const y = sy(value);
    svg.appendChild(line(pad.left, y, box.width - pad.right, y, "#2b323c"));
    svg.appendChild(text(pad.left - 6, y + 4, fmt(value, 1), "end"));
  }
  svg.appendChild(text(pad.left, box.height - 5, new Date(minX).toLocaleTimeString(), "start"));
  svg.appendChild(text(box.width - pad.right, box.height - 5, new Date(maxX).toLocaleTimeString(), "end"));

  series.forEach((entry, index) => {
    const color = SERIES_COLORS[index % SERIES_COLORS.length];
    // 欠測で線をつながない。つなぐと「その間も測れていた」ように見える
    let run = [];
    const flush = () => {
      if (run.length > 1) svg.appendChild(polyline(run, color));
      run = [];
    };
    for (const point of entry.points) {
      if (point.value === null) flush();
      else run.push(`${sx(point.ts_ms)},${sy(point.value)}`);
    }
    flush();

    const item = el("span", null);
    const swatch = el("i");
    swatch.style.background = color;
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(entry.metric));
    legend.appendChild(item);
  });
}

function svgNode(name, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function line(x1, y1, x2, y2, stroke) {
  return svgNode("line", { x1, y1, x2, y2, stroke, "stroke-width": 1 });
}

function polyline(points, stroke) {
  return svgNode("polyline", { points: points.join(" "), fill: "none", stroke, "stroke-width": 1.6 });
}

function text(x, y, content, anchor) {
  const node = svgNode("text", { x, y, "text-anchor": anchor, fill: "#9aa4b2", "font-size": 11 });
  node.textContent = content;
  return node;
}

async function fetchJson(path, params) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) url.searchParams.set(key, value);
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

async function loadHistory() {
  const metrics = Object.keys(lastLatest ? lastLatest.metrics : {}).filter((m) => m.startsWith("air."));
  const temps = metrics.filter((m) => !m.endsWith("_humidity"));
  const humidity = metrics.filter((m) => m.endsWith("_humidity"));
  const note = document.getElementById("chart-note");

  const load = (list) =>
    Promise.all(
      list.map((metric) =>
        fetchJson("/api/v1/series", { metric, window: currentRange.window, agg: currentRange.agg })
          .then((body) => ({ metric, points: body.points, agg: body.agg, downsampled: body.downsampled }))
      )
    );

  try {
    const [tempSeries, humiditySeries] = await Promise.all([load(temps), load(humidity)]);
    drawChart("chart-temp", "legend-temp", tempSeries);
    drawChart("chart-humidity", "legend-humidity", humiditySeries);
    const used = tempSeries[0] || humiditySeries[0];
    note.textContent = used
      ? `粒度 ${used.agg}${used.downsampled ? "（点数の上限に合わせて粗くしました）" : ""}`
      : "";
  } catch (error) {
    note.textContent = `履歴を取得できません: ${error.message}`;
  }
}

let lastLatest = null;

function applyLatest(latest) {
  lastLatest = latest;
  renderCards(latest);
  renderDerived(latest);
}

async function refresh() {
  try {
    const [health, alerts] = await Promise.all([
      fetchJson("/api/v1/health"),
      fetchJson("/api/v1/alerts", { limit: 20 }),
    ]);
    renderBanner(health);
    renderAlerts(alerts.alerts);
  } catch (error) {
    const banner = document.getElementById("banner");
    banner.textContent = `API に接続できません: ${error.message}`;
    banner.classList.remove("hidden");
  }
}

/** WebSocket。切れたら指数バックオフで再接続し、その間は状態を出し続ける。 */
function connect() {
  const conn = document.getElementById("conn");
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${window.location.host}/api/v1/stream`);

  socket.onopen = () => {
    retryDelayMs = 1000;
    conn.textContent = "ライブ";
    conn.className = "conn live";
  };
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "latest") applyLatest(message.latest);
  };
  socket.onclose = () => {
    conn.textContent = `切断（${Math.round(retryDelayMs / 1000)}秒後に再接続）`;
    conn.className = "conn down";
    setTimeout(connect, retryDelayMs);
    retryDelayMs = Math.min(retryDelayMs * 2, 30000);
  };
  socket.onerror = () => socket.close();
}

function renderRanges() {
  const container = document.getElementById("ranges");
  container.replaceChildren();
  for (const range of RANGES) {
    const button = el("button", range === currentRange ? "active" : null, range.label);
    button.addEventListener("click", () => {
      currentRange = range;
      renderRanges();
      loadHistory();
    });
    container.appendChild(button);
  }
}

async function start() {
  renderRanges();
  applyLatest(await fetchJson("/api/v1/latest"));
  await refresh();
  await loadHistory();
  connect();
  setInterval(refresh, 5000);
  setInterval(loadHistory, 60000);
}

start();
