# 決定記録 0050: Model Confidence / OOD の判定方式と confidence に応じた Authority 制限

- **種別**: Decision Record
- **Status**: FINAL（2026-09-20、リポジトリ所有者が承認）
- **Date**: 2026-09-19
- **Supersedes**: なし
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md) /
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md)（§2.3 / §2.5 (b)(c) / §2.8 / §2.9） /
  [`0029-telemetry-loss-classes.md`](0029-telemetry-loss-classes.md) /
  [`0031-thermal-dataset-contract.md`](0031-thermal-dataset-contract.md) /
  [`0048-thermal-model-artifact-and-inference.md`](0048-thermal-model-artifact-and-inference.md) /
  `docs/requirements.md` Q-23 / #79 / #82 / #84 / #86 / #92 / #104
- **対象 Issue**: #85

## 1. Context

0027 §2.4 と 0028 §2.5 (c) は、Learned MPC を使ってよい条件として「confidence が stage ごとの閾値
以上で、OOD ではない」ことを求めた。#79 の Controller Gate は提案に付いた `confidence` / `ood` を
消費して Fallback へ即時に退避するが、**その値をどう作るか**は決めていない（要件 Q-23、#85）。

#85 は次を求める。

- feature range の逸脱、室温・Power・Fan state の未経験領域、欠測パターン、モデルの uncertainty、
  prediction residual の drift、model / version の不一致を判定する
- HIGH は通常の MPC 範囲、MEDIUM は Demand の変更幅と最低 Demand を制限、LOW / OOD は Fallback
- confidence は Critical Safety の代わりにならない。「予測値が出た = 信用できる」と扱わない
- authority policy は設定に置く
- confidence と authority の判断を #82 の trace に残し、offline dataset で FP / FN を評価する

実機 dataset はまだ無い（0048 §1）。判定の**方式**と**設定の形**はいま決められるが、閾値の**値**は
実データの評価まで決められない。本記録は方式と形を決め、値はすべて暫定値として設定に置く。

## 2. Decision

### 2.1 Confidence Profile（学習範囲の artifact）

モデルごとに `ModelConfidenceProfile` を作る。0048 §2.4 と同じ規律で扱う。

- Pydantic strict schema の canonical JSON。pickle や実行可能な値を持たない。SHA-256 を持つ
- **モデルに束縛する**: model ID / version / artifact SHA-256 / feature・target schema の
  checksum / 学習 split の checksum。作成時に dataset と split がモデルの provenance と一致しなければ拒否する
- 範囲・欠測の組み合わせ・support cell は **train だけ**から作る（モデルが実際に学習した範囲）
- residual の基準（出力ごとの RMS）だけは **validation** から作る。train の in-sample 誤差は楽観的すぎるため。
  test / purged は使わない
- 呼び出し側が明示する値（support 軸と bin の境界、residual の下限）には既定値を置かない（0048 §2.3 と同じ）

Profile の中身は次のとおり。

| 項目 | 内容 |
|---|---|
| feature range | feature metric ごとの usable な値（missing / stale / suspect でない）の min / max と件数 |
| fan range | zone ごとの effective demand の min / max |
| missing pattern | window 内に usable でない cell を含む metric の組み合わせと件数 |
| support cell | 呼び出し側が指定した軸（feature metric または `fan.<zone>`）を bin に分けた組と件数。action 時点の値で数える |
| residual scale | 出力ごとの validation residual RMS（`residual_scale_floor` で下限を付ける） |

Registry（#104）の `confidence_model` artifact としての登録・昇格は本記録の範囲外とする（5. 未決事項）。

### 2.2 判定の構成要素

推論1回ごとに次の7つを評価する。**すべて決定論的**で、同じ Profile・設定・入力・予測・residual の
状態からは同じ結果になる。

