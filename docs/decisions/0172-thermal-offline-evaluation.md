# 決定記録 0172: 反実仮想 Thermal Model の offline 評価の形と `offline_evaluation_ref`（読み取り専用の評価の CLI・未学習の baseline との比較・合否の置き場所・Registry の検証）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-26、リポジトリ所有者の承認。§5 の 1〜11 はすべて推奨の案）
- **Date**: 2026-10-26
- **Supersedes**: [0062](0062-model-registry-operations.md) §2.1 の `validate` のうち、
  `kind` = `thermal_model` かつ `capability` = `counterfactual_action` の artifact を自由記述の `--offline-evaluation-ref` で
  validated にできる点**のみ**を置き換える（0150 §5 の 14 が `register` について行ったのと同じ範囲の置き換え。0062 の他の点は変えない）。
- **関連**: [0031](0031-thermal-dataset-contract.md) §2.5 / [0048](0048-thermal-model-artifact-and-inference.md) §1 / §5 /
  [0050](0050-model-confidence-ood-and-authority.md) §5 の 1・2 / [0054](0054-offline-evaluation-attribution-and-gates.md) §2.4〜§2.8 /
  [0056](0056-model-drift-detection-and-retraining-triggers.md) / [0057](0057-authority-rollout-stage-changes.md) /
  [0061](0061-rl-supervisor-policy-artifact-and-binding.md) / [0062](0062-model-registry-operations.md) §2.1〜§2.3 /
  [0074](0074-supervisor-shadow-wiring-and-episode-evaluation.md) §2.3 / [0079](0079-model-artifact-formats.md) §2.3 / §5 の 5 /
  [0084](0084-model-artifact-anchor-support.md) / [0087](0087-dataset-v2-action-grid.md) / [0098](0098-registry-binding-settled-points.md) /
  [0104](0104-authority-raise-least-privilege.md) / [0105](0105-registry-shared-root-settled-points.md) /
  [0109](0109-dataset-declared-changes-cli.md) / [0150](0150-train-cli.md) §2.2 / §2.6 / §2.10 / §5 の 5 /
  [0151](0151-load-state-distribution-cli.md) / `docs/thermal-model.md` / `docs/model-registry.md` / `docs/offline-evaluation.md` /
  AGENTS.md「絶対に守るルール」1・2・4・7・9
- **対象 Issue**: #84（関連: #85 / #86 / #89 / #91 / #92 / #104 / #217）

---

## 1. Context

### 1.1 何が止まっているか

`coldaisle-train`（0150。段 1 は実装済み）で、実データから反実仮想 Thermal Model artifact v2 を candidate として登録できる
ようになった。しかし candidate から先へ進む根拠が無い。

| 止まっているもの | 根拠 |
|---|---|
| validated にできない | `ModelRegistry.mark_validated()` は `offline_evaluation_ref` を必須にし、snapshot の検証も「validated の audit には `offline_evaluation_ref` が要る」「production には `offline_evaluation_ref` と `shadow_evaluation_ref` が要る」を課す（`src/coldaisle/control/model_registry.py` の `RegistrySnapshot` の lifecycle の再現（747 行付近）と `promote()`（1491 行付近）） |
| ところが中身が決まっていない | Registry が確かめるのは「1〜500 文字の文字列」だけ（`_validate_reference`）。`coldaisle-registry validate --offline-evaluation-ref` は任意の文字列を受け取る（`src/coldaisle/registry.py`）。0150 §2.6 / §5 の 5 は thermal model の `offline_evaluation_ref` に何を入れるかを「別の記録（§2.10 の段 2）」へ先送りした |
| #84 の production | 受入基準「未学習 baseline より有意に未来温度を予測できる」「MAE / RMSE だけでなく worst-case / underprediction」「workload 別・室温帯別」「future leakage 禁止」を判定する手順が無い（0048 §1 が「実データなしには判定できない」とし §5 に残した） |
| #217 の C2 | `docs/ubuntu-deploy.md` 6.7 の手順 9 の C2 は「Registry に thermal_model の Production artifact が初めてできた後」 |
| #86 / #89 の実データの段階 | fand は production だけを読む（0077 §2.6 / 0098）。RL の探索は反実仮想 artifact が production に無い間は `promotable=False` しか出ない（0058 §3 / 0150 §2.2） |
| #85 の閾値（0050 §5 の 1・2） | `model_confidence` の値・support 軸の境界を決める材料（実データでの誤差の分布）が無い |
| #92 の rollout | Authority を上げる前提の production が無い |

