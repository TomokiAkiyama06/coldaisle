# Control Model Registry

`coldaisle.control.model_registry.ModelRegistry` は、Learned Thermal Model、Confidence
Model、Supervisor Policy、Feature Transform の artifact lifecycle をローカル filesystem
で管理する。モデルを実行する層ではなく、どの immutable bytes を利用可能とみなすかを
決める境界である。

## 安全境界

- Registry は artifact を deserialize・import・execute しない。検証後も返すのは immutable
  `bytes` だけである。
- 読込・確保の上限は `config/model-registry.yaml` にだけ置き、コードに既定値を持たない
  （AGENTS.md ルール9）。`ModelRegistry(root, clock, limits=load_model_registry_limits(Path("config")))`
  のように必ず渡す。
- `registry.json` は `max_snapshot_bytes`（現在4 MiB）を上限とし、artifactと同じくfstatでsizeを
  確認してから読む。さらにPydanticが検証する前に、artifactと同じ1 passの走査で入れ子の深さ
  （`max_snapshot_json_nesting_depth`、現在16）と値の数（`max_snapshot_json_tokens`、現在250,000）を
  検査する。どれかを超えたsnapshotは確保せずに `INVALID_REGISTRY` としてFallbackさせる。
  上限を超えるsnapshotは書き込みも `RegistryCapacityError` で拒否し、自分で読めない状態を作らない。
  登録時は、更新後のsnapshotが上限に収まることをartifactを書く前に確認する。書き込み順は
  artifact → snapshotのまま（途中でcrashしても、残るのは参照されないartifactだけ）とし、
  artifactまたはsnapshotの書き込みに失敗した場合は、lock内でsnapshotを読み直し、そのartifactが
  参照されていないと確認できたときだけ、`O_NOFOLLOW` で開いたdirectory fd内でartifactと
  version directoryをbest-effortで削除する（snapshotを読めなければ削除しない）。
  `os.replace()` の後のdirectory fsyncだけが失敗した場合は `RegistryDurabilityError` を返す。
  このとき内容はすでに置換済みであるため、snapshotなら登録は確定しているとみなし、削除しない。
  正当なsnapshotは約18 byte/token、約52 token/audit eventで、token上限より先にbyte上限
  （約4,000 event）に達する。
- 壊れたsnapshotの検証で、Pydanticが要素ごとにerrorを積み上げないようにする。error objectは
  JSON tokenよりはるかに大きいためである。`RegistrySnapshot` から到達できるsequence / mapping
  fieldはすべて、最初の不正な要素で止める。`audit` / `source_runs` / `authority_compatibility` は
  `FailFast`、`artifacts` / `production` / `hyperparameters` はfail-fastのvalidatorを通すため、
  error数は要素数に比例しない。新しいcontainer fieldを追加すると、model treeを走査するテストが
  fail-fastの確認対象に加えるよう求める。
- artifact payloadは `max_artifact_bytes`（現在8 MiB）を上限とし、登録時と読込時の両方で拒否する。読込は
  `open(O_NOFOLLOW | O_NONBLOCK)`した同じfdを`fstat()`してregular fileとsizeを先に確認し、
  preflight時のsize分とgrowth検出用1 byteだけを読む。FIFOで停止せず、読込中の短縮・拡張や
  path差替えから別のbytesを組み立てない。
- JSON artifactは`json.loads()`でobject graphを作る前に、bytesを1 passで走査する。文字列
  （escapeを含む）内の括弧は構造として数えず、入れ子の深さ（`max_json_nesting_depth`、
  現在32）と値の数（`max_json_tokens`、objectのkey・scalar・containerを含む。現在250,000）を
  上限とする。byte上限内でも細かいcontainerを大量に並べて数百MBのobjectを作らせ、
  MemoryErrorで制御processを落とす経路を塞ぐ。閉じていない文字列は末尾まで1回で読んで拒否し、
  走査を入力長に対して線形に保つ。超えたartifactは登録を拒否し、読込時は
  `INVALID_ARTIFACT_FORMAT`としてFallbackさせる。
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
`previous_artifact` はpointerを動かすpromotion / rollbackのauditだけが持てる。

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
