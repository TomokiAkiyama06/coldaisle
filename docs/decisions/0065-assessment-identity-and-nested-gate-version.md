# 決定記録 0065: assessment の identity に model version を含め、入れ子の `ModelGateDecision` に版を持たせる

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-21
- **Supersedes**: [`0059-decision-trace-model-artifact.md`](0059-decision-trace-model-artifact.md)
  の **§2.1 の「`artifact_sha256` / `model_id` / `artifact_verification` /
  `input_action_ts_ms` が `prediction` のそれと一致することも要求する」という
  照合対象の列挙**（`model_version` を足す）と、**§2.2 の `ControlTick` v7 の規則**
  （入れ子の `ModelGateDecision` にも版を持たせる点を足す）だけ。
  0059 のそれ以外の決定（artifact を `ModelGateDecision` に置くこと、identity を宣言では
  なく導出にするという原則そのもの、適用 arm の artifact の勘定と報告 v2、適用側の arm を
  昇格の根拠にできるようにすること、§2.5 の「記録の無さは常に unknown であって
  completeness ではない」）は**すべてそのまま有効である**
- **関連**: [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md) §2、
  [`0048-thermal-model-artifact-and-inference.md`](0048-thermal-model-artifact-and-inference.md) §2.4、
  [`0050-model-confidence-ood-and-authority.md`](0050-model-confidence-ood-and-authority.md) §2.5、
  [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md) §2.4、
  GitHub #82 / #85 / #92 / #159
- **対象 Issue**: #159

## 1. Context

0059 は `FINAL` になったあと、レビューで2つの取りこぼしが見つかった。
**どちらも 0059 §2 の原則の側は正しく、その原則が届いていない欄・層があった。**

1. **版（`model_version`）だけが宣言のままだった**（codex #4057753197）。
   0059 §2.1 は「identity は宣言ではなく導出」と決め、`ConfidenceAssessment` が
   `prediction` を持ち、`artifact_sha256` / `model_id` / `artifact_verification` /
   `input_action_ts_ms` を `prediction` と一致させることにした。
   **`model_version` はこの列挙に入っていなかった。**
   `ConfidenceAssessment.model_version` が自由文字列、`ThermalPrediction.model_version` が
   semver という型の差を避けたためだが、判断として正しくない。

   **同じ artifact bytes を指す登録が2つあると穴になる。** artifact が同じなら
   artifact の照合は通り、推論 ID は `prediction` から導出されたまま提案と一致する。
   そこで版だけを top-level で書き換えた assessment（と同じく書き換えた提案）は、
   **B の判定を A の版の実績として通してしまう。**

2. **入れ子の `ModelGateDecision` の版を上げていなかった**（codex #4057753201）。
   0059 §2.2 は `ControlTick` を v7 へ上げ、「古い version に意味の違う欄を足さない」と
   決めたが、入れ子の `ModelGateDecision` は `schema_version: Literal[1]` のままだった。
   その結果、**v7 の trace が「v1 と名乗るのに v1 には無かった欄（`artifact_sha256`）を
   持つ」記録を書ける。** `SCHEMA_VERSION` の changelog は「`ModelGateDecision` の
   schema version も 2 へ上げる」と書いており、実装と食い違っていた。

## 2. Decision

### 2.1 照合対象に `model_version` を足す（0059 §2.1 の列挙を置き換える）

`ConfidenceAssessment` は、`prediction` の次の欄との一致を要求する。

| 欄 | 0059 §2.1 | 本記録 |
|---|---|---|
| `artifact_sha256` | 要求 | 要求 |
| `model_id` | 要求 | 要求 |
| **`model_version`** | **（列挙に無い）** | **要求** |
| `artifact_verification` | 要求 | 要求 |
| `input_action_ts_ms` | 要求 | 要求 |

