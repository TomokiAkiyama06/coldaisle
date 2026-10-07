# 決定記録 0106: learned simulator の step の判定の記録の中身と、episode / 学習報告の schema 版

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が推奨案で承認。§6）
- **Date**: 2026-10-07
- **Supersedes**: なし（[0074](0074-supervisor-shadow-wiring-and-episode-evaluation.md) §2.2 の episode の版 2 の上に版 3 を足す。0074 の他の点は変えない）
- **関連**: [0050](0050-model-confidence-ood-and-authority.md) §2.2 /
  [0058](0058-rl-supervisor-training-environment.md) §2.2 / §2.3 / §2.6 /
  [0074](0074-supervisor-shadow-wiring-and-episode-evaluation.md) §2.2 /
  [0079](0079-model-artifact-formats.md) §2.4 / §2.5 / §2.9 の段 6 / §5 #8 /
  [0084](0084-model-artifact-anchor-support.md) §2.1 / §2.2
- **対象 Issue**: #105（PR #247。0079 §2.9 の段 6）

## 1. Context

0079 §2.9 の段 6 は「`AttestedThermalDynamics.bind` を段 2 の型だけにし、同梱 Profile で step の OOD を
**記録**する（`promotable` の条件は変えない）」とだけ決めている。PR #247 の実装で、次の4点は記録に明文が無い。

1. 「step の OOD」として何を記録するか
2. simulator の中で residual の証拠をどう扱うか
3. 判定に使う `model_confidence` の設定をどこから取るか
4. 記録の欄を足した `EpisodeResult`（版 2）と、それを入れ子に持つ `SupervisorPolicyTrainingReport`（版 2）の
   schema 版をどうするか。PR #247 の Codex レビュー（P1）は、欄を足したまま版 2 を名乗ると、
   古い reader は新しい結果を `extra="forbid"` で拒み、新しい reader は記録の無い旧 `registry_attested` の
   step を拒むので、版を上げるべきだと指摘した。`TRAINING_REPORT_SCHEMA_VERSION` の docstring も
   「入れ子の比較の形が変わったら上げる」としている

2026-10-07 にリポジトリ所有者が §2 の4点をすべて推奨案で承認した（§6）。版の扱い（§2.4）は承認の後に
PR #247 で実装した。数値のしきい値・設定値は新しく決めない。

## 2. Decision

### 2.1 記録の中身（決着（2026-10-07 所有者の決定、推奨案））

`StepRecord.simulator_assessment`（`LearnedStepAssessment`）に、step ごとに次の2つを残す。

- (a) **入力 window の anchor 推論の判定**: 運転時と同じ `CounterfactualConfidenceAssessor`（同梱 Profile v2 と
  `model_confidence`）が出す `confidence`・`ood`・OOD の構成要素（detail 付き。0050 §2.2 の形のまま）。
  held の列の support（0084 §2.1）もここに含まれる
- (b) **掛けた action 列の step ごとの support**: その step の applied を action schema の格子の間保った列を、
  window の action を anchor にして `StepSupportChecker`（候補 plan と同じ関数・margin なし。0084 §2.2）に
  照らし、外れた最初の点（step・zone・cell）

`registry_attested` の採点できる step は記録を**必ず**持ち、他の出どころの step は持てない。記録は遷移・採点・
`promotable` の条件に効かない（0079 §5 #8 のまま）。

### 2.2 residual の証拠は渡さない（決着（2026-10-07 所有者の決定、推奨案））

simulator の中に実測は無く、自分の予測と照らしても drift は測れない。`residual=None` で判定する
（`cap_before_residual_evidence` が掛かる）。記録の `confidence` は運転時の値と同じ意味ではないので、
読むときは `ood` と構成要素を見る。

### 2.3 設定は `fan-policy.yaml` の `model_confidence` を `bind` に明示して渡す（決着（2026-10-07 所有者の決定、推奨案））

環境は生成時に、dynamics の設定が自分の `fan-policy.yaml` の `model_confidence` と一致すること、
環境の `episode.step_ms` が artifact の action schema の `step_ms` と一致することを確かめ、違えば受け取らない。
条件 hash（`dynamics.conditions()`）に同梱 Profile の hash と設定を入れる。

