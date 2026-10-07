# Learned Thermal Model v1

Issue #84のうち、実機datasetなしで検証できるartifact・学習・読み取り専用推論の契約を
実装する。設計判断は[決定記録0048](decisions/0048-thermal-model-artifact-and-inference.md)に
記録している。決定記録は`Proposed`であり、実データ評価とproduction利用は未承認である。

## 対応範囲

- Thermal Dataset v1のwindowを、metric / 時刻 / value / missing / stale / suspectの固定順で入力する。
  値の無いsuspect（`inf`等）はmissingとして入力し、`suspect_mask`は値のあるsuspectだけに立てる
- Front / Rear / Topの実際の`effective_demand`をFan action featureにする
- horizon × target metricの全組み合わせを同時に返す
- trainだけで欠測補完・標準化・ridge fitを行い、validation / testの未来情報を使わない
- model / feature / target schema、dataset・split・source run provenance、numeric payloadを
  strictなcanonical JSON artifactに保存する
- artifact全体のexact bytes checksumとmetadataを#104 Model Registryへ渡せる
- 保存済みDataset exampleを同じartifactへ流すReplay推論を行う

window幅、sample period、horizon、metric集合、ridge lambdaに本番既定値はない。Dataset specと
`RidgeTrainingSpec`で必ず指定する。artifact / schema / trainerにはresource枯渇を防ぐ構造上限が
あるが、これはpure-Python v1の安全境界であり本番parameterの推奨値ではない。
trainerはtrainだけでなくdataset / split全体の件数、feature / target cell数、source reference、
action / fault metadataを事前検査し、output数を含むridge演算量も行列構築前に制限する。
artifact payload、source run、runtime window / mappingの各containerもschema上限と入口の事前検査を
持つ。metric名と、共有参照がserialization時に反復される場合を含む総text byte数も制限する。

## 学習境界

```python
from coldaisle.control.model import (
    RidgeTrainingSpec,
    train_ridge_baseline,
    verify_training_dataset_artifact,
)

source = verify_training_dataset_artifact(
    dataset,
    published_manifest_path,
    published_examples_path,
)

artifact = train_ridge_baseline(
    source,
    purged_chronological_split,
    RidgeTrainingSpec(
        model_id="rack-thermal",
        model_version="0.1.0",
        created_at="2026-09-18T10:00:00+09:00",
        ridge_lambda=explicitly_selected_value,
        code_commit="0123456789abcdef",
    ),
)
```

`training_dataset_version`はcallerが文字列で指定せず、検証した#83 artifact directoryの
`dataset-<32 hex>`名から導出する。manifest canonical bytesとexamples JSONL checksumを実体fileに
照合した`VerifiedTrainingDatasetArtifact`だけをtrainerが受け取るため、別datasetのaliasを誤って
記録できない。

trainerはsplitが元dataset全件を重複なく分類し、train → validation → testの完全な観測期間が
重ならないことを再検証する。normalizationと係数はtrainだけから作る。targetがmissing / stale /
suspectなら、そのhorizon / metric headの学習からだけ除く。有効labelが0件のheadはfail closedに
する。

## artifactとRegistry

`canonical_artifact_bytes()`が作るUTF-8 JSON bytesだけを保存する。pickle / joblib、Python class
path、任意import、実行可能codeはartifactに含めない。係数・切片・正規化値はすべて有限値で、
列数・output順・内部checksumをload時に再検証する。

`registry_metadata()`は#104の`ArtifactMetadata`へ次を1対1で移せる値を返す。両branch統合後は
`ArtifactMetadata.model_validate_json(registry_metadata_json_bytes(metadata))`というstrict JSON
bridgeを使う。strict Python mappingではEnum文字列を暗黙変換しない。

- `kind=thermal_model`, `artifact_format=json`
- model ID / semantic version / timezone付き作成時刻
- Dataset artifact alias / source run alias
- `thermal-features-v1` / `thermal-targets-v1`
- exact artifact bytesのSHA-256、model family、ridge lambda、code commit
- `authority_compatibility=(SHADOW,)`