Supervisor policy には既に専用の validated の入口がある（`validate_supervisor_policy()`。0074 §2.3。報告を作り直して照合し、
gate が `pass` のときだけ `supervisor-episode:<digest>` を記録する）。thermal model には同じ形のものが無く、0062 の CLI に
任意の文字列を渡す経路だけが開いている。

### 1.2 本記録の範囲

AGENTS.md の「仕様に無い判断をした場合、実装より先に決定記録を残して人間の承認を得る」に従い、評価の**形**を先に決める。
**本記録では実装しない。合否の閾値の値も決めない**（実データの後。§2.5）。決めるのは、評価の入力・比べる相手・指標・
合否の構造・結果の artifact と digest・Registry が確かめること・試験である。

Controller 構成の比較（0054 / `evaluate.py`。#91）は「適用された構成」の実測を比べるもので、本記録の「model の予測と観測の
比較」とは別物である。混ぜない（§4）。ただし 0054 の規律（辞書式の gate・fail closed・時計を持たない・`conditions_sha256`）は
そのまま借りる。

## 2. Decision

### 2.1 入口と置き場所

- 合成の起点 `src/coldaisle/thermal_evaluate.py`、console script `coldaisle-thermal-evaluate`
- **どこからも import しない**（0150 §2.1 の `train.py` と同じ扱い）。import の約束も 0150 §2.1 と同じ形（モジュール単位の
  許可の一覧と禁止の一覧。直接の import を AST で走査する）にする。禁止の一覧は 0150 §2.1 と同じものに `coldaisle.train` を足す
- **読み取りだけ。** 書くのは `--out` の 1 ファイルだけで、Registry・設定・DB に書かない。本番の DB は開かない（§2.2）
- **LLM に依らない。** AI 層のツールにも読み取り API にも出さない（ルール 1）
- **人が手で実行する。** timer やデーモンから起動しない（0150 §5 の 7 と同じ）
- 報告の型と digest の関数は**葉のモジュール** `src/coldaisle/thermal_evaluation_report.py`（`coldaisle` を import しない。
  `training_output.py` と同じ扱い）に置き、評価の CLI と `coldaisle-registry`（§2.7）が同じ関数を使う

### 2.2 評価の入力

| 引数 | 中身 |
|---|---|
| `--registry-root <dir>` と `--artifact <model_id>@<version>` | 評価する artifact。**Registry に登録された bytes を評価する**（`ModelRegistry.load_version()`。docstring のとおり「Replay or Offline Evaluation」用の読み取り）。`RegistryCounterfactualThermalModel.from_verified_artifact()`（L1〜L12）を通す。状態は candidate であることを求める（validated・production・retired の評価は報告を作るが、§2.7 の validate には使えない） |
| `--dataset <dir>` | **artifact を学習した dataset そのもの**（§5 の 7）。`load_training_dataset_artifact_v2()`（0150 §2.4 の 1）で読み、alias・`manifest_sha256`・`examples_sha256` が artifact の `manifest.training_data` と一致しなければ拒否する |
| `--validation-start-ms` / `--test-start-ms` | 学習のときの split の境界（`training-report.json` の `split`）。下の照合に使う |
| `--evaluation-config <path>` | 評価の設定（§2.5） |
| `--metrics-config <path>` / `--calibration <path>` | L9（較正の digest）と metric の単位の照合に使う（0150 §2.3 と同じ） |
| `--out <path>` | 報告の JSON。**まだ無いこと**（`O_CREAT \| O_EXCL`。親は 0150 §2.5 の手順 1 と同じ `dataset.open_output_root` の検査で開く） |

**時間のリークを閉じる照合**（1 つでも外れれば拒否して `--out` を作らない）:

1. `split_temporally_v2(examples, validation_start_ms, test_start_ms)` で split を作り直し、その `split_sha256` が artifact の
   `manifest.training_data.split_sha256` と一致し、`window.test` が一致する。境界を取り違えた・別の split で学習した artifact を
   評価しない
2. 評価に使うのは **`test` だけ**。`train` は学習に、`validation` は Profile の residual の基準に使われた（0150 §2.4 の 5）。
   `purged` は使わない（0031 §2.5 / 0054 §2.5 と同じ扱い）
