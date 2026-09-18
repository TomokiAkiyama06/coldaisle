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

// `stale` には文言を置かない（決定記録 0039 §2.3）。古さは画面全体の赤帯が言う。
// `stale` のメトリクスが1つでもあれば health も `stale` になり、赤帯が必ず出る
// （API の `_is_stale`）。7枚のカードに同じ「古い」を並べても読み手に何も足さない
const QUALITY_LABEL = { ok: "正常", missing: "欠測", suspect: "疑わしい" };

// 集計の粒度ごとのバケット幅。生データは観測から推定する
// 周期（ミリ秒）。タイムアウトもここから決める（別の設定を増やさない）
const REFRESH_INTERVAL_MS = 5000;
const HISTORY_INTERVAL_MS = 60000;
// 定期更新が返るのを待つ上限。**周期の2倍。** 1周期ぶんの遅れは許し、
// それを超えて返らない要求は打ち切る（返らないまま次の更新を止め続けないように）
const REFRESH_TIMEOUT_MS = REFRESH_INTERVAL_MS * 2;
const HISTORY_TIMEOUT_MS = HISTORY_INTERVAL_MS;

const STEP_MS = { "1m": 60000, "5m": 300000, "1h": 3600000 };
const GAP_FACTOR = 2; // この倍以上空いたら「測れていない区間」とみなす

const SERIES_COLORS = [
  "#7fb3ff", "#7fd6a5", "#f0b86e", "#e08283", "#b39ddb", "#6fd0d8", "#c9d17a",
];

let currentRange = RANGES[0];
// 表示名の表（`GET /api/v1/metrics`）。**内部のメトリクス名を画面に出さない**（決定記録 0039 §2.2）
let catalog = null;
let socket = null;
let retryDelayMs = 1000;

/** 数値を桁をそろえて出す。null は「—」。 */
function fmt(value, digits = 2) {
  return value === null || value === undefined ? "—" : Number(value).toFixed(digits);
}

/**
 * メトリクス名 → 表示名。**`air.room` のような内部名を画面に出さない。**
 * 表が取れていない・設定に無いときだけ名前そのものに戻る（空欄より誤読が少ない）。
 */
function labelOf(metric) {
  if (!metric) return "—";
  const entry = catalog && (catalog.metrics[metric] || catalog.derived[metric]);
  return entry ? entry.label : metric;
}

/** 派生値の式。「GPU吸気 − 室温」。何から何を引いた値かを添える。 */
function formulaOf(name) {
  const entry = catalog && catalog.derived[name];
  return entry ? `${labelOf(entry.minuend)} − ${labelOf(entry.subtrahend)}` : "";
}

/** 現在値カードに出すメトリクス。温湿度の7つ（Issue の「7メトリクス」）。 */
function isCardMetric(metric) {
  return metric.startsWith("air.");
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
    if (!isCardMetric(metric)) continue;
    const card = el("div", `card q-${item.quality}`);
    card.appendChild(el("div", "label", labelOf(metric)));
    const value = el("div", "value", fmt(item.value));
    if (item.unit) value.appendChild(el("span", "unit", item.unit));
    card.appendChild(value);
    const badge = QUALITY_LABEL[item.quality];
    if (badge) card.appendChild(el("span", "badge", badge));
    container.appendChild(card);
  }
}

function renderDerived(latest) {
  const container = document.getElementById("derived");
  container.replaceChildren();
  for (const [name, value] of Object.entries(latest.derived)) {
    const card = el("div", "card");
    card.appendChild(el("div", "label", labelOf(name)));
    const shown = el("div", "value", fmt(value));
    const unit = catalog && catalog.derived[name] ? catalog.derived[name].unit : "C";
    shown.appendChild(el("span", "unit", unit));
    card.appendChild(shown);
    // 計算できないとき（入力のどちらかが ok でない。決定記録 0009 §2.2）も式は出す。
    // 「—」だけだと、何が足りないのかを辿れない
    const formula = formulaOf(name);
    if (formula) card.appendChild(el("div", "formula", formula));
    container.appendChild(card);
  }
}

/**
 * 最新値から見た「古さ」。**WebSocket で届いた品質と赤帯を食い違わせない。**
 *
 * カードは WebSocket（1秒ごと）で `stale` になるが、health の問い合わせは5秒ごと。
 * health だけで赤帯を決めると、最大5秒「赤いカードがあるのに文言が無い」画面になる
 * （決定記録 0039 §2.3 は、stale の文言を赤帯に任せている）。
 *
 * **判定はサーバの `latest.stale` だけを使う。** 各メトリクスの品質を自分で見ると、
 * 事象メトリクス（`sys.dropped_samples` など。起きたときにしか書かれない）の
 * `stale` まで拾い、**一度でも取りこぼしがあれば赤帯が消えなくなる。**
 * `latest.stale` は health と同じ規則（`_is_stale`。周期メトリクスだけを見る）で、
 * カードに出す `air.*` はすべて周期メトリクスなので、stale のカードがあれば必ず立つ。
 */
