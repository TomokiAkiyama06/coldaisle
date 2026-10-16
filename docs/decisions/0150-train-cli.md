# 決定記録 0150: 学習の CLI `coldaisle-train`（Thermal Dataset v2 から反実仮想 Thermal Model artifact v2 を作り、Model Registry へ candidate として登録する入口）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし（既存の決定が「学習の CLI の PR で行う」と先送りした配線を、どの形で行うかを決める。
  [0102](0102-calibration-change-log-settled-points.md) §2.3・[0112](0112-training-replay-path-required.md) §2.1 / §2.2 の 3・
  [0099](0099-calibration-change-log.md) §2.6・[0096](0096-calibration-binding-digest.md) §5 #4 の決定は置き換えない）
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
- `train.py` が import してよいのは、上の表の関数・型、`store`（読み取り）・`ingest`（`training_entry` 経由の再生）・
  `control/model_registry`・`logs` だけ。`control/hardware`・`control/safety`・`control/reactive`・`control/loop`・
  `serial`・`subprocess`・`ai` を import しない（試験で走査する。§2.9）
- **LLM に依らない。** AI 層のツールにも読み取り API にも出さない（AGENTS.md ルール1。0062 §2.1 と同じ）
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

1. `verify_training_dataset_artifact_v2`: 公開物の bytes と directory 名の alias に束縛する
2. `verify_training_dataset_v2`: 元の入力の fingerprint・export の束縛・本番の DB の行と trace を照合し、一時の専用 DB で
   作り直した bytes が一致することを求める（0112 §2.3 / 0116）
3. `verify_training_calibration`: 期間の終わり（全 example の `label_end_ms` の最大）より後に較正の記録があれば拒否し、
   `--calibration` の写像が期間に効いていた行と一致しなければ拒否する（0099 §2.6 / 0096 §5 #4）。さらに
   `--declared-changes` の `calibration_changed` が期間の後にあっても拒否する（0109 の和集合と同じ向き）
4. `split_temporally_v2`（`--training-spec` の境界）
5. `train_counterfactual_ridge`（`train` だけ）→ `fit_confidence_profile_v2`（範囲は `train`、residual の基準は
   `validation`）→ `seal_counterfactual_artifact`
6. `canonical_counterfactual_artifact_bytes` と `counterfactual_registry_metadata`
7. **読み込み側の検査を作った bytes に通す。** 作った bytes を読み直し、loader と同じ内容の検査
   （`check_artifact_contents` を `--calibration` の値と `--metrics-config` つきで。L9 の較正の digest を含む）を通してから
   書く。Registry の封筒（checksum・metadata の全欄照合）に依る検査は、`--apply` で登録した直後に `load_version` で
   読み直し、`RegistryCounterfactualThermalModel.from_verified_artifact`（L1〜L12）を通して確かめる。作れたが runtime で
   読めない artifact を candidate のまま気づかずに残さない（登録後に通らなければ終了コード 1 と、その artifact の
   `retire` を人へ促すログ。CLI は `retire` しない。§2.5）

`test` は学習にも Profile にも使わない。件数と期間を報告に載せるだけ（§5 の 5）。

失敗は構造化ログに理由を出して**終了コード 1**。「何も残さない」の約束は **`--out` の公開（§2.5 の手順 6）の前の失敗**に
限る。本節の 1〜7（7 は `check_artifact_contents` まで）の失敗と、公開の手順の 1〜6 の失敗では、`--out` は作られない
（ファイルごとの rename はしない）。

公開の後の失敗（`--apply` のときだけ起きる）は、`--out` を消さずに残す。消すと、Registry に登録済みかもしれない bytes の
唯一の手元の写しを失うためである。

| 失敗した段 | 終了コード | 残るもの | 回復 |
|---|---|---|---|
| 公開の前（検査・学習・公開の手順 1〜6） | 1 | 何も残らない（強制終了なら親の `.coldaisle-train-stage-*` だけ） | 原因を直して同じ引数で再実行 |
| 公開の後・Registry の登録の失敗（lock・revision・同じ版が登録済み・権限・limits） | 1 | `--out`（3ファイル）。Registry は変わらない（登録の確定点の前） | `--out` の `artifact.json` と `metadata.json` を `coldaisle-registry register` へ渡す。同じ版が登録済みなら `--model-version` を上げて別の `--out` で作り直す |
| 登録の後・`load_version` と L1〜L12 の検査の失敗 | 1 | `--out` と、Registry の **candidate**（production は動かない） | ログに artifact の key と `--out` の path を出す。人が原因を確かめ、`coldaisle-registry retire` で candidate を退ける。CLI は `retire` しない |