3. `test` が空なら拒否する（空の評価を「何も落ちなかった」と読ませない。0054 §2.6）
4. 0150 §2.4 の 2〜3（元の CSV からの作り直し・較正の記録との照合）は**繰り返さない**。dataset の bytes は artifact の
   `training_data` の digest に束縛され、その digest は学習の入口がそれらの検査を通した後に封じたものだからである。
   0109 の宣言された変更・較正の変更の検査も同じ理由で dataset の生成と学習の入口に任せる

### 2.3 予測の作り方と比べる baseline

各 test example について、`ObservedThermalInput.from_example(example)` と `recorded_trajectory(example, action_schema)`
（`control/model/counterfactual_training.py`。学習の当てはめと同じ変換）で、**実際に適用された action 列のもとでの予測**
（`predict_trajectory`）を作る。観測された未来はその action 列のもとで起きたものなので、比べられるのはこの予測だけである。

- 予測できなかった example（action が学習した範囲の外・入力の欠け・格子の不一致など、model が予測を返さない場合）は
  **除外して理由ごとに数える**。補わない。除外の割合は §2.6 の `evidence` の段で判定する
- target のセルが欠測（`missing_mask`）・`quality != ok` の出力は、その (example, 出力) だけを除外して数える

**未学習の baseline**（§5 の 1）:

| baseline | 定義 | 使い方 |
|---|---|---|
| `persistence`（必須） | 出力 (horizon, metric) の予測値 = anchor の時点の window の最後のフレームの同じ metric の値。そのセルが欠測・stale・`quality != ok` なら、その (example, 出力) は baseline なし | 合否の `skill` の段（§2.6）。**model と baseline の両方が値を持つ (example, 出力) の対だけ**で比べる（対にしない比較で母数をずらさない） |
| `train_mean`（報告だけ） | 出力ごとの `train` の target の平均（`train` だけから。test の統計を使わない） | 報告に載せるだけで合否に使わない |

target の metric が window の feature に無い出力は `persistence` を作れない。その出力は §2.6 の `evidence` の段で `blocked` に
なる（判定できないことを合格にしない）。

**action の効き目の確認**（§5 の 6）: 同じ model で、action 列を `hold_effective`（anchor の action を保つ。0084 §2.1）に替えた
予測も作り、`action_changed`（action 列のどこかの step が `prior_action` と違う）example だけで、実際の action 列の予測の誤差と
比べる。action の経路を学べていない model（action を変えても予測が変わらない）を見つけるためである。

### 2.4 指標

すべて出力 (horizon_ms, metric) ごとに出す（出力は artifact の target schema の順）。誤差は `e = 予測 − 観測`、
**underprediction** は `u = max(0, 観測 − 予測)`（予測が実際より低い＝冷えていると見誤る向き。Fan を下げる側へ誤らせる）。

| 指標 | model | persistence | train_mean |
|---|---|---|---|
| 件数（使った対・除外の理由ごとの数） | ○ | ○ | ○ |
| MAE・RMSE・bias（`e` の平均） | ○ | ○ | ○ |
| `u` の上側の分位（設定の `quantiles`。nearest-rank。`control/evaluation/stats.summarize`） | ○ | ○ | — |
| worst-case: `u` の最大・`\|e\|` の最大と、その上位 `worst_case_count` 件の `example_id`・`action_ts_ms`・`e` | ○ | ○ | — |
| 対の差 `d = \|e_model\| − \|e_persistence\|` のブロックの集計と検定（下記） | ○ | — | — |
| Profile の `residual_scales` で割った `\|e\|` の分位（#85 の閾値の材料。合否に使わない） | ○ | — | — |

**有意の判定**（§5 の 2）: test の example を `action_ts_ms` で、重ならない連続の時間ブロック（長さは設定の `block_ms`）に
分ける。ブロックごとに `d` の平均を取り、`d < 0`（model が良い）のブロックの数で**片側の符号検定**（二項分布の正確な p 値。
整数と分数で計算し乱数を使わない）を行う。隣り合う anchor の window は重なり、example は独立でないので、example ではなく
ブロックを単位にする。`d = 0` のブロックは数から除く。

**層別**: 次の層ごとに上の表を出す。