function staleFromLatest(latest) {
  if (!latest.stale) return { stale: false, seconds: null };
  // 経過秒も周期メトリクス（カードの air.*）から。事象メトリクスの経過は受信の古さではない
  const ages = Object.entries(latest.metrics)
    .filter(([metric]) => isCardMetric(metric))
    .map(([, item]) => item.age_seconds)
    .filter((age) => typeof age === "number" && age >= 0);
  return { stale: true, seconds: ages.length ? Math.min(...ages) : null };
}

/**
 * 画面全体の警告。**データが古いときに黙らない。**
 * デーモンが止まっていることが一目で分かる状態にする（受入基準）。
 *
 * 「1件も無い」「未来」は health から、「古い」は**新しいほうの最新値**から決める。
 * 最新値は WebSocket で health より先に届くため（staleFromLatest）。
 */
function renderBanner() {
  const health = lastHealth;
  const banner = document.getElementById("banner");
  const age = document.getElementById("age");
  const fromLatest = lastLatest ? staleFromLatest(lastLatest) : null;
  const stale = fromLatest ? fromLatest.stale : Boolean(health && health.stale);
  // **重いものから順に、成り立つものをすべて出す。** 1つだけ選ぶと、WebSocket の
  // 更新（applyLatest → ここ）が API の失敗の文言を消してしまう。失敗は
  // lastFetchError に残り、定期更新が丸ごと成功するまで消えない
  const messages = [];
  if (lastFetchError) messages.push(`API に接続できません: ${lastFetchError}`);
  if (health && health.last_sample_ts_ms === null) {
    messages.push("データが1件も届いていません。取り込みデーモンを確認してください。");
  } else if (health && health.data_age_seconds < 0) {
    // 受信時刻が未来。時計のずれか、圧縮再生中の DB を見ている（決定記録 0007 §2.11）
    messages.push("受信時刻が未来です。時計がずれているか、時間圧縮で再生中の DB を見ています。");
  } else if (stale) {
    const measured = fromLatest && fromLatest.seconds !== null ? fromLatest.seconds : health && health.data_age_seconds;
    const seconds = typeof measured === "number" ? `（最終受信から ${Math.round(measured)} 秒）` : "";
    messages.push(`データが古い${seconds}。取り込みが止まっている可能性があります。`);
  }
  if (messages.length > 0) {
    banner.textContent = messages.join(" / ");
    banner.classList.remove("hidden");
  } else if (health) {
    banner.classList.add("hidden");
  }
  if (!health) return;
  const source = health.source ? ` / ${health.source}` : "";
  if (health.data_age_seconds === null) age.textContent = "";
  else if (health.data_age_seconds < 0) age.textContent = `最終受信 未来${source}`;
  else age.textContent = `最終受信 ${fmt(health.data_age_seconds, 1)} 秒前${source}`;
}

const ALERT_STATE_LABEL = { pending: "判定中", firing: "発生中", resolved: "解消" };
const SEVERITY_LABEL = { info: "情報", warning: "警告", critical: "重大" };

/** アラート一覧（FIRING / RESOLVED）。表にして、状態と重大度を文言でも出す。 */
function renderAlerts(alerts) {
  const container = document.getElementById("alerts");
  container.replaceChildren();
  if (alerts.length === 0) {
    container.appendChild(el("p", "empty", "アラートはありません。"));
    return;
  }
  const table = document.createElement("table");
  table.className = "alert-table";
  const head = document.createElement("tr");
  for (const label of ["状態", "重大度", "ルール", "対象", "開始", "詳細"]) {
    head.appendChild(el("th", "", label));
  }
  table.appendChild(head);
  for (const alert of alerts) {
    const row = document.createElement("tr");
    row.className = `sev-${alert.severity} state-${alert.state}`;
    row.appendChild(el("td", "state", ALERT_STATE_LABEL[alert.state] || alert.state));
    row.appendChild(el("td", "severity", SEVERITY_LABEL[alert.severity] || alert.severity));
    row.appendChild(el("td", "", alert.rule_id));
    row.appendChild(el("td", "", alert.metric ? labelOf(alert.metric) : "—"));
    row.appendChild(el("td", "", new Date(alert.started_ms).toLocaleString()));
    row.appendChild(el("td", "", alert.detail || ""));
    table.appendChild(row);
  }
  container.appendChild(table);
}

