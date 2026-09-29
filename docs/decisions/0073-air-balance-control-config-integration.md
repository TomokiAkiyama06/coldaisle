# 決定記録 0073: `air-balance.yaml` の Control Config への統合、未校正時の起動、`estimated_flow` の記録

- **種別**: Decision Record
- **Status**: FINAL（2026-09-29、リポジトリ所有者が承認）
- **Date**: 2026-09-29
- **Supersedes**: [`0033-air-balance-config-boundary.md`](0033-air-balance-config-boundary.md) §2 の
  「`uncalibrated` を runtime controller は起動時に拒否する」の一文のみ（§2.2 で置き換える。0033 の他の節は有効）
- **関連**: [`0033-air-balance-config-boundary.md`](0033-air-balance-config-boundary.md) §2 / §5、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.3 / §2.4 / §2.7、
  [`0026-three-zone-fan-control.md`](0026-three-zone-fan-control.md)、
  [`0052-learned-mpc-optimizer-and-hard-constraints.md`](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.3、
  [`0054-offline-evaluation-attribution-and-gates.md`](0054-offline-evaluation-attribution-and-gates.md) §2.7、
  [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md) §2.4、
  [`0060-control-loop-runtime.md`](0060-control-loop-runtime.md) §2.4、
  [`docs/control-config.md`](../control-config.md)、[`docs/air-balance-model.md`](../air-balance-model.md)、
  [`docs/airflow-model.md`](../airflow-model.md)
- **対象 Issue**: #81 / #74

## 1. Context

決定記録 0033（FINAL）は `air-balance.yaml` を4つ目の Control Config とし、4ファイルを
一括検証・一括採用すると決めた。一方で、次の3点を §5 の未決として残した。

1. 4ファイル一括の schema version と移行方法
2. `basis` と #75 の測定記録の機械的な照合形式
3. Air Balance の推定結果を #82 の decision trace へ格納する schema

いまの `main` の状態は次のとおり。

| 箇所 | 状態 |
|---|---|
| `ControlConfig.from_directory()` | `fan-hardware.yaml` / `safety.yaml` / `fan-policy.yaml` の3ファイルだけを読む（`CONTROL_CONFIG_VERSION = 10`） |
| `AirBalanceConfig` / `ConfiguredAirBalanceModel` | 純粋モデルとして実装済み。`uncalibrated` は `allow_uncalibrated_for_testing=True` が無いと `UncalibratedAirBalanceError` |
| `coldaisle-fand` | Air Balance を配線していない。Learned MPC の worker も未配線で、制御器は Fallback だけ |
| `ZoneRecord.estimated_flow` | 欄はあるが、loop が値を入れないため**常に `None`**。offline 評価は毎 tick `no_estimated_flow` を数える |
| `ControlTickRuntime.config`（`ControlConfigDigest`） | 3ファイルの SHA-256 だけ。schema version は `ControlConfig.trace_metadata()` にあるが、起動ログにしか出ず、保存される `ControlTick` には無い |
| `EvaluationProvenance` / `conditions_sha256` / `RolloutEvidence` | 設定は `fan-hardware.yaml`（provenance と条件のみ）・`safety.yaml`・`fan-policy.yaml` だけを束縛する。昇格の照合（`_check_evidence()`）は `fan-policy.yaml` と `safety.yaml` の hash だけをいまの設定と比べる |

このままでは #81 の「制御ループへの接続」と、#74 の「`estimated_flow` を記録する」を
実装できない。さらに、#75 の実測が終わるまで**校正済みの `air-balance.yaml` は存在しない**。
0033 は「`uncalibrated` を runtime controller は起動時に拒否する」と決めた。
これをそのまま実装すると、#75 まで `coldaisle-fand` は通常運転に入れない。
本記録はこの一文を**置き換える**（解釈で両立させない。0033 側に `Superseded by` を追記する）。

本記録はこの3点（統合の版と移行・未校正と不在時の起動・`estimated_flow` の格納）と、
Air Balance を制御へつないだ後に必要になる**昇格の証拠への `air-balance.yaml` の束縛**（§2.6）を決める。
0033 の2点目（`basis` の機械照合）は扱わない（§5）。

## 2. Decision

### 2.1 4ファイル一括の版と移行

- `ControlConfig` に `air_balance: AirBalanceConfig` を加え、`CONFIG_FILENAMES` に
  `"air_balance": "air-balance.yaml"` を加える。`ControlConfig.from_directory()` は
  **4ファイルすべてを読み、すべて検証できたときだけ**返す（0033 §2 のとおり）
- `ConfigSource.name` と `ConfigSources` に `air-balance.yaml` を加える。
  `trace_metadata()` は4ファイルの名前・schema version・SHA-256 を返す
- 束ねた版 `CONTROL_CONFIG_VERSION` を **10 → 11** に上げる。
  各ファイルの版は変えない（`fan-hardware.yaml` 1、`safety.yaml` 3、`fan-policy.yaml` 9）
- `air-balance.yaml` 自身は **schema version 2** にする（§2.4 の `thermal_inputs` を加えるため）。
  v1 は起動前に拒否し、**自動補完しない**（`docs/control-config.md` の各版の移行と同じ規則）。
  v1 の既存物は `tests/fixtures/air_balance_uncalibrated.yaml` だけで、runtime の `config/` には無い
- `AirBalanceConfig` の定義場所は `control/air_balance.py` のままにし、`control/config.py` が
  それを import する。`air_balance.py` は `config.py` を import しないので循環しない
  （レイヤの向きは control 内で閉じる）
- **live reload はしない**（0033 §2、0028 §2.7）。変更は再起動後の `STARTUP` の Max を経て反映する

**移行手順**（実装 PR の説明と `docs/control-config.md` に書く）:

1. 運用の設定ディレクトリに `air-balance.yaml`（v2）を置く。#75 の前なら
   `source.status: uncalibrated` のもの（§2.2）
2. 新しいコードへ更新して再起動する

リポジトリの `config/` には、ほかの3ファイルと同じく**実運用の `air-balance.yaml` を置かない**
（0033 §2）。雛形は `tests/fixtures/` に置いた未校正例を v2 へ直したものを使う。

### 2.2 不在・不正・未校正のときの `coldaisle-fand` の起動

| `air-balance.yaml` の状態 | 扱い | 動作 |
|---|---|---|
| **無い** | Control Config の不正 | 0028 §2.7 の「hardware は正しく、ほかが不正」と同じ。**全 zone Max（`config_invalid`）**、正しい設定で再起動するまで解除しない |
| **形が不正**（schema 違反・v1・曲線の単調性違反など） | Control Config の不正 | 同上 |
| **`source.status: uncalibrated`** | **検証は通す。Air Balance を無効にして起動する** | 制御は Air Balance 無しの経路（いまの Fallback と同じ）で動く。起動ログと毎 tick の trace に `disabled: uncalibrated` を残す |
| **`source.status: calibrated`** | 検証を通し、Air Balance を有効にする | §2.3 のとおり推定を記録し、Learned MPC を配線したときは cost の項として渡す |

要点:

- **0033 §2 の「runtime controller は起動時に拒否する」を、「runtime は未校正の
  characterization を検証したうえで、そこから `ConfiguredAirBalanceModel` を作らない」に置き換える。**
  0033 のその一文は本記録で失効する（Supersedes）。
  デーモンは `allow_uncalibrated_for_testing` を渡す経路を持たず、未校正の曲線・比・熱の閾値が
  requested demand にも trace の推定値にも入らない。**起動そのものは止めない**
- 無効のときに失うのは **Air Balance による requested の引き上げ提案と推定の記録だけ**である。
  Air Balance は Safety ではなく（0033、`docs/air-balance-model.md`）、Critical Safety の
  floor・CPU cooling floor・Reactive Guard はどれも変わらない。したがって無効の状態は、
  いま `main` が動いている状態と同じで、**より危険な側へは動かない**
- 「無い」を「無効」と読まない。ファイル名の誤記や置き忘れを、黙って Air Balance 無しで
  走らせる経路にしないため（0033 が防ごうとした部分適用と同じ種類の事故）
- 有効・無効は **`source.status` だけから決める**。別のフラグ（`enabled: false` など）は
  作らない。校正済みのまま止めたい場合の扱いは §5

### 2.3 有効なときに Air Balance が制御へ効く範囲

本記録で決めるのは**観測（記録）と、Learned MPC への受け渡し**までとする。

- 毎 tick、Hardware Backend が実際に書いた **applied demand**（§2.5 (a)）で `evaluate()` を呼び、
  §2.5 の記録を作る。requested でも effective でもなく applied を使うのは、実際に回っている量を
  記録するためである。requested には Safety の floor が入っておらず（0028 §2.4）、effective には
  backend の `minimum_stable_demand` への引き上げと、起動時の `startup_demand` の kick が入っていない
  （`SimulatedFanBackend._safe_demand()`。低 demand の運転で風量を過小に記録してしまう）
- Learned MPC の worker を配線するとき（#86 / #104）は、`ControlConfig.air_balance` から作った
  `AirBalanceModel` と `BalanceBand` を `LearnedMpcController` に渡す（0052 §2.3 のとおり、
  目標帯は `air-balance.yaml` が持ち MPC へ写さない）。無効のときは `None` を渡し、
  cost の項は 0 になる（`MpcCostModel` の既存の区別。「不明」と「未接続」を混ぜない）
- **MPC の balance の項は、候補の demand を hardware の写像に通した値で評価する。**
  いまの `MpcCostModel._balance_cost()` は候補の `step.demands` をそのまま `evaluate()` に渡すが、
  backend は profile の `minimum_stable_demand` 未満の値を引き上げてから書く
  （`SimulatedFanBackend._safe_demand()`）。そのままでは optimizer が実際には起きない風量の
  状態で比を採点し、適用後の比が目標帯の外になる plan を選びうる。trace の推定（applied demand。
  §2.5 (a)）と同じ定義にそろえるため、次のようにする:
  - 引き上げの規則（`max(demand, minimum_stable_demand)`）を `fan-hardware.yaml` の profile から
    決まる**純粋関数**として1箇所に置き、backend と `MpcCostModel` の両方がそれを使う
    （2つの実装を持たない。MPC は hardware を呼ばない。0052 の境界は変えない）
  - Learned MPC を配線するとき、Air Balance を渡すなら同じ `ControlConfig` の profile も
    `MpcCostModel` に渡す。**Air Balance を渡して profile を渡さない組み合わせは構成時に拒否する**
    （`MpcCostUnusableError`。runtime は model 読込の失敗として Fallback を続ける）
  - `startup_demand` の kick は backend の起動状態に依存する一時的な値なので写像に含めない。
    kick は `minimum_stable_demand` 以上への一時的な引き上げで、その間の実際の比は trace
    （applied demand）に残る
  - 写像は balance の項の評価にだけ使い、plan の requested そのものは変えない（Guard / Safety の
    入力を書き換えない）。Acoustic の項への同じ扱いは #94 で決める（§5）
- **Fallback Controller の requested に `coordinate()` を掛けるかは本記録では決めない。**
  Baseline の requested を変える制御の変更であり、安全・制御系の変更として別途の承認が要る（§5）

いずれの場合も Air Balance の出力は requested までで、Reactive Guard と Critical Safety を
迂回しない（AGENTS.md ルール2・5）。Top の要求は `case_aux_exhaust` のままで、CPU cooling floor の
所有者は Critical Safety のまま（0026、`docs/air-balance-model.md`）。

### 2.4 `air-balance.yaml` v2: 熱の入力の束縛

`ThermalInputs`（GPU 吸気・case ΔT・CPU package・GPU 温度）を tick の snapshot のどの
metric から取るかを、コードに書かず設定に置く（AGENTS.md ルール9）。

```yaml
schema_version: 2
# ... v1 と同じ（model_id / source / flow_unit / zones / balance / thermal_limits）
thermal_inputs:
  gpu_intake_c: <metric 名 | null>
  case_delta_c: <metric 名 | null>
  cpu_package_c: <metric 名 | null>
  gpu_temperature_c: <metric 名 | null>
```

- 各 metric は Metric Catalog に存在し、単位が `C` であることを起動時に検証する（`fan-policy.yaml`
  の Fallback 入力と同じ規則）
- `null` はその指標を使わない。**stale / missing の値は `None` のまま渡し、0 で埋めない**
  （`ThermalInputs` の既存の規則。欠測そのものは 0029 と Critical Safety が扱う）
- 束縛した metric は **Control Loop の入力契約（`build_input_contract()`）へ加える**。
  Metric Catalog で検証できても、契約に無い metric は `StoreTelemetrySource` が問い合わせず、
  毎 tick `None` になって熱の制約が決して効かない（例: Safety にも `fan-policy.yaml` にも出てこない
  `gpu.0.hotspot`）。規則は `fan-policy.yaml` の入力と同じにする:
  - 派生値（`d.*`）は Catalog の派生定義で材料の metric へ展開し、材料を契約へ加える
  - すでに契約にある metric（Safety の必須入力など）はその重要度・許容遅延のまま使う
  - 新たに加わる metric は **`ADVISORY`**、許容遅延は `_advisory_stale_ms()` と同じく同じ源の
    必須入力に揃える。決められない metric は**起動時に拒否する**（Control Config の不正。§2.2）。
    `ADVISORY` なので、欠測しても Telemetry Health や Safety の判断は変わらず、値が `None` になるだけ
  - 契約は検証済みの設定から数え直す（手書きの表を持たない。`build_input_contract()` の既存の方針）。
    `source.status` にかかわらず加える（無効の間も、校正後に初めて拒否が見つかる事態を作らない）
- 束縛する metric の具体名は本記録で決めない（§5）

### 2.5 `ControlTick` への格納（`estimated_flow` と Air Balance の記録）

`ControlTick` の schema version を **9 → 10** に上げ、次を加える。

**(a) zone ごと: `ZoneRecord.estimated_flow`（欄は既存）**

- 値は、その zone の **applied demand を `air-balance.yaml` の曲線で EFU に写した値**とする。
  単位は EFU で、CFM ではない（`docs/air-balance-model.md`）
- **applied demand** は、Hardware Backend が effective demand に profile の制約
  （`minimum_stable_demand` への引き上げ、未起動 zone の `startup_demand`）を掛けた後の、
  PWM へ写す直前の demand（0.0..1.0）とする。`FanHardwareResult` に `applied_demand` を加えて
  backend が返し（実機 backend も同じ Protocol で返す）、`ZoneRecord` にも `applied_demand`
  （v10 で新設、書き込み結果の無い tick は `None`）として残す。PWM の raw 値から逆算しない
  （raw への量子化と、実機 backend の写像の違いを記録の定義に持ち込まないため）
- 次のときは **`None`**（0 にしない）:
  - Air Balance が無効（§2.2）
  - その tick にその zone の Hardware 書き込み結果が無い、または `write_ok` / `readback_ok` が
    偽（回っていない Fan に風量を書かない）
  - **その tick の** その zone の `FanHardwareResult.fault` が `None` でない（`TACH_STALL` を含む）。
    backend の fault は `_apply()` の後に次 tick の Safety 入力へ回り、Critical Safety は
    `stall_window_ms` が経つまで stall を有効な fault にしない。simulated の stall は
    `write_ok` / `readback_ok` がどちらも真のまま返る。したがって有効な fault だけを見ると、
    stall の最初の tick から窓が満ちるまでの間に風量が記録されてしまう。当該 tick の結果を直接見る
  - その zone の stall が Critical Safety の有効な fault に含まれる（窓が満ちた後、backend の報告が
    途切れても残る間）
- RPM の読み戻しから推定する方式は採らない（§4）。曲線の入力は demand のまま

**(b) tick ごと: `air_balance`（新設、v10 では必須）**

```text
air_balance:
  schema_version: 1
  status: enabled | disabled
  disabled_reason: uncalibrated | null        # disabled のときだけ値を持つ
  demand_basis: applied                        # 何の demand で評価したか（固定値。将来の拡張の余地）
  model_id / source_status / config_sha256     # 検証済みの air-balance.yaml から写す（enabled / disabled とも必須）
  q_front / q_rear / q_top                     # zone ごとの q（EFU）。AirBalanceEstimate と同じ定義
  estimated_intake / estimated_exhaust / balance_ratio   # AirBalanceEstimate と同じ定義
  state: balanced | intake_heavy | exhaust_heavy | thermally_limited | unknown | disabled
  thermal_limited: true | false                # AirBalanceEstimate.thermal_limited と同じ定義。state と独立
  thermal_reasons: [...]
```

`state` の `disabled` は **trace 側の型だけ**が持つ値で、`AirBalanceState`（純粋モデルの出力）には
加えない。モデルは無効のとき呼ばれないので、`disabled` を返す経路を作らない。

**検証の不変条件**（`ControlTick` の読み書きの両方で検証し、破れた記録は拒む）:

| 条件 | `status: enabled` | `status: disabled` |
|---|---|---|
| `disabled_reason` | `null` | `uncalibrated`（非 null） |
| `source_status` | `calibrated` | `uncalibrated` |
| `model_id` / `config_sha256` | 必須 | 必須（どの未校正ファイルで起動していたかを残す） |
| `config_sha256` | `runtime.config.air_balance_sha256` と一致 | 同左 |
| `q_front` / `q_rear` / `q_top` | `EffectiveFlow` または `null`（下記） | すべて `null` |
| `estimated_intake` / `estimated_exhaust` / `balance_ratio` | `AirBalanceEstimate` の不変条件のとおり | すべて `null` |
| `state` | `disabled` 以外 | `disabled` |
| `thermal_limited` | `bool(thermal_reasons)` と一致（下記） | `false` |
| `thermal_reasons` | `AirBalanceEstimate` のとおり（下記。`state: unknown` でも非空になりうる） | `[]`（空の列。`null` にしない） |
| zone の `estimated_flow` | `q_*` と zone ごとに一致（下記） | Front / Rear / Top すべて `None` |

- `enabled` のとき、`q_*` / `estimated_intake` / `estimated_exhaust` / `balance_ratio` / `state` は
  `AirBalanceEstimate` の不変条件（比は3つの q が揃い `q_front > 0` のときだけ、
  `estimated_intake == q_front`、`estimated_exhaust == q_rear + q_top`、など）をそのまま守る
- `ControlTick` の検証で、`q_front` / `q_rear` / `q_top` をそれぞれ Front / Rear / Top の
  `ZoneRecord.estimated_flow` と突き合わせ、**どちらも `None` か、同じ値**でなければ拒む。
  zone ごとに対応させるので、Rear と Top の値の入れ替わりも検出できる
- (a) で `None` の zone は、その `q_*` も `None` にする。1つでも `None` があるときは
  `balance_ratio` を出さず `state: unknown` とする（`enabled` のまま。`disabled` と混ぜない）
- **熱制約は `state` と独立に残す。** `AirBalanceEstimate` と同じく、`thermal_limited == bool(thermal_reasons)`
  を常に守り、比が出せる（`state` が `unknown` 以外の）ときだけ
  `thermal_limited == (state == thermally_limited)` を要求する。`state: unknown` のときは
  `thermal_limited: true` と非空の `thermal_reasons` を許す（既存の `_unknown_estimate()` と同じ）。
  Fan の fault で `q_*` が欠けた tick は、熱の証拠が最も要る tick でもある。風量が不明という理由で
  温度の超過を記録から落とさない
- 熱の理由は風量と無関係に、束縛した `thermal_inputs`（§2.4）から `evaluate()` と同じ規則
  （`_thermal_reasons()`）で求める。`q_*` が欠けて `evaluate()` の結果をそのまま使えない tick も、
  この規則を1箇所の実装から呼び、2つ目の分類を作らない
- `disabled` は上の表の形だけを許す。「推定できなかった」（`enabled` + `unknown`）と
  「使っていなかった」（`disabled`）を、同じ形の記録にしない
- 0060 §2.4 と同じく、**版が中身を表さない記録を作らない**。v10 を名乗る tick は `air_balance` を
  必ず持ち、v9 以前は持たない

**(c) 設定の版と hash: `ControlTickRuntime` を schema version 2 にする**

0033 §2 の「decision trace には4ファイルすべての schema version と SHA-256 を残す」を、
保存される `ControlTick` の中で満たす（起動ログの `trace_metadata()` だけでは、tick の記録から
どの版の設定で回っていたかを言えない）。

- `ControlConfigDigest` に次を加える。runtime v2 ではすべて必須、v1 ではどれも持たない
  - `air_balance_sha256`
  - `control_config_version`（束ねた版。§2.1 の 11）
  - `fan_hardware_schema_version` / `safety_schema_version` / `policy_schema_version` /
    `air_balance_schema_version`（`ConfigSource.schema_version` をそのまま写す）
- 既存の `fan_hardware_sha256` / `safety_sha256` / `policy_sha256` はそのまま。
  4ファイルそれぞれについて、版と SHA-256 の組が1つの digest に揃う
- 値は `ControlConfig.sources` から写し、手で書かない（起動ログの `trace_metadata()` と同じ出どころ）
- `ControlTick` v10 は runtime v2 を要求する

**(d) 保存済みの trace と offline 評価**

- v1〜v9 の trace はそのまま読める（新しい欄は `None`）。移行で書き換えない
- offline 評価（0054）は `estimated_flow` が揃った tick だけで比を数える既存の規則のまま。
  `disabled` の tick は `no_estimated_flow` の理由を `air_balance_disabled` として別に数え、
  「推定できなかった」と「使っていなかった」を分ける
- counterfactual の行に Air Balance の欄を置かない規則（0054 §2.2）は変えない。
  本記録が記録するのは**実際に適用された applied demand の推定だけ**である

### 2.6 昇格の証拠を `air-balance.yaml` と `fan-hardware.yaml` に束縛する

Air Balance を Learned MPC の cost へ渡すと（§2.3）、曲線と目標帯は optimizer の判断を変える。
tick ごとの hash（§2.5 (c)）だけでは、characterization A で集めた証拠が B へ切り替えた後の
昇格を許せてしまう。0057 §2.4 の「束縛済み」に `air-balance.yaml` を加える。

同じ理由で **`fan-hardware.yaml` も昇格の照合に加える**。§2.3 で MPC の balance の項は profile の
`minimum_stable_demand` による引き上げを通した demand で評価するので、profile が変わると optimizer が
見る cost も変わる。いまの `main` では hash は報告の provenance（`fan_hardware_config_sha256`）と
`conditions_sha256` にあるが、`RolloutEvidence` と `_check_evidence()` がいまの設定と比べないため、
profile A で集めた証拠と承認が profile B の下で昇格を通ってしまう。以下の `air-balance.yaml` の規則は、
特記しない限り `fan-hardware.yaml` にも同じく適用する。

- `EvaluationProvenance` に `air_balance_config_sha256`（必須）を加え、評価時の
  `ControlConfig.sources.air_balance.sha256` を写す
- `conditions_sha256` の材料（`_provenance()` の digest）に同じ hash を加える。
  Air Balance の設定だけが違う2つの評価が、同じ条件を名乗らないようにする
- Offline Evaluation の報告の版を上げる。いまの `EVALUATION_REPORT_SCHEMA_VERSION` は 2 なので **3** にする
- `RolloutEvidence` に `air_balance_config_sha256` と `fan_hardware_config_sha256` を加える
  （後者は報告の `provenance.fan_hardware_config_sha256` と、承認時の
  `ControlConfig.sources.fan_hardware.sha256` を写す）
- 昇格の照合（`_check_evidence()`）は、`fan-policy.yaml` / `safety.yaml` と同じく、
  `air-balance.yaml` と `fan-hardware.yaml` のそれぞれについて、
  **承認の証拠・報告の provenance・いま動いている設定の3つが一致しなければ昇格を拒む**
  （`AuthorityEvidenceError`）
- **評価に使った trace が、その hash の設定で記録されたことを確かめる。** 上の3つの一致だけでは、
  評価時に characterization B を読みながら、A で記録された v10 の trace や、hash を持たない
  v1〜v9 の trace を消費した報告が、B を名乗って照合を通ってしまう（評価時の hash を provenance へ
  写すだけでは、trace の出どころを表さない）。そこで:
  - Offline Evaluation は、消費した各 tick の `runtime.config.air_balance_sha256`（§2.5 (c)）を
    評価時の `ControlConfig.sources.air_balance.sha256` と突き合わせ、一致・不一致・欠落
    （runtime v1、すなわち v9 以前の tick）の件数を報告 v3 の provenance に
    `air_balance_trace_binding` として残す（`conditions_sha256` の材料にも入れる）
  - 同じく、消費した各 tick の `runtime.config.fan_hardware_sha256`（runtime v1 から既存）を
    評価時の `ControlConfig.sources.fan_hardware.sha256` と突き合わせ、件数を
    `fan_hardware_trace_binding` として報告 v3 の provenance に残す（`conditions_sha256` の材料にも入れる）。
    runtime v1 の tick も hash を持つので欠落は通常 0 だが、欄が無い tick は欠落として数える
  - **消費した tick が1つ以上あり、すべてが一致したときだけ**、その報告を昇格の証拠に使える。
    不一致または欠落が1件でもあれば、報告は出してよいが昇格の証拠にはしない。
    `_check_evidence()` はこの件数を見て、不一致・欠落が 0 でなければ `AuthorityEvidenceError` で拒む。
    **`air_balance_trace_binding` と `fan_hardware_trace_binding` の両方**がこの条件を満たすことを要する。
    Air Balance の条件が v10 の tick を要求するので、v9 以前の trace は `fan-hardware.yaml` の hash が
    一致しても昇格の証拠にならない
  - 該当する tick を黙って除外して残りで評価する方式は採らない（除外の仕方で結果を選べてしまい、
    除外したことが報告から見えにくくなる）。一致しない trace が混ざった評価は、証拠として丸ごと無効にする
  - 同じ突き合わせを `fan-policy.yaml` / `safety.yaml` の hash にも行うかは本記録では決めない（§5）。
    この2ファイルは `_check_evidence()` による証拠・provenance・いまの設定の3者照合を既に持つ
- `MIN_EVIDENCE_REPORT_SCHEMA_VERSION` を **3** に上げる。v2 以前の報告は Air Balance の設定を
  言えないので、昇格の証拠にしない（「記録の無さは不明であって完全ではない」。0059 §2.5）
- journal に既に残った昇格 event の `RolloutEvidence` は書き換えない。`AuthorityJournal` の版を
  **1 → 2** に上げ、v2 の新しい昇格 event の証拠は `air_balance_config_sha256` と `fan_hardware_config_sha256` を
  必ず持ち、v1 の event は持たないまま読む（過去の記録として読むだけで、新しい昇格の根拠にはならない）
- Air Balance が無効（`uncalibrated`）でもファイルはあり hash も決まるので、無効の間の証拠も
  その未校正ファイルに束縛される。`calibrated` へ差し替えた後は hash が変わり、
  **無効の間に集めた証拠では昇格できない**（Air Balance 無しの挙動の証拠を、有効な構成へ流用しない）

## 3. Consequences

### 良くなること

- 0033 の4ファイル一括採用が実装でき、Air Balance の設定だけが別版になる部分適用が起きない
- #75 の前でも `coldaisle-fand` は通常運転に入れる。未校正の値は制御にも記録にも入らない
- 未校正で無効な期間と、校正済みで有効な期間を trace だけで区別できる
- `estimated_flow` が埋まり、offline 評価の Air Balance 報告と `no_estimated_flow` の gap が
  実データで意味を持つ。trace の比と MPC の cost が同じ曲線・同じ定義から出る
- どの characterization で回っていたかを、tick ごとの `config_sha256` と runtime の digest で追える
- 保存された tick だけで、4ファイルの schema version と SHA-256 が言える（0033 §2）
- 別の characterization、または別の `fan-hardware.yaml` の profile で集めた証拠で authority を昇格できない（§2.6）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 設定ディレクトリに4つ目のファイルが要る。置き忘れると全 zone Max で止まる | 大きな音と `config_invalid` のログで気付ける側に倒す。移行手順を `docs/control-config.md` と実装 PR に書く（2.1） |
| 未校正の間、Air Balance を持つ完成形の経路が動かない | 無効な状態はいまの `main` と同じ挙動で、安全側の層は変わらない。#75 の後に `calibrated` へ変えて再起動する |
| `ControlTick` と `ControlTickRuntime` の版が上がり、`FanHardwareResult` にも `applied_demand` が要る。reader・backend・試験の更新が要る | 既存の版の追加（v8 / v9）と同じ手順。旧版の trace は書き換えない |
| 評価報告 v3・`AuthorityJournal` v2 への更新で、v2 以前の報告は昇格の証拠に使えなくなる | 評価をやり直せば v3 の報告が出る。昇格は証拠を取り直すまで止まる側（Shadow / いまの stage のまま）に倒れ、安全側を弱めない |
| v9 以前の trace と、別の characterization または別の `fan-hardware.yaml` で記録された trace は昇格の証拠にならない（§2.6）。`fan-hardware.yaml` を変えると、それまでの証拠では昇格できない。v10 の trace が溜まるまで昇格できない | 昇格が止まる側に倒れるだけで、安全側は弱めない。v9 以前の trace は評価・分析には引き続き使える |
| MPC の balance の項が `fan-hardware.yaml` の profile にも依存する（§2.3） | 引き上げの規則を1箇所の純粋関数にし、backend と同じ定義を使う。profile は同じ `ControlConfig` から渡し、欠ければ構成時に拒否する |
| applied demand からの推定は、Fan が指令どおり回っていない場合を過大に見積もる | 書き込み失敗・読み戻し不一致・その tick の backend fault（窓が満ちる前の stall を含む）・有効な stall の zone は `None` にする（2.5 (a)）。RPM 基準への切り替えは #75 の結果で判断する（§5） |
| `air-balance.yaml` の熱閾値（状態の分類用）と `safety.yaml` の閾値が2箇所にある | 前者は Safety ではない分類用の値であることを変えない（0033）。Safety の判断は `safety.yaml` だけが持つ |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `air-balance.yaml` を任意にし、**無ければ Air Balance 無しで起動**する | 置き忘れ・誤記が黙って「無効」になる。0033 が防ぐ部分適用と同じ種類の事故を、検出できない形で残す |
| **未校正なら `config_invalid` として全 zone Max** にする | #75 が終わるまで通常運転に入れず、Max の騒音が続く。Air Balance は Safety ではないため、無効にしても安全側の層は弱まらず、Max にする根拠が無い |
| **未校正ならデーモンを起動しない**（BIOS の制御のまま終了） | 同上。さらに、承認済みの hardware・safety・policy での制御まで止める |
| 未校正でも警告付きで有効にする | 0033 §4 で却下済み（警告を見落とすと根拠の無い q と比が requested へ効く） |
| 有効・無効を別のフラグ（`enabled`）で持つ | `status: uncalibrated` と `enabled: true` のような矛盾した組み合わせを作れる。有効の条件は「校正済みであること」だけで足りる |
| `ControlTick` を上げず、`estimated_flow` だけを埋める | どの曲線で推定したか、無効だったのか推定できなかったのかを trace から言えない |
| Air Balance の記録を別テーブルに置く | 1 tick 1 記録の decision trace（0030 / 0060 §2.4）と別に突き合わせが要り、欠けた行の意味が曖昧になる |
| **requested demand** で推定を記録する | Safety の floor が入っておらず、実際に回った風量と食い違う（特に Top の CPU cooling floor） |
| **effective demand** で推定を記録する | backend の `minimum_stable_demand` への引き上げと `startup_demand` の kick が入らず、低 demand の運転で風量を過小に記録する |
| applied demand を **PWM の raw 値から逆算**する | raw への量子化と backend ごとの写像の違いが記録の定義に入る。backend が写像前の値を返せば足りる |
| 無効の tick を `state: unknown` と空でない欄で表す | 「推定できなかった」と「使っていなかった」が同じ形になり、offline 評価が区別できない |
| 保存 tick に設定の版を持たず、起動ログの `trace_metadata()` だけに残す | tick の記録単独ではどの版の設定で回っていたかを言えず、0033 §2 を満たさない |
| `air-balance.yaml` の hash を tick にだけ残し、昇格の証拠に束縛しない | characterization A で集めた証拠で、B に差し替えた後の昇格を許せてしまう |
| 評価時の hash を provenance に写すだけで、消費した trace の hash と突き合わせない | 評価時に B を読めば、A や hash を持たない v9 以前の trace から作った報告が B を名乗って照合を通る |
| 一致しない tick を除外し、残りの tick で昇格の証拠を作る | 除外の仕方で結果を選べ、報告から見えにくい。混ざった評価は証拠として丸ごと無効にする方が安全側 |
| MPC の balance の項を候補の demand のまま評価する | backend が `minimum_stable_demand` へ引き上げた後の実際の比と食い違い、目標帯の外になる plan を選びうる |
| MPC の balance の項に Safety の floor まで写す | Safety の floor は tick ごとの Critical Safety の判断で、MPC の予測区間では決まらない。MPC に Safety の判断を複製しない（AGENTS.md ルール5）。実際の比は trace の applied demand に残る |
| `air_balance` に zone ごとの q を持たず、吸排気の合計だけを zone の記録と照合する | Rear と Top の入れ替わりを検出できない。`AirBalanceEstimate` と形が変わり、既存の不変条件を流用できない |
| `thermal_inputs` の metric を Catalog の検証だけで済ませ、入力契約に加えない | 契約に無い metric は取り込まれず毎 tick `None` になり、熱の制約が黙って効かない |
| **RPM の読み戻し**から推定する | 曲線は demand → Airflow Index で定義されており、RPM → 風量の対応は #75 の実測がまだ無い。MPC の cost と同じ定義で記録できなくなる |
| `thermal_inputs` の metric 名をコードに書く | AGENTS.md ルール9。metric の束縛は測定構成で変わる |
| `AirBalanceConfig` を `config.py` へ移す | 純粋モデルと設定形式が離れ、#75 の成果物を追いにくくなる（0033 §4 と同じ理由）。import の向きは現状で循環しない |

## 5. 未決事項

| 論点 | どこで決めるか |
|---|---|
| **Fallback Controller の requested に `coordinate()` を掛けるか**（Baseline の挙動を変える） | #81 の実装前に別の決定記録と所有者の承認。安全・制御系の変更のため |
| `thermal_inputs` に束縛する具体的な metric（例: `d.case_delta` を使うか） | #75 / #81 の実測後。設定の値として `status` 付きで置く |
| 校正済みのまま Air Balance を止めたいときの手段（`uncalibrated` へ戻すのか、別の手段か） | 必要になった時点で。本記録は別フラグを作らない |
| RPM の読み戻しを使った推定への切り替え | #75 で demand と RPM のどちらが風量をよく説明するかを見てから |
| `basis` と #75 の測定記録の機械的な照合形式（0033 §5 の2点目） | #75 の測定記録の形式が決まった後 |
| 版番号の衝突（`ControlTick` v10・`ControlTickRuntime` v2・束ねた版 11・評価報告 v3・`AuthorityJournal` v2） | 別の記録・実装が先にその番号を使った場合は、実装の時点で次の空いた番号を使う。欄の意味は本記録のとおり |
| offline 評価が、trace の `runtime.config` の `fan-policy.yaml` / `safety.yaml` の hash と評価時の設定の不一致を検出するか | `air-balance.yaml` と `fan-hardware.yaml` の hash は §2.6 で突き合わせを決めた。ほかの2ファイルは未実装のまま、別の記録で |
| MPC の Acoustic の項も hardware の写像を通した demand で評価するか | #94。本記録は balance の項だけを決める（§2.3） |
| Air Balance の推定に要求との対応づけを持たせるか（0052 §5） | 本記録は `demand_basis: applied` の記録だけを決める。MPC 側は #86 |