構造化ログには失敗した段（`before_publish` / `register` / `post_register_check`）を必ず出し、どの行に当たるかを
人が取り違えないようにする。

### 2.5 出力と、既定では Registry を書かないこと

`--out` のディレクトリに次の3つを置く。`--out` が**既にあれば拒否する**（上書きしない）。

| ファイル | 中身 |
|---|---|
| `artifact.json` | 登録する canonical bytes（§2.4 の 6） |
| `metadata.json` | `ArtifactMetadata` の strict JSON（`coldaisle-registry register --metadata` へそのまま渡せる形） |
| `training-report.json` | §2.7 の digest の一覧・split ごとの件数と期間・artifact の SHA-256・Profile の SHA-256・除いた example の件数 |

#### 公開の手順（3つを1回の rename で出す）

ファイルごとに一時ファイルから rename すると、1つ目の rename の後に process が強制終了したとき一部だけが `--out` に残り、
再実行も「既にある」で拒まれる。そこで既存の Dataset の writer（`src/coldaisle/dataset.py` の `write_dataset`）と同じ手順で、
**ディレクトリ全体を1回の rename で公開する**（ただし置き換えない rename。手順 6）。

1. `--out` の親を `O_DIRECTORY | O_NOFOLLOW` で開き、親の中の writer 用の lock ファイルを `flock(LOCK_EX)` する
   （実行した uid の所有の通常ファイル・group / other が書けない・link 数 1 を確かめる。`_lock_output_root` と同じ）。
   `--out` の不存在の確認と rename を、同じ親へ書く他の writer と直列にする
2. `--out` が無いことを確かめる（あれば拒否）
3. **同じ親の中**（同じファイルシステム）に `0700` の一時ディレクトリ `.coldaisle-train-stage-<32 hex>` を作る
   （推測できない名前。衝突したら作り直す）
4. 3つのファイルを一時ディレクトリへ `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` で書き、**それぞれ fsync** する
5. **一時ディレクトリを fsync** し、開いた fd と path の `(st_dev, st_ino)` が同じことを確かめる（差し替えの検知）
6. **置き換えない rename** で公開する: Linux の `renameat2(親の fd, 一時ディレクトリ, 親の fd, --out, RENAME_NOREPLACE)`
   を ctypes で libc から呼ぶ。`--out` が既にあれば `EEXIST` で失敗し、拒否する。POSIX の `os.rename` は、確認と rename の
   間に（lock に協調しない同じ uid の process が）作った**空のディレクトリを黙って置き換える**ので使わない。
   writer の lock は協調する書き手どうしにしか効かず、この窓を閉じない。`renameat2` が libc に無い・`ENOSYS`・`EINVAL`
   （ファイルシステムが対応しない）のときは**失敗にする**（`os.rename` へ落とさない）。リポジトリには同じ用途の既存の
   実装が無いので、共有の部品として1か所に置く（下記）
7. **親を fsync** する（rename したディレクトリの項を永続にする）
8. 失敗したとき（公開の前）は、自分で作った一時ディレクトリの**既知の3つの名前だけ**を unlink して rmdir する。
   後始末の失敗は元の例外を隠さない（`suppress(OSError)`）。`--out` には何も現れない。手順 2 の確認は早く拒否するための
   もので、拒否の正しさは手順 6 の `RENAME_NOREPLACE` が持つ

process が強制終了したときに残りうるのは親の中の `.coldaisle-train-stage-*` だけで、`--out` は「無い」か「3つがそろって
ある」のどちらかになる。残った一時ディレクトリは名前が一意なので再実行を妨げない。自動では消さない（Dataset の writer と
同じ。人が確かめて消す）。手順は Dataset の writer と共有する（実装の PR で `dataset.py` の非公開の関数を、ファイル名の
組を引数に取る共有の部品へ移す。手順を写して2つにしない）。いまの `write_dataset` も `os.rename` を使っており同じ窓を
持つ。共有の部品へ移すときに Dataset の writer も `RENAME_NOREPLACE` にするかは §5 の 9。

#### `--apply` の Registry 側の書き込み

`ModelRegistry.register_candidate` は、artifact を `artifacts/<kind>/<model_id>/<version>/` へ一時ファイルと
`os.replace` で書いてから、`registry.json` を同じく原子的に置き換える。**`registry.json` の置き換えが登録の確定点**で、
その前に強制終了しても残るのは参照されない artifact だけである（snapshot が無い bytes を指す状態は起きない）。
同じ版の再登録は同じ path へ `os.replace` で上書きするので、残った artifact が再実行を妨げない。したがって Registry 側に
同じ問題は無く、本記録では変えない。