| 構成要素 | OOD にする条件 | score |
|---|---|---|
| model binding | 予測の model ID / version / artifact SHA-256 / 入力時刻が Profile と違う | 1（一致） |
| feature range | 学習範囲の幅で正規化したはみ出しが `range_margin` を超える。学習で一度も usable でなかった信号に値が来た。学習で幅 0 の信号が少しでも外れた | `1 - はみ出し / range_margin` |
| fan state range | 同上を Fan の effective demand に適用 | 同上 |
| support | action 時点の組（室温・Power・Fan state など）の学習件数が `min_support_count` 未満、または軸の値が usable でない | `min(1, 件数 / full_support_count)` |
| missing pattern | 欠測の組み合わせの学習件数が `min_missing_pattern_count` 未満（0029 の「学習時に無かった欠測の組み合わせは OOD」） | 1 |
| uncertainty | （OOD にしない） | Thermal Model v1 は uncertainty を出さない（0048 §2.5）。その間は confidence の**上限** `cap_without_uncertainty` を課す |
| residual drift | 直近 `residual_window` 件の正規化 residual の RMS が `residual_drift_ood_ratio` 以上 | 1（比 ≤ 1）から比が OOD 倍率で 0 になる線形 |

- **confidence は構成要素の score と上限の最小値**とする。1つでも OOD なら `ood = true`、confidence は 0
- residual の証拠は **forecast（1回の予測）単位**で数える。1回の予測が持つ horizon × metric の
  出力の数では数えない。出力の数で数えると、多出力の予測1回だけで最低件数を満たしてしまう。
  `residual_window` / `residual_min_samples` の単位も forecast とする
- 全出力を照合し終え、**全出力に OK の観測があった** forecast（matched）だけを証拠に数える。forecast の誤差は
  出力ごとの正規化 residual の二乗平均とし、drift の比は証拠に数えた forecast の誤差の平均の平方根とする
- **window は解決した forecast を順に数える。** matched だけでなく、許容幅の中で観測が揃わなかった
  forecast（expired）と照合待ちの上限で捨てた forecast（dropped）も window の枠を占める。照合できない
  forecast が続けば、古い「当たっていた」証拠は押し出される
- **証拠は鮮度を持つ。** 解決から `residual_max_age_ms` を過ぎた matched は数えない。証拠を読む時点で
  許容幅を過ぎた照合待ちも expired にするので、観測が止まっても古い証拠は残らない
  （Telemetry の stale で Safety が働くことを前提にしない）
- 照合済みの forecast が `residual_min_samples` に満たない間は、confidence に上限
  `cap_before_residual_evidence` を課す。**予測が当たっている証拠が無い状態を満点にしない**
- residual の証拠は Profile の SHA-256、数えた monitor の window・許容幅・鮮度、証拠を見た時刻
  （`as_of_ts_ms`）を持つ。assessment は別の Profile・別の設定の証拠と、**この推論の action 時刻以外で
  見た証拠**を拒否する（モデルを差し替えた直後や、以前に見た証拠の使い回しで上限を外さない）。
  同じ action の予測を2回数えない
- 照合の許容幅は Profile の最短 horizon より小さくなければならない（DatasetSpec の
  `target_tolerance_ms` と同じ不変条件）。horizon は Profile にあるため、monitor の生成時に検証する
- 入力の形が feature schema と合わない場合は判定を作らず例外にする（予測自体も失敗する）。
  worker はこれを `LearnedFailure` として Gate へ渡し、Fallback になる（0028 §2.7）
- residual の照合は予測と同じ時間軸（`expected_ts_ms` と観測の時刻）で、Dataset の target 選択
  （0031 §2.2）と同じ規則で行う。期待時刻に**最も近い**観測を `±residual_match_tolerance_ms` の中でだけ採り
  （前後どちら側でもよい）、**同距離なら過去側**を採る。観測は action より後に限り、metric ごとに
  **品質が OK の値**だけを候補にする（Profile の residual 基準も OK の label だけから作るため）。許容幅の中で観測が揃わなかった forecast は証拠に数えず件数だけ残す。
  照合待ちは構造上の上限を持つ

