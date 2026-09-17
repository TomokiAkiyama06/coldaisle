# 決定記録 0048: Thermal Model v1 artifactと読み取り専用推論境界

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-18
- **Supersedes**: なし
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md) /
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) /
  [`0031-thermal-dataset-contract.md`](0031-thermal-dataset-contract.md) / #83 / #85 / #86 /
  #91 / #104
- **対象 Issue**: #84

## 1. Context

#84は、過去windowとFan actionから複数horizon・複数metricの未来熱状態を予測する
Learned Thermal Modelを求める。#85のConfidence / OODと#86のMPCは同じ推論境界を使い、
#104のRegistryはartifactの互換性・provenance・checksumを検証する必要がある。

一方、実機datasetはまだ無く、window / horizon / metric集合、model familyの優劣、正則化量、
予測誤差やauthorityの許容値は決められない。「未学習baselineより有意」という#84の受入基準も
実データなしには判定できない。そこで本記録は、実測値を固定せずに検証できるデータ境界、
安全なartifact、決定的baseline、読み取り専用推論だけを定める。

## 2. Decision

### 2.1 versioned schema

Thermal Model v1は次の独立した版を持つ。

- feature schema v1: dataset schema version、window幅、sample period、stale閾値、metric順、
  flatten後の列名順
- target schema v1: horizonの昇順とtarget metric順
- inference input / prediction v1
- model artifact v1

featureは各window frame・metricについて `value` / `missing_mask` / `stale_mask` /
`suspect_mask` を順序付きで持ち、Front / Rear / Topの `effective_demand` を明示的に持つ。
missing / stale / suspectな値はtrain集合の同じ列の平均へ置換し、標準化後の0とする。同時に
各mask列を残すため、観測された0やusableな平均値と区別できる。Fanの熱応答に直接作用しない
`requested_demand`、未来target、target mask、絶対時刻、source aliasはfeatureに入れない。
`source_ts_ms`は数値featureにしないが入力metadataとして保ち、各frameより未来でないこと、
metric内で逆行しないこと、ageがstale閾値以上ならstale maskが立つことを推論時にも検証する。
staleは鮮度の独立軸なのでmissing / suspectとの併存を許し、missingとsuspectだけを排他にする。

Dataset v1はaction時点のControlTickだけを持ち、各label時刻までの後続Fan action列を持たない。
そのためv1 baselineのcapabilityは`observational_replay`、authority compatibilityは`SHADOW`
だけとする。anchor actionを任意のcandidate actionへ変えた反実仮想予測や、#86 optimizerの
内部modelとして使えるとは宣言しない。後続action trajectoryを持つDataset版と実測評価が
揃うまで、学習された係数をFan actionの因果効果として解釈しない。

inference inputはaction時刻で終わるwindowとFan actionだけを持つ。推論結果はhorizonごとの
target metric値だけを返し、Demand / PWM / authorityを返さない。したがって、このInterfaceから
Fan Hardware、Reactive Guard、Critical Safetyを呼ぶ経路は作らない。

### 2.2 leakageを防ぐ学習境界

学習関数は、検証済み`ThermalDataset`とpurged chronological `DatasetSplit`を同時に受け取る。
splitの全exampleが元datasetと完全一致し、重複せず、全件がtrain / validation / test / purgedの
いずれか一つに属することを検証する。trainの`label_end_ms`より後にvalidationの
`history_start_ms`、validationの`label_end_ms`より後にtestの`history_start_ms`があることを
検証し、学習・正規化はtrainだけから求める。validation / test / purgedのtargetや統計は学習器へ
渡さない。split全体のbucket名・example ID・期間をhashし、artifact provenanceへ残す。
trainerはcaller指定のDataset aliasを受けない。#83が公開した同じdirectory内のcanonical manifestと
examples JSONLをregular fileとして検証し、directory basenameから公開aliasを導出した
`VerifiedTrainingDatasetArtifact`だけを受ける。学習直前にもmanifest / examples checksumを現在の
Datasetへ照合する。

### 2.3 決定的な軽量baseline

最初の実装はmulti-output ridge linear baselineとする。flatten済みfeatureをtrain集合だけの
平均・scaleで標準化し、horizon / target metricごとに観測済みlabelだけを使って独立にfitする。
headごとに採用された行のfeature / labelを中心化し、係数は明示指定された正の`ridge_lambda`に
よる正則化付き最小二乗、切片は中心化を戻した値とする。
乱数・外部BLAS・追加ML dependencyを使わず、pivot規則を固定したsolverで同じ入力から同じ
artifact bytesを得る。labelが1件も無いoutputは学習を拒否する。

`ridge_lambda`、window / horizon / metric集合に本番既定値を置かない。baselineはInterfaceと
pipelineの検証用であり、実データ評価なしにproduction model、Confidence、OOD、authorityの
根拠として扱わない。

### 2.4 安全なartifactとprovenance

artifactはPydantic strict schemaで検証できるcanonical UTF-8 JSONだけを使う。pickle、joblib、
任意module import、実行可能codeを禁止する。payloadにはfeatureの平均・scaleと、outputごとの
有限な係数・切片・学習件数だけを置く。manifestとpayloadは相互にshapeを検証し、payloadの
canonical SHA-256をmanifestへ記録する。

