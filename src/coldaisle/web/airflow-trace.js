// decision trace（`GET /api/v1/control/latest`）→ エアフロー画面の `page.control`。
// #106 / 決定記録 0046 §2.3 / 0071 §2.3 / §2.6。
//
// **版の解釈はここ1か所だけで行う**（0071 §2.3「画面の変換は1か所に置く」）。
// API は保存した ControlTick の JSON をそのまま返し、検証も投影もしない（0071 §2.4）。
// **DOM に触らない関数だけを置く**（node からそのまま試せるようにするため。airflow-status.js と同じ）。
//
// 規則（0071 §2.3 の表）:
//   - その版に欄が無い（v4 の trace に model_gate が無い等）→「この版の記録には無い」。
//     **「無し」「正常」と言わない**。欄の有無は JSON のキーではなく**版**で決める
//     （ControlTick は None の欄を書き出さないので、キーが無いことは「値が無い」でもありうる）
//   - 欄はあるが値が無い（v9 で supervisor が null 等）→ 項目ごとの表示（例: 出力なし）
//   - 画面が知らない版 → 「未対応の版」として制御由来の項目を出さない。**通常状態に見せない**
//   - 古さは読む側で決める（0071 §2.6）。v8 以降は `runtime.tick_period_ms` × 設定の倍数
//     （`GET /api/v1/airflow/config` の `control_trace.stale_after_tick_periods`）。
//     v1〜v7 は周期を持たないので経過時間だけを出し、「古い／新しい」を言わない。
//     `age_ms` が負（壁時計が戻った直後）は「新しい」とせず判定不能
//   - **表示専用。** ここから制御へ返す経路は無い（trace は証拠であり、制御の入力ではない）
"use strict";

