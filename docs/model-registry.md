# Control Model Registry

`coldaisle.control.model_registry.ModelRegistry` は、Learned Thermal Model、Confidence
Model、Supervisor Policy、Feature Transform の artifact lifecycle をローカル filesystem
で管理する。モデルを実行する層ではなく、どの immutable bytes を利用可能とみなすかを
決める境界である。

## 安全境界

- Registry は artifact を deserialize・import・execute しない。検証後も返すのは immutable
  `bytes` だけである。
- artifact payloadは8 MiBを上限とし、登録時と読込時の両方で拒否する。読込は
  `open(O_NOFOLLOW | O_NONBLOCK)`した同じfdを`fstat()`してregular fileとsizeを先に確認し、
  preflight時のsize分とgrowth検出用1 byteだけを読む。FIFOで停止せず、読込中の短縮・拡張や
  path差替えから別のbytesを組み立てない。
- 現在受理する形式は、構文とtop-levelの型を非実行で検証できるJSONだけである。pickle、
  joblib、Python objectを含むframework固有checkpointに加え、構造validator未導入のONNX /
  safetensorsも`LOADED`にしない。binary形式は安全なvalidatorと一緒に将来schemaへ追加する。
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
`os.replace()` で原子的に切り替える。registry root・artifact directoryを新規作成した場合も、
配下へ進む前に親directoryをfsyncし、crash後にdirectory entryだけが失われないようにする。
管理操作には `expected_revision` を渡すため、同じ状態を見て行った二重promotionの一方は `ConcurrentUpdateError` になり、後勝ちで判断を上書きしない。
root配下のdirectory・lock・snapshot・artifactは`openat`相当の`dir_fd`と`O_NOFOLLOW`で開き、
symlinkまたは非regular fileを拒否する。artifact IDから組み立てたpathでroot外を読み書きしない。

新しいProductionへのpromotion時、旧Productionは`retired`になる。rollback targetには、旧Production、
それまでのrollback targetの順に、checksumとformatの再検証を通った最初のartifactを残す。どちらも
通らなければtargetは無く（`None`）、promotion自体は続行する。選ばれたtargetはpromotion auditの
`rollback_target`に記録する（決定記録0037）。互換性はここでは判定せず、rollback実行時に検証する。
Rollback前にも旧artifactのchecksumとschema互換性を再検証し、成功時はpointer、lifecycle、
理由・時刻・human approvalの監査eventを同じsnapshotで原子的に更新する。
`HumanApproval` はaction、target artifact ref、checksum、承認対象revisionへ固定し、別artifact・
別操作・更新後snapshotへ再利用できない。snapshot読込時はauditを先頭から再生し、登録、検証、
承認付きpromotion / rollbackを経ずに作られたProduction pointerを拒否する。

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
