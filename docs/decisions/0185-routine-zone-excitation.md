# 決定記録 0185: 普段の運転の Baseline に、zone ごとの小さな上乗せ（揺らぎ）を足す

- **種別**: Decision Record
- **Status**: FINAL（2026-11-19、リポジトリ所有者の承認。§5 の 1〜6 はすべて推奨の案）
- **Date**: 2026-11-19
- **Superseded by**: §2.1 の「位置」の行、§2.3 の CPU と GPU の温度の条件、§2.4 の `margin_c` は [`0186`](0186-zone-excitation-after-gate.md)（Gate の後・Guard の前に掛け、温度の条件は `cpu_max_c` / `gpu_max_c` で書く）
- **Supersedes**: なし。[0183](0183-zone-step-excitation-for-thermal-model.md) §4 で却下した案「fand の auto の曲線に zone ごとのずれを入れて
  普段から分ける」を、本記録の形（Baseline の後で上げるだけ・既定は off）に限って開き直す。0183 の決定（段の試験）は変えない
- **関連**: [0028](0028-fan-control-contracts.md) §2.3 / [0053](0053-control-shadow-mode-and-counterfactual-logging.md) / [0078](0078-air-balance-fallback-coordination.md) /
  [0085](0085-coordination-failure-bypasses-gate.md) / [0087](0087-dataset-v2-action-grid.md) / [0090](0090-authority-bound-to-loaded-config.md) /
  [0119](0119-safety-values-first-host.md) / [0121](0121-hardware-test-procedure.md) §2.1 / [0156](0156-hint-gate-spec-schema.md) /
  [0172](0172-thermal-offline-evaluation.md) / [0174](0174-no-airflow-characterization.md) / [0181](0181-dataset-anchor-stride-and-training-limits.md) /
  [0182](0182-dataset-in-step-action-mean.md) / [0183](0183-zone-step-excitation-for-thermal-model.md) /
  AGENTS.md「絶対に守るルール」2・3・5・9
- **対象 Issue**: #84 / #83（実装は #81 の協調と同じく `control/` の Baseline の側）

## 1. Context

0183 の段の試験を 2 回行った（2026-11-12・11-16）。11-16 は CPU の冷却の floor が揺れず、各段の example が 20 件中 13〜19 件残った。
試験の生データ（各段の最後の 4 分の平均と、直前の R の段との差。0.60 → 0.90）では、zone ごとの効きがはっきり見える。

| 上げた zone | `air.gpu_exhaust` | `air.top_exhaust` | `air.rear_exhaust` |
|---|---|---|---|
| Front | +0.17 ℃ | −1.47 ℃ | −0.27 ℃ |
| Rear | −0.07 ℃ | −0.99 ℃ | −2.54 ℃ |
| Top | −0.31 ℃ | −0.29 ℃ | −3.86 ℃ |

`air.rear_exhaust` の応答は 120 s で 96〜102 %、`air.top_exhaust`（Front を上げたとき）は 120 s で 50 % まで進む。

ところが、11-16 の試験を train に入れて学習した model（試行 15。ridge、train 25,597 件）の効き（+0.3 を 120 s 保つ）は、
`air.rear_exhaust` で Front +0.88・Rear −0.67・Top −1.00 ℃、`air.gpu_exhaust` で Top +1.27 ℃ と、**大きさが 3〜4 倍小さく、
向きも一部が逆**だった。1 つの zone だけが動いた example は Front 10・Rear 20・Top 25 件しかなく、残りの 2.5 万件は fand の auto が
3 zone をそろえて動かす普段の運転である（0183 §1）。zone の列が強く相関するので、数十件の試験の区間は普段の運転に埋もれる。

model が zone の効きを取り違えると、予測も外れる。試行 14 では、R1 の段の頭（auto の約 0.7 → manual の 0.6）で `air.gpu_exhaust`
の 120 s 先を 6.5 ℃ 低く外し、Fan が動いた区間の層の underprediction の 0.99 分位は 5.7 ℃ だった（0172 の上限は 2.0 ℃）。

段の試験を繰り返しても、1 回（約 1 時間 40 分・所有者の在宅が要る）で zone ごとに十数件しか増えない。普段の運転の中で、zone ごとに
独立した変化が少しずつ、しかし毎日入る形が要る。

## 2. Decision

### 2.1 何を足すか

Baseline（Fallback）の requested に、**1 つの zone だけを小さく上げる区間**（以下「揺らぎ」）を、決定論的な予定で挟む部品を足す。