順序は **`--out` の公開 → Registry の登録**とする。2つの間で止まったときは `--out` だけがあり、`coldaisle-train` の
再実行は `--out` が既にあるので拒否される。このときは `--out` の `artifact.json` と `metadata.json` を
`coldaisle-registry register` へ渡せば同じ bytes が登録される（§5 の 1 の推奨の理由と同じ）。登録の失敗は構造化ログに
`--out` の path を出し、この手順を促す。

- **既定では Registry を書かない。** `--out` へ書いて終わる（`coldaisle-calibrate` / `coldaisle-memory` の「既定では
  書かない」と同じ流儀）
- `--apply --registry-root <dir> --registry-limits <dir> --actor <名前> --reason <文>` を付けたときだけ、`artifact.json` と `metadata.json` と
  **同じ bytes** を `ModelRegistry.register_candidate` へ渡す（§5 の 1 / 2）
- **`--registry-limits <dir>` は `--apply` のとき必須**（`model-registry.yaml` のあるディレクトリ。既定値を置かない）。
  `ModelRegistry` は `ModelRegistryLimits` を要し、その値は `config/model-registry.yaml` にだけあってコードに既定値が無い。
  `load_model_registry_limits` で1回読み、**登録（`register_candidate`）と登録の後の `load_version`（§2.4 の 7）の両方に
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
  `--declared-changes` / `--quality-config` / `--metrics-config` の bytes の SHA-256・再生の入力の `source_sha256` と
  `export_binding_sha256`。artifact の manifest が既に持つもの（`training_data`・`calibration_binding`）は重ねて計算しない
- `code_commit` は `--code-commit` で明示して渡す。CLI は `git` を呼ばない（`subprocess` を使わない）。`--apply` のときは
  必須にする（§5 の 8）

### 2.8 安全の境界（既存の規律の確認）

- `requested_demand`・PWM・hwmon・Authority Stage・制御設定へ届く経路を持たない（AGENTS.md ルール1・2）
- 本番の DB は読み取り専用で開く（`read_training_production`）。書くのは `--out` と、`--apply` のときの Registry だけ
- Critical Safety・Reactive Guard・Fallback は変えない。artifact が runtime で拒否されても Fallback で動く（0079 §2.6）

### 2.9 試験（実機不要）

`tmp_path` と、既存の `tests/test_training_entry.py` の合成の dataset の作り方を使う。`@pytest.mark.hardware` は付けない。

- 2回走らせて `artifact.json` の bytes が一致する（golden ではなく自己一致。係数の値は固定しない）
- `--apply` 無しでは Registry のディレクトリを作らず、ファイルが1つも増えない
- `--apply` で candidate になり、production pointer と revision 以外の記録が変わらない。同じ版の2回目は終了コード 1
- 拒否: 公開物の改ざん・`--replay-path` の取り違え・期間の後の較正の記録・`--calibration` の取り違え・`--out` が既にある・
  `support_axes` に `fan.<zone>` が欠ける。どれも `--out` が作られず、親に一時ディレクトリも残らない
- 公開の手順: 一時ディレクトリへ書いた後・rename の前に例外を起こしても `--out` が無いこと。手順 2 の後・rename の前に
  **空の `--out` ディレクトリを lock を取らずに作る**と、`EEXIST` で拒否され、その空のディレクトリが置き換えられずに残ること。
  `renameat2` が使えない（ctypes の関数を差し替えて `ENOSYS` を返す）ときに失敗し、`os.rename` へ落ちないこと
- 公開の後の失敗: 登録の失敗・登録の後の検査の失敗で終了コード 1、`--out` が残り、ログに段が出ること（§2.4 の表）
- `--apply` で `--registry-limits` が無いと引数の段で拒否され、何も書かないこと
- import の走査: `train` を import する module が無いこと、`train` が §2.1 の禁止の module を import しないこと
- 作った artifact が `check_artifact_contents` を通り、`--apply` の後に `from_verified_artifact`（L1〜L12）を通ること（§2.4 の 7）

### 2.10 実装の段階

| 段 | 内容 |
|---|---|
| 0 | 本記録（Proposed） |
| 1 | `src/coldaisle/train.py`・console script・`config/training.example.yaml`・§2.9 の試験。`training_entry` と `docs/thermal-dataset.md` の「学習の CLI はまだ無い」を直す。AGENTS.md のコマンド一覧と `docs/thermal-model.md` に1行足す |
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
| 9 | Dataset の writer（`write_dataset`）も `RENAME_NOREPLACE` にするか | **する**（共有の部品へ移す実装の PR で。拒否を強めるだけで、通っていた正しい実行は変わらない） | (a) 学習の CLI だけにし、Dataset の writer は別の Issue で直す |