### 2.3 判定は worker、切替は Gate

- 判定（`ConfidenceAssessor`）は Learned MPC と同じ worker 側で動く。出すのは提案に付ける
  `confidence` / `ood` と理由だけで、**demand / PWM / authority を出さない**
- Registry を通っていない artifact（`offline_unverified`）の判定は制御の提案に付けられない（0048 §2.4）
- **判定は1回の推論に束縛する。** 入力と予測の canonical JSON の SHA-256 を推論の識別子
  （`inference_id`）とし、Learned MPC の提案と assessment の両方が持つ。識別子が違う判定は提案に
  付けられない（同じ model version でも、以前の in-distribution な判定を別の入力の提案へ付け替えさせない）
- **Gate は提案の値を信用しない。** Learned MPC の提案は assessment そのものと一緒に Gate へ渡す。Gate は
  assessment を検証し直し、Registry 検証済み・同じ推論・同じ model version・提案と同じ confidence / ood で
  なければ `confidence_unattested` として Fallback にする。assessment の無い提案も同じ
- Fallback への切替、confidence level、authority の帯は #79 の Controller Gate が決める。
  出せるのは requested までで、**Reactive Guard と Critical Safety は後段で常に掛かる**（0028 §2.4）。
  OOD の間も Critical Safety はそのまま有効である

### 2.3.1 authority の不変条件

Fallback より上の authority（Learned MPC の requested）は、次を**すべて**満たすときだけ与える。
試験は不変条件ごとに、破る入力を与えて Fallback になる（または入力自体が作れない）ことを確かめる。

| # | 不変条件 | 担う箇所 |
|---|---|---|
| A1 | 提案の confidence / ood は、この推論（`inference_id`）の Registry 検証済み assessment と一致する | Gate（`confidence_unattested`）、`apply_to()` |
| A2 | assessment は OOD でなく、confidence が stage の下限以上。モード AUTO・stage LIMITED 以上・Safety NORMAL・復帰 hold（0028 §2.5 (c)） | Gate |
| R1 | HIGH（帯なし）には、鮮度のある residual 証拠が `residual_min_samples` 件以上要る。無ければ confidence は `cap_before_residual_evidence`（< `high_min_confidence`）に抑えられる | Assessor、設定検証 |
| R2 | window は解決した forecast を順に数え、照合できなかった forecast も枠を占める。解決から `residual_max_age_ms` を過ぎた証拠は数えない | Monitor |
| R3 | 証拠は同じ Profile・同じ設定で、この推論の action 時刻に見たもの | Assessor |
| R4 | 照合は OK の値だけ・±許容幅の最近傍（同距離は過去側）・許容幅 < 最短 horizon・同じ action を2回数えない | Monitor |
| C1 | `high_min_confidence >= gate_min_confidence.full`、`cap_before_residual_evidence < high_min_confidence`、`residual_min_samples <= residual_window`、`full_support_count >= min_support_count`、`residual_max_age_ms > residual_match_tolerance_ms` | 設定検証 |

### 2.4 confidence level と authority

Gate は tick ごとに confidence を3つに分ける。

| level | 条件 | Learned MPC の扱い |
|---|---|---|
| HIGH | `confidence >= model_confidence.high_min_confidence` | stage の範囲（LIMITED / EXPANDED の帯と許可 zone、FULL はそのまま） |
| MEDIUM | `gate_min_confidence[stage] <= confidence < high_min_confidence` | stage の範囲に加え、Fallback を中心とした MEDIUM 帯（`medium_limit.limit_up` / `limit_down`）に収める |
| LOW | `confidence < gate_min_confidence[stage]`、または OOD | Fallback（既存の `low_confidence` / `ood`） |

