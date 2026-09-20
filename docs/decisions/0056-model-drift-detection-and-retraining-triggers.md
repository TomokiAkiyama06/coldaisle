# 決定記録 0056: Thermal Model の drift 検知の置き場所と再学習の条件

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-20
- **Supersedes**: なし
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md)、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.4 / §2.5 / §2.9、
  [`0037-model-registry-rollback-target.md`](0037-model-registry-rollback-target.md)、
  [`0048-thermal-model-artifact-and-inference.md`](0048-thermal-model-artifact-and-inference.md)、
  [`0050-model-confidence-ood-and-authority.md`](0050-model-confidence-ood-and-authority.md) §2.2 / §2.3、
  [`0053-control-shadow-mode-and-counterfactual-logging.md`](0053-control-shadow-mode-and-counterfactual-logging.md) §2.3 / §2.4、
  [`0054-offline-evaluation-attribution-and-gates.md`](0054-offline-evaluation-attribution-and-gates.md) §2.3 / §2.6 / §2.7、
  GitHub #85 / #90 / #91 / #92 / #104
- **対象 Issue**: #93

## 1. Context

長期運用では環境・Fan 特性・季節・構成が変わる。#93 は、その変化で Learned Thermal Model が
劣化したことを検知し、再学習の要求へつなげることを求める。

未決だったのは4点である。

1. **drift 検知をどこに置くか。** 0050 §2.2 は既に residual drift を confidence の構成要素として
   持っている。ここへ**2つ目の経路**を作ると、どちらが本物か分からなくなる
2. **何を drift の証拠にしてよいか。** 0053 §2.3 は「掛かっていた action を識別できた区間
   （`scored`）だけが予測誤差である」と決めた。`unidentifiable` の差は制御器の違いとモデル誤差の
   混合であって、drift ではない
3. **証拠が薄いときに何と言うか。** 0054 §2.3 は coverage を一級の出力にし、下限に満たない
   指標を伏せた。drift では「薄い証拠で drift 無しと言わない」ほうが重い
4. **検知から再学習・昇格までの経路。** 自動でオンライン学習し Production を書き換える経路は
   AGENTS.md ルール1〜5 に反する

加えて、#85 / #86 / #90 / #91 のレビューで繰り返し見つかった失敗の型がある。**同じ穴をここでも開けない。**

- 一部だけの要約を、全体の要約として扱う
- 同じ入力を2回渡すと件数が増える（複製で下限を満たせる）
- 識別子を束縛せず、別のモデル・別の推論の結果を「この model の実績」として数える
- 契約に書いた上限とコード側の上限が食い違う（黙って広い側で動く）
- 証拠の時刻に**処理した時刻**（壁時計）を使う

## 2. Decision

### 2.1 drift 検知は runtime と offline に分かれる。**runtime 側は 0050 のまま、足さない**

| 層 | 何をするか | 実装 |
|---|---|---|
| runtime（1 tick） | 直近の照合済み forecast の正規化 residual が `residual_drift_ood_ratio` 以上なら OOD。confidence を下げ、Gate が authority 帯を狭めるか Fallback へ退避する | **既にある**（`ConfidenceComponent.RESIDUAL_DRIFT` / `ResidualDriftMonitor`。0050 §2.2） |
| offline（運用期間） | 保存済みの証拠から、Production artifact に対する drift を判定し、**再学習の要求**を出す | 本記録（`coldaisle.control.drift`） |

- **#93 は runtime に新しい判定を足さない。** 「Drift 時に Authority Gate へ通知する」（#93 受入基準）は
  0050 の経路がそのまま満たす。judgement は worker（`ConfidenceAssessor`）で作られ、`ControllerGate` が
  検証して Fallback / 帯を決める（0050 §2.3）。**足すと二重になり、どちらが authority を決めたのか
  trace から言えなくなる**
