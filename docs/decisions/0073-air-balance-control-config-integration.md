# 決定記録 0073: `air-balance.yaml` の Control Config への統合、未校正時の起動、`estimated_flow` の記録

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-29
- **Supersedes**: [`0033-air-balance-config-boundary.md`](0033-air-balance-config-boundary.md) §2 の
  「`uncalibrated` を runtime controller は起動時に拒否する」の一文のみ（§2.2 で置き換える。0033 の他の節は有効）
- **関連**: [`0033-air-balance-config-boundary.md`](0033-air-balance-config-boundary.md) §2 / §5、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.3 / §2.4 / §2.7、
  [`0026-three-zone-fan-control.md`](0026-three-zone-fan-control.md)、
  [`0052-learned-mpc-optimizer-and-hard-constraints.md`](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.3、
  [`0054-offline-evaluation-attribution-and-gates.md`](0054-offline-evaluation-attribution-and-gates.md)、
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
| `ControlTickRuntime.config`（`ControlConfigDigest`） | 3ファイルの SHA-256 だけ |

このままでは #81 の「制御ループへの接続」と、#74 の「`estimated_flow` を記録する」を
実装できない。さらに、#75 の実測が終わるまで**校正済みの `air-balance.yaml` は存在しない**。
0033 は「`uncalibrated` を runtime controller は起動時に拒否する」と決めた。
これをそのまま実装すると、#75 まで `coldaisle-fand` は通常運転に入れない。
本記録はこの一文を**置き換える**（解釈で両立させない。0033 側に `Superseded by` を追記する）。

本記録はこの3点（統合の版と移行・未校正と不在時の起動・`estimated_flow` の格納）を決める。
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
  model_id / source.status / config_sha256     # AirBalanceMetadata をそのまま写す（enabled のとき）
  q_front / q_rear / q_top                     # zone ごとの q（EFU）。AirBalanceEstimate と同じ定義
  estimated_intake / estimated_exhaust / balance_ratio   # AirBalanceEstimate と同じ定義
  state: balanced | intake_heavy | exhaust_heavy | thermally_limited | unknown
  thermal_reasons: [...]
```

- `enabled` のとき、`q_*` / `estimated_intake` / `estimated_exhaust` / `balance_ratio` / `state` は
  `AirBalanceEstimate` の不変条件（比は3つの q が揃い `q_front > 0` のときだけ、
  `estimated_intake == q_front`、`estimated_exhaust == q_rear + q_top`、など）をそのまま守る
- `ControlTick` の検証で、`q_front` / `q_rear` / `q_top` をそれぞれ Front / Rear / Top の
  `ZoneRecord.estimated_flow` と突き合わせ、**どちらも `None` か、同じ値**でなければ拒む。
  zone ごとに対応させるので、Rear と Top の値の入れ替わりも検出できる
- (a) で `None` の zone は、その `q_*` も `None` にする。1つでも `None` があるときは
  `balance_ratio` を出さず `state: unknown` とする
- `disabled` のときは推定の欄をすべて `null` にし、理由だけを残す
- 0060 §2.4 と同じく、**版が中身を表さない記録を作らない**。v10 を名乗る tick は `air_balance` を
  必ず持ち、v9 以前は持たない

**(c) 設定の hash: `ControlTickRuntime` を schema version 2 にする**

- `ControlConfigDigest` に `air_balance_sha256` を加える。runtime v2 では必須、v1 では持たない
- `ControlTick` v10 は runtime v2 を要求する

**(d) 保存済みの trace と offline 評価**

- v1〜v9 の trace はそのまま読める（新しい欄は `None`）。移行で書き換えない
- offline 評価（0054）は `estimated_flow` が揃った tick だけで比を数える既存の規則のまま。
  `disabled` の tick は `no_estimated_flow` の理由を `air_balance_disabled` として別に数え、
  「推定できなかった」と「使っていなかった」を分ける
- counterfactual の行に Air Balance の欄を置かない規則（0054 §2.2）は変えない。
  本記録が記録するのは**実際に適用された applied demand の推定だけ**である

## 3. Consequences

### 良くなること

- 0033 の4ファイル一括採用が実装でき、Air Balance の設定だけが別版になる部分適用が起きない
- #75 の前でも `coldaisle-fand` は通常運転に入れる。未校正の値は制御にも記録にも入らない
- 未校正で無効な期間と、校正済みで有効な期間を trace だけで区別できる
- `estimated_flow` が埋まり、offline 評価の Air Balance 報告と `no_estimated_flow` の gap が
  実データで意味を持つ。trace の比と MPC の cost が同じ曲線・同じ定義から出る
- どの characterization で回っていたかを、tick ごとの `config_sha256` と runtime の digest で追える

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 設定ディレクトリに4つ目のファイルが要る。置き忘れると全 zone Max で止まる | 大きな音と `config_invalid` のログで気付ける側に倒す。移行手順を `docs/control-config.md` と実装 PR に書く（2.1） |
| 未校正の間、Air Balance を持つ完成形の経路が動かない | 無効な状態はいまの `main` と同じ挙動で、安全側の層は変わらない。#75 の後に `calibrated` へ変えて再起動する |
| `ControlTick` と `ControlTickRuntime` の版が上がり、`FanHardwareResult` にも `applied_demand` が要る。reader・backend・試験の更新が要る | 既存の版の追加（v8 / v9）と同じ手順。旧版の trace は書き換えない |
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
| `ControlTick` の版番号の衝突 | 別の記録が先に v10 を使った場合は、実装の時点で次の番号を使う。欄の意味は本記録のとおり |
| Air Balance の推定に要求との対応づけを持たせるか（0052 §5） | 本記録は `demand_basis: applied` の記録だけを決める。MPC 側は #86 |