- **室温帯**: anchor の時点の window の最後のフレームの `air.room`（設定の `room_temperature_metric`）を、設定の帯
  （最後の帯だけ上限なし。0054 §2.8 と同じ形）に入れる。値が欠測・stale なら `unknown` の層
- **workload**（§5 の 4）: anchor の時点の window の CPU と GPU の電力（`power.cpu.package`・`power.gpu.0`）の、設定の窓の平均を設定の帯に入れた組み合わせ
  （0151 と同じ考え方。GPU Mode の申告は使わない）。値が欠ければ `unknown`。`context.workload_regime` は**報告に別の層として
  載せるだけ**にする（producer が未接続の期間は `None` になるため。`control/model/dataset.py` の `ActionContext`）
- **`action_changed`** と `action_held`（上記）

層の件数・ブロック数が設定の下限に届かなければ、その層は `insufficient` と書き、合否の条件にしない（数字は出す）。

### 2.5 合否の基準の置き場所（構造と値を分ける）

- **構造は本記録で決める**: 段の順（§2.6）・比べる相手（`persistence`）・対で比べること・ブロックの符号検定・underprediction の
  向き・層の作り方・`insufficient` の扱い・fail closed の規則
- **値は設定に置き、実データの後で決める**: `config/thermal-evaluation.example.yaml`（schema v1）に**仮の値の例だけ**を置く
  （0150 §5 の 3 と同じ。§5 の 8）。値はすべて `{value, status: provisional}` の形とし、コードに既定値を置かない（ルール 9）。
  実運用の値は実データの評価を見た後、所有者の承認を得て別の記録（または本記録の後続）で決める

```yaml
schema_version: 1
outputs:                         # artifact の target schema の出力と完全に一致すること（過不足は拒否）
  - {metric: gpu.0.core, direction: higher_is_worse}   # direction は v1 では higher_is_worse だけ
quantiles: [...]                 # underprediction の上側の分位
worst_case_count: <int>
block_ms: {value: <int>, status: provisional}
room_temperature_metric: air.room
room_temperature_bands: [...]    # 最後の帯だけ上限なし
workload_bands: {window_ms: ..., cpu_power: [...], gpu_power: [...]}
minimum: {test_examples: ..., blocks: ..., stratum_examples: ..., stratum_blocks: ..., predicted_fraction: ...}
gate:
  skill:  {alpha: ..., minimum_mae_improvement_fraction: ...}
  underprediction: {quantile: ..., maximum_c: ..., worst_case_maximum_c: ...}
  stratum_regression_tolerance_c: ...   # 十分な件数のある層で model の MAE が persistence をこれ以上上回れば落とす
```

`outputs` の過不足・未知の `direction`・`status` の欠け・未知の鍵は読み込みで拒否する。閾値・絶対温度上限は `safety.yaml` から
写さない（0054 §2.6 と同じ。本記録の評価は予測誤差を見るもので、安全の上限は判定に使わない）。

### 2.6 gate（辞書式。上の段が落ちたら下の段で覆らない）

1. `evidence` — test の件数・ブロック数が下限以上・予測できた割合が `predicted_fraction` 以上・**全出力に `persistence` がある**・
   設定の `outputs` が target schema と一致する
2. `underprediction` — 全出力で、全体と十分な件数の各層について、`u` の設定の分位が `maximum_c` 以下、`u` の最大が
   `worst_case_maximum_c` 以下（**worst-case は層で薄めない**）
3. `skill` — 全出力（§5 の 3）で、MAE の改善の割合 `1 − MAE_model / MAE_persistence` が下限以上**かつ**ブロックの符号検定の
   p 値が `alpha` 以下。さらに十分な件数のある層のどれでも、model の MAE が persistence の MAE を
   `stratum_regression_tolerance_c` より上回らない

- 結果は `pass` か `blocked`（落ちた最初の段と理由の一覧）。欠測・未設定・下限未満は `blocked` で、`pass` にしない
- §5 の 6 を推奨の案で決めた場合、action の効き目の確認は報告に載せるだけで段に入れない
- gate は**助言**で、validated にするかは人が §2.7 の手順で決める（0054 §2.4 と同じ境界）。`pass` は production への昇格や
  authority の上げの承認ではない（それぞれ 0062 §2.2 の承認ファイル・0057 / 0086 の承認で行う）。artifact の
  `authority_compatibility` は `(shadow,)` 固定のまま（0150 §2.2）