- offline の検知器は **confidence にも authority にも触れない。** 出すのは報告と再学習の推奨だけで、
  `Demand` / PWM を表現する型を持たず、`coldaisle.control.hardware` / `safety` / `reactive` /
  `serial` / `subprocess` を import しない（0054 §2.7 と同じ規律。試験で走査する）
- 検知器は**時計を持たない。** 時刻はすべて証拠（照合に使った観測の時刻、照合期限、action 時刻）から
  来る。報告に生成時刻の欄を持たない（0054 §2.7）

### 2.2 証拠は既存の機構から取る。**drift 専用の収集経路を作らない**

| drift 候補（#93） | 使う証拠 | 出どころ |
|---|---|---|
| prediction residual の継続悪化 | `ShadowOutcome`（`status: scored` のみ）の誤差を、Profile の validation residual RMS で正規化 | #90 の export（0053 §2.4） |
| 同一 Demand に対する thermal response 変化 | 同上（予測と実測の差そのもの） | 同上 |
| feature distribution shift / Room Temp 帯の変化 | `ModelConfidenceProfile` の feature range・support cell・欠測の組み合わせと、直近入力の照合 | #83 Dataset と #85 Profile |
| Fan 交換 / sensor 交換 / calibration 変更 | **人が宣言する変更イベント**（`DeclaredChange`） | 運用（2.5） |

- 正規化の基準（出力ごとの residual scale）は **Profile から取る**。drift 設定に写さない（0054 §2.6）
- feature / support / 欠測の判定は **`ConfidenceAssessor` と同じ実装**を呼ぶ
  （`ConfidenceAssessor.coverage()`）。閾値も runtime の `model_confidence`
  （`range_margin` / `min_support_count` / `min_missing_pattern_count`）をそのまま使う。
  **drift 設定へ写さない。** 写すと「runtime は範囲内、offline は範囲外」が起きる
- PWM→RPM curve の drift（#93 の候補の1つ）は **v1 の範囲外**とする。比較の基準になる
  characterization artifact がまだ無く、基準の無い比較は数字だけが出る（5. 未決事項）

### 2.3 数えてよい証拠の規則（**破れると drift が作り話になる**）

- **`status: scored` の outcome だけ**を誤差に使う。`unidentifiable` は coverage に数えるだけで、
  誤差はどこにも出さない（0053 §2.3）
- **全出力が照合できた outcome だけ**を比に数える（0050 §2.2 の forecast 単位と同じ）。
  一部の出力しか照合できなかった `scored` は「採点したが比には入れない」として別に数える。
  出力の数で数えると、多出力の予測1回で最低件数を満たしてしまう
- **「全出力」は束ねた予測の出力の集合から決める。** 照合結果に残っている出力だけを見ると
  （`ShadowOutcome.complete` はそれしか見ない）、**記録から出力を落とすだけで**
  「全出力が揃った forecast」に仕立てられる。予測に無い出力と、同じ出力の二重の照合結果は拒み、
  落ちた出力は**照合できなかった**のと同じに数える
- **証拠の時刻は、記録された照合の規則の中にしか置けない。** 照合に使った観測は action より後で、
  期待時刻から `match_tolerance_ms` 以内でなければならない（0053 §2.3）。外を許すと、
  宣言された変更より前の証拠を後ろへずらして数えさせたり、trend の bucket を並べ替えたりできる
- **識別子で束縛する。** counterfactual の `model_version` / `artifact_sha256` / `model_id` が
  Profile の binding と一致するものだけを数える。別のモデルの区間は `foreign_model` として
  数え、混ぜない。outcome は `inference_id` + `plan_digest` で counterfactual に結び直し、
  **結べなければ受け取らない**（0054 §2.6）
- **同じ証拠を2回数えない。** 同じ `(tick_id, ts_ms)` の行、同じ `(inference_id, plan_digest)` の
  outcome が2度現れたら**入力の誤りとして拒む**。「数えないだけ」にすると、行を複製するだけで
  coverage の下限を満たせる。**入力分布の側も同じ**で、同じ action 時刻の推論入力を2度は数えない
  （1件を並べ直すだけで `minimum_inputs` を満たせてしまう）
