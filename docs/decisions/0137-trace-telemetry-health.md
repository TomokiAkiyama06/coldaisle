# 決定記録 0137: tick ごとの telemetry の健全度と Guard の閾値の組を decision trace に載せる（ControlTick v15）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-15、リポジトリ所有者の承認）
- **Date**: 2026-10-15
- **Supersedes**: なし（0071 §2.3 の版の解釈の規則はそのまま使い、v15 の欄を足すだけ）
- **関連**: [0028](0028-fan-control-contracts.md) §2.3 / [0029](0029-telemetry-loss-classes.md) §2.1 /
  [0030](0030-control-decision-trace-storage.md) / [0060](0060-control-loop-runtime.md) §2.4 /
  [0071](0071-control-trace-read-api.md) §2.3 / §2.4 /
  [0123](0123-cpu-power-floor-tests.md) §2.2 (b) / §2.3 /
  [0136](0136-t7-t8-t9-acceptance-exceptions.md) §2.3 /
  AGENTS.md ルール 2 / 3 / 8 / 9
- **対象 Issue**: #308

---

## 1. Context

State Estimator（`coldaisle.control.state`）は tick ごとに telemetry の健全度（`TelemetryHealth`。`NORMAL` / `DEGRADED`）を決め、
Reactive Guard はそれを読んで閾値の組（`GuardThresholdProfile`。`normal` / `conservative`）を選ぶ。
ところが、どちらも decision trace（`ControlTick`）に載っていない。

- 健全度は trace にもログにも出ない
- 閾値の組は、Guard の介入の開始・解除のログ（`reactive guard started` / `released`）にしか出ない。発火しなかった tick の組は分からない

そのため、次の 2 つを実機の記録から直接確かめられない。

1. 0123 §2.2 (b) の T9 (b): CPU Power の収集が止まった間に健全度が `DEGRADED`・閾値の組が `conservative` になり、
   新しい sample を観測した tick で `NORMAL`・`normal` に戻ること。0136 §2.3 は、これを本 Issue のテストで満たすと決めた
2. 0123 §2.3 の T1（soak）で記録する「telemetry の健全度が `NORMAL` だった時間の割合」

`SafetyState`（Critical Safety の裁定）は trace にあるが、別の軸である。CPU Power や空気のセンサー 1 本の欠けは
fault を上げず `SafetyState` は `NORMAL` のまま、健全度だけが `DEGRADED` になる（0029 §2.1 / 0123 §1）。

## 2. Decision

### 2.1 trace の欄: `telemetry_health`（v15 で必須）

`ControlTick` の最上位に `telemetry_health` を足す。形は次のとおり（`TelemetryHealthRecord`）。

| 欄 | 型 | 意味 |
|---|---|---|
| `schema_version` | `1` | この塊の形の版 |
| `health` | `"normal"` / `"degraded"` | その tick の snapshot の `telemetry_health`（State Estimator が決めた値をそのまま写す） |
| `degraded_inputs` | metric 名の配列（昇順・重複なし） | 健全度を `DEGRADED` にした入力。有効で、重要度が advisory でなく、値として使えない（`missing` / `stale` / 品質が `ok` でない）signal の metric |
| `guard_threshold_profile` | `"normal"` / `"conservative"` / `null` | その tick の Reactive Guard の判断（`ReactiveGuardDecision.threshold_profile`）。Guard が例外で判断を返さなかった tick だけ `null` |

- **v15 には必須。** 保存済みの v1〜v14 は欄なしのまま読める。欄が無いことは「記録が無い」であって「`NORMAL` だった」ではない
  （0060 §2.4 の「版が中身を表さない記録を作らない」と同じ規律）
- 不変条件（`ControlTick` の検証で拒否する）:
  - `health` が `degraded` ⇔ `degraded_inputs` が空でない
  - `guard_threshold_profile` が `null` でなければ、`conservative` ⇔ `health` が `degraded`（Guard の規則そのもの。
    食い違う記録は、健全度と閾値がつながっていないことを意味するので作らない）
  - `guard_threshold_profile` が `null` の tick は、同じ tick の `faults` に `guard_exception` がある
- **Guard を通らない tick** は、Guard が例外を出した tick だけである。snapshot が組み立てられない tick
  （telemetry を読めない・契約に無い metric）でも、制御ループは空の frame で snapshot を作り直す（すべて `missing`）ので、
  健全度は `degraded`・`degraded_inputs` は有効な非 advisory の入力すべて、Guard は `conservative` で判断する。
  このとき `null` にはしない