- **上げるだけ。** 揺らぎの出力は、その tick の Baseline（`mode: apply` の Air Balance の協調の後の値。0078 §2.5）を、どの zone でも
  下回らない。破れたら不具合として例外にし、0085 と同じく fault にして揺らぎを止める
- **1 度に 1 つの zone だけ。** 揺らぎの区間（`on`）の間、選んだ zone の requested に `amplitude` を足す。ほかの zone は変えない。
  `1.0` を超える分は切る（切れた量は trace に残す。§2.5）
- **位置**: Fallback → Air Balance の協調 → **揺らぎ** → Model Confidence / OOD Gate → Reactive Guard → Critical Safety。
  Guard と Critical Safety は揺らぎの後に、いまと同じく掛かる（AGENTS.md ルール 2・5）。揺らぎは Guard の ceiling も Safety の
  floor・`forced_max` も迂回しない
- **ML に依存しない。** 予定は乱数の種と時刻だけから決まり、Telemetry にも model にも依存しない（ルール 3）。LLM は関わらない（ルール 1）

### 2.2 予定

- 揺らぎは `on` の区間と `off` の区間を交互に繰り返す。`on` は `on_ms` の固定、`off` は `[off_min_ms, off_max_ms]` の一様乱数
- `on` の区間ごとに、zone を `zones`（設定）から一様に選ぶ。直前と同じ zone も選びうる
- 乱数は `seed`（設定）と fand の起動の `run_id` から作る、決定論的な列とする。同じ `seed`・同じ `run_id`・同じ条件の列で、同じ予定になる
  （Replay で再現できる。#86 の受入基準と同じ考え）
- `on` の区間の途中で §2.3 の条件が外れたら、その区間をそこで打ち切り、`off` へ移る（zone を下げるのは Baseline へ戻ることで、
  Baseline より下げることではない）。条件が戻っても、打ち切った区間は再開しない

### 2.3 掛ける条件（どれか 1 つでも外れたら、その tick は揺らぎを掛けない）

- `operating_mode` が `AUTO`（`MANUAL` / `MAX` / 起動・停止の段では掛けない）
- active controller が Baseline（Fallback）。Learned MPC が active の tick では掛けない（§5 の 4）
- Critical Safety の `safety_state` が `normal`、fault が無い
- Reactive Guard が直前の tick で floor / ceiling を掛けていない
- CPU と GPU の温度が、0121 §2.1 の中止の基準より `margin_c` 以上低い（`cpu.package`・`gpu.0.core`。設定の `margin_c`）
- 設定の `allowed_hours`（ローカル時刻の時間帯）の中（§5 の 3）

### 2.4 設定

`fan-policy.yaml` に `zone_excitation` を**必須**の欄として足す（`schema_version` を 1 つ上げる。束ねた版 `CONTROL_CONFIG_VERSION` も上げる）。

```yaml
zone_excitation:
  mode: "off"                                   # off | apply。雛形の既定は off。引用符で書く（YAML 1.1 の off）
  zones: [front, rear, top]                     # 選ぶ zone。空は不正
  amplitude: {value: 0.10, status: provisional} # 足す幅（demand）。0.10 は 0182 の step の中の幅の上限と同じ（§3）
  on_ms: {value: 300000, status: provisional}   # 5 分
  off_min_ms: {value: 600000, status: provisional}   # 10 分
  off_max_ms: {value: 1200000, status: provisional}  # 20 分
  margin_c: {value: 8.0, status: provisional}   # 中止の基準からの余裕（℃）
  allowed_hours: {value: [0, 24], status: provisional}  # [開始, 終了) のローカル時刻。[0, 24] は終日
  seed: 1                                       # 予定の乱数の種
```

- `mode` は人が設定と再起動で変える（0078 §2.8 と同じ。authority journal を経ない）
- 不正な値（`amplitude` が `(0, 0.3]` の外、`off_min_ms > off_max_ms` など）は Control Config の不正として扱い、全 zone Max
  （`config_invalid`）で止まる。黙って `off` と読まない
- 値はルール 9 のとおり設定にだけ置く。コードに既定値を持たない

### 2.5 記録

- decision trace（ControlTick）に `zone_excitation` を足す: `mode`・`state`（`off` / `on` / `skipped`）・`zone`・足した量・
  `1.0` で切れた量・掛けなかった理由（§2.3 のどれか）・区間の番号。ControlTick の版を 1 つ上げる（§5 の 5）
- Thermal Dataset v2 は `effective` を action として使うので、揺らぎの区間も**普段の example としてそのまま入る**。Dataset の
  形式は変えない
