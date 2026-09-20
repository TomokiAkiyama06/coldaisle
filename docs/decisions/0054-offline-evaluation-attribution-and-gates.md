# 決定記録 0054: Offline Evaluation の帰属規則・coverage の扱い・rollout gate

- **種別**: Decision Record
- **Status**: Proposed（**リポジトリ所有者の承認が要る**。§2 は承認まで確定しない）
- **Date**: 2026-09-20
- **Supersedes**: なし
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md)、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.3 / §2.4 / §2.5、
  [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md)、
  [`0031-thermal-dataset-contract.md`](0031-thermal-dataset-contract.md) §2.2、
  [`0050-model-confidence-ood-and-authority.md`](0050-model-confidence-ood-and-authority.md)、
  [`0052-learned-mpc-optimizer-and-hard-constraints.md`](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.2、
  [`0053-control-shadow-mode-and-counterfactual-logging.md`](0053-control-shadow-mode-and-counterfactual-logging.md) §2.3 / §5、
  GitHub #91 / #92
- **対象 Issue**: #91

## 1. Context

#91 は「同一 Dataset / Replay 条件で Controller 構成を比較する」基盤である。
0053 §5 は **「採点できる区間が少ない場合の扱い」を #91 の論点として送った。**
実装に入る前に閉じておかないと、評価そのものが根拠の無い数字を出す。

未決だったのは4点である。

1. **比較対象（arm）を何で識別するか。** Issue は "Learned MPC" と "Learned MPC + Reactive Guard"
   を別の比較対象として挙げるが、**適用された制御に Guard / Critical Safety を通らない経路は無い**
   （AGENTS.md ルール2、0027 / 0028 §2.4）。そのまま arm にすると、実在しない構成の運転実績を
   比較することになる
2. **実行されなかった提案に、温度などの実測の成果を帰属させてよいか。**
   0053 §2.3 は「別の action が掛かっていた区間の実測から反実仮想を推定すること」を範囲外にした
3. **採点できる区間が少ないときの扱い**（0053 §5 がここへ送った論点）
4. **rollout gate をどう組むか。** 「Safety 違反を他の cost 改善で相殺しない」を、集計の形として
   どう保証するか

加えて、#85 / #86 / #90 のレビューで繰り返し見つかった失敗の型がある。**同じ穴をここでも開けない。**

- 束縛していない識別子で、別の推論の結果を「この提案の実績」として数える
- 証拠の時刻ではなく**処理した時刻**（壁時計）を使う
- 写した契約と**食い違う上限**をコード側に置く
- 検証していない値を「記録されているから」として指標に通す

## 2. Decision

### 2.1 arm は「適用された構成」と「適用されなかった提案」の**2つの名前空間**に分ける

- **factual arm**（適用された構成）: `(applied_controller, supervisor_policy, authority_stage)` を
  **decision trace の証拠から**決める。設定ファイルの宣言ではなく、その tick に実際に記録されていた値を使う
- **counterfactual arm**（適用されなかった提案）: `(counterfactual controller, supervisor_policy, authority_stage)`。
  `ShadowRecord` から取る
- **Reactive Guard と Critical Safety は arm の区別に使わない。** 常に経路にあるからである
- 「MPC 単体 vs MPC + Guard」は arm の差ではなく、**同じ arm の中の `requested` → `effective` の分解**
  として出す。Guard / Safety が requested をどれだけ動かしたか（介入回数・介入した zone・bound_by の内訳）
  を測る。**「Guard の無い運転」を推定しない**
- 2つの名前空間は報告の型でも混ぜない。counterfactual の行に適用実績の欄を作らない

### 2.2 実測の成果は factual arm にしか帰属させない

counterfactual arm の報告に置けるのは、次の3つだけである。

| 置いてよい | 出どころ |
|---|---|
| 要求 demand から**そのまま計算できる**量（変動量・ハンチング・approximate acoustic cost） | `ShadowCounterfactual.requested` |
| optimizer の結果（latency / timeout / error / 評価回数 / cost と baseline cost） | `ShadowCounterfactual.optimizer_status` ほか |
| **`status: scored` な outcome の予測誤差だけ** | `ShadowOutcome`（0053 §2.3） |

温度・threshold margin・ΔT・Air Balance・RPM の欄は counterfactual の型に**置かない**（書けなくする）。
適用されなかった提案の下で温度がどうなったかは、記録からは言えない。

### 2.3 coverage は一級の出力にする（0053 §5 の未決を閉じる）

counterfactual arm ごとに必ず次を出す。

- `outcomes` / `scored` / `unidentifiable`（**理由別**）/ `identifiable_fraction`
- 出力単位の `outputs` / `matched_outputs` / `unmatched`（**理由別**）

規則:

- `identifiable_fraction` と `scored` が設定の下限に満たない arm の予測指標は
  **`insufficient_coverage` として扱い、gate を通さない**（fail closed）。
  「採点できた僅かな区間だけが良かった」を rollout の根拠にしない
- 下限に満たないときは**予測指標を出さない**。coverage と理由の内訳だけを残す。
  少数の区間の平均を、全体の予測精度に見える形で並べない
- `unidentifiable` / `unmatched` は**落とさずに理由別に数える**。「予測が無かった」と
  「採点できなかった」を区別する（0053 §2.4 と同じ理由）

### 2.4 gate は辞書式で、Safety が先に立つ

判定は3段で、**上の段が落ちたら下の段の値では覆らない**。

1. `safety` — **worst-case segment で判定する**（平均で薄めない）
   - 適用された arm: 絶対温度上限を超えた観測数・`EMERGENCY` の tick 数・fault を持つ tick 数・
     最小 threshold margin
   - counterfactual arm: **記録された Critical Safety floor を下回った要求の数**。
     実行されていない提案について記録から言える安全側の指標はこれだけで、温度の実績は存在しない
2. `evidence` — coverage の下限、必要な入力（設定・観測）の有無
3. `cost` — underprediction・optimizer latency / timeout

- 1 が1つでも落ちれば結果は `blocked`。2 / 3 がどれだけ良くても `pass` にならない
- 欠測・未設定・coverage 不足は `blocked`（**`pass` にしない**）。判定できないことを合格にしない
- gate の結果は**助言**である。昇格・降格の判断は #92 と人が行う（0053 §5 と同じ境界）

### 2.5 時系列 split（未来を漏らさない）

- run を境界時刻で**順序付き・重ならない** segment に切る。**最後の segment が `holdout`**、
  それより前は `calibration`。gate は **holdout だけ**で判定する
- segment の指標は、その segment の **`[start, end)` の中の証拠だけ**から決まる
- counterfactual の outcome は、**その証拠がすべて `end_ms` より手前にある**ときだけ
  その segment に入れる（`max(expected_ts_ms) + outcome_match_tolerance_ms < end_ms`）。
  跨ぐものは `purged` として数え、どの segment の指標にも入れない
  （0031 / #84 の `split_temporally` が境界を跨ぐ example を purge するのと同じ扱い）
- **segment の報告は `end_ms` 以降の入力に依存しない。** あとから未来のデータを
  足しても、前の segment の報告の bytes は変わらない（試験で確かめる）
- 持ち越しの適用 demand（区間の直前の tick）は**過去側の証拠**なので使ってよい。
  demand は次の tick まで掛かり続けるためで、これを外すと境界で識別できなくなる（0053 §2.3）

### 2.6 照合と閾値は評価側で作り直さず、**契約から取る**

- `outcome_match_tolerance_ms` / `applied_demand_tolerance` は `fan-policy.yaml` の `shadow`
  から取る。評価用に別の値を持たない
- 既に記録されている `ShadowOutcome.match_tolerance_ms` が評価に使う値と違えば **fail closed**。
  違う幅で照合された結果を、同じ coverage として並べない
- outcome は `inference_id` と `plan_digest` で counterfactual に結び直し、**結べなければ数えない**
  （0052 §2.2 / 0053 §2.2 の識別子をそのまま使う。新しい識別の仕組みを作らない）
- 絶対温度上限は `safety.yaml` の `absolute_temp_ceiling_c` を使う。**評価設定に写さない**
- 派生 ΔT の式は `config/metrics.yaml` の `derived` を引く。**評価設定に写さない**
- 観測の品質規則は `OutcomeObservation.usable`（`Quality.OK` だけ）をそのまま使う

### 2.7 評価は読み取りのみで、時計を持たない

- `coldaisle.control.hardware` / `safety` / `reactive` / `serial` / `subprocess` を import しない
  （AGENTS.md ルール1 / 2 / 6。**試験で走査する**）
- **壁時計を使わない。** 報告に生成時刻の欄を持たない。時刻はすべて証拠から来る
- 同じ入力からは同じ bytes を出す。run ごとに trace と観測の digest を残す
  （`coldaisle.dataset` の `_trace_digest` / `_telemetry_digest` と同じ形）

### 2.8 設定（`config/evaluation.yaml`。schema v1）

値はすべて**実測前の暫定値**（`{value, status: provisional}`）として扱い、**既定値をコードに置かない**。

```yaml
schema_version: 1
temperature_metrics: [...]          # 比較する温度 metric。閾値は safety.yaml から取る
delta_metrics: [...]                # config/metrics.yaml の derived 名
room_temperature_metric: air.room
percentiles: [...]
room_temperature_bands: [...]       # 最後の帯だけ上限なし
hunting: { demand_deadband: {...}, rpm_deadband: {...} }
observation_match_tolerance_ms: {...}  # 観測を tick へ結び付ける許容幅（全 metric 共通）
worst_case_count: <int>
gate: { safety: {...}, evidence: {...}, cost: {...} }  # §2.4 の3段
```

## 3. Consequences

- 「実在しない構成の比較」を型の段階で作れなくなる。Issue の "MPC vs MPC + Guard" は
  **分解**として答える
- Shadow の間は多くの区間が `unidentifiable` になる（0053 §3）。**それが coverage として見える。**
  少ない採点区間を根拠に rollout しようとすると、gate が `blocked` を返す
- `identifiable_fraction` が上がるまで、Learned MPC の予測精度は「まだ言えない」という答えになる。
  これは制限ではなく、観測から言えることの範囲そのものである
- 適用された arm の gate には cost の条件が無い。同じ条件の別の運転が存在しない以上、
  適用実績に対する cost の合否は決められないためで、**決められないものを置かない**
- 適用された Learned MPC の optimizer latency / timeout は、現在の `ControlTick`（v6）には
  残らない（`ControllerProposal` は trace に埋まっていない）。counterfactual として動いた区間から
  しか読めない。**欄を埋め合わせず、理由付きで「無い」と書く**
- 報告に生成時刻が無いため、いつ作ったかは git / ファイル側で管理することになる

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| "Guard 無し MPC" を arm として比較する | 適用経路に存在しない。実績が無いものを推定で埋めることになる（0027 / 0028 §2.4） |
| counterfactual の区間の実測温度を、その提案の成果として並べる | 掛かっていたのは別の action。制御器の違いとモデル誤差が混ざる（0053 §2.3） |
| `unidentifiable` を除いた区間だけで予測精度を出し、そのまま rollout の根拠にする | 採点できた区間は「適用値がたまたま plan と一致した区間」で、母集団が偏っている。coverage を添えずに出さない |
| coverage 不足を「評価対象外」として gate から外す | 判定できないことが合格になる。fail closed にする |
| Safety の指標を他の cost と同じ重みで合成スコアにする | 合成した時点で相殺が起きる。辞書式にする |
| Safety の判定を run 全体の平均で行う | 1回の危険な run が、良い run の数で薄まる。worst-case で判定する |
| 評価設定に絶対温度上限や ΔT の式を写す | `safety.yaml` / `metrics.yaml` と食い違ったときに、評価だけが別の契約で動く（#85 / #90 で見つかった型） |
| 評価用に独自の照合許容幅を持つ | 記録された coverage と意味が変わる。記録済みの `match_tolerance_ms` と照らして fail closed にする |
| outcome を時刻の近さで counterfactual に結び付ける | 同じ tick に複数の候補がありうる。`inference_id` + `plan_digest` で結ぶ |
| 報告に生成時刻を入れる | 壁時計が入ると、同じ入力から同じ bytes が出なくなる。再現性の判定に使えない |
| 未来のデータを含めて全体統計を作り、segment ごとに切り出す | 正規化や percentile を通して未来が漏れる。segment は evidence window の中だけで閉じる |

## 5. 未決事項

- **所有者の承認が要る。** 承認まで本記録は `Proposed` であり、§2 は確定していない
- `config/evaluation.yaml` の実運用値（percentile・帯・deadband・gate の閾値）は**実測後に確定する**。
  いまはすべて provisional で、確定には基準となる測定が要る
- 適用された Learned MPC の optimizer 実績を trace に残すか（`ControlTick` の追加が要る）は
  別 issue とする。いまは counterfactual の区間からだけ読める
- RL Supervisor を含む比較（#89 / #105）は、同じ arm の枠で足せるが、本記録では扱わない
- 昇格 / 降格の運用（#92）はここで決めない。gate は助言である