/**
 * センサー構成（#14 / FR-403）。**どの物理プローブがどのメトリクスか。**
 *
 * ケース内で差し替えると、較正のオフセットもラベルも静かに間違ったまま
 * 運用が続く（spec-review W-03）。ROM を出して人が突き合わせられるようにする。
 *
 * 印は `sensor.changed`（API がその場のデータから出す）で決める。
 * **アラートの文面から読み取らない。** 文面は最初の不一致のまま更新されない
 * ことがあり、あとから別のチャネルがずれても印が動かない。
 * 一覧に入りきらなかった古いアラートを取り逃す問題も避けられる。
 */
function renderDevices(devices) {
  const container = document.getElementById("devices");
  container.replaceChildren();
  if (devices.length === 0) {
    container.appendChild(el("p", "empty", "起動バナーをまだ受け取っていません。"));
    return;
  }
  for (const device of devices) {
    const card = el("div", "device");
    const meta = [device.fw && `fw ${device.fw}`, device.interval_ms && `${device.interval_ms}ms`]
      .filter(Boolean)
      .join(" · ");
    card.appendChild(el("div", "title", `${device.device_id}${meta ? ` — ${meta}` : ""}`));
    if (device.last_hello_at) {
      const at = new Date(device.last_hello_at).toLocaleString();
      card.appendChild(el("div", "meta", `最終バナー ${at}`));
    }
    const table = document.createElement("table");
    table.className = "sensors";
    const head = document.createElement("tr");
    for (const label of ["チャネル", "計測点", "種別", "GPIO", "記録された ROM", "いまの ROM"]) {
      head.appendChild(el("th", "", label));
    }
    table.appendChild(head);
    for (const sensor of device.sensors) {
      const row = document.createElement("tr");
      if (sensor.changed) row.className = "changed";
      row.appendChild(el("td", "", sensor.channel));
      row.appendChild(el("td", "", labelOf(sensor.metric)));
      row.appendChild(el("td", "", sensor.kind));
      row.appendChild(el("td", "", sensor.gpio === null ? "—" : String(sensor.gpio)));
      row.appendChild(el("td", "rom", sensor.rom || "—"));
      // 食い違っていないときは空にする。**同じ値を2列に出しても読みにくいだけ**
      row.appendChild(el("td", "rom", sensor.changed ? sensor.observed_rom || "（無し）" : ""));
      table.appendChild(row);
    }
    card.appendChild(table);
    container.appendChild(card);
  }
}

/**
 * 想定される点の間隔。**線を切る判断に使う。**
 * 取り込みが止まった区間には行そのものが無く、`null` の点すら来ない。
 */
function expectedStep(entry) {
  if (STEP_MS[entry.agg]) return STEP_MS[entry.agg];
  let smallest = Infinity;
  for (let i = 1; i < entry.points.length; i += 1) {
    const delta = entry.points[i].ts_ms - entry.points[i - 1].ts_ms;
    if (delta > 0 && delta < smallest) smallest = delta;
  }
  return Number.isFinite(smallest) ? smallest : 0;
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
    // 欠測で線をつながない。つなぐと「その間も測れていた」ように見える。
    // **点が無い区間も同じ。** 取り込みが止まると行そのものが来ないので、
    // 時刻の飛びを見て切る（値が null の点すら存在しない）
    const step = expectedStep(entry);
    let run = [];
    let previous = null;
    const flush = () => {
      if (run.length > 1) svg.appendChild(polyline(run, color));
      run = [];
    };
    for (const point of entry.points) {
      const jumped = previous !== null && step > 0 && point.ts_ms - previous > step * GAP_FACTOR;
      if (point.value === null || jumped) flush();
      previous = point.ts_ms;
      if (point.value !== null) run.push(`${sx(point.ts_ms)},${sy(point.value)}`);
    }
    flush();

    const item = el("span", null);
    const swatch = el("i");
    swatch.style.background = color;
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(labelOf(entry.metric)));
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

async function fetchJson(path, params, signal) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) url.searchParams.set(key, value);
  const response = await fetch(url, signal ? { signal } : undefined);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

/**
 * 周期的な読み込みの順序づけ。**「適用済みより新しい応答だけを適用する」。**
 *
 * 要求ごとに開始時の通し番号を持たせ、**最後に適用した番号**より大きいときだけ使う。
 * 「最後に開始した番号」と比べると、周期より遅いエンドポイントは毎回次の要求に
 * 追い越されて一度も表示されない（遅いだけで正しい応答が捨てられ続ける）。
 * この規則なら、遅い応答も、より新しい応答が先に適用されていない限り使われる。
 *
 * 返らないまま固まる要求は AbortController で打ち切る（fetch にはタイムアウトが無い）。
 */