- MEDIUM 帯と stage の帯は**狭い方を採る**（両方の範囲の共通部分）。どちらの帯も Fallback の値を含むので空にならない
- `limit_down` は「Fallback からどこまで下げてよいか」なので、**最低 Demand の制限を兼ねる**
- `high_min_confidence >= gate_min_confidence.full` を設定検証で強制する。これより低いと、Fallback 境界の直上で
  帯なしの authority を得てしまう
- `cap_before_residual_evidence < high_min_confidence` を設定検証で強制する。予測が当たっている証拠が
  無い間に HIGH（帯なし）へ届かせない
- **`cap_without_uncertainty` は設定のままとし、上限をコードで強制しない**（2026-09-20、所有者の判断）。
  uncertainty を出さないモデルを構造的に HIGH から締め出すのではなく、**学習が進むにつれて設定で
  authority を広げる**。学習期間は保守的な暫定値（2.5）で MEDIUM に留め、#90 / #91 の評価で証拠が
  積み上がった段階で所有者が値を広げる。
  危険温度への対応は confidence に依存しない。Reactive Guard（#80）と Critical Safety（#78）が
  決定論的に floor / forced Max を掛ける（0028 §2.4）。Confidence / OOD が動かせるのは
  **その前段の `requested` だけ**である（2.3）
- SHADOW の tick も記録用に level を付ける（最も緩い LIMITED の下限で分ける）。requested は Fallback のまま
- 昇格は引き続き人だけが行う（0028 §2.9 承認点 5）。confidence は stage を**上げない**。下げる向き（帯を狭める・Fallback）にだけ働く

### 2.5 設定（`fan-policy.yaml` v6）

0028 §2.8 のとおり Gate の閾値は `fan-policy.yaml` に置く。`model_confidence` を追加し、
schema version を 6 に上げる。v5 以前は自動補完せず起動前に拒否する。

```yaml
model_confidence:
  high_min_confidence: {value: ..., status: provisional}
  medium_limit:
    limit_up: {value: ..., status: provisional}
    limit_down: {value: ..., status: provisional}
  range_margin: {value: ..., status: provisional}
  min_support_count: {value: ..., status: provisional}
  full_support_count: {value: ..., status: provisional}
  min_missing_pattern_count: {value: ..., status: provisional}
  residual_window: {value: ..., status: provisional}
  residual_min_samples: {value: ..., status: provisional}
  residual_match_tolerance_ms: {value: ..., status: provisional}
  residual_max_age_ms: {value: ..., status: provisional}
  residual_drift_ood_ratio: {value: ..., status: provisional}
  cap_without_uncertainty: {value: ..., status: provisional}
  cap_before_residual_evidence: {value: ..., status: provisional}
```

**学習期間の暫定値**（2026-09-20、所有者の方針。値は実データの評価で広げる）。
`status: provisional` のまま運用し、`basis` に評価の記録を付けて広げていく。

```yaml
model_confidence:
  high_min_confidence: {value: 0.90, status: provisional}
  medium_limit:
    limit_up: {value: 0.10, status: provisional}
    limit_down: {value: 0.05, status: provisional}
  range_margin: {value: 0.05, status: provisional}
  min_support_count: {value: 5, status: provisional}
  full_support_count: {value: 50, status: provisional}
  min_missing_pattern_count: {value: 5, status: provisional}
  residual_window: {value: 50, status: provisional}
  residual_min_samples: {value: 20, status: provisional}
  residual_match_tolerance_ms: {value: 1000, status: provisional}   # 最短 horizon より小さくする
  residual_max_age_ms: {value: 900000, status: provisional}
  residual_drift_ood_ratio: {value: 2.0, status: provisional}
  cap_without_uncertainty: {value: 0.85, status: provisional}
  cap_before_residual_evidence: {value: 0.70, status: provisional}
```

この組み合わせでは、uncertainty を出さない Thermal Model v1 は MEDIUM までに留まり、Fallback 近傍の
帯の中で動く。`cap_without_uncertainty` を `high_min_confidence` 以上へ広げるのは設定の変更であり、
**コードの上限ではない**。広げる根拠は #90 / #91 の評価とし、変更は所有者が承認する。