### 2.7 結果の artifact・digest・`offline_evaluation_ref`・Registry の検証

**報告**（`--out`。schema v1）は canonical JSON（`sort_keys`・UTF-8・末尾の改行 1 つ）で、壁時計の欄を持たない
（0054 §2.7）。中身:

- `artifact`: `model_id`・`version`・`sha256`（Registry の記録の checksum）・`confidence_profile_sha256`
- `dataset`: alias・`manifest_sha256`・`examples_sha256`。`split`: 2 つの境界・`split_sha256`・各集合の件数と期間
- `config_sha256`（`--evaluation-config` の bytes。parse と同じ buffer から。0150 §2.7）・`metrics_config_sha256`・`calibration_sha256`
- `code`: 報告の schema の版と、評価の手順の版の定数
- §2.4 の指標・層・worst-case・検定と、§2.6 の gate の結果
- `conditions_sha256`: 上の識別・設定・split の境界・版をすべて覆う digest（0054 §2.7 の「条件をすべて覆う」）

**`offline_evaluation_ref`** は `thermal-offline-v1:<報告の bytes の SHA-256 の 64 hex>` とする（`supervisor-episode:<digest>` と
同じ形）。報告の bytes そのものの保管は §5 の 10。

**validated にする手順**（§5 の 9 の推奨）: `coldaisle-registry validate` に `--thermal-evaluation <報告>` と
`--evaluation-config <path>` を足し、`--offline-evaluation-ref` と排他にする。CLI は artifact を deserialize も実行もしない
（0062 §2.3 を保つ）で、次をすべて確かめてから `mark_validated()` を 1 回だけ呼ぶ。1 つでも外れれば Registry を変えない。

1. 報告を `max_snapshot_bytes` で上限を切って読み（0062 §2.1）、`thermal_evaluation_report` の strict な型で検証し、
   canonical な bytes であること（読み直して書き出した bytes と一致）
2. 報告の `artifact` が `--artifact` と同じ key を名指し、`sha256` が Registry の記録の checksum と一致し、状態が candidate
3. `--evaluation-config` の bytes の SHA-256 が報告の `config_sha256` と一致し、その設定が読み込みの検査を通る
4. **gate を報告の指標と設定から作り直し**（§2.6 は報告の数字と設定だけで決まる純関数にする）、報告の gate と一致し、`pass`
5. ref を `thermal-offline-v1:<sha256>` として渡す（ref は CLI が作る。人が文字列を渡す経路を作らない）

`kind` = `thermal_model` かつ `capability` = `counterfactual_action` の artifact に `--offline-evaluation-ref` を渡すと拒否し、
`--thermal-evaluation` を促す（0150 §5 の 14 と同じ判定）。他の kind の自由記述の経路は変えない。

`ModelRegistry.mark_validated()` 自身も、その kind では ref が `thermal-offline-v1:` + 64 hex の形であることを書き込みの時点で
確かめる（§5 の 11）。snapshot の検証（読み込みの時点）は変えない（既存の Registry を読めなくしない）。

**脅威の前提は 0150 §2.5 と同じ**: 同じ uid の悪意（報告の数字を書き換えて gate を作り直す）は前提の外。検査が捕まえるのは
取り違え（別の artifact・別の設定・`blocked` の報告・手で直した `outcome`）である。

### 2.8 安全の境界

- `requested_demand`・PWM・hwmon・Authority Stage・制御設定へ届く経路を持たない（ルール 1・2）。評価の CLI は Registry にも書かない
- fand・learnd は報告を読まない。validated は制御へ届かない（fand は production だけを読む。0077 §2.6 / 0098）
- Critical Safety・Reactive Guard・Fallback・Model Confidence / OOD の判定は変えない。評価が `pass` でも、runtime の低信頼時の
  Fallback（ルール 4）はそのまま効く

### 2.9 試験（実機不要）

`tmp_path` と、既存の `tests/test_training_entry.py` / `train.py` の試験の合成の dataset を使う。`@pytest.mark.hardware` は付けない。

- **合否の向き**: 未来が action に依存する合成の系で学習した artifact は `pass`。観測が一定で persistence が完全な系では
  `skill` で `blocked`。予測を一様に低くずらした model（MAE は小さいが underprediction が大きい）は `underprediction` で
  `blocked`（MAE だけでは通ってしまうことを試験で示す）