- **外から渡された Shadow export は、同じ trace と観測から数え直した結果と1欄ずつ照らす**
  （0054 §2.6 と同じ扱い）。識別子と許容幅だけを見ても `status` / `observed` / `error` / 時刻は
  書き換えられる。一致しなければ受け取らず、一致したら**数え直したほうを使う**
- **宣言した期間の外の証拠を使わない。** 期間を絞って「証拠が無い」はずの区間を見ているのに、
  古い健全な export の行や Dataset の example が判定を埋めてはならない。
  Dataset は checksum だけでなく `ThermalDataset` の検証も通す（manifest ごと差し替えた
  dataset は checksum では閉じない）
- **証拠の時刻は証拠自身から決める。** outcome の時刻は照合に使った観測のうち最も新しい時刻、
  1つも照合できなければ照合期限（最後の期待時刻 + 許容幅）とする（0050 §2.2 と同じ）
- 予測の出力が Profile の target schema に無ければ**例外で閉じる**。binding が一致している以上、
  schema が違うのは記録か Profile のどちらかが壊れている
- **誤差は、記録された予測そのものから数え直す。** 照合結果に書かれた `predicted` / `error` を
  信じると、出力どうしで予測値を入れ替えるだけで degraded を ok にできる。期待時刻も同じ理由で
  予測の値と照合する（別の step の実測を「この出力の当たり」にさせない）

### 2.4 coverage を一級の出力にし、**足りない証拠から「drift 無し」を出さない**

- 報告は signal ごとに `outcomes` / `scored` / `counted` / `unidentifiable`（**理由別**）/
  `identifiable_fraction` / 出力単位の `outputs` / `matched_outputs` / `unmatched`（理由別）/
  `predicted_metrics` / `unscored_metrics` を必ず持つ
- **1度も採点できなかった metric があれば `sufficient` にしない**（0054 §2.3 と同じ理由。
  残りの metric だけで「悪化していない」と言えてしまう）
- 下限（`minimum_scored_outcomes` / `minimum_identifiable_fraction`）に満たなければ
  **比を出さない**（`ratio` を欠測にする）。judgement は `insufficient_evidence` とし、
  **`ok` にしない**（fail closed）
- residual trend（#93 の「residual trend を可視化・保存できる」）は、証拠時刻順に
  **一定件数ずつ**の bucket に切って出す。**bucket ごとに自分の件数だけで判定し、
  足りない bucket は比を持たない。** 隣の bucket から証拠を借りない。
  coverage が足りない signal には trend を付けない
- **構造上限を理由に報告が作れなくなる形にしない**（#90 で見つかった「狭い写し」の型）。
  理由・出どころの内訳は種類の上限まで並べ、**残りは1つに集約して件数を保つ**。
  trend の bucket 数が上限を超えるときは bucket を設定値の整数倍へ粗くする（判定の規則は変えない）。
  metric 名や metric 数の上限は**写し元の契約と同じ値**にし、長い欠測の組み合わせは digest 付きに畳む
- judgement は `ok` / `warning` / `degraded` / `insufficient_evidence` の4つ。報告全体の judgement は
  **`degraded` > `insufficient_evidence` > `warning` > `ok`** の順で決める。
  drift があると言えるなら言い、言えないなら「言えない」と言う。**`ok` は全 signal が
  `ok` のときだけ**

### 2.5 宣言された変更（Fan 交換 / sensor 交換 / 較正変更）は証拠を**切る**

- 変更は `DeclaredChange`（種別・時刻・説明）として人が与える。検知器がハードウェアを覗いて
  推測することはしない
- **最後の変更より前の証拠は数えない。** 変更前の residual と変更後の residual を平均すると、
  変化が薄まって見える。除いた件数は `excluded_by_change` として残す
- 変更が1つでもあれば `recharacterization_required` を立てる。Fan / sensor / 較正が変われば、
  モデルが学習した対応関係そのものが変わるため、residual の証拠が足りているかに依らない