- 健全度と閾値の組の型（`TelemetryHealth` / `GuardThresholdProfile`）は `control/schema.py` へ移し、
  `control/state.py` と `control/reactive/guard.py` から同じ型を再 export する（trace と制御で別の enum を持たない）
- `degraded_inputs` は snapshot の signal から数え直す（snapshot の形は変えない。`STATE_SNAPSHOT_SCHEMA_VERSION` は 1 のまま。
  Learned worker への frame も変わらない）

`degraded_inputs` は Issue の範囲より 1 つ多い。T1 で健全度が `DEGRADED` だった時間を見つけたとき、どの入力が欠けたかを
同じ行から言えないと、収集の停止なのか 1 本のセンサーの断線なのかを DB の別の表と時刻で突き合わせることになり、
0123 §1 のとおり 1 tick の精度で突き合わせられないため。大きさは入力の契約の非 advisory の metric の数（いまは 8。T_SENSOR を有効にすると 9）で抑えられる。

### 2.2 版: `ControlTick` を v15 にする

- `SCHEMA_VERSION` を 15 にする。保存済みの v1〜v14 の読み方は変えない
- API（`/api/v1/control/*`）は 0071 §2.3 / §2.4 のとおり保存した JSON をそのまま返す。API の形は変わらない
- 画面（`airflow-trace.js`）は 0071 §2.3 の表のとおり追従する:
  - `KNOWN_VERSIONS` に 15、`SINCE.telemetry_health = 15` を足す
  - 「入力の健全度」の項目を足す。v14 以前は「この版の記録には無い」（**「正常」と言わない**）。`normal` は「そろっている」、
    `degraded` は欠けている入力の数と「急変への対応は保守側の閾値」、`guard_threshold_profile` が `null` なら
    「急変への対応の判断なし（例外）」を出す
- 既存の trace の読み手（Offline Evaluation・Shadow の集計・`coldaisle-air-balance-shadow`・`coldaisle-supervisor-shadow`・
  Dataset・Drift）は `ControlTick` の型で読むだけで、新しい欄を判断に使わない
- **LLM には渡さない**（ルール 8 / 0071 §2.8）。AI ツールに trace を読む口を足さない

### 2.3 T1 の「健全度が `NORMAL` だった時間の割合」は SQL で数える。CLI は足さない

T1 の時間の割合は、記録した tick の数の割合で数える（tick の周期は一定で、1 tick が `tick_ms` の時間を表す）。
`control_traces` を読み取り専用で開き、次の SQL で数える。

```sql
SELECT
  COUNT(*) AS ticks,
  SUM(json_extract(trace_json, '$.telemetry_health.health') = 'normal') AS normal_ticks,
  SUM(json_extract(trace_json, '$.telemetry_health.guard_threshold_profile') = 'conservative') AS conservative_ticks,
  SUM(json_extract(trace_json, '$.telemetry_health.guard_threshold_profile') IS NULL) AS guard_unknown_ticks
FROM control_traces
WHERE schema_version >= 15 AND ts_ms >= :start_ms AND ts_ms < :end_ms;
```

- 割合は `normal_ticks / ticks`。分母は**記録した v15 の tick だけ**で、fand が止まっていた時間・trace を保存できなかった tick・
  v14 以前の tick は数えない。T1 の記録には、割合と一緒に `ticks` と期間の長さ（÷ `tick_ms`）を並べ、記録の欠けを見えるようにする
- `DEGRADED` の内訳は `json_each(json_extract(trace_json, '$.telemetry_health.degraded_inputs'))` で metric ごとに数える
- この SQL は `tests/` で実際の control loop が書いた trace に対して走らせ、本記録の文面と結果がずれたら CI で落とす
- `coldaisle-soak-report` には足さない。あれは取り込みの soak（#47。`readings` を読む）の集計で、fand の trace を読まない。
  T1 の集計の道具を作るかは §5 に残す
- Guard の発火の回数（0123 §2.3）は、いまのとおり `reactive guard started` のログで数える。本記録は変えない

### 2.4 0136 §2.3 のテストを本 PR で入れる

0136 §2.3 のテストを、本 Issue の実装と同じ PR で入れる。