未信頼artifactによるresource枯渇を避けるため、v1はartifact 8 MiB、window 512 frames、
feature metric 64、feature column 512、target horizon 32、target metric 32、target output 128を
構造上限とし、source runは1,024件までとする。baseline trainerはさらにtrain / dataset全体を
各100,000 examples、dataset / split全体を各1,000,000 feature cells・target cellsまでに制限する。
source referenceとaction / fault metadataにも個別・総数・文字列長の上限を設ける。各outputの
normal equationとsolverを含む保守的な演算量
`outputs * (train_examples * features^2 + features^3)`も50,000,000 work unitsまでとする。
metric名は120文字、dataset / splitの反復を含むtextは各8 MiB、inference mapping keyの反復は
1 MiBまでとする。時刻・horizon・ID用の整数は非負のsigned 64-bit範囲に制限する。
これは本番のwindow / metric / dataset size推奨値ではなく、v1 pure-Python実装の安全境界である。
上限超過は展開・hash・JSON round-trip・ridge行列構築より前に拒否する。

#104との統合に備え、manifestは最低限次を持つ。

- Registryと同じpatternのmodel ID、semantic version、timezone付きRFC 3339作成時刻、model family
- training dataset artifact alias / schema version / manifest・examples checksum /
  公開用source run aliasとsource hash
- purged split checksum、train example件数
- feature / target schema versionと各canonical checksum
- code commit（取得できる場合）、hyperparameter summary
- authority compatibility

このbaselineのauthority compatibilityは常に`SHADOW`だけとする。offline / shadow evaluation
referenceはartifact payloadに埋めず、#104のlifecycle metadataが後から付与する。artifact checksumは
canonical artifact bytesから外側で計算でき、#104のRegistryがproduction pointerとは別に保持する。
loaderは#104の`VerifiedArtifact`と、呼出側が実際に要求したauthority stageを受け取る。
checksumだけでなくidentity、dataset、source runs、作成時刻、schema、commit、family、
hyperparameter、authority tupleをpayloadと照合し、SHADOW以外への暗黙downgradeはしない。
Registry経路の`RidgeThermalModel`とin-memory評価専用の`OfflineRidgeThermalModel`は別型にし、
predictionも`registry_verified` / `offline_unverified`を識別できる。#85 / #86のdeployment境界は
`RegistryThermalModel`だけを受ける。
#84はpromotion / rollback / filesystem publicationを実装しない。
現行#104の`VerifiedArtifact`はpublic constructorでも作成できるため、このlabelをRegistry
lifecycle通過やauthorizationの証明にはしない。#85 / #86に統合する前に#104側で
sealed / opaqueな発行境界とallocation前のartifact size上限を実装する。

### 2.5 読み取り専用推論と失敗時の意味

`ThermalModel` Protocolはmodel identity / capability / feature schema / target schemaの読み取りと、純粋な
`predict(input) -> prediction`だけを公開する。入力schema・metric・時刻間隔・mask・artifact
checksum / shapeが合わない場合は予測値を返さず失敗する。暗黙の列補完・列並べ替え・version
変換はしない。同じartifactと同じinputは同じpredictionを返す。

Replayのeager APIはexample数だけでなく、example × feature、example × output、
example × feature × outputの複合budgetをpredict呼出前に検査する。各exampleのwindow / metric shapeも
model feature schemaへ先に照合し、上限またはshape違反時は部分的なpredictionを作らない。

この失敗をFallbackへ切り替えるのは#85 / #86 / #79の責務であり、Thermal Model自身は
Fallback demandを作らない。uncertaintyは将来追加できるが、v1 baselineは提供しない。

## 3. Consequences

- #83のwindow / action / mask / targetを、必要な数値featureと検証metadataへ決定的に変換できる
- multi-horizon / multi-outputを単一のversioned InterfaceでReplayできる
- targetや評価期間の統計が学習へ混ざる経路をvalidatorで閉じられる
- JSON artifactは人間が検査でき、ロード時の任意code実行を避けられる
- 追加ML dependencyなしでpipelineを検証できるが、大規模dataset向けの速度や精度は保証しない
- v1はobservational Replayだけを契約し、時系列のcandidate action列は#83のdatasetが将来action列を
  持つ版になった時点でschema versionを上げて検討する
- artifactのpromotion / rollback / production pointerは#104に残る
- Registry lifecycleの発行元証明は#104のsealed / opaque artifact値が実装されるまで未達である

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| pickle / joblibでPython objectを保存する | 信頼していないartifactのloadが任意code実行になる |
| validation / testも使って正規化する | 評価期間の分布が学習へ漏れる |
| target欠測を0として学習する | 0℃という観測と欠測を混同する |
| horizon / metricごとに別artifactを作る | version / provenanceの組み合わせが増え、MPCが一貫した予測を得にくい |
| baselineに本番向けwindow / horizon /正則化量を既定する | 実データなしの候補値を確定してしまう |
| modelからrequested demandを直接返す | Thermal ModelとMPCの責務を混ぜ、Safety pipelineを迂回し得る |

## 5. 未決事項

- 実機dataset上でのmodel family、feature transform、ridge lambda、window / horizon / metric集合
- 未学習baselineに対する有意差、workload / 室温帯別誤差、worst-case / underprediction評価
- uncertainty方式とConfidence / OOD閾値（#85）
- 時系列candidate action列を扱うdataset / model schema
- Registryのpublication / promotion / rollback（#104）
- #104 `VerifiedArtifact`のsealed化とartifact全体のallocation前size上限
- production authorityの上限と人間承認（#92 / #104）

本記録はProposedである。このPRのmergeをschema / baseline / inference境界の承認点とし、
実データ評価やproduction authorityの承認とはみなさない。
