# Control Model Registry

`coldaisle.control.model_registry.ModelRegistry` は、Learned Thermal Model、Confidence
Model、Supervisor Policy、Feature Transform の artifact lifecycle をローカル filesystem
で管理する。モデルを実行する層ではなく、どの immutable bytes を利用可能とみなすかを
決める境界である。

## 安全境界

- Registry は artifact を deserialize・import・execute しない。検証後も返すのは immutable
  `bytes` だけである。
- 受理する形式名は JSON / ONNX / safetensors に限定する。pickle、joblib、Python objectを
  含むframework固有checkpointは受理しない。
- JSON artifact は構文とtop-levelの型も検証する。ONNX / safetensors の構造検証と推論は、
  後続のformat固有consumerの責務である。
- artifact checksum、feature schema、target schema、authority compatibility のどれかが
  合わなければ `ArtifactLoadResult.fallback_required` は `True` になる。Registry 自身は
  Fan Demand、PWM、Authority Stageを変更しない。
- Productionが無い、registryが壊れている、artifactが消失・破損している場合もloadは例外で
  制御起動を止めず、#79がFallbackを選べるread-only結果を返す。

## Lifecycleとfilesystem

状態は `candidate → validated → production → retired` の順に進む。Candidateを直接
Productionとして暗黙loadすることはない。`mark_validated()` はoffline evaluation参照を、
`promote()` はshadow evaluation参照と明示的な `HumanApproval` を必須にする。

```text
<root>/
  registry.json
  .registry.lock
  artifacts/<kind>/<model-id>/<semantic-version>/artifact.payload
```

`registry.json` はartifact本体と分離したProduction pointer、全artifactのlifecycle、監査eventを
1つのversioned snapshotとして持つ。更新はfilesystem lock内で一時ファイルをfsyncし、
`os.replace()` で原子的に切り替える。管理操作には `expected_revision` を渡すため、同じ状態を
見て行った二重promotionの一方は `ConcurrentUpdateError` になり、後勝ちで判断を上書きしない。

新しいProductionへのpromotion時、旧Productionは`retired`のknown-good rollback targetになる。
Rollback前にも旧artifactのchecksumとschema互換性を再検証し、成功時はpointer、lifecycle、
理由・時刻・human approvalの監査eventを同じsnapshotで原子的に更新する。

## 後続Issueとの接続

- #79 Fallback Controller: `ArtifactLoadResult.status` と `fallback_required` を消費する。Registryは
  Fallback demandを生成しない。
- #82 Control Logging: `ArtifactLoadResult.trace_metadata()` をdecision traceへ足せる。現在の
  `ControlState.model_version` には `VerifiedArtifact.model_version` を渡せる。Promotion / rollback
  の時刻、理由、approvalは `RegistrySnapshot.audit` から追跡できる。
- #84 / #85 / #89: 各format固有loaderと推論interfaceを実装し、`VerifiedArtifact.payload` だけを
  入力にする。Registry内に任意コード実行経路を追加しない。
- #90 / #91: `load_version()` でcandidate / validated / retiredを含む明示versionを固定できる。
  `load_production()` と混ぜず、評価対象のversionを暗黙に変えない。
- #92 Authority Rollout: `ModelCompatibility.authority_stage` で互換性だけを検査する。モデルの
  promotionはAuthority Stageを変更せず、Authorityの昇格は別のhuman approval boundaryに置く。

この実装には実機、温度計モジュール、シリアルポート、Fan Hardwareへの書き込み経路はない。