すべて `status` / `basis` の追跡対象とし、`provisional_values()` の起動ログに出す。
**本記録は値を決めない。** 値は #90 / #91 の offline / shadow 評価を根拠に、所有者の承認で `confirmed` にする。

### 2.6 decision trace（`ControlTick` v5）

`ControlTick` の schema version を 5 に上げ、`model_gate`（`ModelGateDecision`）を追加する。

- 中身: model version、推論の識別子（`inference_id`）、裏付けの有無（`attested`）、confidence、ood、
  提案の自称値との不一致（`proposal_mismatch`）、confidence level、authority stage、Learned MPC を選んだか、
  適用した制限（`stage_band` / `stage_zone` / `medium_confidence_band`）、構成要素ごとの理由（最大16件）
- **記録するのは検証できた値だけ。** confidence / ood は検証済み assessment の値を書く。提案が自称した値は
  書かない。assessment が無い・束縛できない tick は `attested: false` とし、confidence / ood / 理由を
  空のまま残す（level は LOW）。提案の自称値が assessment と違えば、assessment の値を書いたうえで
  `proposal_mismatch` に差分を残す。後から評価（#90 / #91）と stage の判断（#92）が読むため、
  OOD の推論が HIGH に見える記録を作らない
- `ControlState` の `model_confidence` / `model_ood` も同じ値にする。`attested: false` の tick では
  どちらも `null` にする
- **demand を持たない。** requested / effective は従来どおり zone の記録に残る
- v5 で Learned MPC を active にした tick には `model_gate` を必須とし、`ControlState` の
  `model_version` / `model_confidence` / `model_ood` / `authority_stage` / `active_controller` と一致させる
- OOD は LOW、LOW で Learned MPC を選ばない、MEDIUM で選んだら MEDIUM 帯を記録する、を型で検証する
- v1〜v4 の保存済み trace はそのまま読める

### 2.7 offline 評価

`evaluate_ood_detection` は、正解ラベル付きの case（保存済み dataset の Replay と合成した OOD 入力）に対して
true / false positive / negative、FP 率・FN 率、構成要素ごとの OOD 件数、誤判定した case の名前を返す。
制御には使わない。閾値の確定（2.5）の根拠はこの評価の結果とする。

## 3. Consequences

- 「予測値が出た」ことが authority の根拠にならない。学習範囲・support・欠測・residual・model identity の
  どれか1つでも崩れれば、その tick で Fallback へ退避する
- MEDIUM でも ML を完全には止めず、Fallback 近傍に閉じ込めて shadow と同じ条件の実績を積める
- 判定は決定論的で、合成・Replay の入力だけで試験できる。実機は要らない
- Thermal Model v1 は uncertainty を出さない。学習期間の暫定値（2.5）では `cap_without_uncertainty` が
  HIGH の下限未満なので v1 は MEDIUM までだが、これは**設定であり、コードの上限ではない**。
  条件をまたいだ実績が積み上がれば、所有者が設定で authority を広げられる。
  uncertainty を持つ予測 schema を足すときは version を上げて構成要素を拡張する
- **危険温度の扱いは confidence から独立している。** Confidence / OOD Gate が出せるのは requested までで
  （`ControllerSelection` は effective / PWM を持たない）、`DemandComposer.compose()`
  （`src/coldaisle/control/safety/critical.py`）が Guard の floor / ceiling、Critical Safety の
  最低安全 demand・CPU cooling floor・下げる速さの制限・forced Max を必ず後段で掛ける。
  `confidence.py` / `gate.py` は hardware / safety / reactive のどの module も import しない
  （`tests/test_model_confidence.py::test_confidence_layer_stays_upstream_of_guard_and_safety`)
- `fan-policy.yaml` と `ControlTick` の schema version が上がる。v5 以前の設定は起動前に拒否される

