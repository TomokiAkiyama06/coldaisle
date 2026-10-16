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
- `train.py` が import してよいのは、上の表の関数・型、`store`（読み取り）・`ingest`（`training_entry` 経由の再生）・
  `control/model_registry`・`training_output`（§2.5）・`logs` だけ。`control/hardware`・`control/safety`・`control/reactive`・`control/loop`・
  `serial`・`subprocess`・`ai` を import しない（試験で走査する。§2.9）
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
| 公開の前の強制終了（`SIGKILL`・電源断） | — | **`COMPLETE.json` の無い `--out`**（中のファイルは途中まで。印の一時名が残ることもある）。link の後・`--out` の fsync の前の電源断でも、link が永続でなければこの状態に戻りうる | 再実行は `--out` があるので拒否され、そのとき `--out` に有効な完成の印が無いことをログに出す。人が確かめて `--out` を消し、同じ引数で再実行する。CLI は自動では消さない（自分が作ったものか確かめられないため） |
| 公開の後・一時名の unlink と `--out` と親の fsync の失敗（公開の手順 6。`--apply` の有無に依らない） | 1 | `--out`（3ファイルと有効な `COMPLETE.json`。中身はすべて fsync 済みで、永続でないのはディレクトリの項だけ。unlink の失敗なら印の一時名も残るが、確かめる関数は見ない）。`--apply` なら Registry へは進まない | ログに段 `publish_durability` と `--out` の path を出す。中身は完成しているので、そのまま使ってよい（`--apply` なら `coldaisle-registry register --training-output <--out>` で登録する）。電源断の後に `--out` が無い・印が無効なら、消して同じ引数で再実行する（その前に並行の `register` で同じ版の candidate が登録されていれば、Registry 側の bytes は Registry が fsync している。`coldaisle-registry status` で確かめ、あれば作り直さない） |
| 公開の後・Registry の登録の失敗（lock・revision・同じ版が登録済み・権限・limits、および artifact ファイルの段の `RegistryDurabilityError`） | 1 | `--out`（3ファイルと完成の印）。Registry は変わらない（登録の確定点の前） | `coldaisle-registry register --training-output <--out>` で登録する。同じ版が登録済みなら `--model-version` を上げて別の `--out` で作り直す |
| 登録は確定したが永続が確かでない（`registry.json` の置き換えの後の `RegistryDurabilityError`。下記） | 1 | `--out` と、Registry の **candidate**（見えている。production は動かない） | CLI はそのまま**登録の後の検査（`load_version` と L1〜L12）へ進む**。検査が通ってもログに段 `register_durability` を出して終了コード 1。人は `coldaisle-registry status` で candidate があることを確かめる。電源断の後に無くなっていたら、`coldaisle-registry register --training-output <--out>` で登録する。**もう一度 `coldaisle-train --apply` で登録し直さない**（見えている間は「既にある」になる） |
| 登録の後・`load_version` と L1〜L12 の検査の失敗 | 1 | `--out` と、Registry の **candidate**（production は動かない） | ログに artifact の key と `--out` の path を出す。人が原因を確かめ、`coldaisle-registry retire` で candidate を退ける。CLI は `retire` しない |

構造化ログには失敗した段（`before_publish` / `cleanup_failed` / `publish_durability` / `register` / `register_durability` /
`post_register_check`）を必ず出し、どの行に当たるかを人が取り違えないようにする。

### 2.5 出力と、既定では Registry を書かないこと

`--out` のディレクトリに次の3つと完成の印を置く。`--out` が**既にあれば拒否する**（上書きしない）。

| ファイル | 中身 |
|---|---|
| `artifact.json` | 登録する canonical bytes（§2.4 の 6） |
| `metadata.json` | `ArtifactMetadata` の strict JSON（`coldaisle-registry register --metadata` へそのまま渡せる形） |
| `training-report.json` | §2.7 の digest の一覧・split ごとの件数と期間・artifact の SHA-256・Profile の SHA-256・除いた example の件数 |

#### 公開の手順（`--out` を最初に排他的に作り、最後に完成の印を付ける。OS に依らない）

**推奨案（§5 の 10 の (A)）で書く。** 使うのは macOS / Ubuntu で共通の POSIX の操作だけで、OS ごとの関数を呼ばない
（NFR-07 を変えない）。