let historySeq = 0;
let historyAppliedSeq = 0;
let refreshSeq = 0;
let refreshAppliedSeq = 0;
let refreshInFlight = false;
// WebSocket で最新値を受け取った回数。定期更新の最新値がこれより古ければ使わない
let streamVersion = 0;

/**
 * 打ち切り付きの要求。`run(signal)` を呼び、上限を過ぎたら中断する。
 *
 * **終わったら必ず中断する。** `Promise.all` の1件が先に失敗すると、残りの要求は
 * 走り続ける（結果は誰も使わない）。成功・失敗どちらでも、決着したあとに abort する。
 * 決着後の abort は無害で、**元の失敗の文言は変えない**（打ち切りと言い換えるのは、
 * 上限に達して中断したときだけ）。
 */
async function withTimeout(timeoutMs, run) {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await run(controller.signal);
  } catch (error) {
    if (timedOut) throw new Error(`${Math.round(timeoutMs / 1000)}秒以内に応答がありません`);
    throw error;
  } finally {
    clearTimeout(timer);
    controller.abort(); // 残っている兄弟の要求を止める
  }
}

async function loadHistory() {
  const seq = ++historySeq;
  const range = currentRange; // 要求した期間。**選び直されていたら使わない**
  const metrics = Object.keys(lastLatest ? lastLatest.metrics : {}).filter((m) => m.startsWith("air."));
  const temps = metrics.filter((m) => !m.endsWith("_humidity"));
  const humidity = metrics.filter((m) => m.endsWith("_humidity"));

  const load = (list, signal) =>
    Promise.all(
      list.map((metric) =>
        fetchJson("/api/v1/series", { metric, window: range.window, agg: range.agg }, signal)
          .then((body) => ({ metric, points: body.points, agg: body.agg, downsampled: body.downsampled }))
      )
    );
  // 適用済みより新しく、かつ**いま選ばれている期間**の応答だけを使う。
  // 期間が違う応答は、番号が新しくても画面と食い違う
  const usable = () => seq > historyAppliedSeq && range === currentRange;

  try {
    const [tempSeries, humiditySeries] = await withTimeout(HISTORY_TIMEOUT_MS, (signal) =>
      Promise.all([load(temps, signal), load(humidity, signal)])
    );
    if (!usable()) return;
    historyAppliedSeq = seq;
    lastSeries = { temp: tempSeries, humidity: humiditySeries };
    drawCharts();
    const used = tempSeries[0] || humiditySeries[0];
    historyNote = used
      ? `粒度 ${used.agg}${used.downsampled ? "（点数の上限に合わせて粗くしました）" : ""}`
      : "";
  } catch (error) {
    if (!usable()) return;
    historyAppliedSeq = seq;
    historyNote = `履歴を取得できません: ${error.message}`;
  }
  renderNote();
}

// グラフ下の注記。**履歴と表示名の状態を別々に持つ。** 1つの欄を両方が上書きすると、
// 表示名が取れたあとも「表示名を取得できません」が次の履歴更新（60秒）まで残る
let historyNote = "";
let catalogNote = "";

function renderNote() {
  document.getElementById("chart-note").textContent = [historyNote, catalogNote]
    .filter(Boolean)
    .join(" / ");
}

// 最後に受け取った応答。**表示名の表が後から届いたときに描き直すため**に持つ
let lastLatest = null;
let lastHealth = null;
// 直近の定期更新の失敗。**定期更新が丸ごと成功するまで残す**（赤帯を WebSocket で消さない）
let lastFetchError = null;
let lastAlerts = null;
let lastDevices = null;
let lastSeries = null;
let historyLoaded = false;

function drawCharts() {
  if (!lastSeries) return;
  drawChart("chart-temp", "legend-temp", lastSeries.temp);
  drawChart("chart-humidity", "legend-humidity", lastSeries.humidity);
}

/**
 * 手元にある応答で全部を描き直す。表示名の表が届いた時点で呼ぶ。
 * カードだけ描き直すと、**アラート・センサー構成・凡例に内部名が残る。**
 */
function rerenderAll() {
  if (lastLatest) applyLatest(lastLatest);
  if (lastAlerts) renderAlerts(lastAlerts);
  if (lastDevices) renderDevices(lastDevices);
  drawCharts();
}