Runtime loaderは#104がchecksum検証して返した`VerifiedArtifact`と、呼出側が実際に要求した
authority stageを直接要求する。metadataとpayloadのどちらかだけを差し替えてもloadせず、LIMITED
以上をSHADOWへ読み替えない。`RidgeThermalModel`はこの経路からしか構築できない。
in-memory artifactを使う評価は別型`OfflineRidgeThermalModel`になり、predictionにも
`offline_unverified`を明示する。#85 / #86のdeployment consumerは`RegistryThermalModel`だけを受ける。
offline / shadow evaluation referenceとpromotion / rollbackは#104が管理し、payloadへ埋め込まない。

**更新（#86 / 決定記録 0052 §2.1）**: #104に`ArtifactAttestation`を追加し、`VerifiedArtifact`は
これを必須項目として持つようになった。`ArtifactAttestation`はpublic constructorを持たず
`ModelRegistry`の検証経路だけが発行するため、`VerifiedArtifact`はRegistryの外では組み立てられない。
上記のsealed / opaqueな発行境界はこれで満たす。ただし暗号的な保証ではなく、同一プロセス内の
悪意ある偽造は防げない（決定記録 0050 §3）。狙いは、検証していないartifactを取り違えて制御経路へ
渡す配線の誤りを型で止めることである。artifact読み込みの8 MiB上限をallocation前に適用することは
引き続きblockerとして残る。

**更新（#86 / 決定記録 0052 §2.1）**: `ThermalRegistryMetadata` に `capability` を足し、manifestの
capabilityをそのまま#104へ申告する（Registry schema version 2）。#86 はRegistryが発行した
attestationのcapabilityだけを見て内部モデルの可否を決める。v1 artifactのcapabilityは
`observational_replay`固定なので、**登録されるcapabilityも必ずそれになり、#86 は常に拒否する**。
反実仮想artifactを定義するときに満たすべき条件は決定記録 0052 §2.1 に列挙した。

（元の記述）現行#104の`VerifiedArtifact`はpublic constructorを持つため、`registry_verified`は
「そのnominal typeとchecksum / metadata / payloadの整合を#84が再検証した」ことだけを表す。
Registry lifecycle、promotion、production authorizationの証明には使わない。

## 読み取り専用推論

`ThermalModel.predict()`は`ObservedThermalInput`を受け、未来温度の`ThermalPrediction`だけを
返す。Demand、PWM、ControllerProposalは返さず、Fan HardwareやSafety pipelineへの参照も持たない。
uncertaintyはv1 ridge baselineでは`None`であり、Confidenceを捏造しない。

同じartifact bytesと同じ観測入力の結果は決定的である。入力のwindow幅、period、stale閾値、
metric集合、source時刻、mask、schemaがartifactと一致しなければ予測せず失敗する。nested dictの
構築後変更や`model_copy`によるvalidator迂回も、predict入口の再検証で拒否する。#85 / #86が
この失敗をFallbackへ接続する。

`replay_predictions()`は全結果を返す前にexample × feature × outputの演算量と、feature / output
総cell数を検査し、shape不一致もpredict呼出前に拒否する。これは本番batch sizeではなく
eager Replay APIのresource安全境界である。

## 重要な制約と残作業

Dataset v1はanchor ControlTickのactionしか持たず、各targetまでの後続Fan action trajectoryを
持たない。従ってv1 artifactのcapabilityは`observational_replay`、authorityはSHADOWだけである。
actionを候補値へ変えたcounterfactual予測や#86 MPC optimizerの内部modelとして使ってはならない。
これには、将来のeffective action trajectoryまたはaction holdを証明するDataset schemaの追加が要る。

また、#84の最低予測対象にある`d.case_delta`は現行`config/metrics.yaml`に未登録で、Dataset v1は
保存済みmetricから派生targetを作らない。`air.rear_exhaust`と`air.front_intake`の品質・時刻を保った
versioned derived-target契約が必要である。

実機到着後に最低限、次を完了するまでIssue #84をcloseしない。