- `power.cpu.package` の時刻（`source_ts_ms`）だけを止め、ほかの入力は毎 tick 新しい時刻で渡す
- 入力は本番と同じ経路で組み立てる: `build_input_contract()`（本番の契約）→ `ControlLoop` の `_TelemetryAgeTracker` と
  `ControlStateEstimator` → `ReactiveGuard`。`ControlLoop.tick()` を通し、trace の `telemetry_health` で確かめる
- 確かめること: `stale_after`（`telemetry.cpu_power_ms`）を超えた tick で `health` が `degraded`・`degraded_inputs` が
  `["power.cpu.package"]`・`guard_threshold_profile` が `conservative`。超える前の tick は `normal`・`normal`。
  新しい sample を渡した最初の tick で `normal`・`normal` に戻る。その間 fault は上がらず `SafetyState` は `NORMAL` のまま
- 既存の `test_one_air_probe_loss_is_degraded_but_not_the_critical_group` と
  `test_degraded_snapshot_uses_the_configured_conservative_threshold_set` はそのまま残し、代わりにしない

このテストが CI で通ったら、0136 §2.3 のとおり T9 (b) の 2 行目と 3 行目の健全度の部分を満たす。
導入先の `/opt` を v15 に更新した後の T1 の間に、実機の trace でも確かめる（0123 §2.3 / 0136 §2.3）。

## 3. Consequences

### 良くなること

- 健全度と閾値の組を、1 tick ごとに判断と同じ行で読める。T9 (b) と T1 の割合を推論でなく記録で言える
- `DEGRADED` の原因の入力が同じ行に残る
- 健全度と Guard の閾値が食い違う記録は作れないので、v15 の trace そのものが両者のつながりの証拠になる

### 悪くなること・その緩和

- trace の 1 行が数十 byte 大きくなる。緩和: 1 s に 1 行で、入力の契約の大きさで上限がある
- `ControlTick` を作る箇所（テストを含む）がすべて新しい欄を渡す必要がある。緩和: 既存の版上げ（v11〜v14）と同じ手順
- 不変条件に反する記録を作ろうとすると、trace の組み立てで例外になり tick が終わらない。緩和: 書き込みと heartbeat の後で
  組み立てるのはいまと同じで、ほかの不変条件と同じ扱い（不具合として process を終わらせ、引き継ぎで Max）。値はどちらも
  同じ snapshot から機械的に写すので、食い違うのは Guard か Estimator の規則が変わったときだけ
- 導入先は `/opt` を更新するまで v14 のまま。緩和: 0136 §2.3 のとおり T9 (b) は CI のテストで満たす

## 4. 却下した代替案

- **ログにだけ出す（毎 tick、または変化したときだけ）**: 判断と同じ行に無いと、0123 §1 のとおり DB の時刻とログの時刻を
  1 tick の精度で突き合わせられない。変化のときだけだと、ログが回転・欠落した区間の状態を言えない。API・画面からも読めない
- **`ControlState` の中に置く**: `ControlState` は Safety・mode・制御器の状態の塊で、Learned worker の入力や Shadow の比較でも
  使う。健全度は入力の質であって制御の状態ではないので混ぜない
- **`guard_threshold_profile` を記録せず、`health` から読む側で導く**: T9 (b) は Guard が実際に選んだ組を見ることが目的で、
  導いた値はそれを示さない
- **snapshot の全 signal（品質・古さ）を載せる**: 1 行が数 KB になる。健全度の判断に要る `degraded_inputs` だけにする
- **`coldaisle-soak-report` に T1 の項目を足す**: 取り込みの soak の道具で、fand の trace を読まない。読む DB と判定の単位
  （0070）が違う道具を混ぜない
- **既存の 2 つのテスト（空気のセンサーと Guard 単体）で T9 (b) を満たす**: 0136 §4 のとおり、CPU Power の古さから
  健全度と閾値までを 1 本の経路でつないでいない

## 5. 未決事項

- T1 の集計（`duration_ms`・`deadline_exceeded`・`*_telemetry_stale`・健全度の割合・Guard の発火）を CLI にするか。
  いまは §2.3 の SQL と 0121 §2.3 の手順で人が数える。T1 の後に、手で数えた手間を見て所有者が決める
- 画面に健全度の時間の推移（帯）を出すか。本記録は最新の 1 tick の項目だけを足す（0046 / #106 の後続で決める）
