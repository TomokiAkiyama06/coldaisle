# 決定記録 0150: 学習の CLI `coldaisle-train`（Thermal Dataset v2 から反実仮想 Thermal Model artifact v2 を作り、Model Registry へ candidate として登録する入口）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし（既存の決定が「学習の CLI の PR で行う」と先送りした配線を、どの形で行うかを決める。
  [0102](0102-calibration-change-log-settled-points.md) §2.3・[0112](0112-training-replay-path-required.md) §2.1 / §2.2 の 3・
  [0099](0099-calibration-change-log.md) §2.6・[0096](0096-calibration-binding-digest.md) §5 #4 の決定は置き換えない。
  [0031](0031-thermal-dataset-contract.md) §3 の Dataset の writer の公開（共通の `os.rename`）も置き換えない。§5 の 9）
- **関連**: [0031](0031-thermal-dataset-contract.md) §2.5 / [0048](0048-thermal-model-artifact-and-inference.md) /
  [0050](0050-model-confidence-ood-and-authority.md) / [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.1 /
  [0054](0054-offline-evaluation-attribution-and-gates.md) / [0056](0056-model-drift-detection-and-retraining-triggers.md) §2.5 / §2.6 /
  [0058](0058-rl-supervisor-training-environment.md) §2.6 / §3 / [0061](0061-rl-supervisor-policy-artifact-and-binding.md) /
  [0062](0062-model-registry-operations.md) §2.1〜§2.3 / [0077](0077-learned-proposal-handoff.md) §2.6 /
  [0079](0079-model-artifact-formats.md) §2.1 / §2.3 / §2.4 / §2.9 / [0084](0084-model-artifact-anchor-support.md) /
  [0086](0086-authority-approver-uid.md) / [0087](0087-dataset-v2-action-grid.md) / [0094](0094-dataset-v2-settled-points.md) /
  [0096](0096-calibration-binding-digest.md) §2.4 / §2.6 / §5 #4 / [0098](0098-registry-binding-settled-points.md) /
  [0099](0099-calibration-change-log.md) §2.6 / §2.7 / [0100](0100-replay-export-binding.md) §2.8 /
  [0102](0102-calibration-change-log-settled-points.md) §2.3 / [0104](0104-authority-raise-least-privilege.md) §2.1 / §2.3 /
  [0105](0105-registry-shared-root-settled-points.md) / [0109](0109-dataset-declared-changes-cli.md) /
  [0112](0112-training-replay-path-required.md) / [0116](0116-dataset-cli-copies-production-traces.md) /
  `docs/thermal-dataset.md` / `docs/thermal-model.md` / `docs/model-registry.md` / `docs/requirements.md` Q-21 / Q-23 / Q-25
- **対象 Issue**: #84（関連: #85 / #86 / #89 / #104）

## 1. Context

学習に要る部品は関数としてはそろっている。

| 段 | 既にあるもの | 決定記録 |
|---|---|---|
| Dataset v2 の公開物への束縛 | `control/model/counterfactual_training.verify_training_dataset_artifact_v2` | 0031 / 0087 |
| 元の入力・本番の DB との照合と作り直し | `training_entry.verify_training_dataset_v2` | 0100 §2.8 / 0112 / 0116 |
| 較正の記録との照合 | `calibration_log.verify_training_calibration` | 0099 §2.6 / 0096 §5 #4 |
| split | `control/model/dataset.split_temporally_v2` | 0031 §2.5 |
| 学習（ridge・計画 action の列つき） | `counterfactual_training.train_counterfactual_ridge` | 0079 段 2 / 0084 / 0096 §2.6 |
| 同梱 Profile v2 | `counterfactual_confidence.fit_confidence_profile_v2` / `seal_counterfactual_artifact` | 0079 段 3 / 0084 |
| 登録する bytes と metadata | `counterfactual.canonical_counterfactual_artifact_bytes` / `counterfactual_registry_metadata` | 0079 §2.3 |
| candidate の登録 | `ModelRegistry.register_candidate`（`coldaisle-registry register`） | 0062 / 0104 / 0105 |

ところがこれらを1回の実行で順に呼ぶ入口が無い。0102 §2.3・0112 §2.1 は、検査の関数の必須の引数を CLI へ配線するのを
「学習の CLI の PR」へ先送りしたままで、`training_entry` の docstring と `docs/thermal-dataset.md` も「学習の CLI は
まだ無い」と書いている。そのため実データから反実仮想 artifact を作れず、Registry に `counterfactual_action` の
candidate が1つも無い。#86（Learned MPC）は束縛できる内部モデルが無く、#89（RL Supervisor）は 0058 §3 のとおり
「action が効いていない条件」でしか回せない。

AGENTS.md の「仕様に無い判断をした場合、実装より先に決定記録を残して人間の承認を得る」に従い、CLI の形を先に決める。
**本記録では実装しない。**

## 2. Decision

既に決まっていること（artifact の形・検証の順・Registry の lifecycle・昇格の承認）は**参照するだけで変えない**。
本記録が決めるのは、それらを束ねる入口の形と、その入口に持たせないものである。

### 2.1 入口と置き場所

- 合成の起点 `src/coldaisle/train.py`、console script `coldaisle-train`（`pyproject.toml`）
- **どこからも import しない**（`authority_cli.py` と同じ扱い）。とくに `control_daemon`・`learned_worker`・
  `learned_channel`・`control/`・`api/`・`ai/`・`server`・`event_entry`・`control_admin` から import しない
- **import の約束は、モジュール単位の許可の一覧と禁止の一覧の2つ**とする（関数・メソッドの単位では並べない。漏れが続くため）。
  対象は `train.py` と `training_output.py` が**直接** import するもの（AST で走査する。§2.9）

  **許可の一覧**（`coldaisle` の中。§2 の手順と実在のコードを突き合わせて決めた）

  | module | 根拠（§2 の手順 → 実在のコード） |
  |---|---|
  | `coldaisle.training_entry` | §2.4 の 2: `verify_training_dataset_v2`・`TrainingDatasetRefused` |
  | `coldaisle.calibration_log` | §2.4 の 3: `verify_training_calibration` |
  | `coldaisle.declared_changes` | §2.3 / §2.7: `parse_declared_changes`（実装の PR で足す bytes の API。同じ bytes の `file_sha256` を返す）・`DeclaredChangesError` |
  | `coldaisle.control.drift.model` | §2.3: `parse_declared_changes` が返す `DeclaredChange` の型（`training_entry` が受け取る型。`control/drift` の中でこれだけ） |
  | `coldaisle.metrics` | §2.3 / §2.4 の 5・7 / §2.7: `MetricCatalog.from_yaml_bytes_with_sha256`（実装の PR で足す bytes の API） |
  | `coldaisle.store`（読むだけの API） | §2.3 / §2.7: `QualityRules`（`model_validate`）。DB は `training_entry` が読み取り専用で開く |
  | `coldaisle.ingest.calibration` | §2.3 / §2.7: `Calibration.from_json_bytes`（`ingest` の中でこれだけ。取り込みの Source は使わない） |
  | `coldaisle.control.model.*` | §2.4 の 1・4〜7 / §2.7: `counterfactual_training`（`load_training_dataset_artifact_v2`（実装の PR で足す。§2.4 の 1）・`train_counterfactual_ridge`・`CounterfactualTrainingSpec`）・`counterfactual_confidence`（`seal_counterfactual_artifact`）・`counterfactual`（`CounterfactualThermalModelArtifact.model_validate_json`・`canonical_counterfactual_artifact_bytes`・`counterfactual_registry_metadata`・`counterfactual_registry_metadata_json_bytes`・`check_artifact_contents`・`RegistryCounterfactualThermalModel.from_verified_artifact`）・`calibration_digest`（`RuntimeCalibration`。読んだ `Calibration` から作る）・`confidence`（`ConfidenceProfileSpec`・`SupportAxis`）・`dataset`（`split_temporally_v2`）・`training`（`split_sha256`） |
  | `coldaisle.control.schema` | §2.2: `AuthorityStage`（`authority_compatibility` を `(shadow,)` に固定する型。値の型だけ） |
  | `coldaisle.control.model_registry` | §2.5: `ModelRegistry`（`register_candidate`・`load_version`）・`ModelRegistryLimits`・`ArtifactMetadata`・`ModelRegistryError` とその派生 |
  | `coldaisle.training_output` | §2.5: 公開の手順・path の照合・印を確かめる関数 |
  | `coldaisle.dataset` | §2.5 の手順 1: `_open_output_root` を公開にした関数 |
  | `coldaisle.logs` | 構造化ログ |

  `training_output.py` が import してよい `coldaisle` の module は無い（§2.5）。`coldaisle` の外は標準ライブラリ（下の禁止を除く）・
  `pydantic`・`yaml` だけ。

  **禁止の一覧**（直接 import したら試験が落ちる）: `coldaisle.control.loop`・`coldaisle.control.operating_mode`・
  `coldaisle.control.authority`・`coldaisle.control.hardware`・`coldaisle.control.safety`・`coldaisle.control.reactive`・
  `coldaisle.control.fallback`・`coldaisle.control.mpc`・`coldaisle.control.supervisor`・`coldaisle.control.rl`・
  `coldaisle.control_daemon`・`coldaisle.learned_worker`・`coldaisle.learned_channel`・`coldaisle.control_admin`・
  `coldaisle.authority_cli`・`coldaisle.event_entry`・`coldaisle.registry`（別の合成の起点）・`coldaisle.ai`・`coldaisle.api`・
  `coldaisle.server`・`coldaisle.notify`・`serial`・`subprocess`・`ctypes`・`socket`。`coldaisle.control`（パッケージそのもの）も
  禁止し、`coldaisle.control.<module>` の形で許可の一覧のものだけを import する（`coldaisle/control/__init__.py` は `loop`・
  `hardware` などを再 export しているため）

  **推移的な import は約束しない。** 実在のコードでは、許可した module を import するだけで禁止の module が読み込まれる:
  `coldaisle.control.<module>` を読むと親の `coldaisle/control/__init__.py` が `loop`・`hardware`・`safety`・`mpc` などを
  import し、`training_entry` は `daemon` を通じて `ai`・`notify` を import する（`training_entry` 自身が再生のために
  `Daemon` を使う。0112 §2.3）。これは既存の構成で、本記録では変えない。約束は「`train.py` から禁止の module の機能を
  **呼ぶ経路を書かない**」ことで、それを直接の import の走査で確かめる。LLM に依らないこと・Fan へ書かないことは、
  `train.py` が `ai` / `notify` / `hardware` を呼ばないこと（直接の import が無ければ名前で呼べない）と、`training_entry` の
  再生が通知・説明を渡さないこと（`verify_training_dataset_v2` の `Daemon(...)` は `source`・`store`・`normalizer`・`source_name`・
  `dataset_run_alias` だけで作り、Notifier も Explainer も渡さない）で保つ
- **LLM に依らない。** AI 層のツールにも読み取り API にも出さない（AGENTS.md ルール1。0062 §2.1 と同じ）
- 0062 の `coldaisle-registry` には `register --training-output` を1つ足す（§2.5。既存の操作と引数は変えない）
- **人が手で実行する。** systemd の timer やデーモンからは起動しない（本番のオンライン学習はしない。Q-25 は開いたまま
  §5 の 7）

### 2.2 最初の版で学習するもの

- **反実仮想 Thermal Model artifact v2**（`ridge_linear_v1`・`thermal-features-v2`・capability
  `counterfactual_action`）と、その**同梱 Confidence Profile v2**（0079 §2.1）。1つの `thermal_model` artifact として作る。
  `confidence_model` kind は使わない（0079 §2.1）
- v1 artifact（`train_ridge_baseline`。観測の再生だけ）は作らない。#86 が束縛できないため
- **RL Supervisor policy は含めない**（§5 の 6）。`control/rl/training.py` の探索は、反実仮想 artifact が production に
  無い間は全 arm の requested が同一になり `promotable=False` しか出ない（0058 §3 / `rl/training.py` の docstring）。
  thermal の candidate が人の承認で production（`shadow`）になってから、RL の入口を別の記録で決める
- `authority_compatibility` は **`(shadow,)` 固定**とし、引数で変えられない。それより上の stage は実測評価で裏づけてから
  （0079 §2.3）、新しい版の artifact として作る。その経路は本記録では作らない

### 2.3 入力（すべて必須・既定値を置かない）

| 引数 | 中身 | 根拠 |
|---|---|---|
| `--dataset <dir>` | `coldaisle-dataset --dataset-version 2` の公開物（`dataset-<32 hex>/` の manifest と `examples.jsonl`） | 0031 / 0087 |
| `--replay-path <dir>` | dataset を作った元の日次 CSV と export の manifest | 0112 §2.1 |
| `--calibration-history-db <path>` | 本番の DB（読み取り専用で開く。較正の記録・`csv_exports`・trace） | 0099 §2.6 / 0116 |
| `--declared-changes <path>` | dataset を作ったときと同じ宣言の YAML（空も `changes: []` で明示） | 0109 |
| `--quality-config <path>` | 再生と builder が使う品質の設定 | 0112 §2.3 |
| `--calibration <path>` | 学習データの期間に効いていた較正ファイル（`calibration.json`） | 0096 §2.4 |
| `--metrics-config <path>` | `config/metrics.yaml`（`metric_binding` の単位） | 0079 §2.3 |
| `--training-spec <path>` | 学習の選択の YAML（下記） | AGENTS.md ルール9 |
| `--model-id` / `--model-version` | artifact の identity。同じ版の再登録は Registry が拒否する | 0062 |
| `--created-at <RFC 3339>` | artifact の `created_at`（壁時計を読まない。§2.7） | 0079 §2.3 |
| `--out <dir>` | 出力先のディレクトリ。**まだ無いこと**（§2.5 の公開の手順で作る） | — |

`--training-spec` の YAML（schema v1）は次を持つ。どれも**本番の既定値を置かない**（0048 §2.3 / 0079 §2.3 と同じ）。
リポジトリには仮の値の例（`config/training.example.yaml`）だけを置く（§5 の 3）。

- split の境界 `validation_start_ms` / `test_start_ms`（0031 §2.5）
- `ridge_lambda`
- Profile の `support_axes`（全 zone の `fan.<zone>` 軸が要る。0079 §2.5）と `residual_scale_floor`

任意の引数は `--code-commit <hex>` だけ（§2.7）。

### 2.4 検査と学習の順。公開の前に1つでも外れれば何も書かない

1. **`--dataset` の読み込みと公開物への束縛を、1回の読みで行う。** 既存の `verify_training_dataset_artifact_v2` は構築済みの
   `ThermalDatasetV2` を第1引数に取り、`examples.jsonl` は hash するだけで読まない。ディレクトリから `ThermalDatasetV2` を作る
   読み手は本番のコードに無い。そこで実装の PR で `control/model/counterfactual_training.py` に
   `load_training_dataset_artifact_v2(dataset_dir: Path) -> VerifiedTrainingDatasetArtifactV2` を足す:
   - directory 名が公開用の alias（`dataset-<32 hex>`）であることを確かめ、既存の `_open_directory_without_symlinks` で開く
   - `manifest.json` と `examples.jsonl` を、開いた `dir_fd` から既存の `_read_regular_file_at` で**それぞれ1回だけ**、既存の上限
     （`MAX_DATASET_MANIFEST_BYTES` / `MAX_DATASET_EXAMPLES_BYTES`。`control/model/training.py`）で読む。上限を超えれば parse の前に拒否する
   - manifest が canonical な公開 bytes であること、**読んだ examples の bytes の SHA-256** が `manifest.examples_sha256` と一致する
     ことを、parse の前に確かめる（hash と parse を同じ buffer から行う）
   - examples の各行を `DatasetExampleV2.model_validate_json` で読み、`ThermalDatasetV2(manifest=..., examples=...)` を作る（型の
     検査はそのまま効く）。`examples_jsonl_bytes(dataset.examples)` が読んだ bytes と完全に一致することを求める（canonical でない行を拒む）
   - 束縛の token は `verify_training_dataset_artifact_v2` と同じものを使い、`manifest_sha256`・`examples_sha256` も読んだ bytes から取る

   既存の `verify_training_dataset_artifact_v2` は変えない（試験と既存の呼び出しのため）。`train.py` はそれを呼ばず、新しい関数だけを呼ぶ
2. `verify_training_dataset_v2`: 元の入力の fingerprint・export の束縛・本番の DB の行と trace を照合し、一時の専用 DB で
   作り直した bytes が一致することを求める（0112 §2.3 / 0116）
3. `verify_training_calibration`: 期間の終わり（全 example の `label_end_ms` の最大）より後に較正の記録があれば拒否し、
   `--calibration` の写像が期間に効いていた行と一致しなければ拒否する（0099 §2.6 / 0096 §5 #4）。さらに
   `--declared-changes` の `calibration_changed` が期間の後にあっても拒否する（0109 の和集合と同じ向き）
4. `split_temporally_v2`（`--training-spec` の境界）
5. `train_counterfactual_ridge`（`train` だけ）→ `seal_counterfactual_artifact`（範囲は `train`、residual の基準は
   `validation`）。**`fit_confidence_profile_v2` を `train.py` から直接呼ばない。** `seal_counterfactual_artifact` が内部で
   1回だけ呼び（`src/coldaisle/control/model/counterfactual_confidence.py`）、`assemble_counterfactual_artifact` で封じるので、
   先に明示して呼ぶと全 train example の support の集計と validation の residual の推論を2回行う。報告の Profile の SHA-256 は、
   封じた artifact の `confidence_profile`（manifest の `confidence_profile_sha256`）から取り、Profile を作り直さない
6. `canonical_counterfactual_artifact_bytes` と `counterfactual_registry_metadata`
7. **読み込み側の検査を作った bytes に通す。** 作った bytes を読み直し、loader と同じ内容の検査
   （`check_artifact_contents` を `--calibration` の値と `--metrics-config` つきで。L9 の較正の digest を含む）を通してから
   書く。Registry の封筒（checksum・metadata の全欄照合）に依る検査は、`--apply` で登録した直後に `load_version` で
   読み直し、`RegistryCounterfactualThermalModel.from_verified_artifact`（L1〜L12）を通して確かめる。作れたが runtime で
   読めない artifact を candidate のまま気づかずに残さない（登録後に通らなければ終了コード 1 と、その artifact の
   `retire` を人へ促すログ。CLI は `retire` しない。§2.5）

`test` は学習にも Profile にも使わない。件数と期間を報告に載せるだけ（§5 の 5）。

失敗は構造化ログに理由を出して**終了コード 1**。「何も残さない」の約束は **`--out` の公開（§2.5 の手順 5 の
`os.link` による `COMPLETE.json` の公開。これが公開の確定点）の前に process の中で起きた失敗で、かつ手順 7 の後始末が
成功したとき**に限る。本節の 1〜7
（7 は `check_artifact_contents` まで）の失敗では `--out` を作らず、公開の手順の 2〜5 の失敗（`os.link` そのものの失敗を含む）
では、自分で作った `--out` を手順 7 のとおり片づける。書き手自身の `COMPLETE.json` は link の前には名前として存在しないので、
片づける前に他の process（`coldaisle-registry register --training-output`）が書き手の印を確かめて登録することは起きない。
後始末が失敗したとき（他の process が `--out` の中に置いた名前が残って `rmdir` が `ENOTEMPTY` になる、など）は握りつぶさず、
「後始末の失敗」として表の行に分ける。

process が強制終了したとき（`SIGKILL`・電源断）は片づけられないので、**`COMPLETE.json` の無い `--out`** が残りうる（§2.5）。
公開の後の失敗は、`--out` を消さずに残す。消すと、Registry に登録済みかもしれない bytes の唯一の手元の写しを失うためである。
公開の後の失敗は `--apply` の無い実行でも起きる（公開の手順 6 の一時名の unlink と fsync）。**link の後は、どの失敗でも
`--out` を片づけない**（link の後は並行する `register --training-output` が登録しているかもしれないため）。

| 失敗した段 | 終了コード | 残るもの | 回復 |
|---|---|---|---|
| 公開の前（検査・学習・公開の手順 1〜5 の例外。`os.link` の失敗を含む）で、後始末が成功 | 1 | 何も残らない（作った `--out` を片づけた） | 原因を直して同じ引数で再実行 |
| 公開の前の失敗で、**後始末が失敗**（例: 手順 5 の `EEXIST`。他の process が `--out` に `COMPLETE.json` を置いたため、既知の名前だけを消した後の `rmdir` が `ENOTEMPTY`） | 1 | **未完成の `--out`**（書き手の既知の名前は消せた分だけ消え、知らない名前が残る）。書き手は公開していない | ログに段 `cleanup_failed`・元の失敗の理由・後始末の例外（`errno`）・`--out` に残った名前の一覧を出す（元の例外は原因として残し、後始末の例外は隠さない）。人は、残った `COMPLETE.json` などが誰のものかを確かめ、`coldaisle-registry status` でそれを使った candidate が登録されていないことを見てから `--out` を消し、同じ引数で再実行する。CLI は知らない名前を消さない |
| 開始の時点・**信頼できない祖先**（根から `--out` の親までのどこかが §2.5 の「path の照合」の (a)(b) を満たさない。照合のたびに検査し、途中で権限が緩められたときもここに入る） | 1 | 開始の時点なら何も残らない（`--out` を作る前）。途中で緩められたときは、その段の行（公開の前なら後始末をして何も残らない、公開の後なら `--out` を残す・登録しない）に従う | ログに段 `untrusted_path`・満たさなかった祖先の path・所有者・mode を出す。人が置き場所を、自分か root の所有で他人が書けないディレクトリ（または `/tmp` の中の自分の所有のディレクトリ）の下へ変えて再実行する |
| 公開の前・**`--out` の名前の差し替え**（手順 5 の link の前の照合で、名前が無いか別のものを指す） | 1 | 書き手のディレクトリ（中の既知の名前は fd で消した。名前の付け替えで別の場所にあれば空のまま残る）と、`--out` の名前の先にある別のもの（書き手は触らない） | ログに段 `out_replaced`・手順 2 で開いた `(st_dev, st_ino)`・いま名前が指す `(st_dev, st_ino)`（あれば）を出す。親は §2.5 の手順 1 の検査で書き手の uid だけが書けるので、起きたなら同じ uid か root の操作である。人が親の中を確かめ、書き手のディレクトリ（空）を消し、`--out` の名前の先を調べてから再実行する |
| 公開の後・**`--out` の名前の差し替え**（手順 6 の後の照合、または `--apply` の登録の直前の照合） | 1 | 書き手の完成した出力（どこかの名前の下）と、`--out` の名前の先の別のもの。**登録しない**（Registry は変わらない） | ログに段 `out_replaced` と2つの `(st_dev, st_ino)` を出す。成功を報告しない。人が確かめ、完成した出力を `coldaisle-registry register --training-output` で確かめてから使うか、消して作り直す |
| 公開の前の強制終了（`SIGKILL`・電源断） | — | **`COMPLETE.json` の無い `--out`**（中のファイルは途中まで。印の一時名が残ることもある）。link の後・`--out` の fsync の前の電源断でも、link が永続でなければこの状態に戻りうる | 再実行は `--out` があるので拒否され、そのとき `--out` に有効な完成の印が無いことをログに出す。人が確かめて `--out` を消し、同じ引数で再実行する。CLI は自動では消さない（自分が作ったものか確かめられないため） |
| 公開の後・一時名の unlink と `--out` と親の fsync の失敗（公開の手順 6。`--apply` の有無に依らない） | 1 | `--out`（3ファイルと有効な `COMPLETE.json`。中身はすべて fsync 済みで、永続でないのはディレクトリの項だけ。unlink の失敗なら印の一時名も残るが、確かめる関数は見ない）。`--apply` なら Registry へは進まない | ログに段 `publish_durability` と `--out` の path を出す。中身は完成しているので、そのまま使ってよい（`--apply` なら `coldaisle-registry register --training-output <--out>` で登録する）。電源断の後に `--out` が無い・印が無効なら、消して同じ引数で再実行する（その前に並行の `register` で同じ版の candidate が登録されていれば、Registry 側の bytes は Registry が fsync している。`coldaisle-registry status` で確かめ、あれば作り直さない） |
| 公開の後・Registry の登録の失敗（lock・revision・同じ版が登録済み・権限・limits、および artifact ファイルの段の `RegistryDurabilityError`） | 1 | `--out`（3ファイルと完成の印）。`registry.json` と参照される artifact は変わらない（登録の確定点の前）。artifact を置いた後の失敗では、**参照されない artifact のファイル**（`artifacts/thermal_model/<model_id>/<version>/`）が残りうる（`register_candidate` の片付けは best-effort） | `coldaisle-registry register --training-output <--out>` で登録する（同じ版の再登録は残った artifact を同じ path へ `os.replace` で上書きするので妨げられない）。登録しないまま残った参照されない artifact は、`coldaisle-registry` に片付けの入口が無いので、人が `coldaisle-registry status` で参照されていないことを確かめてから消す。同じ版が登録済みなら `--model-version` を上げて別の `--out` で作り直す |
| 登録は確定したが永続が確かでない（`registry.json` の置き換えの後の `RegistryDurabilityError`。下記） | 1 | `--out` と、Registry の **candidate**（見えている。production は動かない。読み直す前に他の CLI が `validate` / `retire` で状態を進めていることもある） | CLI はそのまま**登録の後の検査（`load_version` と L1〜L12）へ進む**（読んだ状態もログに出す）。検査が通ってもログに段 `register_durability` を出して終了コード 1。人は `coldaisle-registry status` で candidate があることを確かめる。電源断の後に無くなっていたら、`coldaisle-registry register --training-output <--out>` で登録する。**もう一度 `coldaisle-train --apply` で登録し直さない**（見えている間は「既にある」になる） |
| 登録の後・**`--out` の名前の差し替え**（登録の直後の path の照合。登録の直前の照合の後に祖先などの名前が付け替えられた） | 1 | Registry の **candidate**（登録済み。production は動かない。中身は検査済みの bytes で正しい）と、書き手の完成した出力（どこかの名前の下）。`--out` の path は別のものを指す | ログに段 `out_replaced_after_register`・登録した artifact の key（`model_id` と version）・理由（照合で得た2つの `(st_dev, st_ino)`、path が無い・symlink を含む、の別）を出す。成功を報告しない。人が `coldaisle-registry status` で candidate を確かめ、付け替えの経緯を調べて、使わないなら既存の入口 `coldaisle-registry retire` で外す。CLI は自分で `retire` しない。登録の後の L1〜L12 の検査はこの場合も行い、結果をログに出す |
| 公開の後・**`--out` の中身の書き換え**（手順 6 の後・登録の前または成功の報告の前の再検証で、`COMPLETE.json` か3つのファイルが印やメモリ上の bytes と違う） | 1 | 書き換えられた `--out`。**登録しない**（Registry は変わらない） | ログに段 `out_modified` と、違ったファイルの名前・期待した SHA-256・実際の SHA-256 を出す。成功を報告しない。人が誰が書いたかを確かめ、`--out` を消して別の `--out` で作り直す |
| 登録の後・**`--out` の中身の書き換え**（登録の後・成功の報告の前の再検証で、登録した bytes と違う） | 1 | Registry の **candidate**（登録済み。中身は登録の直前に検証した bytes で正しい）と、書き換えられた `--out` | ログに段 `out_modified_after_register`・登録した artifact の key（`model_id` と version）・違ったファイルと2つの SHA-256 を出す。成功を報告しない。人が `coldaisle-registry status` で candidate を確かめ、使わないなら既存の入口 `coldaisle-registry retire` で外す。CLI は自分で `retire` しない |
| 登録の後・`load_version` と L1〜L12 の検査の失敗 | 1 | `--out` と、Registry の **candidate**（production は動かない） | ログに artifact の key と `--out` の path を出す。人が原因を確かめ、`coldaisle-registry retire` で candidate を退ける。CLI は `retire` しない |

構造化ログには失敗した段（`before_publish` / `cleanup_failed` / `out_replaced` / `publish_durability` / `register` / `register_durability` /
`out_replaced_after_register` / `untrusted_path` / `out_modified` / `out_modified_after_register` / `post_register_check`）を必ず出し、どの行に当たるかを人が取り違えないようにする。

### 2.5 出力と、既定では Registry を書かないこと

`--out` のディレクトリに次の3つと完成の印を置く。`--out` が**既にあれば拒否する**（上書きしない）。

| ファイル | 中身 |
|---|---|
| `artifact.json` | 登録する canonical bytes（§2.4 の 6） |
| `metadata.json` | `ArtifactMetadata` の strict JSON（`coldaisle-registry register --metadata` へそのまま渡せる形） |
| `training-report.json` | §2.7 の digest の一覧・split ごとの件数と期間・artifact の SHA-256・Profile の SHA-256・除いた example の件数 |

#### 脅威の前提

`--out` の公開と登録の手順が守る相手は次の2つだけである。

- **(a) 途中の失敗・電源断・強制終了。** どこで止まっても、`--out` は「無い」「未完成と分かる（有効な `COMPLETE.json` が無い）」
  「完成している」のどれかで、Registry は 0062 / 0104 の確定点の前か後のどちらかにある。残ったものと回復は §2.4 の表に書く
- **(b) 別のユーザー（`--out` の親や祖先に書ける者）による名前の差し替え。** 開始の時点で、根から `--out` の親までの各
  ディレクトリが「信頼できる祖先」（下の「path の照合」の (a) か (b)）であることを確かめ、満たさなければ拒む。満たすとき、
  別のユーザーは途中のどの要素の名前も変えられない: ある要素の名前を変えるにはその親ディレクトリへの書き込みが要るが、
  (a) の親は root か実行した uid だけが書け、(b) の sticky な親（`/tmp` など）では、中の項目の名前を変えられるのはその項目か
  親の所有者と root だけで、(b) は親（D）の所有者と次の要素の所有者をどちらも root か実行した uid に限る。したがって別のユーザーによる名前の差し替えは
  **「開始の時点で拒む」か「起こりえない」のどちらか**になり、以後の照合（fd でたどって dev / inode を比べる）はそれで十分になる。
  root は前提の外（同じ uid と同じ理由）

**同じ uid の悪意あるプロセスは前提の外とする。** 同じ uid は Registry の書き手の権限・設定・`train.py` の実行環境（venv・
作業ディレクトリ）そのものを書き換えられるので、ここで何を検査しても迂回できる。下の「中身の再検証」（`out_modified` /
`out_modified_after_register`）は、同じ uid の**誤った操作**（別の端末で同じ `--out` へ誤って書いた、など）を見つける安価な
検査と位置づけ、悪意への防御とは主張しない。

#### 公開の手順（`--out` を最初に排他的に作り、最後に完成の印を付ける。OS に依らない）

**推奨案（§5 の 10 の (A)）で書く。** 使うのは macOS / Ubuntu で共通の POSIX の操作だけで、OS ごとの関数を呼ばない
（NFR-07 を変えない）。

1. **`--out` の親を、Dataset の writer と同じ検査で開く。** 既存の `dataset._open_output_root`（`src/coldaisle/dataset.py`）を
   使い回す: path の各要素を `/` から symlink に追従せず（`O_DIRECTORY | O_NOFOLLOW`）`dir_fd` でたどって開き、`..` を拒み、
   開いた親が**実行した uid の所有で、group / world が書けない**ことを確かめる（外れれば `PermissionError` で拒否）。0700 は
   ディレクトリの中身を守るが名前は守らないので、親に書ける別のユーザーが作ったばかりの `--out` の名前を付け替えて差し替える
   ことを、この検査で親に書ける者を書き手（と root）に限って防ぐ。実装の PR ではこの非公開の関数を公開の関数にして
   `train.py` から呼ぶ（挙動は変えない。0031 §3 は置き換えない。手順を写して2つにしない）。**この関数が確かめるのは最後に
   開いた親だけで、祖先は確かめない。** 祖先に group / world が書ける場所（`/tmp` の下など）があると、開いた後に親の名前を
   付け替えられ、親の fd に対する照合は古い親を見続ける。そこで公開と登録の前の照合は、親の fd ではなく
   **`--out` の絶対 path を根から解き直して**行う（下の「path の照合」）。
   **`--out` の親（と祖先）は既にあることを求め、無ければ失敗にする**（作らない）。いまの `_open_output_root` は無い要素を
   `0700` で作り、作らない形で呼ぶ引数は無い。作った祖先の項はさらにその親の中にあり、手順 6 が fsync するのは `--out` と直接の
   親だけなので、成功を報告した後の電源断で祖先ごと出力が消えうる。そこで公開の関数にするときに keyword 引数
   `create: bool`（既定は今の挙動の `True`。Dataset の writer の挙動は変えない）を足し、`train.py` は `create=False` で呼ぶ。
   `False` では無い要素で `FileNotFoundError` を出し、終了コード 1（学習の前に拒否。`--out` は作られない）。`train.py` 側で
   先に `os.lstat` で確かめるだけの形は、確かめた後・開く前に消されると作り直してしまうので採らない。この変更も 0031 §3 の
   公開の手順は変えない
2. **`os.mkdir(--out の名前, 0o700, dir_fd=親)` で `--out` を排他的に確保する。** 既にあれば `EEXIST` で拒否する。`mkdir` は
   POSIX で原子的で、既にあるもの（他の process が作った空のディレクトリを含む）を**置き換えない**。「既にある出力は必ず拒む」
   の正しさはこの1手が持ち、確認と公開の間の窓（rename による置き換え）がそもそも無い。作った直後に `--out` を
   `O_DIRECTORY | O_NOFOLLOW` で開き、path の照合（下記）が通ること・中が空であることを確かめる（差し替えの検知）。
   以後は fd（`dir_fd`）だけで書く
3. 3つのファイルを `--out` の中へ `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` で書き、**それぞれ fsync** する。**順は
   `training-report.json` を最初**にする（`artifact.json` があるときは必ず `training-report.json` もある。下の「`--metadata` /
   `--payload` の経路」の判定に使う）
4. **完成の印を一時名で書く。** 3つのファイルの名前・大きさ・SHA-256 を、`--out` の中の一時名
   `.COMPLETE.json.tmp-<32 hex>` へ `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` で書き、fsync する。読む側はこの名前を見ない
5. **link の直前に、path の照合（下記）で `--out` の path がまだ手順 2 で開いたディレクトリを指していることを確かめる。**
   違えば（path が無い・途中に symlink がある・別のものを指す）失敗にし、段 `out_replaced` で終了コード 1（§2.4 の表）。そのうえで
   **`os.link(一時名, "COMPLETE.json", src_dir_fd=--out, dst_dir_fd=--out, follow_symlinks=False)` で印を公開する。**
   `link` は既にある名前を置き換えず `EEXIST` で失敗する（POSIX 共通。NFR-07 を変えない）。**この link が公開の確定点**で、
   `COMPLETE.json` は中身が fsync 済みの状態でだけ名前として現れる。`O_CREAT` で直接作ると、中身を書いて fsync するまでの間に
   名前が見えて、並行する `register --training-output` が確かめて登録したのに書き手が後で片づける、という食い違いが起きる
6. 一時名を unlink し、`--out` を fsync し、親を fsync する（`COMPLETE.json` の項と `--out` の項を永続にする）。ここでの失敗は
   公開の**後**の失敗（§2.4 の表）で、片づけない。fsync の後にもう一度、手順 5 と同じ path の照合をする。違えば
   段 `out_replaced` で終了コード 1 にし、成功を報告しない
7. 手順 2〜5 で例外が起きたとき（link の失敗を含む）は、自分で作った `--out` の中の**既知の名前だけ**（3つのファイル・
   印の一時名）を開いた fd（`dir_fd`）で unlink し、`--out` を rmdir する（rmdir は名前で行うので、その直前に名前が手順 2 で
   確かめた `(st_dev, st_ino)` と同じとき、つまり path の照合が通るときだけ。違えば rmdir せず段 `out_replaced`）。知らない名前
   （他の process が置いた `COMPLETE.json` など）は消さない。後始末の例外は**握りつぶさない**: 元の例外を原因として保ったまま、
   後始末の失敗（`unlink` / `rmdir` の `errno` と、`--out` に残った名前の一覧）を構造化ログに出し、段 `cleanup_failed` で
   終了コード 1 にする（§2.4 の表）。`suppress(OSError)` で黙って捨てない（AGENTS.md「例外を握りつぶさない」）

**path の照合（`training_output.py` の関数。仮に `open_trusted_path(path)` と `path_names_directory(path, fd)`）。** `--out` の
**絶対 path を根から1要素ずつ、ディレクトリの fd でたどり直し、たどりながら各段が信頼できる祖先であることを確かめる**:

- 信頼できる祖先: 根から `--out` の親までの各ディレクトリ D について、fd の `fstat` で次の (a) か (b) を満たすこと。
  (a) D の所有者が root か実行した uid（`os.geteuid()`）で、group / other に書き込めない（`S_IWGRP` / `S_IWOTH` が無い）。
  (b) D に sticky bit（`S_ISVTX`）が立っていて、**D 自身の所有者が root か実行した uid**で、かつ D の中の次の要素（開いた次の
  fd の `fstat`）の所有者が root か実行した uid。sticky なディレクトリでも、そのディレクトリの所有者は中の項目の名前を変え・
  消せるので、D の所有者が別のユーザーなら最後の照合の後に `--out` を差し替えられる（D の所有者を確かめないと (b) は成り立たない）。
  どちらも満たさなければ失敗（段 `untrusted_path`、終了コード 1）。`--out` そのものは手順 2 で書き手が `0700` で作るので
  (a) を満たす
- たどり方: `os.open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW)` から始め、各要素（最後の `--out` も）を
`os.open(名前, O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=ひとつ前の fd)` で開いては前の fd を閉じる。最後に開いた fd の
`fstat` の `(st_dev, st_ino)` が、手順 2 で固定した fd の `fstat` と一致するときだけ一致とする。途中か最後の要素が symlink なら
`ELOOP`、ディレクトリでなければ `ENOTDIR`、無ければ `ENOENT` で開けず、すべて不一致とする。path の文字列を毎回根から解かせる
`os.lstat` の連続は使わない: ある祖先を確かめた後にそれが symlink へ差し替えられると、後の要素の `lstat` がその symlink を
たどってしまい、差し替えを見逃す（例: `/tmp/a` を確かめた後に `/tmp/a` を `a-old` への symlink に替えると、`/tmp/a/out` の
`lstat` は `a-old/out` を見て一致と報告する）。fd でたどれば、各段で開いたものそのものから次を開くので、その時点の path の
各要素が symlink でないディレクトリであることと、最後がいまの path の指すものであることを1回のたどりで確かめられる。
いずれも POSIX で共通の操作で、NFR-07 を変えない。手順 1 で開いた親や祖先の fd は使い回さない。
- **`--out` には symlink を含まない実の path を渡す。CLI は symlink を解かない。** macOS では `/tmp`・`/var`・`/etc` が
  `/private/...` への symlink なので、`--out /tmp/<自分のディレクトリ>/...` は根の次の要素で `ELOOP` になる。そこで:
  - `os.path.realpath` などで先に解いてから照合する形は採らない（解いた後・開く前に途中の要素を差し替えられる窓が戻る）。
    OS ごとの別名の一覧を持って解く形も採らない（§5 の 13 で決定済み）
  - `ELOOP` で開けなかったときは、どの要素が symlink かを `os.readlink(名前, dir_fd=ひとつ前の fd)` で読んでログに出し（読むだけで
    たどらない）、段 `untrusted_path`・終了コード 1 で拒否する。ログには「symlink を含まない実の path（macOS なら `/private/tmp/...`）を
    渡す」ことを書く。開始の時点なら学習の前で、`--out` は作られない
  - この約束は `--out` の親の検査（手順 1 の `_open_output_root`）と `coldaisle-registry register --training-output` の照合も同じ
  - Ubuntu の `/tmp` は実のディレクトリなので、`/tmp/<自分のディレクトリ>` はそのまま受け入れる。macOS では `/private/tmp/<自分のディレクトリ>`
    （`/private` は root の所有で `0755`、`/private/tmp` は `1777`・root の所有）が同じ規則 (a)(b) で受け入れられる
- **開始の時点**（手順 1 で親を開いた直後・手順 2 の前）に `--out` の親までを `open_trusted_path` で検査し、満たさなければ
  学習の前に段 `untrusted_path` で拒む（`--out` は作られない）。以後の照合（手順 2 の直後・link の前・手順 6 の後・登録の前後・
  rmdir の前）は同じたどりで `--out` までを確かめ、信頼の検査も毎回行う（権限が途中で緩められたら `untrusted_path` で失敗）

- **選んだ方式と理由。** fd でたどって inode を比べるだけでは、途中のディレクトリを開いた後にその親の項目を動かせる別の
  ユーザーが差し替えると、たどりが古い fd のまま進んで一致してしまう。そこで標準的な secure path の検査（信頼できる祖先）を
  足す。所有者と権限だけの検査は `/tmp` の下を拒むが、sticky の規則 (b) を入れることで普通の置き場所（`/tmp/<自分のディレクトリ>`
  など）は拒まない。この検査の下では別のユーザーによる名前の差し替えは起こりえない（上の「脅威の前提」の (b)）ので、照合と
  次の操作（link・登録）の間の窓で差し替えられうるのは同じ uid か root だけになる。同じ uid の誤った操作を見つけるために、
  **登録の後にもう一度 path を照合し、外れていれば必ず失敗として報告する**（段 `out_replaced_after_register`。§2.4 の表。
  登録を防ぐとは約束しない）。このとき登録された candidate の**中身は正しい**: 書き手も `register --training-output` も、登録するのは
  開いた fd を確かめる関数に通して返った bytes（書き手ではメモリ上の bytes と一致を確かめたもの）で、どちらも path を
  読み直さない。問題は `--out` の名前が別のものを指していることだけである
- この関数は `training_output.py` に置き、`train.py`（手順 5・手順 6 の後・`--apply` の登録の直前・手順 7 の rmdir の前）と
  `coldaisle-registry register --training-output`（`--out` を開いて印を確かめた後、`register_candidate` を呼ぶ直前と直後）の
  両方が同じ関数を使う。直前で違えば終了コード 1 で Registry を開かない。直後で違えば段 `out_replaced_after_register`
  （終了コード 1）

**読む側は、有効な `COMPLETE.json` の無い `--out` を未完成として拒む。** 有効とは、`COMPLETE.json`（一時名ではない）が
JSON として読めて、書かれた3つのファイルの大きさと SHA-256 が実物と一致することである。link で公開するので `COMPLETE.json` が
書きかけで見えることは手順の上では無いが、読む側は書き手を信用せず毎回この照合をする（ファイルの書きかけや後からの
差し替えもこれで拒める）。確かめる関数は印に書かれた3つのファイルと `COMPLETE.json` だけを読み、他の名前は見ない。

- **中身の再検証（同じ uid の誤った操作を見つける安価な検査。上の「脅威の前提」）。** 手順 6 の後、`--apply` の登録の直前に、
  開いた `--out` の fd を `training_output.py` の確かめる関数に通し（`COMPLETE.json` と3つのファイルの大きさ・SHA-256）、
  **その関数が返した bytes を登録する**（メモリ上の bytes は使わない）。返した bytes がメモリ上の bytes と違えば、登録せずに段
  `out_modified` で終了コード 1。`--apply` の無い実行でも、成功を報告する直前に同じ検証をし、外れれば `out_modified` で
  終了コード 1。`--apply` では登録の後・成功を報告する直前にもう一度検証し、登録した bytes と違えば段
  `out_modified_after_register` で終了コード 1（candidate は登録済み）
- `--apply` の登録は手順 6 が成功した後に行う。**登録の直前にも**、`--out` の名前が
  手順 2 で開いたディレクトリを指していることを path の照合で確かめ、違えば登録せずに段 `out_replaced` で
  終了コード 1（直前の照合で見つかった差し替えでは登録しない）。**登録の後にも**同じ照合をし、違えば段
  `out_replaced_after_register` で終了コード 1（直前の照合の後の差し替えは登録を防げないので、登録の後に必ず報告する）。
  その後の `load_version` と L1〜L12 の検査は path ではなく Registry の bytes を見るので、この照合の代わりにならない
- 手動の登録と回復は **`coldaisle-registry register --training-output <--out>`** で行う（`--metadata` / `--payload` と
  排他）。`register` は下記の確かめる関数で印を確かめ、**確かめたときに読んだ bytes そのもの**（`artifact.json` と
  `metadata.json`）を `register_candidate` へ渡す（確かめた後に読み直さない。差し替えの窓を作らない）。印が無い・無効なら
  終了コード 1 で Registry を開かない
- **`--metadata` / `--payload` の経路は、学習の出力のファイルを拒む。** 手順 3〜5 の間、`artifact.json` と `metadata.json` は
  `COMPLETE.json` がまだ無いのに完成して checksum も合っていることがある。同じ uid が並行してこの経路で登録すると、公開の点より
  前に candidate ができ、その後に印の作成が失敗すると `--out` は片づけられるのに Registry は変わったままになる。これは「脅威の
  前提」の**同じ uid の誤った操作**に当たるので、安価な検査で止める: `register --metadata/--payload` は、`--payload` か
  `--metadata` のファイルと同じディレクトリに `training-report.json` があれば（学習の出力のディレクトリ。手順 3 で最初に書く）、
  `COMPLETE.json` の有無に依らず拒む。**判定は、実際に開いたファイルの項があるディレクトリで行う。** いまの `read_bounded`
  （`src/coldaisle/registry.py`）は `path.open("rb")` で最後の要素の symlink もたどるので、学習の出力の外に置いた symlink を渡すと、
  path の字面の隣を見る判定は symlink のあるディレクトリしか見ず、読むのは学習の出力の中の完成したファイルになる。そこで実装の
  PR で、この2つの入力の読み方を次に変える（§5 の 14）: (1) 親のディレクトリを `O_RDONLY | O_DIRECTORY` で1回開く（親までの
  symlink は今どおりたどる）→ (2) 最後の要素を `os.open(名前, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, dir_fd=親)` で開き
  （symlink なら `ELOOP` で拒否）、`fstat` で regular file であることを確かめ、その fd から上限＋1 byte だけを読む →
  (3) 隣の `training-report.json` の有無を、同じ親の fd に対する `os.stat(..., dir_fd=親, follow_symlinks=False)` で確かめる。
  開いた項と判定するディレクトリが同じ fd に束縛されるので、symlink で判定を外せない。hard link やコピーで別のディレクトリへ
  置いたものは判定が効かないが、同じ uid の意図した操作として下の「欠点」と同じく前提の外とする。上の条件に当たれば
  `COMPLETE.json` の有無に依らず拒み、`--training-output` を使うよう促して終了コード 1 で Registry を開かない。学習の出力は
  `--training-output` の経路（印を確かめる）からだけ登録できる。RL policy など学習の CLI の外の artifact の登録は、最後の要素に symlink を渡さない限り変わらない（§5 の 14）。
  - **推奨とした理由**: もう1つの案「書いている途中のファイルを一時名（`.artifact.json.partial` など）で置き、公開と同時か
    直前に最終の名前へ移す」は、最終の名前へ移すのを印の link の前にすると窓が残り（移した後・link の前に同じ経路で登録
    できる）、link の後にすると「印はあるがファイルが無い」公開の後の失敗の状態が増える。拒む案は公開の手順と状態を変えず、
    登録できる経路を「印を確かめる経路」1つに絞れる
  - 欠点: 学習の出力のファイルを別の場所へ写してから `--metadata` / `--payload` で渡すと、この判定は効かない。同じ uid の
    意図した操作なので前提の外とする。写したものでも `register_candidate` は payload の SHA-256 を metadata と照合するので、
    書きかけのファイルが黙って candidate になることは無い
- **確かめる関数の置き場所は、レイヤ横断の小さなモジュール `src/coldaisle/training_output.py`**（仮の名前）とする。
  印の形（`COMPLETE.json` の pydantic の型）・書く側の公開の手順（§2.5 の手順 2〜7）・path の照合の関数・読む側の確かめる関数
  （`O_DIRECTORY | O_NOFOLLOW` で開き、`dir_fd` で各ファイルを上限つきで読み、大きさと SHA-256 を照合して bytes を返す）を
  1か所に持つ。import するのは `train.py` と `registry.py`（どちらも合成の起点）だけで、標準ライブラリと pydantic の外の
  coldaisle の module を import しない。
  - **読む側の上限は呼び出し側が int で渡し、parse の前に当てる。** `training_output.py` は coldaisle を import しないので
    `ModelRegistryLimits` を受け取らず、`max_artifact_bytes: int` と `max_other_bytes: int` を必須の keyword 引数に取る（既定値なし）。
    当て方は 0062 §2.1 と同じで、`artifact.json` は `max_artifact_bytes`、`COMPLETE.json`・`metadata.json`・`training-report.json` は
    `max_other_bytes` を上限とする。順は (1) `COMPLETE.json` を上限＋1 byte だけ読み、超えれば parse の前に拒否 → (2) 印に書かれた
    各ファイルの大きさが、そのファイルの上限を超えていれば開く前に拒否 → (3) 各ファイルを `O_NOFOLLOW` で開き、`fstat` の大きさが印と
    同じことを確かめ、印の大きさ＋1 byte だけ読む（多く読めれば拒否）→ (4) SHA-256 を照合して bytes を返す
  - 渡す値: `coldaisle-registry register --training-output` は既存の `--limits` から読んだ `ModelRegistryLimits` の
    `max_artifact_bytes` / `max_snapshot_bytes`（`register --metadata/--payload` の `read_bounded` と同じ値。Registry を開く前に当てる）。
    `train.py` は `--apply` のとき `--registry-limits` の同じ2つ、`--apply` の無い実行の再検証（`out_modified`）では自分がメモリに
    持つ bytes の長さ（印と各ファイル。期待する大きさそのもの）を渡す
  根拠:
  - 既存の前例が同じ形である。`csv_export_manifest.py`（export と再生が共有する manifest の形。0100）と
    `declared_changes.py`（`coldaisle-drift` と `coldaisle-dataset` が共有する YAML の形。0109）は、複数の合成の起点が共有する
    ファイルの形を、どの合成の起点にも属さないレイヤ横断の module に置いている
  - `train.py` に置くと、§2.1 の「どこからも import しない」と両立しない（`registry.py` が import できない）
  - `control/model_registry.py` に置くと、制御の process の import の木に入る。`control_daemon` は
    `control/registry_binding.py` を通じて、`learned_worker` と `learned_channel/registry_watch.py` は直接、`model_registry` を
    import している。学習の出力のファイルの形は fand も worker も読まないので、その木に足さない
  - 試験で、`control_daemon`・`control/`・`learned_worker`・`learned_channel`・`api/`・`ai/` が `training_output` を import
    しないことを走査する（§2.9）

process が強制終了したときに残るのは、印の無い（または無効な）`--out` だけである。再実行は手順 2 で拒否されるので、人が
確かめて消す（§2.4 の表）。Dataset の writer（0031 §3）は「親の中の一時ディレクトリ → 共通の `os.rename`」で、強制終了の後に
残るのが隠れた一時ディレクトリである点が違うが、どちらも **macOS / Ubuntu 共通の POSIX の操作だけ**を使う点で一致する。
**`write_dataset` の挙動（0031 §3）は本記録では変えない**（§5 の 9 の申し送り）。

#### `--apply` の Registry 側の書き込み

`ModelRegistry.register_candidate` は、artifact を `artifacts/<kind>/<model_id>/<version>/` へ一時ファイルと
`os.replace` で書いてから、`registry.json` を同じく原子的に置き換える（`src/coldaisle/control/model_registry.py` の
`register_candidate` → `_write_artifact` → `_atomic_write`）。**`registry.json` の置き換えが登録の確定点**で、
その前に強制終了しても残るのは参照されない artifact だけである（snapshot が無い bytes を指す状態は起きない）。
同じ版の再登録は同じ path へ `os.replace` で上書きするので、残った artifact が再実行を妨げない。

ただし例外は2つの意味に分かれる。`_atomic_write` は `os.replace` の**後**にディレクトリの fsync が失敗すると
`RegistryDurabilityError`（「置き換えは済んで見えているが、crash をまたぐ永続が確かでない。呼び出し側は取り消さない」）を出す。
`register_candidate` は、

- artifact ファイルの段（snapshot の置き換えの前）の例外では、参照されない artifact を消して例外を出す。**Registry は変わらない**
- snapshot の置き換えを試みた後の `RegistryDurabilityError` だけは、artifact を消さずにそのまま出す。**candidate は登録済みで
  見えている**（`snapshot_attempted and isinstance(exc, RegistryDurabilityError)` の分岐）

例外の型だけでは2つを区別できない（どちらも `RegistryDurabilityError` になりうる）。そこで CLI は `RegistryDurabilityError`
を受けたら、同じ limits で Registry の snapshot を**1回だけ**読み直し（`ModelRegistry.inspect()`。1回の読みで lifecycle の
状態と audit を含む snapshot を得る）、**その ref の記録が `metadata.json` と同じ `sha256` で `artifacts` にあり、`audit` にその ref の
`REGISTERED` の event があるか**で分ける。**lifecycle の状態は条件にしない。** `RegistryDurabilityError` の時点で Registry の lock は
放されているので、読み直す前に他の権限のある CLI が `validate` / `retire` で状態を進めうる。状態が `candidate` のままであることを
求めると、確定した登録を確定の前の失敗と取り違え、登録の後の検査を飛ばし、「既にある」で拒まれる再登録を促してしまう。
あれば「確定したが永続が確かでない」として、読んだ状態（`candidate` / `validated` / `retired` など）をログに出して登録の後の検査
（`load_version` は状態に依らず読める）へ進み、無ければ登録の失敗として扱う（§2.4 の表）。同じ ref が別の `sha256` で
あるときは、同じ版を別の bytes で他者が登録したので、登録の失敗（同じ版が登録済み）とする。それ以外の `ModelRegistryError` は登録の失敗である。Registry 側の書き方は本記録では
変えない。

順序は **`--out` の公開 → Registry の登録**とする。2つの間で止まったときは `--out` だけがあり、`coldaisle-train` の
再実行は `--out` が既にあるので拒否される。このときは `coldaisle-registry register --training-output <--out>` で、印を
確かめた同じ bytes が登録される（§5 の 1 の推奨の理由と同じ）。登録の失敗は構造化ログに
`--out` の path を出し、この手順を促す。

- **既定では Registry を書かない。** `--out` へ書いて終わる（`coldaisle-calibrate` / `coldaisle-memory` の「既定では
  書かない」と同じ流儀）
- `--apply --registry-root <dir> --registry-limits <dir> --actor <名前> --reason <文>` を付けたときだけ、`artifact.json` と `metadata.json` と
  **同じ bytes** を `ModelRegistry.register_candidate` へ渡す（§5 の 1 / 2）
- **`--registry-limits <dir>` は `--apply` のとき必須**（`model-registry.yaml` のあるディレクトリ。既定値を置かない）。
  `ModelRegistry` は `ModelRegistryLimits` を要し、その値は `config/model-registry.yaml` にだけあってコードに既定値が無い。
  §2.7 の上限つきの1回の読み（`MAX_TRAINING_CONFIG_BYTES`）で `model-registry.yaml` の bytes を得て、その buffer から
  `yaml.safe_load` → `ModelRegistryLimits.model_validate` で作る。**`load_model_registry_limits` と `ModelRegistryLimits.from_file` は
  使わない**（どちらも path から上限なしで読み、§2.7 の読みと別の2回目の読みになって、差し替えた bytes を見うるため）。この1つの
  limits を、**登録（`register_candidate`）と登録の後の `load_version`（§2.4 の 7）の両方に
  同じ limits を使う**。`coldaisle-registry --limits` と `coldaisle-authority raise --registry-limits` は既定値に相対 path の
  `config` を持つが、作業ディレクトリに依って別のファイルを読む余地があるので、ここでは置かない（0134 §2.2 が本番の
  `raise` に絶対 path を求めたのと同じ理由）。読めない・不正なら、公開の前に拒否する（Registry を開く前に limits を読む）
- 呼ぶのは **`register_candidate` だけ**。`mark_validated` / `promote` / `rollback` / `retire` は呼ばない。
  production pointer は動かない（0056 §2.6 の「candidate として登録」）
- 権限は 0104 §2.1 の「Registry の書き手」と同じ。`coldaisle-registry` グループの人が**自分の uid のまま**実行する。
  root・lock・ディレクトリの mode の扱いは `coldaisle-registry register` と同じ `ModelRegistry` の挙動に従い、
  本記録では変えない（0105）
- candidate は制御に届かない。fand は起動時に production だけを読み（0077 §2.6 / 0098）、learned_channel の監視も
  production の変化だけを見る

### 2.6 production へ上げる経路とは分ける

`coldaisle-train` は昇格の経路を一切持たない。candidate から先は既存の入口だけを通る。

```text
coldaisle-train --apply            → candidate         （本記録）
coldaisle-registry validate        → validated         （offline_evaluation_ref が要る。0062）
coldaisle-registry promote         → production        （人が書いた承認ファイル。0062 §2.2）
coldaisle-authority raise          → authority を1段   （承認者の uid。0086 / 0104 / 0134）
```

- `coldaisle-train` に `--validate` / `--promote` のような flag を足さない。承認を引数から組み立てる経路も作らない
  （0062 §2.2）
- thermal model の `offline_evaluation_ref` に何を入れるか（test split の残差の報告の形と合否）は本記録では決めない
  （§5 の 5）

### 2.7 再現性

- **同じ入力・同じ `--created-at`・同じコードからは、同じ `artifact.json` の bytes が出る。** ridge と Profile の作り方は
  決定的で乱数を使わないので、最初の版には **seed の引数を置かない**。将来の trainer が乱数を使うなら、seed を
  `--training-spec` に必須で足し、報告と artifact の `hyperparameters` に載せる（0058 §2.6 と同じ規律）
- **壁時計を読まない。** `created_at` は引数（§5 の 4）。報告にも生成時刻の欄を置かない（0054 §2.7 と同じ）
- `training-report.json` に次の digest を載せる: dataset の alias・manifest と examples の SHA-256・split の SHA-256
  （`split_sha256`）・`--training-spec` の bytes の SHA-256・較正の `offsets_sha256` と照合した記録の行・
  `--declared-changes` / `--quality-config` / `--metrics-config` / `--calibration` の bytes の SHA-256（どれも parse した bytes と同じ
  buffer から。下記）・再生の入力の `source_sha256` と
  `export_binding_sha256`。artifact の manifest が既に持つもの（`training_data`・`calibration_binding`）は重ねて計算しない
- **設定ファイルはすべて1回だけ読み、その bytes から parse し、同じ buffer から hash する。** 読んだ後・digest を取る前に
  差し替えられて、artifact と `training-report.json` が違う設定を指すことを防ぐ。path から2回読む API（parse 用と hash 用）を
  使わない。既存の入口で足りるかは次のとおり

  | 入力 | 読み方 |
  |---|---|
  | `--training-spec` | `train.py` が bytes を読み、`yaml.safe_load` → schema の pydantic の型で検証。hash も同じ bytes |
  | `--quality-config` | `QualityRules.from_yaml(path)` は path から読み digest を返さないので**使わない**。`train.py` が bytes を読み、`yaml.safe_load` → mapping であることを確かめ → `QualityRules.model_validate`（`from_yaml` の本体と同じ処理で、既存の API で足りる）。hash も同じ bytes |
  | `--metrics-config` | `train.py` が上限つきで bytes を読み、`MetricCatalog.from_yaml_bytes_with_sha256(data, origin=...)`（実装の PR で足す。下記）。hash も同じ bytes |
  | `--declared-changes` | `train.py` が上限つきで bytes を読み、`parse_declared_changes(data, origin=...)`（実装の PR で足す。下記）。`file_sha256` も同じ bytes |
  | `--calibration` | `train.py` が bytes を読み、`Calibration.from_json_bytes(data, origin=...)`。hash も同じ bytes（報告の digest。較正の照合は 0099 §2.6 の `offsets_sha256`） |
  | `--registry-limits` | `train.py` が `model-registry.yaml` の bytes を読み、`yaml.safe_load` → `ModelRegistryLimits.model_validate`（`from_file` の本体と同じ）。1回読んだ limits を登録と `load_version` の両方に使う（§2.5） |

  `training_entry` に渡す `QualityRules` も、この1回の読みから作ったものを渡す

- **上の6つの入力はすべて上限で切って読む**（0062 §2.1 の流儀: 上限＋1 byte だけを読み、超えていれば parse の前に拒否する。
  読んでから大きさを判断しない）。読むのは `train.py` の1つの関数で、`O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK` で開き、
  `fstat` で regular file であることを確かめてから読む（`declared_changes._read_regular_file` と同じ開き方）。上限を超えた・
  regular file でない・UTF-8 でない・YAML / JSON として読めない・parser の再帰が尽きた（`RecursionError`。0062 §2.1）は、
  どれも学習の前に構造化ログと終了コード 1 で拒否し、`--out` を作らない。**上限は `train.py` の構造上の定数
  `MAX_TRAINING_CONFIG_BYTES = 1 MiB`（1,048,576 bytes）で、6つの入力に共通**（運用の閾値ではなくメモリを守る構造上の上限。
  既存の `MAX_ARTIFACT_BYTES`・`_MAX_JOURNAL_BYTES`・`MAX_DECLARED_CHANGES` と同じ扱い。§5 の 12 で決定済み）
- **既存の `MetricCatalog.from_yaml_with_sha256`（`src/coldaisle/metrics.py`）と `read_declared_changes`
  （`src/coldaisle/declared_changes.py`）は path からファイル全体を上限なしで読む**（前者は `Path.read_bytes()`、後者は EOF までの
  `os.read` のループ）。そのまま呼ぶと上の約束を満たせないので、`train.py` からは呼ばない。実装の PR で、同じ検証を bytes から
  行う API を足す:
  - `MetricCatalog.from_yaml_bytes_with_sha256(payload: bytes, *, origin: str) -> tuple[MetricCatalog, str]`
    （`from_yaml_with_sha256` の本体のうち、読んだ後の decode・`safe_load`・辞書の確認・`model_validate`・`_validate_names`・
    SHA-256 をここへ移す）
  - `parse_declared_changes(payload: bytes, *, origin: str) -> DeclaredChangesInput`（`read_declared_changes` の本体のうち、
    読んだ後の decode・重複した鍵を拒む loader・mapping の確認・`model_validate`・`file_sha256` をここへ移す）

  既存の2つの関数は、いまの読み方で bytes を得てから新しい関数を呼ぶ薄い包みにする。**fand（`control_daemon` の
  `from_yaml_with_sha256`）と `coldaisle-dataset`（`read_declared_changes`）の挙動・例外・digest は変えない**（上限を足すかは
  本記録では決めない。足すならそれぞれの入口の記録で決める）。手順を写して2つにしない
- `code_commit` は `--code-commit` で明示して渡す。CLI は `git` を呼ばない（`subprocess` を使わない）。`--apply` のときは
  必須にする（§5 の 8）

### 2.8 安全の境界（既存の規律の確認）

- `requested_demand`・PWM・hwmon・Authority Stage・制御設定へ届く経路を持たない（AGENTS.md ルール1・2）
- 本番の DB は読み取り専用で開く（`read_training_production`）。書くのは `--out` と、`--apply` のときの Registry だけ
- Critical Safety・Reactive Guard・Fallback は変えない。artifact が runtime で拒否されても Fallback で動く（0079 §2.6）

### 2.9 試験（実機不要）

`tmp_path` と、既存の `tests/test_training_entry.py` の合成の dataset の作り方を使う。`@pytest.mark.hardware` は付けない。

**Registry の状態の検査は、変わったものと変わらなかったものの両方を言う。** 下の「Registry が変わらない」は、`registry.json` の
bytes が前と同じで、`artifacts/` の下にファイルが増えていないことをいう（`register_candidate` を呼ぶ前に止まる場合）。
「登録が確定しない」は、`register_candidate` を呼んで確定点（`registry.json` の置き換え）の前に失敗した場合で、`registry.json`
の bytes と、**参照される** artifact のファイルが前と同じであることをいう。このとき**参照されない artifact のファイルが残ることは
ありうる**: artifact を置いた後に失敗すると `register_candidate` は `_remove_artifact_if_unreferenced` で消そうとするが、これは
best-effort で `OSError` / `UnsafeRegistryPathError` を握る（`src/coldaisle/control/model_registry.py`）。既存の Registry の性質で、
本記録では変えない（§5 の 11）。「candidate が1つ足される」は、`register_candidate` の
約束（`src/coldaisle/control/model_registry.py` の `register_candidate` と `_append_event`）どおり、前の snapshot と比べて
(1) `revision` がちょうど 1 増え、(2) `artifacts` に新しい key が1つだけ足されて状態が `candidate`・metadata が `metadata.json`
と同じで、(3) `audit` の末尾に `REGISTERED` の event が1つだけ足され（`revision`・`artifact`・`actor`・`reason` が一致）、
(4) **`production` の pointer と、それまでの `artifacts` の各記録と `audit` の各 event はすべて前と同じ**であることをいう
（足されたもの以外が1つでも変われば失敗にする）。

- 2回走らせて `artifact.json` と `metadata.json` の bytes が一致する（golden ではなく自己一致。係数の値は固定しない）。
  `metadata.json` の `sha256` が `artifact.json` の SHA-256 と一致すること。`--created-at` だけを変えると bytes が変わること
  （壁時計を読んでいない・引数が効いていることの確認）
- `--apply` 無しでは、既にある Registry が変わらず、無い `--registry-root` を作らないこと
- `--apply` で candidate が1つ足されること（上の (1)〜(4)）。同じ版の2回目は終了コード 1 で、Registry が変わらないこと
- 拒否: 公開物の改ざん・`--replay-path` の取り違え・期間の後の較正の記録・`--calibration` の取り違え・`--out` が既にある・
  `support_axes` に `fan.<zone>` が欠ける。どれも終了コード 1 で `--out` が作られず、`--apply` を付けていても Registry が変わらないこと
- 公開の手順: 手順 3〜5 の途中で例外を起こす（`os.link` を `EEXIST` 以外で失敗させるのを含む）と、後始末が成功して `--out` が
  無いこと。手順 4 の印の一時名を書いて fsync するまでの間に `COMPLETE.json` が名前として存在しないこと（確かめる関数を
  その時点で呼ぶと「印が無い」で拒むこと）。手順 5 の前に他の process の役で `COMPLETE.json` を置いておくと、link が `EEXIST` で失敗して置き換えず、書き手の既知の名前
  だけが消え、`rmdir` が `ENOTEMPTY` になり、段 `cleanup_failed`・終了コード 1・残った名前の一覧（`COMPLETE.json`）がログに出て、
  `--out` と他の process の `COMPLETE.json` が残ること（黙って成功扱いにしないこと）。`unlink` を失敗させたときも同じ段になること。学習を始める前に**空の `--out` を作っておく**と
  手順 2 の `EEXIST` で拒否され、その空のディレクトリが置き換えられずに残ること。`os.rename` / `os.replace` を `--out` の公開に
  使わないこと（差し替えて呼ばれないことを確かめる）
- 親の検査（`_open_output_root` の使い回し）: `--out` の親が group / world に書ける・他の uid の所有・path に symlink がある・
  `..` を含む・無いとき、学習の前に終了コード 1 で拒否し、`--out` が作られないこと
- 信頼できる祖先: 根から `--out` の親までのどこかに (a)(b) を満たさない祖先があると、学習の前に段 `untrusted_path`・終了コード 1
  で拒否し、`--out` が作られないこと。試す形は、group に書ける祖先（実行した uid の所有・`0775`）・sticky でない world に書ける
  祖先（`0777`）・**他の uid の所有**の sticky な祖先（`1777`。中の次の要素は実行した uid の所有でも拒むこと）・sticky な祖先（`1777`）の中の**他の uid の所有**のディレクトリ（`tmp_path` では他の uid を作れないので、
  `fstat` を包んで所有者を差し替えて試す）・他の uid の所有で group / other に書けない祖先は (a) を満たさないこと。逆に、
  システムの一時ディレクトリの実の path（Linux の `/tmp`、macOS の `/private/tmp`。どちらも `1777`・root の所有）の中の実行した
  uid の所有のディレクトリの下と、root の所有の `0755` の祖先は受け入れること。祖先に symlink を含む path（macOS の `/tmp` の形を
  `tmp_path` の中の symlink で模す）は、symlink の先が信頼できる場所でも段 `untrusted_path`・終了コード 1 で拒否され、ログに
  symlink の要素と実の path を渡す指示が出て、`--out` が作られないこと
  開始の後に祖先の mode を `0777` に緩めると、次の照合で `untrusted_path` になること
- path の照合: `--out` の名前だけでなく、**親や祖先の名前を付け替えて**同じ path に別のディレクトリを置いたとき・path の途中の
  要素を、元のディレクトリ（付け替えた先）への **symlink** に差し替えたとき（例: `/tmp/a` を `a-old` に名前を変え `/tmp/a -> a-old`
  を置く。`lstat` の連続や親の fd を使った照合では一致と見逃す形）・最後の `--out` を symlink にしたときも、`ELOOP` / `ENOTDIR` /
  `ENOENT` で不一致になること。`os.lstat` を使わず `O_NOFOLLOW` の `os.open` でたどっていること（差し替えて確かめる）。一時ディレクトリの実の path の下（sticky・world が書ける
  祖先）の `--out` を拒まないこと。`coldaisle-registry register --training-output` でも、印を確かめた後・登録の前に親の名前を
  付け替えると終了コード 1 で Registry が変わらないこと。`register_candidate` の呼び出しを包んで**登録の直後**に親の名前を
  付け替えると、candidate が1つ足され（上の (1)〜(4)）、その bytes が印を確かめた bytes と同じで、段
  `out_replaced_after_register`・終了コード 1・ログに登録した version が出て、`RETIRED` の event が足されないこと（`train --apply`
  でも同じ）
- 中身の再検証: 手順 6 の後・登録の前に `--out` の `artifact.json` を書き換えると、段 `out_modified`・終了コード 1 で Registry が
  変わらないこと。登録で渡る bytes が確かめる関数の返した bytes であること（関数を包んで確かめる）。`register_candidate` の
  直後に書き換えると、candidate が1つ足され（上の (1)〜(4)）、段 `out_modified_after_register`・終了コード 1・ログに version が
  出て、`RETIRED` の event が足されないこと。`--apply` 無しでも、成功の報告の前に書き換えると `out_modified` で終了コード 1
- 親が無い: `--out` の親（または祖先）が無いと、学習の前に終了コード 1 で拒否し、何も作らないこと（親を作らない）。公開の
  関数の `create` の既定（`True`）で Dataset の writer の既存の試験がそのまま通ること
- 名前の差し替え: 手順 2 の後・手順 5 の link の前に `--out` の名前を付け替えて別のディレクトリを置くと、link をせず段
  `out_replaced`・終了コード 1 で、置かれた別のディレクトリに何も書かず消さないこと。手順 6 の後・登録の前に同じことをすると、
  段 `out_replaced`・終了コード 1 で Registry が変わらず、成功を報告しないこと
- 完成の印: 印が無い・書きかけ（JSON が途中）・大きさや SHA-256 がずれた `--out` を、確かめる関数が未完成として拒むこと。
  印の一時名だけがある `--out` も未完成として拒むこと。強制終了の模擬（手順 3 または 4 の後で止めて片づけを走らせない）の後の
  再実行が、`--out` があることと印が無効なことを出して拒むこと
- 公開の後の失敗（手順 6 の一時名の unlink、または `os.fsync` を `--out` か親の fd でだけ失敗させる）: `--apply` 無しでも終了コード 1・段
  `publish_durability`・`--out` が3ファイルと有効な印つきで残ること。`--apply` なら Registry が変わらないこと（登録へ進まない）
- 登録の `RegistryDurabilityError` の2つの意味: snapshot の置き換えの後で起こしたとき（ディレクトリの fsync を失敗させる）は
  candidate が1つ足されていて（上の (1)〜(4)）、登録の後の検査へ進み、段 `register_durability` で終了コード 1。artifact ファイルの
  段で起こしたときは登録が確定せず（`registry.json` と参照される artifact は同じ。片付けが成功すれば参照されない artifact も
  残らず、片付けも失敗させると参照されない artifact が残りうることを、どちらも確かめる）、段 `register` で終了コード 1
- 確定の後の `RegistryDurabilityError`: `registry.json` の置き換えの後に例外を起こし、読み直しの前に同じ ref を `retire`
  （と `validate`）で進めた場合でも、段 `register_durability` に分類され、登録の後の L1〜L12 の検査を行い、再登録を促さないこと。
  同じ版が別の `sha256` で登録済みのときは段 `register` になること
- 公開の後の失敗: 登録の失敗では終了コード 1・段 `register`・`--out` が残り、登録が確定しないこと。登録の後の検査の失敗では
  終了コード 1・段 `post_register_check`・`--out` が残り、candidate が1つ足されたまま（上の (1)〜(4)）で、CLI が `retire` しない
  （`RETIRED` の event が足されない）こと
- `--apply` で `--registry-limits` が無い・不正だと、学習を始める前に終了コード 1 で拒否され、`--out` が作られず Registry が
  変わらないこと
- import の走査（AST で直接の import を読む）: `train` を import する module が無いこと。`train` の直接の import が §2.1 の
  **許可の一覧**（`coldaisle.control.model.*` は前方一致）と標準ライブラリ・`pydantic`・`yaml` に収まり、**禁止の一覧**に1つも
  当たらないこと（`import coldaisle.control` / `from coldaisle.control import ...` も禁止に当たること）。`training_output` を
  import するのが `train` と `registry` だけで、`training_output` の直接の import に `coldaisle` の module が無いこと。推移的な
  import は検査しない（§2.1）
- `coldaisle-registry register --metadata/--payload` の拒否: 手順 3 の後・手順 5 の前（`COMPLETE.json` が無い・`artifact.json` と
  `metadata.json` は完成）の `--out` のファイルを渡すと終了コード 1 で Registry が変わらないこと。公開の後の `--out` でも拒むこと。
  `training-report.json` の無い別のディレクトリのファイルは従来どおり登録できること。手順 3 で `training-report.json` が最初に
  書かれること。学習の出力の外に置いた、学習の出力の中の `artifact.json` / `metadata.json` への **symlink** を `--payload` /
  `--metadata` に渡すと（`COMPLETE.json` の前でも後でも）終了コード 1 で Registry が変わらないこと。symlink のディレクトリを
  親に持つ path（`<symlink のディレクトリ>/artifact.json`）でも、開いた親の fd で `training-report.json` を見つけて拒むこと
- Dataset の読み込み（§2.4 の 1）: `load_training_dataset_artifact_v2` が `coldaisle-dataset` の公開物から、
  `verify_training_dataset_artifact_v2` と同じ束縛（`artifact_id`・`manifest_sha256`・`examples_sha256`）を返すこと。
  `examples.jsonl` が上限＋1 byte・manifest の `examples_sha256` と違う・canonical でない行（空白や鍵の順を変えた同じ値）・
  型に外れる行・directory 名が alias でない・directory か各ファイルが symlink のときは、学習の前に終了コード 1 で拒否され `--out` が
  作られないこと。各ファイルを1回だけ開くこと（読みの後に差し替えても、使った bytes が1回目のものであること）
- 設定の上限: 6つの設定の入力のそれぞれについて、上限＋1 byte のファイルを渡すと学習の前に終了コード 1 で拒否され、`--out` が
  作られず、読んだ byte 数が上限＋1 を超えないこと（`os.read` を包んで数える）。symlink・FIFO を渡しても拒否されること。
  `MetricCatalog.from_yaml_bytes_with_sha256` と `parse_declared_changes` が、既存の `from_yaml_with_sha256` /
  `read_declared_changes` と同じ入力で同じ結果・同じ digest・同じ例外の型を返すこと（既存の試験はそのまま通ること）
- 設定の1回読み: `--quality-config` / `--training-spec` / `--calibration` / `--metrics-config` / `--declared-changes` / `--registry-limits` を、読んだ直後に差し替えても
  （`open` を包んで2回目の読みで別の bytes を返す）、学習・登録に使った設定と報告の digest が1回目の bytes のものであり、
  path から2回読まないこと
- `coldaisle-registry register --training-output` の上限: `COMPLETE.json`・`metadata.json`・`training-report.json` が
  `max_snapshot_bytes`＋1 byte、`artifact.json` が `max_artifact_bytes`＋1 byte のとき（印の大きさもそれに合わせたもの・印は小さいが
  実物が大きいもの）、どれも終了コード 1 で Registry が変わらず、読んだ byte 数が上限＋1 を超えないこと（`os.read` を包んで数える）。
  印に書かれた大きさが上限を超えるときは、そのファイルを開かないこと
- `coldaisle-registry register --training-output`: 有効な印の `--out` から candidate を登録できること。印が無い・書きかけ・
  SHA-256 がずれた `--out` では終了コード 1 で Registry が変わらないこと。`--metadata` / `--payload` と同時に渡すと拒否し、
  Registry が変わらないこと。成功したときは candidate が1つ足され（上の (1)〜(4)）、`--out` の中身が変わらないこと。確かめた後に
  `artifact.json` を差し替えても、登録された artifact の bytes が確かめた bytes であること（差し替えた bytes ではないこと）
- 作った artifact が `check_artifact_contents` を通り、`--apply` の後に `from_verified_artifact`（L1〜L12）を通ること（§2.4 の 7）。
  逆向きも確かめる: `check_artifact_contents` が通らない artifact（たとえば別の較正の値を渡す）では `--out` が作られないこと

### 2.10 実装の段階

| 段 | 内容 |
|---|---|
| 0 | 本記録（Proposed） |
| 1 | `src/coldaisle/train.py`・`src/coldaisle/training_output.py`・`coldaisle-registry register --training-output`・console script・`config/training.example.yaml`・§2.9 の試験。`training_entry` と `docs/thermal-dataset.md` の「学習の CLI はまだ無い」を直す。AGENTS.md のコマンド一覧と `docs/thermal-model.md` に1行足す |
| 2 | （別の記録）thermal model の offline evaluation の形と `offline_evaluation_ref`（§5 の 5） |
| 3 | （別の記録）RL Supervisor の学習の入口（§5 の 6） |

## 3. Consequences

### 良くなること

- 実データから `counterfactual_action` の candidate を作れるようになり、#86 / #89 が実データの段階へ進める
- 0102 / 0112 / 0099 が先送りした必須の引数が、迂回できない形で1つの入口に配線される
- 既定では書かず、書くときも candidate までなので、学習の実行が制御に届く経路が増えない

### 悪くなること・その緩和

- 引数が多い（必須が 11）。→ 既定値を置かないのは既存の規律（0079 §2.3 / 0099 §2.6）。手順は `docs/thermal-model.md` に
  例を1つ書く
- 学習のたびに元の CSV から作り直すので遅い（0112 の費用）。→ 人が手で走らせる頻度の操作なので受け入れる
- 強制終了の後に、印の無い `--out` がその名前のまま残り、人が消すまで同じ `--out` で再実行できない（Dataset の writer なら
  隠れた一時ディレクトリが残るだけ）。→ 再実行の拒否のログに「印が無効」を出し、回復を §2.4 の表に書く。代わりに OS ごとの
  関数を使わずに済み、NFR-07 を変えない（§5 の 10）
- `--apply` があることで、学習した人がそのまま Registry を書ける。→ 書けるのは candidate だけで、production へは
  validate・人の承認ファイル・authority の承認の3段を通る。権限も既存の書き手と同じで増えない

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| `coldaisle-dataset` に学習のサブコマンドを足す | dataset の生成と学習は失敗の意味も入力も違う。0112 の作り直しの検査を dataset を作った同じ process で行うと、検査の独立性が弱まる |
| `coldaisle-registry` に `train` を足す | Registry の CLI は artifact を deserialize も実行もしない（0062 §2.3）。学習はその境界の外 |
| `--apply` で validated まで進める | `offline_evaluation_ref` の中身が決まっていない。決まっても、評価と学習を同じ実行で行うと評価の独立性が無くなる |
| learnd や fand の中で学習する | 制御の process に学習の負荷と書き込み権限を持ち込む。AGENTS.md の役割の分離と 0077 の「worker は提案だけ」に反する |
| 学習の選択を CLI の引数だけで渡す | 報告に spec の digest を1つで残せない。support 軸の列のような構造を引数で渡すと取り違えやすい |
| 壁時計で `created_at` を埋める | 同じ入力から bytes が変わり、作り直して比べる確認ができない |

## 5. 未決事項（人の判断が要る点。推奨を先頭に）

| # | 論点 | 推奨 | 代替 |
|---|---|---|---|
| 1 | Registry への書き込みを `coldaisle-train` に持たせるか | **`--apply` のときだけ `register_candidate` を呼ぶ**（§2.5）。`--out` の同じ bytes なので、後から `coldaisle-registry register` で登録しても同じ結果になる | (a) 持たせない。`--out` に書くだけで、登録は常に `coldaisle-registry register` を人が別に打つ（境界は最も単純だが、取り違えた metadata を登録する余地が残る） |
| 2 | `--apply` の actor の決め方 | **`--actor` / `--reason` を必須の引数にする**（`coldaisle-registry register` と同じ。0062） | (a) 実行した uid の名前から導く（0086 の承認者と同じ形。0086 §5 の未決 2「Registry の承認者も uid に結びつけるか」と一緒に決めるべきで、ここだけ先に変えない） |
| 3 | 学習の選択（split の境界・`ridge_lambda`・support 軸・residual 下限）の置き場所 | **`--training-spec` の YAML**。リポジトリには仮の値の `config/training.example.yaml` だけ | (a) すべて CLI の引数。(b) `config/training.yaml` を実運用の値で置く（split の境界は dataset ごとに違うので、固定の値を置く意味が無い） |
| 4 | `created_at` | **必須の引数**（§2.7） | (a) 省略時は壁時計。報告に書いた値を次回 `--created-at` で渡せば再現できるが、省略すると再現しない実行が既定になる |
| 5 | test split の扱いと thermal model の `offline_evaluation_ref` | **最初の版は test を使わない**（件数と期間だけ報告）。test の残差の報告の形・合否・`validate` への渡し方は別の記録（§2.10 の段 2） | (a) 最初の版で test の残差を `training-report.json` に載せる（合否を決めないまま数字だけが先に出て、validated の根拠と取り違えられる） |
| 6 | RL Supervisor の学習を同じ CLI に入れるか | **入れない**。thermal の candidate が production（`shadow`）になってから別の記録で決める（§2.2） | (a) `coldaisle-train --role supervisor` を今から用意する（0058 §3 のとおり `promotable=False` しか出ず、使い道が無い） |
| 7 | Q-25（本番でオンライン学習を行うか） | **本記録では行わない**（人が手で実行するだけ。timer を作らない）。Q-25 は #93 / #104 で開いたまま | (a) drift の推奨（0056）を受けて自動で学習・candidate 登録する timer を作る（昇格は人なので制御には届かないが、candidate が増え続ける） |
| 8 | `--code-commit` | **`--apply` のときだけ必須**、`--out` だけなら任意 | (a) 常に任意（Registry の `code_commit` は `null` を許す）。(b) 常に必須 |
| 9 | （申し送り）`write_dataset` にも、確認と `os.rename` の間に作られた空のディレクトリを置き換える同じ窓がある | **本記録では扱わない。** 直すなら 0031 §3（macOS / Ubuntu 共通の `os.rename`）を置き換える別の決定記録で扱う | — |
| 10 | `--out` の公開の手段（NFR-07「macOS / Ubuntu 双方で動作。OS依存はシリアルデバイス検出のみに閉じる」との関係） | **(A) OS に依らない手順**（§2.5 の本文）。`os.mkdir` で `--out` を排他的に確保し、中に書いてから完成の印を一時名で書いて fsync し、`os.link` で `COMPLETE.json` として公開する（既にあれば失敗）。利点: POSIX 共通の操作だけで NFR-07 を変えない・ctypes も OS の分岐も要らず CI（Ubuntu）で全経路を実物で試験できる・`mkdir` の排他で置き換えの窓が無い（前回の指摘を満たす）・0031 §3 の「共通の POSIX の操作だけ」と一貫する。欠点: 強制終了の後に `COMPLETE.json` の無い `--out` が残り、人が消すまで再実行できない・読む側が印を確かめる必要がある（新しい `coldaisle-registry register --training-output` は Registry を開く前に `COMPLETE.json` を確かめる。印を見ないのは既存の `--metadata` / `--payload` の経路だけで、その経路も `register_candidate` の checksum で書きかけを拒む） | (B) NFR-07 を変えて OS ごとの置き換えない rename を使う（Linux `renameat2(RENAME_NOREPLACE)`・macOS `renameatx_np(RENAME_EXCL)`・使えなければ失敗。ctypes の小さな関数1つ）。NFR-07 の「OS依存はシリアルデバイス検出のみに閉じる」を「…シリアルデバイス検出と、決定記録 0150 の出力の公開（置き換えない rename）に閉じる」へ変え、同じ PR で `docs/requirements.md` を直す。利点: `--out` は「無い」か「完成」のどちらかで、強制終了の後に残るのは隠れた一時ディレクトリだけ・読む側に印の確認が要らない。欠点: 要件を変える・macOS の分岐は Ubuntu の CI では差し替えでしか試験できない・ファイルシステムが対応しないと使えない・0031 §3 の writer（共通の `os.rename`）と手段が分かれる |
| 11 | （申し送り）Registry の登録の失敗で、参照されない artifact のファイルが残りうる（`_remove_artifact_if_unreferenced` が best-effort）。`coldaisle-registry` に片付けの入口は無い | **本記録では扱わない**（Registry の書き方と片付けは 0062 / 0104 の範囲）。必要なら Registry の片付けの入口を別の記録で決める | — |
| 12 | 設定の入力（§2.7 の6つ）を読む上限の値と置き場所。**決定済み（所有者、2026-10-16）: 推奨のとおり** | **`train.py` の構造上の定数 `MAX_TRAINING_CONFIG_BYTES = 1 MiB`**（6つに共通）。運用の閾値ではなくメモリを守る構造上の上限で、既存の `MAX_ARTIFACT_BYTES`（`control/model/thermal.py`）・`_MAX_JOURNAL_BYTES`（`control/authority.py`）・`MAX_DECLARED_CHANGES` と同じ扱い。いまの設定は数 KB で、宣言の YAML も `MAX_DECLARED_CHANGES` = 64 件に収まる。利点: `--apply` の無い実行でも追加の設定が要らず、上限を決めるファイル自身の読みに上限が要るという循環が無い | (a) `--registry-limits` を常に必須にし、その `max_snapshot_bytes` を使う（0062 §2.1 と同じ値）。欠点: `--out` だけの実行にも Registry の設定が要り、`model-registry.yaml` 自身は別の上限で読む必要が残る。(b) `--training-spec` に置く。欠点: spec 自身の読みに上限が要る |
| 13 | macOS の `/tmp`・`/var` など、OS が置く symlink を含む `--out`。**決定済み（所有者、2026-10-16）: 推奨のとおり** | **受け入れない**（§2.5 の「path の照合」）。symlink を含まない実の path（`/private/tmp/...`）を人が渡し、CLI は拒否のログでそれを示す。利点: 解く処理が無く、照合の窓が増えない・Dataset の writer（`_open_output_root`）と挙動が揃う・OS ごとの分岐が無い（NFR-07） | (a) root の所有の `/` の中の root の所有の symlink（`/tmp`・`/var`・`/etc`）だけを、`fstat` と `readlink` で確かめて一度だけ解き、先を fd で開いて照合を続ける。欠点: 別名の一覧（macOS の分岐）を持つ・Ubuntu の CI では模してしか試験できない |
| 14 | 既存の `coldaisle-registry register --metadata/--payload` が最後の要素の symlink をたどって読む挙動 | **拒否する**（§2.5。親の fd を1回開き、最後の要素を `O_NOFOLLOW` で開く。学習の出力の判定を開いた項のディレクトリに束縛する）。既存の入口を狭める変更で、RL policy などの登録で `--payload` に symlink を渡していた運用は、実のファイルを渡す形に変わる | (a) symlink を許したまま、開いた fd の `(st_dev, st_ino)` と、隣の `artifact.json` / `metadata.json` の `fstat` を比べる。欠点: 実の親を fd から得る手段が OS ごとに違い（Linux の `/proc/self/fd`・macOS の `F_GETPATH`）、NFR-07 に反する |