- GPU core / CPU package / GPU Intake / case ΔTを含む実datasetの収集とtarget接続
- untrained persistence baselineとの統計的比較
- MAE / RMSEに加えworst-case / underpredictionの評価
- workload regime別・室温帯別評価
- window / horizon / feature・target / ridge lambdaとmodel familyの比較
- action trajectoryを含むcounterfactual契約、Confidence / OOD、MPC、Registry shadow評価

## 反実仮想 artifact v2（決定記録 0079 段 2 / 0084）

Thermal Dataset v2（#83 / 決定記録 0087）の action 列を使い、候補の Fan action 列に対する予測を
申告する artifact v2（`coldaisle.thermal_model` v2、capability `counterfactual_action`）を足した。
v1 artifact は変えず、v1 を v2 として読み替えない。

- **形式**: feature schema `thermal-features-v2`（v1 の列 + 計画 action の列 `plan[k].<zone>`）・
  target schema `thermal-targets-v1`（`dataset_schema_version` 2）・action schema `thermal-actions-v1`
  （`step_ms`・step 数・zone の順・単位・`action_source = effective_demand`）。manifest は学習データの
  時間窓・action の変化の件数・`metric_binding`（単位と派生値の定義。表示名は含めない）・
  `calibration_binding`・`anchor_action_rule = hold_effective`・`payload_sha256`・
  `confidence_profile_sha256` を持つ
- **trainer**（`train_counterfactual_ridge`）: train だけから、記録した action 列で当てはめる。
  horizon `h` の head は `k × step_ms < h` の step の列だけで当てはめ、それ以外の係数を厳密に 0 にする
  （因果の mask）。window・horizon・格子・ridge lambda・authority の互換・較正の digest に既定値は無い
- **Profile v2 の同梱**: trainer の結果（`CounterfactualTrainedModel`）は Profile v2 を含まない。
  `assemble_counterfactual_artifact(trained, profile)` が両者を1つの artifact に封じ、読み込み時と同じ
  検査を作成時にも行う。Profile v2 を train / validation から作る処理は段 3（#85。次の節）
- **読み込み**（`RegistryCounterfactualThermalModel.from_verified_artifact`）: Registry が発行した
  `VerifiedArtifact` だけを受け取り、L1〜L12 を順に検査する。外れたら
  `CounterfactualArtifactRejectedError`（`check` に番号）。runtime の `MetricCatalog` と較正の digest は
  呼び出し側が明示する
- **anchor 推論**（`predict`）: 計画 action の列は `hold_effective`（いま掛かっている effective demand を
  全 step で保つ）。`predict_trajectory` は action schema の格子と完全に一致する列だけを予測する

MPC への束縛（`MpcModelBinding`・`PlanPrediction`・§2.5 の探索範囲の写し）は段 4（#86。次の次の節）、
runtime contract の例と `docs/model-registry.md` の更新は段 5（#104）で行う。

## Confidence Profile v2 と判定器（決定記録 0079 段 3 / 0084）

`control/model/counterfactual_confidence.py`。制御経路（Gate / Guard / Safety）・`ControlTick`・
Registry の schema・`fan-policy.yaml` は変えない。新しいしきい値・設定値は足していない。

- **生成**（`fit_confidence_profile_v2(trained, source, split, spec)`）: 段 2 の学習結果と、その学習に
  使った Dataset v2・split（checksum と train の件数を照合）から作る。範囲・欠測・support cell・
  action 列の範囲は train だけ（anchor action は `prior_action`、action 列は記録した列）。action 列の
  範囲は step ごと（demand の範囲と全 zone の cell (a)）・anchor → step 0（変化量と cell の組 (b)）・
  step の組 `(k, k + 1)` ごと（変化量と cell の組 (c)）に持つ。cell は `spec` の `fan.<zone>` 軸の境界で
  分け、全 zone の軸が無ければ作らない。cell の数の上限（0050 §3）は集合ごとに当て、超えれば作らない
- **residual の基準**: validation の各 example の anchor 推論（`hold_effective`）から作る。held の列が
  step ごとの support の外にある example は基準から除き、件数を `residual_excluded_example_count` に
  残す。ある出力で残りが0件なら Profile を作らない（artifact も作れない）