- Shadow の集計（#90）と offline 評価（0172）の層に、揺らぎの区間かどうかを足すかは、記録が溜まってから決める

## 3. Consequences

良くなること:

- zone ごとに独立した変化が毎日入る。既定の値（5 分の `on`・10〜20 分の `off`）では 1 日に約 70〜95 区間、1 つの zone あたり
  約 25〜30 区間になる。各区間の前後を含めて 30 s ごとの anchor が 10 件余り入るので、1 週間で 1 つの zone あたり約 2,000 件。
  段の試験の 1 回分（十数件）の 100 倍を超える
- `amplitude` 0.10 は 0182 §2.2 の step の中の幅の上限と同じなので、揺らぎの入り・切りの step が `in_step_change` で捨てられにくい
- 上げるだけなので、冷却は弱まらない。Guard と Critical Safety は今のまま最後に掛かる
- 0089 §5 の 1・0090 §5 の 3（`AuthorityRecord` v2）が「次の版上げを待つ」としていたので、ControlTick の版上げに相乗りできる

悪くなること（と緩和策）:

| 悪くなること | 緩和策 |
|---|---|
| 1 つの zone の Fan が 5 分ずつ少し速くなり、音が変わる | `amplitude` は 0.10。`allowed_hours` で夜を外せる（§5 の 3）。`mode: "off"` と再起動で止まる |
| Baseline の運転が設定の曲線（0119）と少し違う | 上げるだけ。違いは trace に毎 tick 残る |
| Fan の電力が少し増える | 時間の約 1/4 だけ、1 つの zone の +0.10 |
| `fan-policy.yaml` の版が上がり、Control Config の digest が変わる | 0090 で authority は Baseline に戻る（今は shadow なので影響なし）。0156 の Workload Hint の証明書は認証し直す（今は `stage: off`） |
| Shadow の比較（Learned の提案 対 Baseline）の Baseline が揺らぐ | 揺らぎの状態を trace に残すので、集計で分けられる |
| 学習データの大半がまだ普段の運転のまま | 揺らぎの example の比率は徐々に上がる。足りなければ `on` の比率を上げる（設定だけ） |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| 段の試験（0183）を何度も繰り返す | 1 回で 1 つの zone あたり十数件。2,000 件に 100 回以上・所有者の在宅が毎回要る |
| 学習の側で試験の区間に重みを付ける・効きの向きに物理の制約を置く | データを足さずに済むが、数十件の試験の区間に強く依存する。trainer の形（ridge）も変える。揺らぎで足りなければ後で足す |
| 下げる揺らぎも入れる（±） | 冷却を弱める区間が普段の運転に入る。Safety の floor に当たりやすい（0183 §5 の 1 と同じ） |
| 3 zone の曲線を普段から別々にずらす（0183 §4 の案そのもの） | 運転の形が常に変わる。上げるだけ・時間の一部だけの本記録の形で足りる |
| Learned MPC の探索として入れる | MPC は Authority が shadow の間は制御しない。Baseline の側でしか、今のデータは採れない |

## 5. 未決事項

| # | 論点 | 推奨案（2026-11-19 所有者の決定はすべてこちら） | 代替案 |
|---|---|---|---|
| 1 | 足す幅 `amplitude` | **0.10**（0182 の幅の上限と同じ。試験の 0.30 で −3.9 ℃ なので、0.10 でも 1 ℃ 前後の差が出る見込み） | 0.15 / 0.20（効きは大きいが、入り・切りの step が捨てられやすい） |
| 2 | `on` / `off` の長さ | **`on` 5 分・`off` 10〜20 分**（時間の約 1/4） | `on` 3 分・`off` 5〜10 分（区間は増えるが、Top の排気の遅い応答（120 s で 50 %）を見にくい） / `on` 10 分 |
| 3 | 掛ける時間帯 `allowed_hours` | **終日**（夜の負荷の少ない時間もデータになる） | 所有者が寝ている時間を外す（例: 8〜23 時） |
| 4 | Learned MPC が active の tick | **掛けない**（Baseline の側の部品に留める） | MPC の提案にも足す（MPC の評価が揺らぎで乱れる） |
| 5 | trace の記録 | **ControlTick の版を上げて `zone_excitation` を足す**（`AuthorityRecord` v2 も相乗り） | 版を上げず、構造化ログだけに残す（Dataset と集計から区間が分からない） |
| 6 | 最初の運用 | **実装・テストの後、所有者が `mode: apply` にして 1 週間** 様子を見る。その後 Dataset → 学習 → 評価 | まず `shadow`（記録だけで掛けない）を 1 日 |