function applyLatest(latest) {
  lastLatest = latest;
  renderCards(latest);
  renderDerived(latest);
  renderBanner(); // カードが stale になったら、同じ応答で赤帯も出す（staleFromLatest）
}

let catalogInFlight = false;

/**
 * 表示名の表を取る。失敗は握りつぶさず注記に出し、次の定期更新で取り直す。
 *
 * **同時に1件だけ**（重なると、古い失敗が新しい成功のあとに返って
 * 「表示名を取得できません」を戻してしまう）。1件だけなので順序の問題は起きない。
 * 固まったまま次を止めないよう、定期更新と同じ上限で打ち切る。
 */
async function loadCatalog() {
  if (catalogInFlight || catalog !== null) return;
  catalogInFlight = true;
  try {
    catalog = await withTimeout(REFRESH_TIMEOUT_MS, (signal) =>
      fetchJson("/api/v1/metrics", undefined, signal)
    );
    catalogNote = ""; // 取れたらその場で消す。履歴の注記は触らない
    rerenderAll(); // 取れた時点で、内部名を出していた箇所をすべて表示名に置き換える
  } catch (error) {
    catalogNote = `表示名を取得できません: ${error.message}`;
  } finally {
    catalogInFlight = false;
  }
  renderNote();
}

/**
 * 定期更新。**最新値もここで取り直す。**
 *
 * 取り込みが止まると新しいサンプルは来ないが、品質は `ok` から `stale` へ変わる。
 * WebSocket 側でも押し出すようにしてあるが（決定記録 0011 §2.7）、
 * 切断中はそれも届かない。ここで取り直さないと、
 * **赤帯が出ているのにカードは「正常」のまま**という自己矛盾した画面になる。
 */
async function refresh() {
  // 表示名の表は設定から来るので一度取れば足りる。**取れるまで毎回試す**
  // （起動直後に API が落ちていても、回復後に内部名のまま残らないように）。
  // **待たない。** 表の取得に失敗しても最新値・health・アラートの更新を止めない
  // （止めると赤帯が出なくなる）。取れるまでは labelOf が名前そのものに戻る
  if (catalog === null) loadCatalog();
  // **同時に1件だけ。** 実行中なら今回は見送る（固まった要求は上限で打ち切られ、
  // 次の周期で再開する）。適用の可否は「適用済みより新しいか」で決める
  if (refreshInFlight) return;
  refreshInFlight = true;
  const seq = ++refreshSeq;
  const streamAtStart = streamVersion;
  try {
    const [latest, health, alerts, devices] = await withTimeout(REFRESH_TIMEOUT_MS, (signal) =>
      Promise.all([
        fetchJson("/api/v1/latest", undefined, signal),
        fetchJson("/api/v1/health", undefined, signal),
        fetchJson("/api/v1/alerts", { limit: 20 }, signal),
        fetchJson("/api/v1/devices", undefined, signal),
      ])
    );
    if (seq <= refreshAppliedSeq) return;
    refreshAppliedSeq = seq;
    // 待っている間に WebSocket がより新しい最新値を届けていたら、そちらを残す。
    // 古い `ok` で新しい `stale` を上書きすると、品質が変わるまで WebSocket は
    // 押し直さないため（stream_state）、次の定期更新まで赤帯が消える
    if (streamVersion === streamAtStart) applyLatest(latest);
    lastHealth = health;
    lastFetchError = null; // 4つとも取れたときだけ消す
    lastAlerts = alerts.alerts;
    lastDevices = devices.devices;
    renderBanner();
    renderAlerts(lastAlerts);
    renderDevices(lastDevices);
    if (!historyLoaded) {
      historyLoaded = true;
      loadHistory();
    }
  } catch (error) {
    if (seq <= refreshAppliedSeq) return;
    refreshAppliedSeq = seq;
    // 赤帯そのものには書かず、状態として残す。直接書くと、次の WebSocket の更新で消える
    lastFetchError = error.message;
    renderBanner();
  } finally {
    refreshInFlight = false;
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
    if (message.type === "latest") {
      streamVersion += 1;
      applyLatest(message.latest);
    }
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

/**
 * 起動。**最初の取得に失敗しても復帰の仕組みを止めない。**
 *
 * 先に再接続とタイマーを立ててから読みに行く。逆にすると、一度の失敗で
 * 「接続中…」のまま固まり、API が回復しても手で読み込み直すまで戻らない。
 */
function start() {
  renderRanges();
  connect();
  setInterval(refresh, REFRESH_INTERVAL_MS);
  setInterval(loadHistory, HISTORY_INTERVAL_MS);
  refresh(); // 失敗しても内側で赤帯にして返る
}

start();
