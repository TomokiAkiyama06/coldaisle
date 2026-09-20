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