| トレードオフ | 緩和策 |
|---|---|
| support cell は軸を増やすと組み合わせが急増し、学習件数が薄くなる | 軸と bin は Profile 作成時に明示し、軸数・cell 数に構造上の上限を置く。評価（2.7）で FP 率を確かめる |
| 箱型の範囲と格子の support は、学習点の間の「穴」を見逃しうる | residual drift と support 件数の score で補う。方式の置き換えは Profile の schema version を上げて行う |
| 起動直後は residual の証拠が無く confidence が抑えられる | 安全側を優先する。上限値は設定で調整でき、証拠が積み上がれば所有者が広げる |
| 同一プロセス内で形の整った assessment を偽造されれば、Gate は見破れない | **受け入れる**（2026-09-20、所有者の判断）。LLM 層には制御権も書き込み権も無く（AGENTS.md ルール1）、`coldaisle-fand` の中で動く制御コード自体が敵対的である場合は本記録の想定範囲外とする。Gate は内部整合・推論への束縛・Registry 検証を確かめ、偶発的な迂回（`apply_to()` を呼び忘れる、古い判定を使い回す）を塞ぐ |
| 暫定値のままでは FP / FN の水準が分からない | 本記録は値を確定しない。#90 / #91 の評価で確定し、所有者が承認する |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| モデルの予測値だけから confidence を作る（予測の大きさ・変化など） | 「予測値が出た = 信用できる」の言い換えになる（#85 の原則） |
| 範囲・support を validation / test も含めて作る | モデルが学習していない範囲を「学習済み」と扱ってしまう |
| residual の基準を train の in-sample 誤差にする | 楽観的すぎて、通常の誤差でも drift と判定しやすい |
| 構成要素の重み付き平均で confidence を作る | 1つの要素が大きく崩れても平均で隠れる。最小値なら崩れた要素がそのまま効く |
| uncertainty が無いモデルは判定を諦めて常に OOD にする | v1 baseline が shadow の実績すら積めなくなる。上限で抑える |
| `cap_without_uncertainty < high_min_confidence` をコードで強制する | authority の広げ方を設定と評価ではなくコードで固定してしまう（2026-09-20、所有者の判断）。危険温度は Guard / Critical Safety が決定論的に見る |
| MEDIUM で stage の帯を置き換える | stage は人が承認した上限（0028 §2.5 (b)）。confidence で広がってはならないので共通部分を採る |
| confidence が高ければ stage を自動で上げる | 昇格は人だけ（0028 §2.9） |
| 判定を Gate（制御ループ）の中で行う | 推論と同じ入力が要り、ループの tick を重くする。worker で判定し、ループは結果だけを消費する（0028 §2.2） |
| 閾値の既定値をコードに置く | AGENTS.md ルール9。実データ前の値を隠す |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | `model_confidence` の全ての値と `gate_min_confidence` の確定 | #90 / #91 の評価の後、所有者の承認 |
| 2 | support 軸（室温・CPU / GPU Power・Fan state のどれを使うか）と bin の境界 | 実機 dataset（#50 / #83）の後 |
| 3 | Confidence Profile を #104 の `confidence_model` artifact として登録・昇格・rollback する経路 | #104 |
| 4 | uncertainty を持つ予測 schema（アンサンブル・分位点など）と、その構成要素 | #84 の後続 |
| 5 | Learned MPC worker（#86）が判定・residual の照合を呼ぶ配線 | #86 |
| 6 | `model_gate` を読む評価（#90 / #91）と降格（#92）での利用 | #90 / #91 / #92 |

本記録は 2026-09-20 にリポジトリ所有者が承認して FINAL になった。承認の対象は**方式・設定の形・trace の形・
authority の不変条件**であり、閾値の値の確定（承認点 2）や authority stage の昇格（0028 §2.9 承認点 5）は
含まない。2.5 の暫定値は学習期間の出発点であり、#90 / #91 の評価を根拠に所有者が広げる。