1. `--out` の親を `O_DIRECTORY | O_NOFOLLOW` で開く
2. **`os.mkdir(--out の名前, 0o700, dir_fd=親)` で `--out` を排他的に確保する。** 既にあれば `EEXIST` で拒否する。`mkdir` は
   POSIX で原子的で、既にあるもの（他の process が作った空のディレクトリを含む）を**置き換えない**。「既にある出力は必ず拒む」
   の正しさはこの1手が持ち、確認と公開の間の窓（rename による置き換え）がそもそも無い。作った直後に `--out` を
   `O_DIRECTORY | O_NOFOLLOW` で開き、path と fd の `(st_dev, st_ino)` が同じこと・中が空であることを確かめる（差し替えの検知）。
   以後は fd（`dir_fd`）だけで書く
3. 3つのファイルを `--out` の中へ `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` で書き、**それぞれ fsync** する
4. **完成の印を一時名で書く。** 3つのファイルの名前・大きさ・SHA-256 を、`--out` の中の一時名
   `.COMPLETE.json.tmp-<32 hex>` へ `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW` で書き、fsync する。読む側はこの名前を見ない
5. **`os.link(一時名, "COMPLETE.json", src_dir_fd=--out, dst_dir_fd=--out, follow_symlinks=False)` で印を公開する。**
   `link` は既にある名前を置き換えず `EEXIST` で失敗する（POSIX 共通。NFR-07 を変えない）。**この link が公開の確定点**で、
   `COMPLETE.json` は中身が fsync 済みの状態でだけ名前として現れる。`O_CREAT` で直接作ると、中身を書いて fsync するまでの間に
   名前が見えて、並行する `register --training-output` が確かめて登録したのに書き手が後で片づける、という食い違いが起きる
6. 一時名を unlink し、`--out` を fsync し、親を fsync する（`COMPLETE.json` の項と `--out` の項を永続にする）。ここでの失敗は
   公開の**後**の失敗（§2.4 の表）で、片づけない
7. 手順 2〜5 で例外が起きたとき（link の失敗を含む）は、自分で作った `--out` の中の**既知の名前だけ**（3つのファイル・
   印の一時名）を unlink し、`--out` を rmdir する（手順 2 で確かめた `(st_dev, st_ino)` と同じときだけ）。知らない名前
   （他の process が置いた `COMPLETE.json` など）は消さない。後始末の例外は**握りつぶさない**: 元の例外を原因として保ったまま、
   後始末の失敗（`unlink` / `rmdir` の `errno` と、`--out` に残った名前の一覧）を構造化ログに出し、段 `cleanup_failed` で
   終了コード 1 にする（§2.4 の表）。`suppress(OSError)` で黙って捨てない（AGENTS.md「例外を握りつぶさない」）

**読む側は、有効な `COMPLETE.json` の無い `--out` を未完成として拒む。** 有効とは、`COMPLETE.json`（一時名ではない）が
JSON として読めて、書かれた3つのファイルの大きさと SHA-256 が実物と一致することである。link で公開するので `COMPLETE.json` が
書きかけで見えることは手順の上では無いが、読む側は書き手を信用せず毎回この照合をする（ファイルの書きかけや後からの
差し替えもこれで拒める）。確かめる関数は印に書かれた3つのファイルと `COMPLETE.json` だけを読み、他の名前は見ない。

- `--apply` の登録は手順 6 が成功した後に、メモリ上の同じ bytes で行う（印を読み直さない）
- 手動の登録と回復は **`coldaisle-registry register --training-output <--out>`** で行う（`--metadata` / `--payload` と
  排他）。`register` は下記の確かめる関数で印を確かめ、**確かめたときに読んだ bytes そのもの**（`artifact.json` と
  `metadata.json`）を `register_candidate` へ渡す（確かめた後に読み直さない。差し替えの窓を作らない）。印が無い・無効なら
  終了コード 1 で Registry を開かない。既存の `--metadata` / `--payload` の経路（RL policy など学習の CLI の外の artifact）は
  変えない。印を確かめずに `--metadata` / `--payload` で渡しても、`register_candidate` は payload の SHA-256 を metadata と
  照合し、metadata は strict JSON として読むので、書きかけのファイルが黙って candidate になることは無い