**版も、識別子の導出に入っている側（`prediction`）からしか動かせない。**
`inference_id` は `derive_inference_id(input_sha256, prediction)` と一致しなければ
型として作れないので（0059 §2.1。この規則は変えない）、次が閉じる。

| 手口 | どこで落ちるか |
|---|---|
| `model_version` の欄だけ書き換える | `prediction` と食い違う → 型が拒む |
| `prediction` の版まで書き換える | `inference_id` の導出が合わない → 型が拒む |
| 識別子も作り直す | もう同じ推論ではない。提案の `inference_id` と合わない → Gate が退ける |

**`ConfidenceAssessment.model_version` は事実上 `ThermalPrediction` と同じ semver になる。**
Gate の `expected_model_version` には Registry の `ArtifactAttestation.version` を渡すので、
運転時の値はもともと semver である。

### 2.2 入れ子の `ModelGateDecision` に版を持たせる（0059 §2.2 へ足す）

`MODEL_GATE_SCHEMA_VERSION = 2` を置き、`ModelGateDecision.schema_version` を
`Literal[1, 2]` にする。

- **v1 の判断は `artifact_sha256` を持てない。** 古い version に意味の違う欄を足さない
  （0030 §2 / 0059 §2.2 と同じ規則を、入れ子にも掛ける）
- `ControlTick` の **v7 以降は v2 の判断を要求**し、**v1〜v6 は v1 を要求**する
- **保存済みの v1 の判断はそのまま読める**

0059 §2.2 の「v7 の attested な判断は artifact 必須」「欄の無い tick は artifact 不明」
という規則は変えない。版の判定が `ControlTick` の版だけから、入れ子の版と合わせた
2層になる。

## 3. Consequences

良くなること。

- **版を宣言で言い直せない。** 同じ artifact を指す登録が複数あっても、どの版の推論かは
  `prediction` から決まる
- **「v1 と名乗るのに v1 には無かった欄を持つ」記録を書けない。** 読む側が
  `schema_version` だけで中身を言える（#74 / 0060 が `ControlTick` で立てたのと同じ線）

悪くなること。

- `ConfidenceAssessment.model_version` に semver 以外を入れられなくなる。運転時は
  もともと Registry の版なので影響は無いが、**試験の便宜で使っていた自由文字列
  （`thermal-v1` など）は使えない。** 実測の前に気づける種類の変更である
- 版の判定が2層になる。`ControlTick` の版だけを見て入れ子の形を決められない

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `model_version` を照合対象から外したままにする | 同じ artifact bytes を指す登録が2つあると、版だけを書き換えた assessment が別の版の実績になる（codex #4057753197）。型の差（自由文字列 vs semver）は理由にならない |
| `ThermalPrediction.model_version` を自由文字列へ広げる | Registry の版の形を緩めることになる。**照合のために縛りを外す**のは向きが逆 |
| 入れ子の `ModelGateDecision` の版を上げず、`ControlTick` の版だけで読み分ける | v7 の trace が「v1 と名乗るのに v1 には無かった欄を持つ」記録を書ける（codex #4057753201）。`SCHEMA_VERSION` の changelog とも食い違う |
| 0059（`FINAL`）を直接書き換えて済ませる | `docs/decisions/README.md`「追記のみ」に反する。**FINAL の記録に許されるのは `Superseded by` の追記だけ**である。範囲を絞った新しい記録を作る |

## 5. 未決事項

- **所有者の承認が要る。** 本記録は 0059（`FINAL`）の §2.1 の列挙と §2.2 の規則を
  置き換える。**コード側の変更は小さな締め直しだが、安全系・制御系の設計変更は
  人間レビューが必須である**（AGENTS.md）。承認までは `Proposed`
- `ConfidenceAssessment` が `prediction` を丸ごと持つことの重さ（tick ごとの in-memory
  object）。いまは digest（`input_sha256`）と予測だけで、入力 window は持っていない。
  実運用の tick 周期で問題になるようなら別の記録で見直す