- **リーク**: 境界を 1 ms ずらす・別の split の artifact を渡すと `split_sha256` の不一致で拒否し `--out` を作らない。test が空なら拒否
- **除外と対**: 欠測の target・stale の persistence・予測できない example が除外され、理由ごとに数えられ、model と baseline が
  同じ対で比べられる
- **層**: 下限未満の層が `insufficient` になり合否に効かない。十分な層で model が persistence より悪いと `blocked`
- **検定**: 小さい n の符号検定の p 値の golden（分数で正確）。`d = 0` のブロックが除かれる
- **決定性**: 同じ入力から 2 回で報告の bytes が一致。`time.time` などを差し替えて呼ばれないこと
- **import の走査**: 0150 §2.9 と同じ形で、`thermal_evaluate.py` と `thermal_evaluation_report.py` の直接の import が許可の一覧の中
- **Registry の validate**: 0150 §2.9 の「Registry が変わらない」「candidate が 1 つ…」と同じ定義で、成功のときは `VALIDATED` の
  event が 1 つだけ足され ref が `thermal-offline-v1:<報告の sha256>`。拒否のとき（別の artifact の報告・sha256 の不一致・
  `blocked`・`outcome` を手で `pass` に直した報告・設定の digest の不一致・canonical でない bytes・反実仮想 thermal model への
  `--offline-evaluation-ref`）は Registry の bytes が変わらない
- `mark_validated()` を直接呼んで形の違う ref を渡すと拒否（その kind だけ）。他の kind の自由記述は今どおり通る

### 2.10 実装の段階

| 段 | 内容 |
|---|---|
| 0 | 本記録（FINAL） |
| 1 | `thermal_evaluation_report.py`・`thermal_evaluate.py`・console script・`config/thermal-evaluation.example.yaml`・§2.9 の評価側の試験。AGENTS.md のコマンド一覧と `docs/thermal-model.md` に 1 行 |
| 2 | `coldaisle-registry validate --thermal-evaluation`・`mark_validated()` の形の検査・§2.9 の Registry 側の試験・`docs/model-registry.md` |
| 3 | 実データで評価を回し、報告を所有者に見せて §2.5 の値を決める（別の記録）。その後に最初の validate |

## 3. Consequences

### 良くなること

- #84 の受入基準（未学習 baseline との有意差・worst-case / underprediction・室温帯 / workload 別・リーク禁止）を、同じ手順で
  繰り返し判定できる
- `offline_evaluation_ref` が「誰かが書いた文字列」から「特定の artifact・dataset・split・設定に束縛された報告の digest」になる
- validated → production（#217 の C2・#86 / #89 の実データ・#92）へ進む道が、承認の段を増やさずに開く
- #85 の閾値の材料（Profile の residual で割った誤差の分布）が同じ報告から得られる

### 悪くなること・その緩和

- **test を何度も見て学習の選択を調整できてしまう**（同じ test の再利用）。→ `ridge_lambda` などの選択は validation で行うことを
  `docs/thermal-model.md` に書く。報告は何度作ってもよいが、validated の根拠になるのは Registry の ref が指す 1 つだけで、
  後から誰でも digest で照合できる。より強い分離（後の期間の別の dataset で評価）は §5 の 7 の代替として残す
- **persistence は短い horizon では強い**ので、全出力で上回る条件は最初の model に厳しいかもしれない。→ 値は実データの後で決め
  （§2.5）、出力の範囲は §5 の 3 で選ぶ