- **確かめる関数の置き場所は、レイヤ横断の小さなモジュール `src/coldaisle/training_output.py`**（仮の名前）とする。
  印の形（`COMPLETE.json` の pydantic の型）・書く側の公開の手順（§2.5 の手順 2〜7）・読む側の確かめる関数
  （`O_DIRECTORY | O_NOFOLLOW` で開き、`dir_fd` で各ファイルを上限つきで読み、大きさと SHA-256 を照合して bytes を返す）を
  1か所に持つ。import するのは `train.py` と `registry.py`（どちらも合成の起点）だけで、標準ライブラリと pydantic の外の
  coldaisle の module を import しない。根拠:
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
を受けたら、同じ limits で Registry を読み直し（`load_version`）、**その ref が candidate として、`metadata.json` と同じ
`sha256` で見えているか**で分ける。見えていれば「確定したが永続が確かでない」として登録の後の検査へ進み、見えていなければ
登録の失敗として扱う（§2.4 の表）。それ以外の `ModelRegistryError` は登録の失敗である。Registry 側の書き方は本記録では
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
  `support_axes` に `fan.<zone>` が欠ける。どれも `--out` が作られない
- 公開の手順: 手順 3〜5 の途中で例外を起こす（`os.link` を `EEXIST` 以外で失敗させるのを含む）と、後始末が成功して `--out` が
  無いこと。手順 4 の印の一時名を書いて fsync するまでの間に `COMPLETE.json` が名前として存在しないこと（確かめる関数を
  その時点で呼ぶと「印が無い」で拒むこと）。手順 5 の前に他の process の役で `COMPLETE.json` を置いておくと、link が `EEXIST` で失敗して置き換えず、書き手の既知の名前
  だけが消え、`rmdir` が `ENOTEMPTY` になり、段 `cleanup_failed`・終了コード 1・残った名前の一覧（`COMPLETE.json`）がログに出て、
  `--out` と他の process の `COMPLETE.json` が残ること（黙って成功扱いにしないこと）。`unlink` を失敗させたときも同じ段になること。学習を始める前に**空の `--out` を作っておく**と
  手順 2 の `EEXIST` で拒否され、その空のディレクトリが置き換えられずに残ること。`os.rename` / `os.replace` を `--out` の公開に
  使わないこと（差し替えて呼ばれないことを確かめる）
- 完成の印: 印が無い・書きかけ（JSON が途中）・大きさや SHA-256 がずれた `--out` を、確かめる関数が未完成として拒むこと。
  印の一時名だけがある `--out` も未完成として拒むこと。強制終了の模擬（手順 3 または 4 の後で止めて片づけを走らせない）の後の
  再実行が、`--out` があることと印が無効なことを出して拒むこと
- 公開の後の失敗（手順 6 の一時名の unlink、または `os.fsync` を `--out` か親の fd でだけ失敗させる）: `--apply` 無しでも終了コード 1・段
  `publish_durability`・`--out` が3ファイルと有効な印つきで残ること。`--apply` なら Registry へ進まないこと
- 登録の `RegistryDurabilityError` の2つの意味: snapshot の置き換えの後で起こしたとき（ディレクトリの fsync を失敗させる）は
  candidate が見えていて、登録の後の検査へ進み、段 `register_durability` で終了コード 1。artifact ファイルの段で起こしたときは
  Registry が変わらず、段 `register` で終了コード 1
- 公開の後の失敗: 登録の失敗・登録の後の検査の失敗で終了コード 1、`--out` が残り、ログに段が出ること（§2.4 の表）
- `--apply` で `--registry-limits` が無いと引数の段で拒否され、何も書かないこと
- import の走査: `train` を import する module が無いこと、`train` が §2.1 の禁止の module を import しないこと。
  `training_output` を import するのが `train` と `registry` だけで、`training_output` が coldaisle の他の module を import しないこと
- `coldaisle-registry register --training-output`: 有効な印の `--out` から candidate を登録できること。印が無い・書きかけ・
  SHA-256 がずれた `--out` では終了コード 1 で Registry が変わらないこと。`--metadata` / `--payload` と同時に渡すと拒否すること。
  確かめた後に `artifact.json` を差し替えても、登録されるのは確かめた bytes であること
- 作った artifact が `check_artifact_contents` を通り、`--apply` の後に `from_verified_artifact`（L1〜L12）を通ること（§2.4 の 7）

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