- **同梱**: `seal_counterfactual_artifact(trained, source, split, spec)` が生成と
  `assemble_counterfactual_artifact` をまとめて行う
- **step ごとの support の照合**（`StepSupportChecker`）: action 列を、観測した値・cell・組の外を通さず
  照らす（margin も件数の下限も掛けない）。外れた最初の点を `StepSupportViolation`（step の番号・
  zone・cell）で返す。held の列の照合・residual の基準の除外・段 4 の候補 plan の照合が同じ関数を使う
- **判定器**（`CounterfactualConfidenceAssessor.for_model(model, policy)`）: 封をした型
  `RegistryCounterfactualThermalModel` の同梱 Profile からだけ作る（別の Profile を渡す引数は無い）。
  判定の規則は v1 と同じ実装を共有し、予測の model ID・版・`artifact_sha256` を封をした型の束縛と
  照らす。anchor 推論の held の列が step ごとの support の外なら `support` を OOD（confidence 0）に
  する。OOD の判定を受けた提案は既存の Gate が Fallback にする
- **residual の照合**（`counterfactual_residual_monitor(model, policy)`）: 同梱 Profile の基準で数える。
  判定器と同じ Profile の証拠だけが受け付けられる
- **offline 評価**: `evaluate_ood_detection` は v2 の封をした型と判定器も受け取る（予測は anchor 推論）

候補 plan の照合（`plan_out_of_learned_range`）は段 4（#86。次の節）、episode での
step の OOD の記録は段 6（#105）で行う。

## Learned MPC への束縛（決定記録 0079 段 4 / 0084 / 0087 §2.5）

`control/mpc/`。Gate / Guard / Safety・`ControlTick`・`fan-policy.yaml` の版は変えない。新しいしきい値・
設定値は足していない。MPC worker のプロセス（決定記録 0077 段階 3）はまだ無いので、ここは worker の外で
試験できる境界までである。

- **束縛**（`MpcModelBinding.from_verified_artifact(verified, metric_catalog=, calibration=,
  authority_stage=, expected_model_version=)`）: Registry が発行した `VerifiedArtifact` を1つ受け取り、
  L1〜L12 を通った封をした型だけを束縛する（model と同梱 Profile は同じ bytes から）。外れたら
  `MpcModelUnusableError`（理由に検査の番号）。production pointer・authority の互換・版の照合は従来どおり。
  別に組み立てた model / Profile を受け取る `for_control` は廃した
- **候補 plan の予測**（`predict_plan`）: plan の格子（`step_ms`・step 数）が action schema と完全に一致
  するときだけ予測し、`PlanPrediction`（plan の識別子と anchor 推論に束ねる）を返す。`ActionPlan.steps[k]`
  は Dataset v2 / artifact v2 の step `k` と同じ区間（0087 §2.5）。`mpc.optimizer` の格子が合わない設定は
  optimizer の生成時に拒む
- **探索範囲の写し**（`plan_support_violation`）: 候補 plan を同梱 Profile v2 の `StepSupportChecker` で
  照らし、外れた候補は評価しない（評価回数にも数えない。margin なし）。出発点（制約へ収めた Fallback の
  requested）が外れれば、optimizer は `error`（`plan_out_of_learned_range`、detail に外れた step・zone・
  cell）を返し、Gate は `optimizer_error` として Fallback を選ぶ。範囲内へ丸めて探索を続けない
- **判定器**: `LearnedMpcController` は判定器を受け取らず、束縛した封をした型と runtime の
  `model_confidence` から `CounterfactualConfidenceAssessor` を作る
- **読み込みの失敗**（`LearnedMpcRuntime.load`）: 検査・較正・格子・authority・版のどれで外れても
  起動を止めず、以後の提案を `MODEL_LOAD_FAILURE`（`failure_reason` 付き）にする。Gate は Fallback を選ぶ
- **runtime の較正**: `coldaisle-fand --calibration <path>` が起動時に1回だけ読み、`RuntimeCalibration`
  （読めなければ `unavailable` と構造化ログ）を作る。既定の path は置かない。worker の loader へ渡す経路は
  worker の実装と合わせて決める（Issue #86）