(function (root) {
  // 版ごとに**初めて現れた**欄（control/schema.py の SCHEMA_VERSION の説明）。
  // v11 は決定記録 0073 §2.5 の Air Balance の記録（`air_balance` と `zones.*.applied_demand`）
  const SINCE = {
    workload_regime: 2,
    supervisor: 3,
    model_gate: 5,
    shadow: 6,
    runtime: 8,
    safety_provenance: 9,
    registry: 10,
    air_balance: 11,
    applied_demand: 11,
  };
  const KNOWN_VERSIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];

  const NOT_IN_VERSION = "この版の記録には無い";
  const ZONE_KEYS = ["front", "rear", "top"];
  const ZONE_NAMES = { front: "前面", rear: "背面", top: "トップ" };

  const OPERATING_MODE = {
    auto: { v: "自動" },
    manual: { v: "手動（人が指定）", tone: "warn" },
    max: { v: "最大（人が指定）", tone: "warn" },
    calibration: { v: "較正", tone: "warn" },
  };
  const SAFETY_STATE = {
    normal: { v: "正常", tone: "ok" },
    startup: { v: "起動中（最大で運転）", tone: "warn" },
    degraded: { v: "一部制限", tone: "warn" },
    emergency: { v: "緊急（最大で運転）", tone: "bad" },
  };
  const AUTHORITY = {
    shadow: "試験運転（学習モデルは制御しない）",
    limited: "制限付き",
    expanded: "拡大",
    full: "全面",
  };
  const REGIME = {
    idle: "待機",
    transient_cpu: "CPU の負荷が変動中",
    transient_gpu: "GPU の負荷が変動中",
    transient_cpu_gpu: "CPU・GPU の負荷が変動中",
    sustained_cpu: "CPU の高負荷が継続中",
    sustained_gpu: "GPU の高負荷が継続中",
    sustained_cpu_gpu: "CPU・GPU の高負荷が継続中",
    cooldown: "負荷が下がった直後",
    unknown: "判断できない",
  };
  const POLICY = { rule_policy: "ルール", rl_policy: "強化学習" };
  const BOUND_BY = {
    forced_max: { text: "安全制御が最大に固定", tone: "bad" },
    safety_floor: { text: "安全制御の下限", tone: "warn" },
    ramp_down: { text: "下げる速さの制限", tone: null },
    guard_floor: { text: "急変への対応（下限）", tone: "warn" },
    guard_ceiling: { text: "急変への対応（上限）", tone: "warn" },
    requested: { text: "制御器の要求どおり", tone: null },
  };
  const FAULT = {
    cpu_telemetry_stale: "CPU の値が届かない",
    gpu_telemetry_stale: "GPU の値が届かない",
    t_sensor_stale: "T_SENSOR の値が届かない",
    air_telemetry_stale: "空気の温度が届かない",
    absolute_temperature_limit: "温度が絶対上限を超えた",
    tach_stall: "ファンの回転を検出できない",
    write_failure: "ファンへの書き込みに失敗",
    readback_mismatch: "書き込んだ値と読み戻しが違う",
    enable_reverted: "ファンの制御が外された",
    fallback_exception: "基本制御の例外",
    guard_exception: "急変への対応の例外",
    tick_overrun: "制御の周期に間に合わない",
    config_invalid: "制御の設定が不正",
  };
  const BALANCE_STATE = {
    balanced: { text: "釣り合っている", tone: "ok" },
    intake_heavy: { text: "吸気が多い", tone: "warn" },
    exhaust_heavy: { text: "排気が多い", tone: "warn" },
    thermally_limited: { text: "熱で制限", tone: "bad" },
    unknown: { text: "推定できない", tone: "na" },
    disabled: { text: "使っていない（未校正）", tone: "na" },
  };

  function has(version, field) {
    return version >= SINCE[field];
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function pct(value) {
    return `${Math.round(value * 100)}%`;
  }

  /** 理由（Reason）の人が読む説明。説明が無ければ、そう言う（識別子 `code` は出さない）。 */
  function reasonText(reason) {
    if (!isObject(reason)) return null;
    return typeof reason.detail === "string" && reason.detail !== "" ? reason.detail : "（説明の記録なし）";
  }

  function label(table, value, unknownText) {
    return Object.prototype.hasOwnProperty.call(table, value) ? table[value] : unknownText;
  }

  /** 知らない値（将来の語彙・壊れた記録）を**正常に見せない**。 */
  const UNKNOWN_VALUE = "記録の値を解釈できない";

  // ---------------------------------------------------------------- 古さ（0071 §2.6）

  /**
   * trace の古さ。`{ state, text }`。state は "fresh" / "stale" / "unknown"。
   * - `multiplier` は設定（`control_trace.stale_after_tick_periods`）。読めなければ null → 判定しない
   * - v1〜v7（周期を持たない）は経過時間だけ。**「古い／新しい」を言わない**
   * - `age_ms` が負 → 判定不能（壁時計が戻った直後。新しいとは言わない）
   */
  function traceFreshness(ageMs, version, body, multiplier) {
    if (typeof ageMs !== "number" || !Number.isFinite(ageMs)) {
      return { state: "unknown", text: "経過時間が分からない" };
    }
    const seconds = (ageMs / 1000).toFixed(1);
    if (ageMs < 0) {
      return { state: "unknown", text: "記録の時刻が未来（時計が戻った可能性）。古さを判定できない" };
    }
    if (!has(version, "runtime")) {
      return { state: "unknown", text: `${seconds} 秒前（この版の記録は周期を持たないため古さを判定しない）` };
    }
    const runtime = isObject(body) ? body.runtime : null;
    const period = isObject(runtime) ? runtime.tick_period_ms : null;
    if (typeof period !== "number" || !(period > 0)) {
      return { state: "unknown", text: `${seconds} 秒前（記録に周期が無いため古さを判定できない）` };
    }
    if (typeof multiplier !== "number" || !(multiplier > 0)) {
      return { state: "unknown", text: `${seconds} 秒前（表示設定を読めないため古さを判定できない）` };
    }
    if (ageMs > period * multiplier) {
      return { state: "stale", text: `${seconds} 秒前（古い。制御デーモンが止まっている可能性）` };
    }
    return { state: "fresh", text: `${seconds} 秒前` };
  }

  // ---------------------------------------------------------------- 項目ごと

  function faultList(body) {
    return Array.isArray(body.faults) ? body.faults.filter(isObject) : [];
  }

  function faultText(fault) {
    const what = label(FAULT, fault.code, "不明な故障");
    return fault.zone ? `${label(ZONE_NAMES, fault.zone, "不明な系統")}：${what}` : what;
  }

  function supervisorChip(version, body) {
    if (!has(version, "supervisor")) return { k: "運転方針", v: NOT_IN_VERSION, tone: "absent" };
    const decision = body.supervisor;
    if (!isObject(decision)) return { k: "運転方針", v: "出力なし（この tick は Supervisor の判断なし）" };
    const active = isObject(decision.active) ? decision.active : {};
    const name = label(POLICY, active.policy, UNKNOWN_VALUE);
    if (active.output === null || active.output === undefined) {
      const fallback = isObject(decision.fallback) ? label(POLICY, decision.fallback.policy, UNKNOWN_VALUE) : null;
      return {
        k: "運転方針",
        v: fallback ? `${name}が失敗し${fallback}で代替` : `${name}（出力なし）`,
        tone: "warn",
      };
    }
    const shadow = isObject(decision.shadow) ? label(POLICY, decision.shadow.policy, UNKNOWN_VALUE) : null;
    return { k: "運転方針", v: shadow ? `${name}（${shadow}は試験運転中）` : name };
  }

  function regimeText(version, body) {
    if (!has(version, "workload_regime")) return NOT_IN_VERSION;
    const regime = body.state.workload_regime;
    if (regime === null || regime === undefined) return "推定なし";
    return label(REGIME, regime, UNKNOWN_VALUE);
  }

  /**
   * 信頼度と想定外の状態（OOD）。v5 以降は Gate の判断（`model_gate`）から。
   * **裏づけの無い（attested でない）数値は出さない**（schema の ModelGateDecision と同じ規律）。
   */
  function gateChips(version, body) {
    if (!has(version, "model_gate")) {
      return [
        { k: "予測の信頼度", v: NOT_IN_VERSION, tone: "absent" },
        { k: "想定外の状態", v: NOT_IN_VERSION, tone: "absent" },
      ];
    }
    const gate = body.model_gate;
    if (!isObject(gate)) {
      return [
        { k: "予測の信頼度", v: "判定なし（学習モデルの提案なし）" },
        { k: "想定外の状態", v: "判定なし（学習モデルの提案なし）" },
      ];
    }
    if (gate.attested !== true) {
      return [
        { k: "予測の信頼度", v: "裏づけなし（使っていない）", tone: "warn" },
        { k: "想定外の状態", v: "裏づけなし（判定できない）", tone: "warn" },
      ];
    }
    const confidence = typeof gate.confidence === "number" ? pct(gate.confidence) : UNKNOWN_VALUE;
    const low = gate.confidence_level === "low";
    const ood =
      gate.ood === true
        ? { k: "想定外の状態", v: "あり", tone: "bad" }
        : gate.ood === false
          ? { k: "想定外の状態", v: "なし", tone: "ok" }
          : { k: "想定外の状態", v: UNKNOWN_VALUE, tone: "warn" };
    return [{ k: "予測の信頼度", v: confidence, tone: low ? "warn" : undefined }, ood];
  }

  function controllerChips(body) {
    const state = body.state;
    const chips = [];
    if (state.active_controller === "learned_mpc") {
      chips.push({ k: "制御方式", v: "学習モデルで予測制御" });
    } else if (state.active_controller === "fallback") {
      const reason = reasonText(state.fallback_reason);
      chips.push({ k: "制御方式", v: "基本制御（予備）", tone: reason ? "warn" : undefined });
      if (reason) chips.push({ k: "切り替えた理由", v: reason, tone: "warn" });
    } else if (state.active_controller === null) {
      chips.push({ k: "制御方式", v: "人が指定（制御器なし）" });
    } else {
      chips.push({ k: "制御方式", v: UNKNOWN_VALUE, tone: "warn" });
    }
    return chips;
  }

  /** Critical Safety / Reactive Guard の介入（Safety override）。zone の `bound_by` から。 */
  function overrideChip(zones) {
    const forced = ZONE_KEYS.filter((key) => zones[key].forced);
    const floor = ZONE_KEYS.filter((key) => zones[key].boundKey === "safety_floor");
    const guard = ZONE_KEYS.filter((key) => ["guard_floor", "guard_ceiling"].includes(zones[key].boundKey));
    const names = (keys) => keys.map((key) => ZONE_NAMES[key]).join("・");
    if (forced.length) return { k: "安全制御の介入", v: `最大に固定（${names(forced)}）`, tone: "bad" };
    if (floor.length) return { k: "安全制御の介入", v: `下限で引き上げ（${names(floor)}）`, tone: "warn" };
    if (guard.length) return { k: "急変への対応", v: `介入中（${names(guard)}）`, tone: "warn" };
    return { k: "安全制御の介入", v: "なし" };
  }

  function safetyProvenanceChip(version, body) {
    if (!has(version, "safety_provenance")) return { k: "安全設定", v: NOT_IN_VERSION, tone: "absent" };
    const provenance = body.safety_provenance;
    if (!isObject(provenance)) return { k: "安全設定", v: UNKNOWN_VALUE, tone: "warn" };
    const disabled = Array.isArray(provenance.disabled_inputs) ? provenance.disabled_inputs.length : 0;
    const parts = [provenance.config_is_provisional ? "暫定値を含む" : "確定値"];
    if (disabled > 0) parts.push(`外している入力 ${disabled}`);
    return {
      k: "安全設定",
      v: parts.join(" ・ "),
      tone: provenance.config_is_provisional || disabled > 0 ? "warn" : undefined,
    };
  }

  function number(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function zoneFromTrace(version, body, key, faults) {
    const record = body.zones[key];
    const demand = record.demand;
    const bound = label(BOUND_BY, demand.bound_by, { text: UNKNOWN_VALUE, tone: "warn" });
    const zoneFaults = faults.filter((fault) => fault.zone === key);
    const reasons = Array.isArray(demand.reasons) ? demand.reasons.map(reasonText).filter(Boolean) : [];
    const controller = body.state.active_controller;
    const proposer =
      controller === "learned_mpc" ? "学習モデルの提案" : controller === "fallback" ? "基本制御の要求" : "人が指定した値";
    const steps = [];

    // 運転方針（Supervisor）
    if (!has(version, "supervisor")) {
      steps.push({ title: "運転方針", value: NOT_IN_VERSION, tone: "absent" });
    } else {
      const chip = supervisorChip(version, body);
      steps.push({ title: "運転方針", detail: chip.v, tone: chip.tone });
    }
    // 制御器の要求
    steps.push({ title: proposer, value: pct(demand.requested), detail: reasonText(record.controller_reason) });
    // Confidence / OOD Gate
    if (!has(version, "model_gate")) {
      steps.push({ title: "信頼度チェック", value: NOT_IN_VERSION, tone: "absent" });
    } else if (!isObject(body.model_gate)) {
      steps.push({ title: "信頼度チェック", value: "判定なし", detail: "学習モデルの提案が無い tick" });
    } else if (body.model_gate.learned_selected === true) {
      steps.push({ title: "信頼度チェック", value: "通過", tone: "ok" });
    } else {
      const ood = body.model_gate.attested === true && body.model_gate.ood === true;
      steps.push({
        title: "信頼度チェック",
        value: "不採用",
        tone: ood ? "bad" : "warn",
        detail: ood ? "想定外の状態のため学習モデルの提案を使っていない" : "学習モデルの提案を使っていない",
      });
    }
    // Reactive Guard
    const guardFloor = number(demand.guard_floor);
    const guardCeiling = number(demand.guard_ceiling);
    if (guardFloor === null && guardCeiling === null) {
      steps.push({ title: "急変への対応", value: "介入なし" });
    } else {
      const parts = [];
      if (guardFloor !== null) parts.push(`下限 ${pct(guardFloor)}`);
      if (guardCeiling !== null) parts.push(`上限 ${pct(guardCeiling)}`);
      steps.push({ title: "急変への対応", value: parts.join(" / "), tone: "warn" });
    }
    // Critical Safety
    if (demand.forced_max === true) {
      steps.push({ title: "安全制御", value: "強制最大", tone: "bad", detail: reasons.join(" ・ ") || null });
    } else {
      steps.push({
        title: "安全制御",
        value: `下限 ${pct(demand.safety_floor)}`,
        tone: demand.bound_by === "safety_floor" ? "warn" : undefined,
      });
    }
    steps.push({
      title: "最終的な出力",
      value: pct(demand.effective),
      detail: `決め手：${bound.text}${reasons.length && demand.forced_max !== true ? `（${reasons.join(" ・ ")}）` : ""}`,
      final: true,
    });
    // Hardware
    const hardware = record.hardware;
    let fanValue = "未書き込み";
    let fanDetail = "この tick はまだファンへ書き込んでいない";
    let fanTone;
    if (isObject(hardware)) {
      fanValue = typeof hardware.rpm === "number" ? `${hardware.rpm.toLocaleString("ja-JP")} rpm` : "回転数の記録なし";
      const ok = hardware.write_ok === true && hardware.readback_ok === true;
      fanDetail = ok
        ? "書き込み・読み戻しとも正常"
        : `${hardware.write_ok === true ? "書き込みは成功" : "書き込みに失敗"} ・ ${
            hardware.readback_ok === true ? "読み戻しは一致" : "読み戻しが一致しない"
          }`;
      if (!ok) fanTone = "bad";
    }
    if (has(version, "applied_demand")) {
      const applied = number(record.applied_demand);
      fanDetail += applied === null ? " ・ 適用した出力：記録なし" : ` ・ 適用した出力 ${pct(applied)}`;
    } else {
      fanDetail += ` ・ 適用した出力：${NOT_IN_VERSION}`;
    }
    if (zoneFaults.length) fanDetail += ` ・ ${zoneFaults.map(faultText).join(" ・ ")}`;
    steps.push({ title: "ファン", value: fanValue, detail: fanDetail, tone: zoneFaults.length ? "bad" : fanTone });

    return {
      requested: demand.requested,
      effective: demand.effective,
      floor: demand.safety_floor,
      forced: demand.forced_max === true,
      fault: zoneFaults.length > 0,
      boundKey: demand.bound_by,
      bound_by: bound.text,
      bound_tone: bound.tone,
      airflow_index: number(record.airflow_index),
      estimated_flow: number(record.estimated_flow),
      reason: reasonText(record.controller_reason) || "（説明の記録なし）",
      trace: steps,
    };
  }

  /** Air Balance（v11。決定記録 0073 §2.5 (b)）。v10 以前は「この版の記録には無い」。 */
  function balanceFromTrace(version, body) {
    if (!has(version, "air_balance")) return { absent: NOT_IN_VERSION };
    const balance = body.air_balance;
    if (!isObject(balance)) return { absent: UNKNOWN_VALUE };
    const state = label(BALANCE_STATE, balance.state, { text: UNKNOWN_VALUE, tone: "warn" });
    const ratio = number(balance.balance_ratio);
    return {
      ratio,
      state: state.text,
      tone: state.tone,
      position: null, // 目標帯は air-balance.yaml にあり trace に無い。帯の上の位置は描かない
      thermal: balance.thermal_limited === true,
    };
  }

  function alertFromTrace(body, zones, faults) {
    const state = body.state;
    const lines = [];
    let tone = null;
    if (state.safety_state === "emergency") {
      lines.push("緊急 — 安全制御が全系統を最大で運転しています");
      tone = "bad";
    } else if (state.safety_state === "degraded") {
      lines.push("一部機能を制限して運転中");
      tone = "warn";
    }
    const forced = ZONE_KEYS.filter((key) => zones[key].forced);
    if (forced.length && state.safety_state !== "emergency") {
      lines.push(`安全制御が${forced.map((key) => ZONE_NAMES[key]).join("・")}を最大に固定しています`);
    }
    if (faults.length) lines.push(`故障：${faults.map(faultText).join(" ・ ")}`);
    if (!lines.length) return null;
    const fallback = state.fallback_active === true && isObject(state.fallback_reason);
    return {
      text: lines.join(" — "),
      note: fallback ? `基本制御で運転中（${reasonText(state.fallback_reason)}）` : "",
      tone,
    };
  }

  /** 読み込める最低限の形か。**壊れた記録を正常に見せない**（読めなければ「読めない」と出す）。 */
  function readable(body) {
    if (!isObject(body) || !isObject(body.state) || !isObject(body.zones)) return false;
    return ZONE_KEYS.every((key) => {
      const zone = body.zones[key];
      if (!isObject(zone) || !isObject(zone.demand)) return false;
      const demand = zone.demand;
      return ["requested", "effective", "safety_floor"].every((name) => number(demand[name]) !== null);
    });
  }

  /**
   * `/api/v1/control/latest` の応答 → 画面の制御の状態。**版の解釈はここだけ。**
   *
   * 返り値は `{ status, ... }`:
   * - `"none"` — trace が1件も無い（制御デーモンを動かしていない・保持期間で消えた）→「未接続」
   * - `"unsupported"` — 画面が知らない版 → 「未対応の版」。制御由来の項目を出さない
   * - `"unreadable"` — 知っている版だが形が読めない → 同じく項目を出さない
   * - `"ok"` — `control` に模擬データ（airflow-mock.js）と同じ形の `page.control` を持つ
   *
   * `multiplier` は `control_trace.stale_after_tick_periods`（読めなければ null）。
   */
  function controlFromLatest(response, multiplier) {
    const trace = isObject(response) ? response.trace : null;
    if (!isObject(trace)) return { status: "none" };
    const version = trace.schema_version;
    const decisionId = typeof trace.seq === "number" ? `#${trace.seq}` : null;
    const base = { schema_version: version, decision_id: decisionId, age_ms: trace.age_ms };
    if (!KNOWN_VERSIONS.includes(version)) {
      const age = typeof trace.age_ms === "number" && trace.age_ms >= 0 ? `${(trace.age_ms / 1000).toFixed(1)} 秒前` : "経過時間を判定できない";
      return { ...base, status: "unsupported", freshness: { state: "unknown", text: `${age}（未対応の版のため古さを判定しない）` } };
    }
    const body = trace.body;
    const freshness = traceFreshness(trace.age_ms, version, body, multiplier);
    if (!readable(body)) return { ...base, status: "unreadable", freshness };

    const faults = faultList(body);
    const zones = {};
    for (const key of ZONE_KEYS) zones[key] = zoneFromTrace(version, body, key, faults);
    const state = body.state;
    const regime = regimeText(version, body);
    const chips = [
      { k: "運転モード", ...label(OPERATING_MODE, state.operating_mode, { v: UNKNOWN_VALUE, tone: "warn" }) },
      { k: "安全状態", ...label(SAFETY_STATE, state.safety_state, { v: UNKNOWN_VALUE, tone: "warn" }) },
      overrideChip(zones),
      ...controllerChips(body),
      { k: "学習モデルの権限", v: label(AUTHORITY, state.authority_stage, UNKNOWN_VALUE) },
      supervisorChip(version, body),
      { k: "負荷の傾向", v: regime, tone: regime === NOT_IN_VERSION ? "absent" : undefined },
      ...gateChips(version, body),
      faults.length
        ? { k: "故障", v: faults.map(faultText).join(" ・ "), tone: "bad" }
        : { k: "故障", v: "なし", tone: "ok" },
      safetyProvenanceChip(version, body),
    ];
    return {
      ...base,
      status: "ok",
      freshness,
      control: {
        source: "trace",
        decision_id: decisionId,
        schema_version: version,
        regime,
        alert: alertFromTrace(body, zones, faults),
        chips,
        zones,
        balance: balanceFromTrace(version, body),
      },
    };
  }

  /**
   * 古さが「新しい」と言えないときは、**正常（緑）の色を外す**。
   * 古い記録の「正常」「なし」を、いまの状態のように見せないため。
   */
  function withoutOkTones(control) {
    const chips = control.chips.map((chip) => (chip.tone === "ok" ? { ...chip, tone: undefined } : chip));
    const zones = {};
    for (const [key, zone] of Object.entries(control.zones)) {
      zones[key] = {
        ...zone,
        trace: zone.trace.map((step) => (step.tone === "ok" ? { ...step, tone: undefined } : step)),
      };
    }
    const balance = control.balance && control.balance.tone === "ok" ? { ...control.balance, tone: undefined } : control.balance;
    return { ...control, chips, zones, balance };
  }

  const api = {
    controlFromLatest,
    traceFreshness,
    withoutOkTones,
    KNOWN_VERSIONS,
    NOT_IN_VERSION,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ColdaisleAirflowTrace = api;
})(this);