- 除いた結果として証拠が下限を割ったら `insufficient_evidence` になる。**これは正しい答えである**
  （交換直後に「劣化していない」と言う根拠は無い）

### 2.6 再学習は**推奨まで**。Production を暗黙に置き換えない

- 検知器が出すのは `RetrainingRecommendation`（要否・理由・対象 artifact・再 characterization の要否）
  だけである。**Registry も設定も書き換えない**
- `human_approval_required` は**型の上で常に真**にする（`Literal[True]`）。「承認不要の再学習」を
  表現できないようにする
- 再学習した成果物は #104 へ **candidate として登録**する。`register_candidate` は Production
  ポインタを動かさない。昇格は #91 の offline evaluation と #90 の shadow 実績、そして人の承認を
  経る（0028 §2.9 承認点 5）。rollback は #104 の known-good artifact へ戻す（0037）
- 推奨は `verdict` と対応させる。**signal 由来の要求は `degraded` のときだけ**立てる。
  例外は 2.5 の宣言された変更で、証拠が足りていても再 characterization と再学習が要る。
  逆に、`insufficient_evidence` のときに「再学習は不要」とは言わない
  （判定できなかった signal を `inconclusive_signals` に残し、報告の judgement もそのまま残す）

### 2.7 報告は Production artifact に束縛する

- `DriftReport` は対象の `model_id` / `model_version` / `artifact_sha256` / `profile_sha256` を持つ。
  **drift event は必ず model / version と対応付く**（#93 受入基準）
- 報告は canonical JSON（sort_keys・生成時刻なし）で、`sha256()` を持つ。同じ入力からは同じ bytes。
  保存して並べれば residual trend の履歴になる
- provenance に drift 設定の hash、`shadow_export_schema_version`、参照した
  `residual_drift_ood_ratio`（runtime の契約）、**証拠として見た期間**を残す。
  期間が報告に無いと、**違う期間を見た2つの報告が同じ bytes を名乗れる**

### 2.8 設定（`config/drift.yaml`。schema v1）

値はすべて**実測前の暫定値**（`{value, status: provisional}`）として扱い、**既定値をコードに置かない**。

```yaml
schema_version: 1
residual:
  minimum_scored_outcomes: {value: ..., status: provisional}   # **比に数えた** outcome の下限
  minimum_identifiable_fraction: {value: ..., status: provisional}
  warning_ratio: {value: ..., status: provisional}
  degraded_ratio: {value: ..., status: provisional}
  trend_bucket_outcomes: {value: ..., status: provisional}
  minimum_bucket_outcomes: {value: ..., status: provisional}
minimum_inputs: {value: ..., status: provisional}
feature_range: { warning_fraction: {...}, degraded_fraction: {...} }
support:       { warning_fraction: {...}, degraded_fraction: {...} }
missing_pattern: { warning_fraction: {...}, degraded_fraction: {...} }
```

設定の検証で強制する不変条件。

- `warning_ratio < degraded_ratio`、`warning_fraction < degraded_fraction`
- `minimum_bucket_outcomes <= trend_bucket_outcomes`
- **`degraded_ratio <= model_confidence.residual_drift_ood_ratio`**（検知器の生成時に照合する）。
  offline が runtime より鈍いと、runtime が OOD で Fallback へ落ちている最中に offline が
  「問題なし」と言う。写しではなく**照合**であり、drift 設定に runtime の値を置かない
- 範囲・support・欠測の閾値は `fan-policy.yaml` の `model_confidence` をそのまま使う。
  **drift 設定に持たない**

## 3. Consequences

- drift の判定は runtime と offline で**役割が分かれる**。tick の判断は 0050、運用期間の判断は本記録。
  authority を動かせるのは前者だけである
- Shadow の間は多くの outcome が `unidentifiable` になる（0053 §3）。drift の答えは
  しばしば `insufficient_evidence` になる。**これは制限ではなく、記録から言えることの範囲**である
- 交換・較正のたびに証拠が切れるので、直後は必ず `insufficient_evidence` +
  `recharacterization_required` になる
