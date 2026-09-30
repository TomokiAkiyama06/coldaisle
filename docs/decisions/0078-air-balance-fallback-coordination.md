# 決定記録 0078: Air Balance の `coordinate()` を Fallback / Baseline の requested に掛ける位置・条件・上限・段階

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-30
- **Supersedes**: なし
- **関連**: [`0073-air-balance-control-config-integration.md`](0073-air-balance-control-config-integration.md) §2.2 / §2.3 / §2.6 / §5（1行目）、
  [`0033-air-balance-config-boundary.md`](0033-air-balance-config-boundary.md)、
  [`0026-three-zone-fan-control.md`](0026-three-zone-fan-control.md)、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.3 / §2.4 / §2.7、
  [`0052-learned-mpc-optimizer-and-hard-constraints.md`](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.3、
  [`0053-control-shadow-mode-and-counterfactual-logging.md`](0053-control-shadow-mode-and-counterfactual-logging.md)、
  [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md) §2.2 / §2.4、
  [`0060-control-loop-runtime.md`](0060-control-loop-runtime.md) §2.4、
  [`0076-implementation-settled-points.md`](0076-implementation-settled-points.md)（`ControlTick` の版の繰り上げの記録）、
  [`docs/air-balance-model.md`](../air-balance-model.md)、[`docs/airflow-model.md`](../airflow-model.md)「Top → Front make-up air」、
  [`docs/control-config.md`](../control-config.md)
- **対象 Issue**: #81（関連: #75 / #74 / #91 / #92）

## 1. Context

決定記録 0073（FINAL）は、`air-balance.yaml` を Control Config に統合し、校正済みなら
applied demand から Air Balance を**記録する**ところまでを決めた。一方で次の1点を §5 の1行目に残した。

> Fallback Controller の requested に `coordinate()` を掛けるか（Baseline の挙動を変える）
> — #81 の実装前に別の決定記録と所有者の承認。安全・制御系の変更のため

いまの `main` の状態は次のとおり。

| 箇所 | 状態 |
|---|---|
| `ConfiguredAirBalanceModel.coordinate()` | 実装済みの純粋関数。Exhaust 過多なら Front make-up air、熱制約を伴う Intake 過多なら Rear → 不足分だけ Top（0026 の順）。`AirBalanceCoordination` の検証で**どの zone も candidate を下回らない**（上げるだけ）。`projected_top_floor` を受け取り Top の風量見積もりにだけ使う |
| `FallbackController` | 3 zone の最大値を共有する決定論的な制御器（全 zone 同じ demand）。Air Balance を知らない |
| `ControlLoop._run_tick()` | Fallback → Critical Safety の評価 → Gate（`_select()`）→ `_requested()` → 合成（0028 §2.4）→ Backend。`coordinate()` を呼ぶ箇所は無い |
| `AirBalanceRecorder` | 校正済みのときだけ `ConfiguredAirBalanceModel` を作り、applied demand の推定を `ControlTick` v11 の `air_balance` に**記録するだけ** |
| `ControllerGate` | LIMITED / EXPANDED では Learned MPC の提案を **Fallback の requested を中心とした帯**（`limit_up` / `limit_down`）へ切り詰める |
| `ControlTick` | v12（`main`）。open の PR #192 が v13 に上げる |
| `fan-policy.yaml` | schema version 9、束ねた `CONTROL_CONFIG_VERSION` は 11 |

Baseline（Fallback）は、Learned MPC を持たない今も、持った後の SHADOW stage・低 Confidence・
ML 停止のときも、実 Fan を握る制御器である。Air Balance を MPC の cost にだけ入れる（0073 §2.3）と、
Baseline が動いている時間のほとんどで「Top の排気が強い CPU 負荷で Front の make-up air が
足りない」（`docs/airflow-model.md`）状態を放置する。逆に Baseline を変えると、最後の拠り所の
挙動が変わる。そのため、掛けるか・どこで・どの条件で・どれだけ・どう段階的に、を先に決める。

**本記録は #75 の実測値を決めない。** 上限の値・保持時間・shadow の合否の基準は、実測の後に
値として決める（§5）。本記録が決めるのは、その値を入れる**枠**と、値が無い間の**安全側の既定**である。

### 所有者に承認を求める判断（FINAL にする前に）

各項目の **（推奨）** が本記録の §2 の内容である。代替を選ぶ場合は、対応する §2 の節と §4 の行を差し替える。

1. **Baseline（Fallback）の requested に協調を掛けるか。**
   **（推奨）掛ける**（§2.1〜§2.2）／ 代替: 掛けない（§4 の A。本記録を Rejected にし、0073 §5 の1行目を「掛けない」で閉じる記録にする）
2. **有効化の仕方と既定。**
   **（推奨）`fan-policy.yaml` の `mode`、既定 `off`、`shadow` → `apply` を人が設定と再起動で進める**（§2.1 / §2.8）／
   代替: `calibrated` なら即適用（§4 の E）、authority journal で段を管理（§4 の N）
3. **`mode: shadow / apply` と `uncalibrated` の組み合わせ。**
   **（推奨）Control Config の不正（`config_invalid`、全 zone Max）**（§2.4）／ 代替: 黙って `off` として起動（§4 の J）
4. **`mode: apply` の協調の例外（`coordinate()` の不具合・下げる値の検出）。**
   **（推奨）0028 §2.7 の「Fallback Controller の例外」と同じ扱い**＝ `fallback_exception` の fault にし、
   その tick は raw baseline、次 tick の Critical Safety が `EMERGENCY`（全 zone Max）。`mode: shadow` では記録だけ（§2.6）／
   代替: raw baseline のまま続け Safety へつながない（§4 の I。0028 §2.7 の一部を置き換える記録になり、0028 へ `Superseded by` を追記する）
5. **協調の設定を変えた後の昇格の証拠。**
   **（推奨）Offline Evaluation の報告に `fan_policy_trace_binding`（0073 §2.6 と同じ形）を加え、
   不一致・欠落が 1 件でもあれば `_check_evidence()` が拒む**（§2.5）／
   代替: 本記録では束縛せず、0073 §5 の未決のまま残す（§4 の P。その間、`mode: off` の trace を `mode: apply` の設定で評価した報告が昇格の証拠に通りうる）
6. **Critical Safety が zone ごとに `forced_max` を出している tick（`DEGRADED` の CPU Telemetry stale・Fan fault）。**
   **（推奨）CPU Telemetry の stale による Top の `forced_max` は `projected_top_floor = 1.0` として協調を続け、
   どの zone でも Fan fault（Top を含む）があれば `skipped`**（§2.3）／
   代替: どの zone でも `forced_max` なら `skipped`（§4 の O）

## 2. Decision（推奨案）

### 2.1 掛ける。ただし既定は `off` で、`shadow` → `apply` の順に人が設定で開く

- `coordinate()` を **Fallback の requested（Baseline）に掛ける経路を作る**
- 経路の有効化は `fan-policy.yaml` の新しい塊 `air_balance_coordination.mode`（`off` / `shadow` / `apply`）で
  人が決める（所有者の判断 1・2）。**既定の雛形は `off`**。値の変更は再起動で反映する（live reload しない。0028 §2.7）
- `mode` は Air Balance そのものの有効・無効ではない。Air Balance の有効・無効は 0073 §2.2 のとおり
  `source.status` だけで決まる。`mode` は「有効な Air Balance の協調提案を **Baseline に適用するか**」という
  制御の方針で、Fallback の曲線・authority の上限と同じく `fan-policy.yaml` が持つ（§4 の F）

### 2.2 位置: Fallback の直後・Gate の前。Critical Safety の**評価**の後、**合成**の前

```text
Fallback.propose()                       … raw baseline（いまの requested）
Critical Safety.evaluate()               … requested を見ない（いまと同じ位置）
  ↓ Top の safety floor を読むだけ
AirBalanceCoordinator.apply(raw baseline, snapshot, safety の Top floor)   ← 本記録
  ↓ coordinated baseline（mode: apply のときだけ raw と違いうる）
ControllerGate.select(fallback = coordinated baseline, learned = …)
  ↓ requested
合成（0028 §2.4）: min(guard_ceiling) → max(guard_floor) → max(safety_floor) → ramp_down → forced_max
  ↓ effective → Hardware Backend
```

- 協調は **requested を作る側**（制御器の出力の加工）に置く。0028 §2.3 の「Air Balance は制御器が
  requested を作るための材料であり、合成の上下限には入らない」のとおり。Reactive Guard の ceiling / floor、
  Critical Safety の floor・`ramp_down`・`forced_max` は、協調後の requested にもいまと同じく掛かる
  （AGENTS.md ルール2・5）。**合成の後・Backend の前に値を足す経路は作らない**
- Critical Safety の裁定を先に評価するのは、いまの loop の順序のまま（Safety は requested を見ない）。
  協調はその裁定の **Top の出力を読み、`projected_top_floor` として渡すだけ**で、裁定を作り直さない・
  書き換えない（`CriticalSafetyDecision` は発行済みの不変オブジェクト）。`projected_top_floor` は
  Top の `forced_max` が真なら **1.0**、偽なら Top の `floor` とする（所有者の判断 6）。
  `DEGRADED` のまま Top だけを Max にする裁定（CPU Telemetry の stale。`_zone_outputs()` の `forced_by_fault`）が
  あり、`floor` だけを渡すと Top の排気を過小に見積もり、Top が Max の最中に Front make-up air を出し損ねるため。
  同じ `forced_by_fault` には Top の Fan fault（stall・書き込み失敗）も入るが、そのときの Top の実際の排気は
  不明か 0 で、1.0 と見積もると存在しない排気をでっち上げる。そのため Fan fault の tick は §2.3 で `skipped` にし、
  **1.0 と見積もるのは CPU Telemetry の stale による Top の `forced_max` だけ**になる。
  Top の実際の排気は `max(case_aux_exhaust, safety_floor)`（`forced_max` なら 1.0）なので、CPU cooling floor による
  排気過多も Front make-up air の判断に入る（`docs/air-balance-model.md`）。floor は `requested.top` に写さない（所有者は Critical Safety のまま）
- 協調は **Fallback の中に入れない**。別の部品（`control/air_balance_coordination.py` の
  `AirBalanceCoordinator`。`control/air_balance.py` を import し、`fallback/` は import しない）にする。
  Fallback は ML にも characterization にも依存しない最後の拠り所のまま残し、raw baseline を常に記録できるようにする
- **Learned MPC の提案には掛けない。** MPC は balance の項を cost に持つ（0052 §2.3 / 0073 §2.3）。
  MPC の出力に重ねると二重に数え、authority の帯と昇格の証拠の外で MPC の出力を変えてしまう（§4 の B）
- Gate へ渡す `fallback` は **coordinated baseline** とする。Gate が Fallback を選べばそれが requested になり、
  LIMITED / EXPANDED で MPC を切り詰める帯の中心も coordinated baseline になる（§2.5）

### 2.3 協調を掛ける条件（tick ごと）

次を**すべて**満たす tick だけ `coordinate()` を呼ぶ。1つでも欠ければ `skipped` として
**raw baseline をそのまま使う**（= いまの `main` の挙動。§2.4 の保持も解く）。

| 条件 | 満たさないときの `skip_reason` | 理由 |
|---|---|---|
| `mode` が `shadow` か `apply` | （`status: off`） | |
| Air Balance が有効（`source.status: calibrated`。0073 §2.2） | `air_balance_disabled` | 未校正の曲線・比を requested に入れない（0033 / 0073） |
| 運転モードが `AUTO` | `operating_mode` | `MANUAL` / `CALIBRATION` の人の値を変えない。`MAX` は `forced_max` が勝つので無意味 |
| snapshot が `AVAILABLE`（Fallback が `propose()` を使った tick） | `snapshot_unavailable` | `propose_without_snapshot()` は曲線の最大値を全 zone に出す保守側の tick で、熱の入力も無い |
| Fallback が提案を返した（例外で `None` でない） | `baseline_unavailable` | `_requested()` の Max の経路を変えない |
| Critical Safety の `state` が `NORMAL` か `DEGRADED` | `safety_state` | `STARTUP` / `EMERGENCY` は `forced_max` で全 zone Max。協調しても effective は変わらず、trace を読みにくくするだけ |
| Critical Safety の裁定の `faults` に、zone つきの Fan fault（`TACH_STALL` / `WRITE_FAILURE` / `READBACK_MISMATCH` / `ENABLE_REVERTED`。`critical.py` の `_FAN_FAULTS`）が **Top を含めてどの zone にも無い**。かつ Front / Rear に `forced_max` が無い | `zone_fan_fault` | Fan fault の zone の `forced_max` は、その zone の実際の風量が demand から言えない（不明か 0）裁定で、Front / Rear でも Top でも同じ。Top の Fan fault を 1.0 と見積もると、存在しない Top の排気で Rear / Top の熱の排気を抑えたり、不要な Front make-up air を足したりする。ほかの zone は `fault_demand` が既に上げている（0028 §2.7）。判定は `CriticalSafetyDecision.faults` の fault code で行い、`SafetyZoneOutput.reason` の文字列は読まない。Front / Rear の `forced_max` は `NORMAL` / `DEGRADED` では Fan fault からしか生じないが、将来の裁定の追加に備えて明示的にも確かめる。CPU Telemetry の stale による Top の `forced_max` は skip せず、`projected_top_floor = 1.0` として扱う（§2.2。所有者の判断 6） |

Telemetry の鮮度は**協調の側で別の閾値を持たない**。熱の入力は 0073 §2.4 の入力契約（`ADVISORY`、
許容遅延は同じ源の必須入力に揃える）を通った値で、stale / missing は `None` になる。
`None` の熱の指標は `thermal_reasons` に入らないので、**熱を根拠にした Rear / Top の引き上げは起きない**
（上げない側 = raw baseline 側に倒れる）。Front make-up air は requested の demand だけから決まり、
Telemetry に依存しない。Telemetry の喪失そのものは Critical Safety が扱う（AGENTS.md ルール3）。

### 2.4 変えてよい量: 上げるだけ・zone ごとの上限・下げる前の保持

zone `z` の raw baseline を `c_z`、`coordinate()` の提案を `p_z` とすると、適用する値は次のとおり。

```text
s     = FanHardwareConfig.stable_demands(c)               … 0073 §2.3 の純粋関数（backend と同じ引き上げ）
top   = 1.0 if safety.zones.top.forced_max else safety.zones.top.floor   … §2.2（Fan fault の tick は §2.3 で skipped 済み）
coord = coordinate(s, thermal, projected_top_floor = top)
p_z   = coord.requested.z  if coord.requested.z > s_z  else c_z     … 引き上げの無い zone は raw のまま
r_z   = max(0, min(p_z, c_z + max_raise_z) - c_z)                     … この tick の引き上げ幅（上げるだけ・上限つき）
h_z   = 直近 release_hold_ms の間（単調時計）の r_z の最大値（この tick を含む。apply でも shadow でも同じ保持の状態）
held_z = min(1.0, c_z + h_z)                                           … 保持込みの値
out_z = held_z（apply）／ c_z（shadow。held_z は counterfactual として記録するだけ）
```

- **下げない。** `out_z >= c_z` をすべての zone・すべての tick で守る。`AirBalanceCoordination` の検証
  （既存）に加え、`AirBalanceCoordinator` の出力でも検証し、破れたら `failed`（§2.6）
- **上限は保持中も守る。** 保持するのは値ではなく**引き上げ幅** `r_z` で、`r_z <= max_raise_z` なので
  `out_z - c_z <= max_raise_z` は保持中の tick でも成り立つ。raw baseline 自身の下げ（Fallback の `decrease_hold` の後）は
  保持しない（raw baseline の挙動を協調で変えない）
- **zone ごとの上限 `max_raise`（demand、0.0..1.0）を `fan-policy.yaml` に置く**（AGENTS.md ルール9）。
  characterization の誤り・曲線の外れが、1 tick で Front を 1.0 へ跳ばすような大きな変化にならないよう、
  影響の幅を設定で閉じる。`max_raise_z = 0` はその zone を協調で動かさないことを意味する
- **評価は `stable_demands()` を通した値で行い、requested には写さない。** backend は
  `minimum_stable_demand` 未満の値を引き上げてから書くので、写像しないと実際には起きない低風量で比を採点する
  （0073 §2.3 が MPC の balance の項に決めたのと同じ理由・同じ関数）。引き上げの無い zone は raw の値のまま
- **下げる前の保持 `release_hold_ms` を置く。** 協調の引き上げ幅が**消えたときも縮んだときも**、
  直近 `release_hold_ms` の最大の引き上げ幅を保つ（上の `h_z`）。`coordinate()` は状態を持たない純粋関数で、
  毎 tick raw baseline から評価し直すので、それ自身にヒステリシスは無い。帯の縁で「上げる・戻す」を
  tick ごとに繰り返すハンチングを抑えるのは、この保持と Critical Safety の `ramp_down` だけである。
  保持の状態は `apply` でも `shadow` でも同じ規則でプロセスのメモリに持ち（再起動で消える）、次の tick で**即座に解く**
  （保持の窓を空にして raw baseline へ戻す。戻す速さは Critical Safety の `ramp_down` が制限する。0028 §2.4）:
  §2.3 の条件が崩れた（`skipped`）tick と、`failed` の tick（§2.6。raw baseline 側に倒す）。
  上げる向きは保持しない（上げる速さは制限しない。0028 §2.4）
- **shadow でも保持を模擬する。** 保持は引き上げの長さ・大きさ・切り替わりの回数を実質的に決めるので、
  保持を除いた `proposed` だけでは `apply` にしたときに Gate へ渡る値と違い、§2.8 の段 2 の判断材料
  （§5 の合否の基準）が apply の挙動を表さない。shadow は `held_z` を `counterfactual_output` として trace に記録し（§2.7）、
  Gate へは raw baseline（`c_z`）を渡す。保持の窓に入れる `r_z` は apply と同じく毎 tick の上限つきの引き上げ幅で、
  shadow では Fan に効かないため、実際の Fan の応答とは独立に「apply ならこう保持した」値になる
  （shadow の Fan は raw baseline のまま回るので、apply の閉ループの挙動まで再現するものではない。§5）
- Top は `case_aux_exhaust` の要求のまま（`top_request_role`）。CPU cooling floor は Critical Safety が
  後で `max()` を取る。協調は Top を**上げることしかできず**、CPU floor を下げる経路は無い

`fan-policy.yaml` に加える塊（値は §5。雛形の値は仮）:

```yaml
air_balance_coordination:
  mode: off                 # off | shadow | apply
  max_raise:                # demand（0.0..1.0）。0 はその zone を動かさない
    front: <#75 の後>
    rear: <#75 の後>
    top: <#75 の後>          # 推奨: 最初の apply では 0（§5）
  release_hold_ms: <#75 の RPM 応答時間の後>
```

- `fan-policy.yaml` の schema version を上げる（9 → **次の空いた番号**。いまなら 10）。旧版は起動前に拒否し、
  **自動補完しない**（`docs/control-config.md` の各版の移行と同じ）。束ねた `CONTROL_CONFIG_VERSION` も
  次の空いた番号へ上げる（いまなら 12）
- 起動時の検証: `max_raise_*` は 0.0..1.0 の有限値、`release_hold_ms` は 0 以上の整数で `tick_ms` の
  倍数に限らない（単調時計で比べる）
- **`mode` が `shadow` / `apply` で `air-balance.yaml` が `uncalibrated` なら Control Config の不正**
  （0028 §2.7 / 0073 §2.2 の「ほかが不正」と同じ、全 zone Max の `config_invalid`）。黙って `off` と読まない。
  「協調を掛けたつもりで掛かっていない」構成を、置き忘れと同じく大きな音で気付ける側に倒す
  （0073 §2.2 が「無い」を「無効」と読まないのと同じ理由。所有者の判断 3、§4 の J）。
  校正済みのまま協調だけ止めるときは `mode: off` にする（0073 §5 の「校正済みのまま Air Balance を止める手段」は
  記録を止めることで、本記録の範囲外）

### 2.5 authority stage との関係

- 協調は ML ではなく、`air-balance.yaml` と `fan-policy.yaml` に束縛された**決定論的な Baseline の一部**である。
  0057 の authority journal（Learned MPC の制御権）の対象にしない。有効化は §2.1 の設定と再起動で行い、
  `raise_stage()` を経ない
- **どの stage でも**、Gate が Fallback を選んだ tick の requested は coordinated baseline になる
  （SHADOW・低 Confidence・OOD・ML 停止・optimizer timeout のどれでも）
- LIMITED / EXPANDED の帯（`_limited_request()`）の中心は coordinated baseline にする。協調は上げるだけなので、
  帯は raw のときより下がらない。帯の幅（`limit_up` / `limit_down`）は変えない
- FULL で MPC が選ばれた tick は、MPC の requested がそのまま使われる（協調は掛けない。§2.2）。
  shadow の counterfactual（0053）の Fallback の値は、その tick の coordinated baseline とする
- **昇格の証拠との関係（所有者の判断 5）**: `mode`・`max_raise`・`release_hold_ms` は `fan-policy.yaml` にある。
  ただし、いまの 0057 §2.4 の照合は**報告の provenance（評価時の設定の hash）といま動いている設定**を比べるだけで、
  消費した trace がどの `fan-policy.yaml` で記録されたかは見ない（trace との束縛は 0073 §2.6 が
  `air-balance.yaml` / `fan-hardware.yaml` にだけ決め、`fan-policy.yaml` は 0073 §5 で未決）。
  このままでは、`mode: off` で記録した trace を `mode: apply` の設定の下で評価し直した報告が照合を通り、
  raw baseline の下の証拠が coordinated baseline の証拠を名乗れる。帯の中心が動く（上の3点目）ので、
  この取り違えは MPC の評価そのものを変える。そこで本記録は 0073 §5 のその行のうち `fan-policy.yaml` を決める:
  - Offline Evaluation は、消費した各 tick の `runtime.config.policy_sha256` を評価時の
    `ControlConfig.sources.policy.sha256` と突き合わせ、一致・不一致・欠落の件数を報告の provenance に
    **`fan_policy_trace_binding`**（0073 §2.6 の `TraceConfigBinding` と同じ形）として残し、`conditions_sha256` の材料にも入れる
  - `_check_evidence()` は、0073 §2.6 の2つの binding と同じく、**消費した tick が1つ以上あり、すべてが一致したときだけ**
    証拠に使い、不一致・欠落が 1 件でもあれば `AuthorityEvidenceError` で拒む。除外して残りで評価する方式は採らない（0073 §2.6 と同じ理由）
  - 評価報告の版と `MIN_EVIDENCE_REPORT_SCHEMA_VERSION` を次の空いた番号へ上げる（旧版の報告は昇格の証拠にしない）。
    `RolloutEvidence` は `fan_policy_config_sha256` を既に持つので、`AuthorityJournal` の版は上げない
  - `safety.yaml` の trace との束縛は本記録でも決めない（0073 §5 のまま）
  - これにより、協調の設定を変えると、変えた後の設定で記録した trace が溜まるまで昇格は止まる（止まる側に倒れる）

### 2.6 失敗の扱い

`AirBalanceCoordinator` の中の例外（`coordinate()` の不具合、`out_z < c_z` の検出を含む）は、
**握りつぶさず `failed` として trace と構造化ログ（ERROR）に残し、その tick は raw baseline を使う**。
そのうえで `mode` により次のとおり扱う（所有者の判断 4）。

| `mode` | 扱い | 根拠 |
|---|---|---|
| `apply` | **0028 §2.7 の「Fallback Controller / Reactive Guard の例外」と同じ扱い。** loop が `fallback_exception` の fault に翻訳し（detail に `air_balance_coordination` を残す）、次 tick の Critical Safety が `EMERGENCY`（全 zone Max）にする | `apply` の協調は Baseline の requested を作る決定論的な層（§2.5）であり、その失敗は不具合である。0028 §2.7 は決定論的な層の失敗を Max にすると決めており、いまの Gate も同じく `fallback_exception` へ翻訳してこの tick は Fallback の値で続ける（`ControlLoop._select()`）。0028 §2.7 の表に新しい区分を足さず、既存の行をそのまま当てはめるので、0028 を置き換えない |
| `shadow` | 記録だけ（fault にしない。Safety の状態は変えない） | shadow の出力は requested に入らず、制御の経路の外にある（0053 の counterfactual と同じ位置づけ）。shadow が Fan を変えないこと（§2.10 の段の独立性）を失敗の時にも守る |

- `mode: apply` の失敗で Max にすると騒音が続くが、正しい設定で再起動するか `mode: off` に戻して再起動するまでの間である。
  raw baseline のまま黙って続ける代替（§4 の I）は 0028 §2.7 と矛盾するので、選ぶなら 0028 §2.7 の一部を置き換える記録になる
- 例外を捕まえる範囲: `ControlLoop._run_tick()` では「ここから合成までは捕まえない」区間が `self._safety.evaluate()` から
  始まり、§2.2 の位置（Safety の評価の後・Gate の前）はその**区間の中**にある。Gate の `_select()` と同じく、
  `try` / `except` は **`AirBalanceCoordinator.apply()` の呼び出しだけ**を囲み、Critical Safety の評価と合成の例外は
  捕まえない（0028 §2.7 の規則は変えない。§2.10 で試験する）

### 2.7 trace への記録（`ControlTick` の次の版）

`ControlTick` に tick ごとの塊 `air_balance_coordination`（新設、その版では必須）を加える。

```text
air_balance_coordination:
  schema_version: 1
  mode: off | shadow | apply                         # 設定の値をそのまま写す
  status: off | skipped | not_needed | shadow | applied | failed
  skip_reason: air_balance_disabled | operating_mode | snapshot_unavailable
             | baseline_unavailable | safety_state | zone_fan_fault | null    # skipped のときだけ非 null
  demand_basis: stable_candidate                     # 固定値。何の demand で評価したか
  candidate:  {front, rear, top} | null              # raw baseline（c）
  proposed:   {front, rear, top} | null              # 上限を掛ける前の p
  output:     {front, rear, top} | null              # 実際に Gate へ渡した値（apply 以外は candidate と同じ）
  counterfactual_output: {front, rear, top} | null   # 保持込みの値 held_z。shadow / apply で記録（apply では output と同じ）
  bounded_by_max_raise: {front, rear, top: bool} | null
  held:       {front, rear, top: bool} | null        # release_hold_ms の保持で h_z > r_z だった zone（shadow は模擬の保持）
  projected_top_floor: demand | null                 # 渡した Top の値（Safety の Top floor。forced_max なら 1.0）
  before_state / before_ratio / projected_state / projected_ratio   # AirBalanceCoordination.before / projected から
  reasons: [front_makeup_air | rear_thermal_exhaust | top_case_aux_exhaust ...]
  failure: {type, detail(≤500)} | null               # failed のときだけ
```

**検証の不変条件**（読み書きの両方で検証し、破れた記録は拒む。0073 §2.5 (b) と同じ流儀）:

| 条件 | 内容 |
|---|---|
| `mode: off` | `status: off`、ほかの欄はすべて `null` / 空 |
| `status: skipped` / `failed` | `output == candidate`（raw baseline を使った）、`counterfactual_output` は `null`。`skip_reason` / `failure` はそれぞれのときだけ非 null。`held` はすべて偽（保持を解いた。§2.4） |
| `status: shadow` | `mode: shadow` と同値（`mode: shadow` で `coordinate()` を呼んだ tick は、上げる必要の有無によらず常に `shadow`。`not_needed` / `applied` にはならない）。`output == candidate`、`proposed` と `counterfactual_output` は記録する（適用しない）。apply なら上げていたかは `counterfactual_output > candidate` で読む。`held` は模擬の保持（§2.4） |
| `status: not_needed` | `mode: apply`、すべての zone で `output == candidate`、`counterfactual_output == output` |
| `status: applied` | `mode: apply`、少なくとも1 zone で `output > candidate`（保持による場合を含む）、`counterfactual_output == output` |
| すべて | 各 zone で `output >= candidate` かつ `output - candidate <= max_raise`。`counterfactual_output` があれば同じく `>= candidate` かつ `- candidate <= max_raise`（保持中も。§2.4 は引き上げ幅を保持する） |
| Gate が Fallback を選んだ tick | `ZoneRecord` の requested が zone ごとに `output` **以上**。等しくないのは、その zone の `controller_reason` が `fallback_transition_floor` のときだけ |

- Gate が Fallback を選んだ tick の requested は、ふつうは `output` と一致する。例外は Learned MPC から Fallback へ
  移った最初の tick で、`ControllerGate._prevent_transition_drop()` が各 zone を直前の requested まで引き上げる
  （`fallback_transition_floor`）。**順序は協調が先、Gate の遷移の floor が後**である（Gate は coordinated baseline を
  受け取って `max(output, 直前の requested)` を取る）。遷移の floor が値を決めた zone の reason は `fallback_transition_floor`
  のまま（detail の `baseline_reason` に協調の reason が入る）とし、協調の reason で上書きしない
- 協調が値を決めた zone の `ZoneRecord.controller_reason` は `air_balance_front_makeup_air` /
  `air_balance_rear_thermal_exhaust` / `air_balance_top_case_aux_exhaust`（`h_z > r_z` の保持中は `air_balance_release_hold`）とし、
  detail に raw baseline の reason code と `candidate` / `output` を残す（`fallback_coordinated_max` と同じ形）
- 版番号は**実装の時点で次の空いた番号**を使う（#192 が先に v13 を使えば v14）。**他の版上げと直列にする**
  （同じ番号を2つの PR が名乗らない）。版上げの PR は `src/coldaisle/web/airflow-trace.js` の
  `KNOWN_VERSIONS` に新しい版を加え、新しい版の fixture（`tests/fixtures/control_tick_vNN.json`）と
  `tests/test_airflow_trace.py` を更新する（0071 §2.3 の「版の解釈は1か所」）。
  0060 §2.4 のとおり、新しい版を名乗る tick は塊を必ず持ち、旧版は持たない。旧版の trace は書き換えない
- trace の本文を LLM のプロンプトへ直接入れない規則（AGENTS.md ルール8）は変えない。AI ツールへは足さない

### 2.8 段階的な展開（shadow を先に）

| 段 | 設定 | 実 Fan への影響 | 進む条件 |
|---|---|---|---|
| 0 | `mode: off`（雛形の既定） | なし（いまと同じ） | — |
| 1 | `mode: shadow` | **なし**。毎 tick `proposed` と保持込みの `counterfactual_output` を記録するだけ | #75 の校正済み `air-balance.yaml` がある（§2.9 の A）。`uncalibrated` では起動しない（§2.4） |
| 2 | `mode: apply`、控えめな `max_raise`（Top は 0 を推奨） | Baseline の requested が上がりうる（下がらない） | shadow の集計（§5 の基準）を所有者が確認し承認する。承認と値は決定記録か PR に残す |
| 3 | `max_raise` の見直し（Top を含む） | 同上 | apply 期間の trace と #75 の response matrix を見て所有者が承認 |

- 段を進めるのは**人の設定変更と再起動だけ**。自動では進まない。戻すのは `mode: off`（または shadow）にして再起動
- shadow の集計は保存済み trace を読むだけの手段で行う（制御プロセスの外。0074 と同じ規律）。
  集計の道具を新しい CLI にするか、既存の offline 評価（#91）に足すかは §5

### 2.9 有効化の前に #75 から要るもの

| 段 | #75 の成果物 | 用途 |
|---|---|---|
| A（shadow の前） | zone ごとの demand → Airflow Index → EFU 曲線（PWM→RPM sweep、`minimum_stable_demand`、最大 RPM を含む）で `source.status: calibrated` の `air-balance.yaml` | `coordinate()` の q と比。無ければ runtime は Air Balance を作らない |
| A | `balance` の `target_ratio` / `minimum_ratio` / `maximum_ratio` | 協調の発火と目標。1.0 を前提にしない（#81） |
| A | `thermal_inputs` の束縛と `thermal_limits` | Rear / Top の引き上げの熱の根拠。束縛しない指標は使われない（0073 §2.4） |
| B（apply の前） | Thermal Effectiveness の response matrix（Front +Δ / Rear +Δ / Top +Δ が GPU Intake・case ΔT・GPU / CPU 温度をどう変えるか） | Front make-up air が GPU 吸気を悪くしないこと、Rear 優先（0026）で熱が下がることの確認 |
| B | RPM の応答時間・再現性 | `release_hold_ms` の下限（Fan が応答し終える前に戻さない） |
| B | 騒音の回答（0067）と demand の対応 | `max_raise` の上限を騒音で決めるときの材料 |

### 2.10 実機なしでの試験

すべて Mock / Replay / simulated backend で行い、`hardware` マーカーを付けない（AGENTS.md ルール7）。
校正済みの値は `tests/fixtures/` の試験用 characterization（`calibrated` を名乗る試験専用のもの。
`config/` には置かない）を使う。

- **下げない**: 3 zone の demand の格子（と hypothesis 等の性質試験）で、`output >= candidate` と
  `output - candidate <= max_raise` をすべての入力で確かめる
- **段の独立性**: 同じ Replay の入力で `off` と `shadow` の loop を回し、Backend に渡る effective が
  **tick ごとに完全に一致**することを確かめる（shadow は Fan を変えない）
- **条件の欠け**: `MANUAL` / `CALIBRATION` / `MAX`、snapshot 不在、Fallback 例外、`STARTUP` / `EMERGENCY`、
  Front / Rear / Top の Fan fault（`TACH_STALL` と書き込み失敗の各 code）、Air Balance 無効のそれぞれで `skipped` と正しい `skip_reason`、`output == candidate`。
  Top の Fan fault と CPU Telemetry の stale が同じ tick に重なっても `skipped` であること
- **Top の `forced_max`**: `DEGRADED` で CPU Telemetry が stale（Top だけ Max）の tick に `projected_top_floor == 1.0` が渡り、
  Top の floor だけを渡した場合より Front make-up air が小さくならないこと
- **合成を迂回しない**: 協調で上げた requested に Guard の ceiling が掛かること、Safety の floor・`forced_max`・
  `ramp_down` がいまと同じく掛かること、Top の CPU cooling floor が協調の有無で変わらないこと
- **失敗**: `coordinate()` が例外を投げる・下げる値を返す偽の model を差し込み、その tick は `failed`・raw baseline・
  ERROR ログであること。`mode: apply` では `fallback_exception` が次 tick の Safety に渡って `EMERGENCY` になること、
  `mode: shadow` では Safety の状態も Backend に渡る effective も変わらないこと
- **捕まえる範囲**: Critical Safety の評価と合成が投げる例外は、協調を入れた後も捕まらずに loop の外へ出ること
  （協調の `try` が `AirBalanceCoordinator.apply()` だけを囲むこと）
- **保持**: simulated clock で、引き上げ幅が消えた・縮んだ後 `release_hold_ms` の間だけ直近の最大の幅が残り、
  保持中も `output - candidate <= max_raise` であること、`skipped` / `failed` の tick で即座に解けること。
  同じ入力列で `shadow` の `counterfactual_output` が `apply` の `output` と tick ごとに一致すること（開ループの入力で比べる）
- **設定**: `shadow` / `apply` + `uncalibrated` が `config_invalid`、旧版の `fan-policy.yaml` の拒否、範囲外の `max_raise` の拒否
- **trace**: §2.7 の不変条件の正負の試験、新しい版の fixture と `airflow-trace.js` の読み取り試験
- **Gate**: LIMITED の帯の中心が coordinated baseline になること、FULL で MPC の requested に協調が掛からないこと、
  Learned MPC から Fallback へ移った最初の tick で `fallback_transition_floor` が coordinated baseline の上に掛かり、
  trace の検証（requested ≥ `output`）を通ること
- **昇格の証拠**: `mode` だけを変えた `fan-policy.yaml` で評価した報告が、変える前の trace を1 tick でも含めば
  `fan_policy_trace_binding` の不一致で `_check_evidence()` に拒まれること

### 2.11 実装の段階

| 順 | Issue / PR | 内容 | 版 |
|---|---|---|---|
| 1 | 本記録（docs） | 所有者の承認で FINAL | — |
| 2 | #81 の実装 PR（a） | `fan-policy.yaml` の `air_balance_coordination` と検証（`uncalibrated` との組み合わせの拒否を含む）、純粋な `AirBalanceCoordinator`（上限・保持・下げない検証）と単体試験、`docs/control-config.md` の移行手順。**loop へは配線しない** | `fan-policy.yaml` 9 → 次、`CONTROL_CONFIG_VERSION` 11 → 次 |
| 3 | #81 の実装 PR（b） | loop への配線（§2.2 の位置・§2.3 の条件・§2.6 の失敗）、Gate へ coordinated baseline を渡す、`ControlTick` の新しい版と `air_balance_coordination`、`airflow-trace.js` の `KNOWN_VERSIONS`、fixture、`docs/air-balance-model.md` の更新 | `ControlTick` 次の空いた番号（#192 の v13 の後なら v14）。**他の版上げの PR と直列** |
| 4 | #91（または #81 の追加 PR） | `fan_policy_trace_binding` と `_check_evidence()` の拒否（§2.5）、offline 評価の Baseline の arm が `mode` に応じて coordinated baseline を再現する、shadow の集計（§5）。**段 6 より前に入れる** | 評価報告と `MIN_EVIDENCE_REPORT_SCHEMA_VERSION` を次の空いた番号へ |
| 5 | #75（人・実機） | 校正済み `air-balance.yaml`（§2.9 の A）、続いて B | — |
| 6 | 運用（人） | `mode: shadow` へ変更・再起動、集計を所有者が確認 | 設定のみ |
| 7 | 運用（人） | 承認後 `mode: apply`（Top の `max_raise` は 0 から） | 設定のみ |

2 と 3 はコードだけで完結し、#75 を待たない（試験用 characterization で試験する）。
実 Fan への効果が出るのは 6 以降で、#75 の後である。

## 3. Consequences

### 良くなること

- Baseline が握っている時間（SHADOW・低 Confidence・ML 停止を含むほとんどの時間）にも、
  Top 排気による負圧・Exhaust 過多を Front make-up air で補える（`docs/airflow-model.md` の懸念）
- 協調は上げるだけ・上限つき・合成の前なので、Guard と Critical Safety の保証はそのまま残る
- `off` / `shadow` / `apply` と trace の `candidate` / `proposed` / `output` により、実 Fan を変える前に
  「掛けていたら何が起きたか」を実データで見られる
- 協調の不具合は `apply` では 0028 §2.7 どおり Max に倒れ、`shadow` では Fan を変えない。どちらも Safety の層は弱まらない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| Baseline（最後の拠り所）が characterization に依存するようになる | 既定 `off`、`calibrated` 以外では開けない、上げるだけ・`max_raise` で幅を閉じる、`mode: off` と再起動で戻せる。Fallback 本体は変えず raw baseline を常に記録する |
| 曲線の誤りで Front などが余分に回り、騒音が増える | `max_raise` の上限、shadow で事前に頻度と大きさを確認、0067 の騒音の回答を材料にする |
| 帯の縁でのハンチング | `coordinate()` は状態を持たずヒステリシスが無いので、抑えるのは引き上げ幅の保持 `release_hold_ms` と Critical Safety の `ramp_down` だけ。shadow の集計で切り替わりの回数を見る（§5）。協調の側に帯のヒステリシスを持たせるかは §5 |
| `fan-policy.yaml` の版上げと、`ControlTick` の版上げが要る | 既存の版の追加と同じ手順。旧版の trace は書き換えない。版上げの PR を直列にする（§2.7） |
| 協調の設定を変えるとそれまでの昇格の証拠が使えなくなり、評価報告の版上げ（`fan_policy_trace_binding`）が要る | 止まる側に倒れるだけで安全側は弱めない（0057 / 0073 §2.6 と同じ形）。段の切り替えは頻繁に行わない |
| LIMITED / EXPANDED の帯の中心が上がり、MPC が下げられる範囲が狭まる | 上げる向きなので安全側。MPC の balance の項と向きは揃う。帯の幅は変えない |
| `apply` の協調の不具合1つで全 zone Max になり、再起動まで騒音が続く | 0028 §2.7 の決定論的な層と同じ扱いで、不具合を黙って続けない。`shadow` の期間に同じコードを実データで回して失敗を先に洗い出す。戻すのは `mode: off` と再起動 |
| Top が `forced_max` の tick に Front make-up air が出る | CPU Telemetry の stale で Top が Max のときの排気過多を補うためで、上げるだけ・`max_raise` の範囲。Fan fault（Top を含むどの zone でも）の tick では協調しない |

## 4. 却下した代替案

| 案 | 利点 | 却下理由 |
|---|---|---|
| **A. Fallback には掛けない**（Air Balance は MPC の cost だけ） | Baseline が最も単純なまま。characterization の誤りが Baseline に届かない | Baseline が動く時間のほとんどで Air Balance が効かず、#81 の協調（Front make-up air）が FULL まで実現しない。上限と shadow で Baseline への影響を閉じられるので、掛けない理由が弱い。**所有者がこちらを選ぶ場合は、本記録を Rejected にし 0073 §5 の1行目を「掛けない」で閉じる記録にする。所有者の判断 1 で選べる** |
| B. Gate の後で、選ばれた requested（Fallback でも MPC でも）に掛ける | 1か所で全制御器に効く | MPC は balance の項を cost に持つので二重に数える。authority の帯と昇格の証拠の外で MPC の出力を変える |
| C. 合成の後（effective）や Backend の直前で足す | 実際の値に直接効く | AGENTS.md ルール2・5 と 0028 §2.3 に反する。Guard の ceiling と `ramp_down` の外で値が変わる。**禁止** |
| D. `FallbackController` の中で掛ける | 部品が増えない | 最後の拠り所が characterization に依存し、raw baseline を記録できない。Fallback の `decrease_hold` と協調の保持が絡む |
| E. `calibrated` になったら shadow 無しで即適用（0073 の `source.status` だけで決める） | 設定が1つ減る | 校正の存在は制御の効果を示さない。実 Fan を変える前に shadow で確かめる段を飛ばす。**所有者の判断 2 で選べる** |
| F. 有効化のフラグを `air-balance.yaml` に置く | Air Balance の設定が1ファイルにまとまる | `air-balance.yaml` は #75 の測定の成果物（characterization）で、制御の方針ではない。0073 §4 が Air Balance の有効化に別フラグを置かないと決めたのと混ざる |
| G. 上限を設けない（`coordinate()` の目標まで上げる） | 目標比に最も早く届く | 曲線の外れ1つで Front が 1.0 へ跳ぶ。騒音と影響の幅を設定で閉じられない |
| H. 双方向に掛ける（Intake 過多で Front を下げるなど） | 騒音を下げられる | 既存の不変条件（協調は cooling demand を下げない）に反する。まだ検証されない model で冷却を下げる |
| I. `apply` の協調の例外でも raw baseline のまま続け、Safety へつながない | Air Balance の不具合で Max の騒音が続かない。raw baseline はいまの承認済みの挙動 | 0028 §2.7（FINAL）は決定論的な層の例外を全 zone Max にすると決めており、`apply` の協調はその層である。選ぶなら 0028 §2.7 の一部を置き換える記録にし、0028 へ `Superseded by` を追記する必要がある（本記録は `Supersedes: なし`）。**所有者の判断 4 で選べる** |
| J. `shadow` / `apply` + `uncalibrated` を黙って `off` として起動 | 起動が止まらない | 協調を掛けたつもりで掛かっていない状態が見えにくい（0073 §2.2 が「無い」を「無効」と読まない理由と同じ）。**所有者の判断 3 で選べる** |
| K. LIMITED / EXPANDED の帯の中心を raw baseline にする | MPC の自由度を変えない | Gate が Fallback を選んだときの値（coordinated）と、帯の基準（raw）が別の Baseline になり、「Baseline」が2つの意味を持つ |
| L. 前 tick の applied demand（実測側）を基準に協調する | 実際に回った風量に近い | 1 tick 遅れの帰還になり、協調自身の出力を次の入力にする。candidate を `stable_demands()` に通せば backend の引き上げは反映できる |
| M. 協調専用の Telemetry 鮮度の閾値を持つ | 明示的 | 0073 §2.4 の入力契約と二重になる。stale は `None` になり、熱による引き上げは起きない側に倒れる |
| O. どの zone でも Critical Safety の `forced_max` があれば `skipped` にする | 単純。Safety が Max を握る tick に協調を重ねない | CPU Telemetry の stale で Top だけが Max になる `DEGRADED` の最中こそ排気過多で Front make-up air が要る（Codex #4141058794）。Top は 1.0 と見積もれば足りる。**所有者の判断 6 で選べる** |
| P. 昇格の証拠を `fan-policy.yaml` の trace に束縛しない（0073 §5 の未決のまま） | 評価報告の版上げが要らない | `mode: off` の trace を `mode: apply` の設定で評価した報告が照合を通り、raw baseline の証拠が coordinated baseline の証拠を名乗れる。LIMITED / EXPANDED の帯の中心が変わるので MPC の評価も変わる。**所有者の判断 5 で選べる** |
| N. 0057 の authority journal で協調の段（shadow / apply）を管理する | 昇格の仕組みを再利用できる | journal は Learned MPC の制御権のためのもので、決定論的な Baseline の方針を混ぜると役割が混ざる（AGENTS.md ルール5）。設定と再起動で足りる。**所有者の判断 2 で選べる** |

## 5. 未決事項

| 論点 | どこで決めるか | 実測待ちか |
|---|---|---|
| `max_raise` の zone ごとの値（最初の apply で Top を 0 にするかを含む） | 段 2 の承認時（PR か決定記録） | **#75 の B と shadow の集計を待つ** |
| `release_hold_ms` の値 | 同上 | **#75 の RPM 応答時間を待つ** |
| `balance` の target / min / max、`thermal_inputs` の束縛、`thermal_limits`（0073 §5 から継続） | #75 / #81 の実測後、`air-balance.yaml` の値として | **#75 の A を待つ** |
| shadow → apply の合否の基準（期間、引き上げの頻度・大きさの上限、`not_needed` ↔ `applied` の切り替わりの回数、熱の指標との対応） | 段 6 の前に所有者と（決定記録か PR） | **shadow の実データを待つ** |
| shadow の集計の道具（新しい CLI か、#91 の offline 評価に足すか） | 実装の段 4 | いいえ |
| shadow の `counterfactual_output` は開ループ（Fan は raw baseline のまま）の値で、apply の閉ループ（上げた Fan が次の熱の入力を変える）は再現しない。その差を合否の基準でどう扱うか | 段 6 の前に所有者と | **shadow の実データを待つ** |
| offline 評価の Baseline の arm が coordinated baseline を再現する形（0054 の attribution との関係） | #91 | いいえ |
| 協調の側に帯のヒステリシス（戻る側の閾値を target 寄りにする等）を持たせるか | shadow の集計で `release_hold_ms` だけでは切り替わりが多いと分かった場合、別の記録 | **shadow の実データを待つ** |
| `safety.yaml` の trace との束縛（0073 §5 の残り） | 別の記録（本記録は `fan-policy.yaml` だけを決める。§2.5） | いいえ |
| 協調の状態を `airflow.html` に表示するか | #106 の後続 | いいえ |
| Rear 優先の順（0026）を response matrix に基づく zone 選択へ変えるか | 0026 を置き換える別の記録（`docs/air-balance-model.md` のとおり） | **#75 の B を待つ** |
| RPM の読み戻しを使った推定への切り替え（0073 §5 から継続） | #75 の結果を見て | **#75 を待つ** |
| 版番号（`fan-policy.yaml`・`CONTROL_CONFIG_VERSION`・`ControlTick`） | 実装の時点で次の空いた番号。欄の意味は本記録のとおり | いいえ |