### 2.4 schema 版を上げる（決着（2026-10-07 所有者の決定、推奨案）。PR #247 の Codex P1 への対応）

- `EPISODE_SCHEMA_VERSION` を 2 → **3** に上げ、`PolicyComparison` も同じ版を持つ
- `TRAINING_REPORT_SCHEMA_VERSION` を 2 → **3** に上げる（入れ子の比較の形が変わったため）
- **版 2 の episode と学習報告は、版の不一致として読まない。** 0074 §2.2 が v1 → v2 で採った扱いと同じで、
  同じ条件・同じ seed から回し直せば版 3 として同じ意味の結果になる（0058）
- `PolicyEpisodeReport`（`POLICY_EPISODE_REPORT_SCHEMA_VERSION` 1）と supervisor policy artifact（版 1）は
  版を上げない。実装時に確かめたとおり、どちらも入れ子に `EpisodeResult` / `PolicyComparison` の形を持たず、
  比較の digest と条件 hash（`comparison_sha256` / `conditions_sha256`）を持つだけである。v3 の比較から作った
  報告は、その digest が v3 の比較を指す
- 版 3 で `promotable` の意味は変えない

## 3. Consequences

- learned simulator の episode を、OOD / support 外の step を含むかで区別して読める（#105 の受入基準
  「uncertainty/OOD な episode を区別して評価できる」）
- 版 2 の結果と版 3 の結果が同じ版を名乗らない。古い reader と新しい reader の食い違いが、入れ子の欄の
  不足ではなく版の不一致として出る
- 悪くなること: 既存の版 2 の episode と学習報告が読めなくなる。緩和策は、同じ条件・同じ seed からの
  回し直し（0074 と同じ）。実在の反実仮想 artifact v2 はまだ無いので、`registry_attested` の版 2 の結果は
  運用上存在しない

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 版 2 のまま欄を足す（PR #247 の初版） | 古い reader と新しい reader が互いの結果を、版ではなく欄の不足 / 余分で拒む（Codex P1） |
| 記録を任意の欄にして、旧 `registry_attested` の step も読めるようにする | 記録を落とした step を型が通す。記録の必須を validator で守れない |
| (a) だけを記録する | 掛けた action 列（anchor → 最初の step の跳びを含む）が support の外でも、入力 window が support の中なら見えない |
| (b) だけを記録する | 入力 window の観測が学習範囲の外（feature range・欠測の組・support cell）でも見えない |
| simulator の予測を「実測」として residual の証拠にする | 自分の予測と照らすので drift は常に 0 に近く、運転時の判定より甘く見える |
| 判定の設定を `rl-training.yaml` に別に持つ | 運転時と違う設定で判定した記録になり、同じ Profile の OOD を別の基準で数える |

## 5. 未決事項

- OOD / support 外の step を `promotable` の条件に入れるか（0079 §5 #8）。#105 の後続の記録
- `PolicyEpisodeReport`（#91）が learned simulator の OOD の数を表に出すか。#91 / #92 側の記録

## 6. 所有者の決定（2026-10-07）

PR #247 で示した推奨案を、2026-10-07 にリポジトリ所有者がすべて承認した。

| # | 判断点 | 決定 |
|---|---|---|
| 1 | 記録の中身（§2.1） | 推奨案: (a) 入力 window の anchor 推論の判定と (b) 掛けた action 列の step ごとの support の両方を記録する。記録だけで `promotable` の条件は変えない |
| 2 | residual の証拠（§2.2） | 推奨案: simulator の中では渡さない（`residual=None`） |
| 3 | 判定の設定の出どころ（§2.3） | 推奨案: `fan-policy.yaml` の `model_confidence` を `bind` に明示して渡し、環境が自分の設定との一致と刻みの一致を確かめる |
| 4 | schema 版（§2.4） | 推奨案: `EPISODE_SCHEMA_VERSION` と `TRAINING_REPORT_SCHEMA_VERSION` を 3 に上げ、版 2 は版の不一致として読まない。`PolicyEpisodeReport` と policy artifact は入れ子の形を持たないので据え置く |
