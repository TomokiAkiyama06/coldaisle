// エアフロー画面の**模擬データ**（#106 / 決定記録 0046）。
//
// **実機の値でも実際の制御の状態でもない。** `airflow.html?mock=normal|override|throttle` のときだけ
// airflow.js がこのファイルを読み込む。実データの表示ではこのファイルを読まない。
// 制御の状態（運転モード・決め手・推定風量など）は、制御の判断記録（#74 / #82）を読める
// ようになるまで画面の確認に使うための仮の値。値はデザイン（Airflow / Airflow-Override）に合わせた。
"use strict";

(function () {
  const WINDOWS = { "1h": 3600000, "6h": 21600000, "24h": 86400000, "7d": 604800000 };
  const STEPS = { "1h": 15000, "6h": 60000, "24h": 60000, "7d": 3600000 };

  const BASE = {
    normal: {
      "air.room": 26.4,
      "air.room_humidity": 48.0,
      "air.front_intake": 27.9,
      "air.gpu_intake": 28.3,
      "air.gpu_exhaust": 28.9,
      "air.top_exhaust": 27.1,
      "air.rear_exhaust": 27.4,
      "gpu.0.core": 64,
      "gpu.0.utilization": 93,
      "power.gpu.0": 185,
      "cpu.package": 58,
      "cpu.utilization": 29,
      "power.cpu.package": 72,
      "fan.front.rpm": 1020,
      "fan.front.pwm": 48,
      "fan.rear.rpm": 820,
      "fan.rear.pwm": 40,
      "fan.top.rpm": 1480,
      "fan.top.pwm": 52,
      "fan.aio_pump.rpm": 2800,
      "fan.vrm.rpm": 1900,
    },
    override: {
      "air.room": 26.6,
      "air.room_humidity": 48.0,
      "air.front_intake": 28.1,
      "air.gpu_intake": 29.4,
      "air.gpu_exhaust": 31.2,
      "air.top_exhaust": 29.6,
      "air.rear_exhaust": 29.9,
      "gpu.0.core": 71,
      "gpu.0.utilization": 98,
      "power.gpu.0": 240,
      "cpu.package": 61,
      "cpu.utilization": 34,
      "power.cpu.package": 80,
      "fan.front.rpm": 1140,
      "fan.front.pwm": 60,
      "fan.rear.rpm": 0,
      "fan.rear.pwm": 100,
      "fan.top.rpm": 1890,
      "fan.top.pwm": 85,
      "fan.aio_pump.rpm": 2800,
      "fan.vrm.rpm": 2100,
    },
  };

  // 模擬の GPU スロットリング（決定記録 0046 §2.6）。0 / 1 のフラグ
  const NO_THROTTLE = {
    "gpu.0.throttle.hw_thermal": 0,
    "gpu.0.throttle.sw_thermal": 0,
    "gpu.0.throttle.hw_power_brake": 0,
    "gpu.0.throttle.sw_power_cap": 0,
    "gpu.0.throttle.hw_slowdown": 0,
  };
  Object.assign(BASE.normal, NO_THROTTLE);
  Object.assign(BASE.override, NO_THROTTLE);
  // 熱による制限（ハードウェア）がかかっている通常運転
  BASE.throttle = {
    ...BASE.normal,
    "air.gpu_intake": 29.1,
    "air.gpu_exhaust": 30.4,
    "gpu.0.core": 86,
    "gpu.0.utilization": 99,
    "power.gpu.0": 210,
    "gpu.0.throttle.hw_thermal": 1,
  };

  const UNITS = { rpm: "rpm", pwm: "%", utilization: "%" };

  function latest(name) {
    const values = BASE[name];
    const metrics = {};
    for (const [metric, value] of Object.entries(values)) {
      const suffix = metric.split(".").pop();
      const unit = metric.includes(".throttle.") ? "flag" : UNITS[suffix] || "C";
      metrics[metric] = { value, unit, quality: "ok", age_seconds: 0.8 };
    }
    return {
      metrics,
      derived: { "d.case_delta": Math.round((values["air.rear_exhaust"] - values["air.front_intake"]) * 100) / 100 },
    };
  }

  function trace(zone, steps) {
    return steps.map((step) => ({ ...step })).concat([
      { title: "ファン", value: zone.rpm, detail: zone.fanDetail },
    ]);
  }

  function normalControl() {
    const zone = (requested, effective, floor, bound, index, flow, reason, rpm) => ({
      requested, effective, floor, forced: false, fault: false,
      bound_by: bound, bound_tone: null, airflow_index: index, estimated_flow: flow, reason,
      trace: trace({ rpm, fanDetail: "書き込み・読み戻しとも正常" }, [
        { title: "運転方針", detail: "ルールで運転中 ・ 高負荷が継続中と判断" },
        { title: "学習モデルの提案", value: `${Math.round(requested * 100)}%`, detail: reason },
        { title: "信頼度チェック", value: "通過", tone: "ok", detail: "信頼度 87% ・ 想定外の状態なし" },
        { title: "急変への対応", value: "介入なし" },
        { title: "安全制御", value: `下限 ${Math.round(floor * 100)}%`, detail: "強制最大なし" },
        { title: "最終的な出力", value: `${Math.round(effective * 100)}%`, detail: "決め手：学習モデルの提案がそのまま通った", final: true },
      ]),
    });
    return {
      decision_id: "#184223",
      age_seconds: 1.2,
      regime: "高負荷が継続",
      alert: null,
      chips: [
        { k: "運転モード", v: "自動" },
        { k: "安全状態", v: "正常", tone: "ok" },
        { k: "制御方式", v: "学習モデルで予測制御" },
        { k: "学習モデルの権限", v: "制限付き" },
        { k: "運転方針", v: "ルール（強化学習は試験運転中）" },
        { k: "負荷の傾向", v: "高負荷が継続中" },
        { k: "予測の信頼度", v: "87%" },
        { k: "想定外の状態", v: "なし", tone: "ok" },
        { k: "故障", v: "なし", tone: "ok" },
      ],
      zones: {
        front: zone(0.48, 0.48, 0.25, "制御器の要求どおり", 0.52, 1.0, "GPU 吸気を 29℃ 以下に保つ", "1,020 rpm"),
        rear: zone(0.4, 0.4, 0.2, "制御器の要求どおり", 0.44, 0.46, "ケース内の熱を背面へ逃がす", "820 rpm"),
        top: zone(0.52, 0.52, 0.35, "制御器の要求どおり", 0.49, 0.62, "CPU の先読み温度が目標を上回る", "1,480 rpm"),
      },
      balance: { ratio: 1.08, state: "釣り合っている", tone: "ok", position: 0.58 },
    };
  }

  function overrideControl() {
    const common = [
      { title: "運転方針", detail: "学習モデルを使わず基本制御へ切り替え" },
      { title: "学習モデルの提案", value: "不採用", tone: "warn", detail: "想定外の状態のため使っていない" },
      { title: "信頼度チェック", value: "不通過", tone: "bad", detail: "信頼度 41% ・ 想定外の状態あり" },
    ];
    return {
      decision_id: "#184390",
      age_seconds: 0.8,
      regime: "高負荷が継続",
      alert: {
        text: "一部機能を制限して運転中 — 背面ファンの回転が検出できません。背面を最大にして再始動を試みています",
        note: "基本制御で運転中（学習モデルは使っていません）",
      },
      chips: [
        { k: "運転モード", v: "自動" },
        { k: "安全状態", v: "一部制限", tone: "warn" },
        { k: "制御方式", v: "基本制御（予備）", tone: "warn" },
        { k: "切り替えた理由", v: "想定外の状態", tone: "warn" },
        { k: "学習モデルの権限", v: "制限付き" },
        { k: "予測の信頼度", v: "41%", tone: "warn" },
        { k: "想定外の状態", v: "あり", tone: "bad" },
        { k: "故障", v: "背面ファン停止", tone: "bad" },
      ],
      zones: {
        front: {
          requested: 0.48, effective: 0.6, floor: 0.6, forced: false, fault: false,
          bound_by: "安全制御の下限", bound_tone: "warn", airflow_index: 0.62, estimated_flow: 1.19,
          reason: "背面の停止を補うため吸気を増やす",
          trace: trace({ rpm: "1,140 rpm", fanDetail: "書き込み・読み戻しとも正常" }, [
            ...common,
            { title: "急変への対応", value: "介入なし" },
            { title: "安全制御", value: "下限 60%", tone: "warn", detail: "背面停止中の最低回転を確保" },
            { title: "最終的な出力", value: "60%", detail: "決め手：安全制御の下限", final: true },
          ]),
        },
        rear: {
          requested: 0.4, effective: 1.0, floor: 1.0, forced: true, fault: true,
          bound_by: "安全制御が最大に固定", bound_tone: "bad", airflow_index: 0, estimated_flow: 0,
          reason: "回転が検出できないため最大で再始動を試行",
          trace: trace({ rpm: "0 rpm", fanDetail: "書き込みは成功 ・ 回転を検出できない" }, [
            ...common,
            { title: "急変への対応", value: "介入なし" },
            { title: "安全制御", value: "強制最大", tone: "bad", detail: "回転停止を検出" },
            { title: "最終的な出力", value: "100%", detail: "決め手：安全制御が最大に固定", final: true },
          ]),
        },
        top: {
          requested: 0.52, effective: 0.85, floor: 0.35, forced: false, fault: false,
          bound_by: "急変への対応", bound_tone: "warn", airflow_index: 0.71, estimated_flow: 0.9,
          reason: "ケース内の熱を上面から逃がす",
          trace: trace({ rpm: "1,890 rpm", fanDetail: "書き込み・読み戻しとも正常" }, [
            ...common,
            { title: "急変への対応", value: "下限 85%", tone: "warn", detail: "GPU 排気の急上昇" },
            { title: "安全制御", value: "下限 35%" },
            { title: "最終的な出力", value: "85%", detail: "決め手：急変への対応", final: true },
          ]),
        },
      },
      balance: { ratio: 0.71, state: "吸気が多い", tone: "warn", position: 0.3 },
    };
  }

  /** 決まった形の揺らぎ。**乱数を使わない**（同じ URL で同じ絵になる）。 */
  function wave(metric, t) {
    let seed = 0;
    for (const ch of metric) seed = (seed * 31 + ch.charCodeAt(0)) % 997;
    return Math.sin(t / 900000 + seed) * 0.6 + Math.sin(t / 170000 + seed * 2) * 0.3;
  }

  function series(name, metric, fromMs, toMs, window) {
    const base = BASE[name][metric];
    const step = STEPS[window] || 60000;
    const points = [];
    if (base === undefined) return { points, agg: "raw", downsampled: false };
    const start = Math.ceil(fromMs / step) * step;
    for (let ts = start; ts < toMs; ts += step) {
      const w = wave(metric, ts);
      let value;
      if (metric.endsWith(".utilization")) value = Math.max(0, Math.min(100, base - 25 + w * 30));
      else if (metric.endsWith(".rpm")) value = base === 0 ? 0 : base + w * 120;
      else if (metric.startsWith("air.")) value = base + w * 0.6;
      else value = base + w * 4;
      points.push({ ts_ms: ts, value: Math.round(value * 10) / 10, quality: "ok" });
    }
    return { points, agg: "raw", downsampled: false };
  }

  window.ColdaisleAirflowMock = {
    scenario(name) {
      return { latest: latest(name), control: name === "override" ? overrideControl() : normalControl() };
    },
    windowMs(window) {
      return WINDOWS[window];
    },
    series,
  };
})();