- 判定は決定論的で、合成の証拠だけで試験できる。実機は要らない
- 再学習の自動化は**止まる**。候補の登録までは自動化できるが、Production への反映は人が承認する

| トレードオフ | 緩和策 |
|---|---|
| 証拠の下限が高いと、実際の drift を報告できない期間が長い | 下限は設定（provisional）。#90 / #91 の実績で所有者が調整する。trend の bucket は下限とは別に持つ |
| feature shift の判定が Profile の箱型範囲と格子 support に依存する（0050 §3 と同じ弱点） | residual と併せて見る。判定方式を変えるときは Profile の schema version を上げる |
| 宣言された変更を人が入れ忘れると、交換前後の証拠が混ざる | 混ざった証拠は residual を悪化させる方向に出るので、`degraded` として現れる。「良く見せる」向きには倒れない |
| PWM→RPM curve の drift を v1 で見ない | tach の異常は Critical Safety の tach stall（0034）が決定論的に見る。curve の比較は characterization artifact を足してから（5.） |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| offline の drift 判定結果を confidence へ直接反映する | 0050 の経路と二重になり、authority を動かしたのがどちらか trace から言えなくなる。runtime の証拠は runtime で見る |
| drift 検知に `unidentifiable` の差も使う（件数が稼げる） | 掛かっていたのは別の action。差は制御器の違いとモデル誤差の混合で、モデルの劣化ではない（0053 §2.3） |
| 証拠が足りないときに「drift 無し（ok）」と答える | 薄い証拠で「劣化していない」と言うことになる。`insufficient_evidence` にする（fail closed） |
| 比を出力（horizon × metric）の数で数える | 多出力の予測1回で最低件数を満たす。forecast / outcome 単位にする（0050 §2.2） |
| 一部の出力しか照合できなかった予測も比に入れる | 照合できた出力だけの誤差は、当たりやすい出力に偏る。全出力が揃った outcome だけを数える |
| trend の bucket を、足りない bucket は隣と統合して埋める | 「証拠は薄い区間から、指標は厚い区間から」になる（0054 §2.3 と同じ型） |
| 重複した行・結べない outcome を「数えないだけ」にする | 行を複製するだけで coverage の下限を満たせる。拒む |
| drift を検知したら candidate を自動で Production へ昇格する | AGENTS.md ルール1〜5、0028 §2.9 承認点 5。検証していないモデルが制御に入る |
| drift 設定に `residual_drift_ood_ratio` や `range_margin` を写す | 契約が2箇所になり、offline だけ別の閾値で動く（#85 / #90 / #91 で繰り返し見つかった型） |
| 報告に生成時刻を入れる | 壁時計が入ると同じ入力から同じ bytes が出ない。時刻はすべて証拠から来る（0054 §2.7） |
| 交換イベントを telemetry から自動推定する | 推定の誤りがそのまま「証拠を捨てる / 混ぜる」に直結する。宣言を入力にする |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | `config/drift.yaml` の値の確定（比・割合・下限） | 実データと #90 / #91 の評価の後、**所有者の承認** |
| 2 | PWM→RPM curve / 実効風量の drift（基準となる characterization artifact の形） | #33 / Air Balance characterization の後に別 issue |
| 3 | drift 報告を #104 の artifact / event として保存するか（いまは JSON を書き出すだけ） | #104 |
| 4 | 再学習した candidate の自動生成（いつ・どの dataset で）と、その起動条件 | #92 / 再学習の運用を決める issue |
| 5 | drift の判定を降格（authority stage を下げる）の根拠に使うか | #92 |
| 6 | `DeclaredChange` を運用メモリ（`memory/`）や event 入口（0045）から読む配線 | 別 issue |

**本記録は値を決めない。** 2.8 の設定値は出発点であり、確定には基準となる測定と
**リポジトリ所有者の承認**が要る。安全系・制御系の設計変更は人間レビューを必須とする（AGENTS.md）。