- **ブロックの符号検定は保守的**（効果の大きさを捨てる）。→ 効果の大きさは MAE の改善の割合の下限で別に見る
- **CLI が 1 つ増え、`coldaisle-registry validate` の引数が増える。** → 既存の引数と他の kind の経路は変えない
- **同じ uid の偽造は防げない。** → 0150 §2.5 と同じ前提。取り違えの検査と、報告の digest が Registry の監査に残ることで追える

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| `coldaisle-train` の報告に test の残差を載せ、それを ref にする | 0150 §5 の 5 で却下済み。学習と評価を同じ実行で行うと評価の独立性が無くなる（0150 §4） |
| 0054 / `coldaisle-evaluate`（#91）に thermal model の arm を足す | 0054 は適用された構成の実測を比べる。予測と観測の比較は帰属の規則も入力（trace と dataset）も違い、型を混ぜると 0054 §2.1 の名前空間の分離が崩れる |
| `offline_evaluation_ref` に報告の JSON を直接入れる | 500 文字の上限に入らない。上限を広げると snapshot が膨らみ、0062 の上限の前提が変わる |
| Registry の CLI が validate のときに評価を走らせ直す | CLI が artifact を deserialize・実行することになり 0062 §2.3 の境界を破る。dataset と設定を Registry の権限の process へ持ち込む |
| 合否の閾値の値を本記録で決める | 実データが無く、決めた値に根拠が無い（0048 §1 / 0079 §5 の 5 と同じ理由） |
| gate を MAE / RMSE の平均だけにする | #84 の受入基準に反する。低くずらした model が平均で通る（§2.9 の試験で示す） |
| 予測を `hold_effective`（anchor 推論）だけで作る | 観測された未来は実際の action 列のもとで起きたもので、action が変わった区間では比べられない |

## 5. 判断点（推奨を先頭に）

| # | 内容 | 選択肢（推奨を先に） |
|---|---|---|
| 1 | 「未学習の baseline」として合否に使うもの | **(推奨) `persistence` を必須、`train_mean` は報告だけ** / `persistence` と `train_mean` の両方を上回ることを必須 / `persistence` に加え、action を使わない ridge（v1 の観測の再生）も必須 |
| 2 | 「有意に」の判定 | **(推奨) 時間ブロックの片側の符号検定（乱数なし・正確な p 値）＋MAE の改善の割合の下限** / moving block bootstrap の信頼区間（seed を設定に置く）/ Diebold–Mariano 検定（HAC の帯域を設定に置く）/ 検定をせず改善の割合の下限だけ |
| 3 | baseline を上回ることを求める出力の範囲 | **(推奨) target schema の全出力** / 設定で列挙した出力だけ（少なくとも metric ごとの最長の horizon）/ 全出力をまとめた 1 つの値 |
| 4 | workload の層の軸 | **(推奨) window の CPU / GPU の電力の帯（0151 と同じ考え方）。`workload_regime` は別の層として報告だけ** / `context.workload_regime` を合否の層に使う（未接続の期間は全部 `unknown`）/ 両方を合否の層に使う |
| 5 | 合否に使う母集団 | **(推奨) 予測できた全 test example（Profile の support の中だけの集計は報告に別に載せる）** / Profile の support の中の example だけ（runtime は support の外で Fallback するため） |
| 6 | action の効き目の確認（実際の action 列と `hold_effective` の比較） | **(推奨) 最初の版は報告だけ** / gate の段に入れる（`action_changed` の層で実際の action 列の誤差が小さいことを必須にする）/ 作らない |
| 7 | 評価の test の出どころ | **(推奨) artifact を学習した dataset の test split だけ（`split_sha256` で束縛）** / 学習の期間より後の別の dataset を必須（較正の L9・宣言された変更の照合を評価でもやり直す）/ どちらも受け付ける |
| 8 | 閾値の値 | **(推奨) リポジトリには仮の値の `config/thermal-evaluation.example.yaml` だけ。実運用の値は実データの評価の後に別の記録** / いま `config/thermal-evaluation.yaml` に暫定値（`provisional`）を置く |
| 9 | validated にする経路 | **(推奨) `coldaisle-registry validate --thermal-evaluation`（報告と設定を照合し gate を作り直す。反実仮想 thermal model への自由記述の ref は拒否。0062 §2.1 の一部を置き換え）** / 評価の CLI に `--apply` を持たせ `mark_validated()` を呼ぶ（0150 の `--apply` と同じ形）/ 自由記述の経路を残し、報告の digest を人が渡す運用だけにする |
| 10 | 報告の bytes の保管 | **(推奨) validate のときに Registry が artifact の隣（`artifacts/thermal_model/<id>/<version>/offline-evaluation.json`）へ写し、ref の digest と照合できるようにする** / 人が `var/` に保管し、Registry は digest だけを持つ |
| 11 | ref の形を `mark_validated()` でも確かめるか | **(推奨) 書き込みの時点だけ確かめる（その kind だけ。snapshot の検証は変えない）** / CLI だけで確かめる / snapshot の検証にも入れる（既存の Registry に別の形の ref があると読めなくなる） |
