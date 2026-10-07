# 決定記録 0107: MPC worker（0077 段階 3）が frame の列から推論の入力を作る規則（観測 window・anchor の action・提案を作らない周期・residual・worker の入力）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が §5 の7点すべてを推奨案で承認。§6）
- **Date**: 2026-10-07
- **Supersedes**: なし（0077 / 0092 / 0101 が決めていなかった点への追加。どの記録も置き換えない）
- **関連**: [0077](0077-learned-proposal-handoff.md) §2.3 / §2.5 / §2.6 / §2.7 / §2.10 段階 3 /
  [0092](0092-learned-frame-applied-from-hardware-result.md) / [0095](0095-learned-channel-stage1-settled-points.md) /
  [0098](0098-registry-binding-settled-points.md) / [0101](0101-calibration-via-learned-frame.md) /
  [0031](0031-thermal-dataset-contract.md) §2.2 / [0087](0087-dataset-v2-action-grid.md)（`prior_action`）/
  [0050](0050-model-confidence-ood-and-authority.md) §2.2（residual）/ [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.2 /
  [0079](0079-model-artifact-formats.md) §2.4 / `src/coldaisle/control/mpc/controller.py`（`LearnedMpcRuntime`）/
  `src/coldaisle/control/model/thermal.py`（`ObservedThermalInput`）/ `src/coldaisle/dataset.py`（`_window_frame`）
- **対象 Issue**: #86

## 1. Context

0077 §2.10 の段階 3 は MPC worker（`coldaisle-learnd --role mpc`）を作る。0077 §2.3 は「観測 window は worker が
frame の列から組み立てる」「届かなかった frame を補間しない。欠けは欠けとして window に残す」と決め、0092 は frame の
`applied` を各 zone の `FanHardwareResult.applied_demand`（確かめられなければ `null`）と決め、0101 は較正を frame v3 で
運ぶと決めた。

ところが `LearnedMpcRuntime.propose()` の入力（`ObservedThermalInput`・選ばれた `SupervisorOutput`・Fallback の
`ControllerProposal`・Safety floor・`ResidualEvidence`）を **frame の列からどう作るか**は、どの記録も決めていない。
実装の途中で次の点が記録の外にあると分かった（PR で見つけた点。実装より先に所有者の判断を要する。AGENTS.md「迷ったら」）。

| 点 | 何が決まっていないか | なぜ実装で決められないか |
|---|---|---|
| A | 観測 window の格子と、各格子時刻にどの frame の値を当てるか | 学習（Dataset、0031 §2.2）は生の readings を格子時刻ごとに as-of で引く。runtime は tick ごとの snapshot しか持たない。frame の欠けを as-of で古い frame から埋めると「補間しない」（0077 §2.3）と読み方が分かれる |
| B | anchor の action（`ObservedThermalInput.action`）をどの frame の `applied` から取るか | Dataset v2 は anchor の tick より**厳密に前**の tick の effective（`prior_action`。0087）。frame の `applied` は**その tick の**結果（0092）。どちらに揃えるかで推論の意味が変わる |
| C | 提案を作れない周期（window が足りない・anchor の action が欠測・frame の `supervisor` / `baseline` が `null`）に何を返すか | `LearnedFailure` は `model_load_failure` / `optimizer_exception` の2値だけで、どちらも意味が合わない。値を足すと 0077 §2.5 が避けた閉じた列挙の変更になる |
| D | `ResidualEvidence`（0050 §2.2 の residual drift の証拠）を worker が持つか | 持たなければ confidence は `cap_before_residual_evidence` に抑えられ HIGH にならない。持つなら照合の観測の出どころ（frame の snapshot）と保持を決める必要がある |
| E | artifact の feature metric が frame の snapshot の signal に無いとき | Dataset の feature は保存された readings のどの metric でもよいが、snapshot の signal は制御の入力契約（`ControlInputContract`）の metric だけ。無い metric は毎回欠測になる |
| F | worker の目的関数の任意依存（Air Balance・`fan_hardware`・acoustic）の作り方 | 0077 §2.3 は worker が自分で Control Config を読み frame の `config` と照合すると決めたが、そこから何を作るかは書いていない |

## 2. Decision（2026-10-07 所有者が推奨案で承認）

### 2.1 観測 window（点 A）

- 格子は **artifact の feature schema のまま**作る。`action_ts_ms` は window に使う最新の frame の `snapshot.ts_ms`、
  格子時刻は Dataset と同じ `range(action_ts_ms - window_ms, action_ts_ms + 1, sample_period_ms)`（0031 §2.2 / `dataset.py`）
- 各格子時刻 `t` には、**受け取った frame のうち `snapshot.ts_ms <= t` で最新のもの**を当て、その snapshot の
  `SnapshotSignal` から Dataset と同じ規則で cell を作る
  - `value` / `source_ts_ms` はその signal の値。signal が無い・`value` が無い → missing（`source_ts_ms` が無ければ `null`）
  - stale: `quality` が `stale`、または `t - source_ts_ms >= stale_after_ms`（feature schema の値）
  - suspect: `quality` が `suspect` で値がある
- **欠けた frame を飛び越えて as-of しない。** 欠けは**受け取った frame の `tick_id` が飛んだことで証明できるとき**だけ
  とする。当てる frame `f` の後に受け取った frame があり、その中で最も古いものの `tick_id` が `f.tick_id + 1` でない
  （間の tick が捨てられた・worker が止まっていた）なら、その格子時刻の cell はすべて missing（`source_ts_ms = null`）にする。
  これが 0077 §2.3 の「補間しない。欠けは欠けとして window に残す」の読み方で、Confidence / OOD（0050）と Gate が Fallback へ倒す
  - **window に使う最新の frame（anchor）には後続を求めない。** 後続はまだ届いていないのが正常であり、欠けの証拠ではない。
    anchor の frame が当たる格子時刻（`t = action_ts_ms`）の cell は、その frame の signal から作る
- frame は `tick_id` の昇順で持ち、`tick_id` が戻る・`snapshot.ts_ms` が戻る frame を受け取ったら、それより前の列を捨てる
  （同じ `run_id` の中では起きない想定。起きたら安全側＝window の作り直し）。`run_id` が変われば列を捨てる（0101 §2.3）
- 列の長さは feature schema の `window_ms` と anchor の action（§2.2）に要る分だけ持つ。window が足りない間は提案を作らない（§2.3）

### 2.2 anchor の action（点 B）

- `ObservedThermalInput.action` は、window に使う最新の frame（tick `N`）の**直前の tick（`N - 1`）の frame の `applied`** から作る。
  Dataset v2 の `prior_action`（anchor の tick より厳密に前で直近の tick の effective。0087）と同じ時点で、学習時と推論時の
  意味を揃える。値の出どころは 0092 のとおり `FanHardwareResult.applied_demand`
- tick `N - 1` の frame が無い、またはどれかの zone の `applied` が `null`（0092 の欠測）なら、その周期は提案を作らない（§2.3）。
  欠測を effective・直前の値・`requested` で埋めない（0092 §2.2）

### 2.3 提案を作らない周期（点 C）

次のときは、その周期に `propose()` を呼ばず、**結果を送らない**（heartbeat だけ送る。0077 §2.7）。Gate はこれまでどおり
直前の結果の期限切れか `learned_proposal_unavailable` で Fallback にする。`LearnedFailure` / `FallbackCause` に値を足さない。

- **立ち上がり中**: 同じ `run_id` で受け取った最も古い frame の `snapshot.ts_ms` が、window の最も古い格子時刻より後
  （格子の先頭に当てる frame がまだ無い）。window の**途中の**欠けはここに含めない。途中の欠けは §2.1 のとおり missing の cell
  として `propose()` へ渡し、Confidence / OOD と Gate に判断させる
- anchor の action に要る tick `N - 1` の frame が無い、またはどれかの zone の `applied` が `null`（§2.2）
- 使う frame の `supervisor` か `baseline` が `null`（`propose()` はどちらも必須）
- frame の `expected_artifacts.thermal_model` が `null`（0077 §2.6。production が無い）
- 検証を通った v3 の frame がまだ無い `run_id`（0101 §2.3）

失敗（`model_load_failure`）を返すのは、記録が失敗と決めた場合だけにする: `config_mismatch`（0077 §2.3。frame の `config` が
一致するまで毎周期）・`artifact_not_production`（0077 §2.6。1回返して別の `run_id` まで止まる）・
`calibration_changed_within_run`（0101 §2.3。同じ）・束縛の作り直しや L1〜L12・L9 の失敗（0077 §2.3 / 0101 §2.4。
`LearnedMpcRuntime.load` が返す `MODEL_LOAD_FAILURE`）・§2.5 の `feature_metric_not_in_snapshot`。

### 2.4 residual の証拠（点 D）

- **段階 3 では `residual=None` で `propose()` を呼ぶ。** confidence は `cap_before_residual_evidence` に抑えられ、帯なしの
  HIGH にはならない（0050 R1。安全側）。authority は既定の `SHADOW` のまま（0077 §2.10）なので、制御の結果は変わらない
- worker に residual の照合（frame の snapshot を実測として forecast を照らす）を持たせるのは、shadow の集計（#90）との
  関係を含めて別の記録で決める

### 2.5 feature metric と snapshot の signal（点 E）

- 束縛を作る時点（0101 §2.3 の各時点）で、artifact の feature metric と目的関数の metric がすべてその frame の
  `snapshot.signals` にあるかを確かめる。無ければ束縛を作らず `model_load_failure`（理由 `feature_metric_not_in_snapshot`、
  detail に足りない metric 名）を返し、別の `run_id` まで止まる（入力契約は fand の起動時に決まり、同じ `run_id` では変わらない）。
  毎回 missing の window で推論して OOD に任せるより、構成の誤りとして見える形にする

### 2.6 worker の入力と任意依存（点 F）

- worker は `--config-dir` の Control Config（4ファイル）を `ControlConfig.from_directory` で読み、その digest を frame の
  `config` と照合する（0077 §2.3）。目的関数の任意依存は**同じ `ControlConfig` から fand と同じ規則で**作る:
  `air_balance_enabled` のときだけ Air Balance Model・`BalanceBand`・`fan_hardware` を渡し、無効なら渡さない。
  acoustic は #94 まで渡さない
- 較正ファイル・Telemetry・SQLite・API は読まない（0077 §2.3 / 0101 §2.1）。worker の CLI に較正の path を持たせない
- registry は `--registry-root` / `--registry-limits` で読み、frame の `expected_artifacts.thermal_model` の3つ組だけを
  検証して読み込む（0077 §2.6 / 0098 §2.1）
- worker は L8（metric の単位・派生の定義の照合。0079 §2.4）に要る Metric Catalog を `--metrics` で読む。**fand と同じ
  catalog であることの束縛**は §5 #7 で決着した（frame v3 に catalog の SHA-256 を載せ、worker は自分が読んだ catalog の
  SHA-256 と違えば束縛を作らず `model_load_failure`（理由 `metric_catalog_mismatch`）を返す。`config_mismatch` と同じく一致する
  まで毎周期）。frame には SHA-256 だけを載せ、path を載せない（AGENTS.md ルール10）
- `--role supervisor` は段階 4（#89）まで起動を拒む

### 2.7 実行単位

- heartbeat は推論とは**別のスレッド**から `heartbeat_interval_ms` ごとに送る。推論だけが固まったときは提案が期限切れ
  （`learned_proposal_expired`）で Fallback になり、プロセスごと固まったときは idle で切られる（0077 §2.7 の役割分担）
- `propose()` は `mpc.period_ms` ごと（worker の単調時計）に、最後に受け取った frame に対して1回呼ぶ（0077 §2.3）。
  その前に registry の production の照合（0077 §2.6）と束縛の作り直しの判定（0077 §2.3 / 0101 §2.3）を行う

## 3. Consequences

### 良くなること

- 学習（Dataset v1 / v2）と推論で、window の格子・mask の規則・anchor の action の時点が一致する
- frame の欠け・`applied` の欠測が、推測で埋められずに Fallback へ倒れる（0077 §2.3 / 0092 と一貫する）
- 閉じた列挙（`LearnedFailure` / `FallbackCause`）も trace の版も変えない

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 1つの frame の欠けで、その格子時刻を含む window の間（`window_ms` のあいだ）提案の品質が落ちる | 安全側（OOD・低 confidence で Fallback）に倒れるだけ。欠けの頻度は段階 3 の shadow で数える（0077 §3） |
| anchor の action が1 tick 前の値で、提案が使われる時点（数 tick 後）の action とずれる | 学習時と同じ意味。ずれは `mpc.max_source_age_ms` の範囲に収まる |
| residual の証拠が無い間は HIGH にならない | 段階 3 は SHADOW。LIMITED 以上で必要になる前に別の記録で決める |
| snapshot の時刻は tick の時刻で、Dataset の as-of（生の readings の時刻）と最大1 tick 違う値を当てうる | 元観測の `source_ts_ms` は snapshot の signal がそのまま運ぶので、stale の判定は Dataset と同じ時刻で行える |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| Dataset と同じく、欠けた frame を飛び越えて古い frame へ as-of し、stale mask だけに任せる | 欠けた tick の間に readings が更新されていても古い値を当てる。0077 §2.3 の「補間しない」と読み方が分かれる。所有者が学習との一致を優先するなら採れる（§5 #1 の代替） |
| anchor の action を tick `N` 自身の `applied` にする | 提案が使われる時点の action には近いが、Dataset v2 の `prior_action` と時点がずれ、学習と推論の意味が変わる（§5 #2 の代替） |
| 提案を作れない周期に `optimizer_exception` / `model_load_failure` を返す | 理由が意味と合わない。新しい値を足すと Gate の閉じた列挙と worker の通信の型が変わる（0077 §2.5 が避けたこと） |
| worker が residual の照合を段階 3 から持つ | 照合の出どころと保持の設計が要り、段階 3 の範囲（0077 §2.10）を超える |
| feature metric が無いときも束縛を作り、毎回 missing で推論する | 構成の誤りが OOD に紛れ、原因が trace から読めない |

## 5. 未決事項

2026-10-07、所有者が7点すべてを推奨案で決めた（§6）。「代替」の列は判断前の記録である。開いている点は無い。

| # | 判断点 | 決着（2026-10-07 所有者の決定、推奨案） | 代替（判断前の記録） |
|---|---|---|---|
| 1 | 観測 window で欠けた frame を飛び越えて as-of するか（§2.1） | **飛び越えない**（欠けた tick 以後の格子時刻は missing） | Dataset と同じく古い frame へ as-of し、stale mask に任せる |
| 2 | anchor の action の時点（§2.2） | **tick `N - 1` の `applied`**（Dataset v2 の `prior_action` と同じ） | tick `N` の `applied` |
| 3 | 提案を作れない周期（§2.3） | **何も送らない**（heartbeat だけ。Gate は期限切れ / unavailable） | 失敗を返す（既存の値に寄せるか、値を足す） |
| 4 | residual の証拠（§2.4） | **段階 3 は `None`**（HIGH にしない。照合は別の記録） | 段階 3 で worker に residual の照合を持たせる |
| 5 | feature metric が snapshot に無い artifact（§2.5） | **束縛の時点で `model_load_failure`（`feature_metric_not_in_snapshot`）** | 束縛して毎回 missing の window で推論し、OOD に任せる |
| 6 | 任意依存と CLI（§2.6 / §2.7） | **同じ Control Config から fand と同じ規則で作る・heartbeat は別スレッド・`--role supervisor` は段階 4 まで拒む** | — |
| 7 | worker の Metric Catalog を fand のものに束縛するか（§2.6。Codex の指摘） | **frame v3 に `metric_catalog_sha256` を足し、worker は違えば `metric_catalog_mismatch` の `model_load_failure`**（0101 の v3 の形に欄が1つ増える。v3 は同じ PR で入れるので版は 3 のまま） | 束縛せず、同じパッケージ・同じ配備の catalog を使う運用に任せ、起動ログに SHA-256 を残すだけにする |

frame の列を再現のために保存するか（0077 §5「別の場所で決める点」）は、段階 3 では保存しない（#86 の再現性の受入基準は
試験で記録した frame の列から確かめる）。保存先・保持期間は別の記録で決める。

## 6. 承認記録

**2026-10-07、リポジトリ所有者が §5 の7点すべてを推奨案で承認し、本記録を FINAL にした。**

| §5 の判断点 | 決定 | 本記録 |
|---|---|---|
| 1 | 観測 window で欠けた frame を飛び越えて as-of しない（`tick_id` の飛びで証明できる欠けの格子時刻は missing。最新の frame に後続を求めない） | §2.1 |
| 2 | anchor の action は直前の tick（`N - 1`）の `applied`（Dataset v2 の `prior_action` と同じ時点） | §2.2 |
| 3 | 提案を作れない周期（立ち上がり中・anchor の action の欠測・`supervisor` / `baseline` が `null` など）は何も送らない（heartbeat だけ）。途中の欠けは missing の cell として `propose()` へ渡す | §2.3 |
| 4 | 段階 3 は `residual=None` | §2.4 |
| 5 | feature metric が snapshot に無い artifact は束縛の時点で `model_load_failure`（`feature_metric_not_in_snapshot`） | §2.5 |
| 6 | 任意依存は同じ Control Config から fand と同じ規則で作る・heartbeat は別スレッド・`--role supervisor` は段階 4 まで拒む | §2.6 / §2.7 |
| 7 | frame v3 に `metric_catalog_sha256` を足し、worker は違えば `model_load_failure`（`metric_catalog_mismatch`） | §2.6 |

§5 に開いている点は無い。
