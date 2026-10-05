# 決定記録 0090: fand は、起動時に読んだ Control Config に対して承認された分だけ authority を有効にする

- **種別**: Decision Record
- **Status**: Proposed（2026-10-05、リポジトリ所有者が方針を承認。PR #216 のマージで FINAL）
- **Date**: 2026-10-05
- **Supersedes**: なし（0057 / 0072 / 0086 / 0089 の規定は変えない。実効 stage の上限をもう1つ足すだけ）
- **関連**: [0089](0089-authority-bound-to-loaded-artifact.md)（同じ形の照合を artifact について行う。本記録はその config 版） /
  [0057](0057-authority-rollout-stage-changes.md) §2.2 / §2.4 / §2.7 /
  [0072](0072-control-admin-entry.md) §2.6 /
  [0073](0073-air-balance-control-config-integration.md) §2.6（証拠に `air_balance_config_sha256` / `fan_hardware_config_sha256` を足した） /
  [0086](0086-authority-approver-uid.md)（`coldaisle-authority raise`） /
  `docs/control-config.md`（Control Config の4ファイル） /
  AGENTS.md「絶対に守るルール」2・3・4・5
- **対象 Issue**: #92（PR #216 の Codex P1「Bind raises to the daemon's loaded config snapshot」）

---

## 1. Context

`coldaisle-authority raise` は、`--config-dir` の Control Config の4ファイル（`fan-hardware.yaml` /
`safety.yaml` / `fan-policy.yaml` / `air-balance.yaml`）を**実行した時点で**読み、その内容の hash と
証拠（`RolloutEvidence` の `fan_policy_config_sha256` / `safety_config_sha256` /
`air_balance_config_sha256` / `fan_hardware_config_sha256`）と評価報告の provenance が一致することを
確かめて journal を上げる（0057 §2.4 / 0073 §2.6）。一方 `coldaisle-fand` は Control Config を
**起動時に1回だけ**読み、再起動まで使い続ける。journal の変化は毎 tick の `stat` で読み直す（0072 §2.6）。

このため、0089 が artifact について閉じたのと同じ穴が config について残っている（Codex P1、PR #216）。

1. fand が設定 A で起動する
2. 人が4ファイルを設定 B へ差し替える（fand は読み直さない）
3. 人が B で集めた証拠で `raise` する。CLI は「いまディスクにある B」に対して正しく検証して journal を上げる
4. fand は journal の変化を次の tick で読み、**動いている A（policy・safety・hardware の設定）が
   B のために承認された authority を得る**

2026-10-05、所有者は次を決めた。**0089 と同じく fand の側で照合し、不一致は Baseline として扱う。**
これは 0057 / 0072 / 0086 / 0089 のどれにも無い安全側の挙動の追加なので、実装の前にここへ記録する。

## 2. Decision

### 2.1 照合の対象

- **承認側**: journal の昇格 event の `approval.evidence` にある4つの設定の hash
  （`fan_policy_config_sha256` / `safety_config_sha256` / `air_balance_config_sha256` /
  `fan_hardware_config_sha256`）。`raise_stage()` が書く時点の `ControlConfig.sources` と一致を確かめたもの
- **実行側**: fand が起動時に読んだ `ControlConfig` の `sources`（`ConfigSources`。4ファイルそれぞれの
  bytes の SHA-256）。以下「loaded config」。`raise_stage()` が照らすのと**同じ型・同じ単位**の値を、
  fand の組み立てで制御に使う `ControlConfig` そのものから渡す
- `AuthorityRuntime` の**既定値の無い必須の引数**（`loaded_config_sources`）にする。渡し忘れが
  「何にも照らさない runtime」を作らないためである（0089 §2.1 の `loaded_artifact_sha256` と同じ）
- 照らす単位は**ファイルごとの hash**（4つ）。どれか1つでも違えば不一致である（§5 の 1）

### 2.2 規則

`AuthorityRuntime` は、実効 stage の上限にもう1つ **config の上限**を掛ける。

- journal の stage が Baseline（`SHADOW`）なら、上限は掛けない
- それ以外は、**journal が最後に Baseline にいた時点より後の昇格 event すべて**の証拠の4つの hash が
  loaded config と一致するときだけ上限を掛けない。1件・1ファイルでも違えば上限は **Baseline**
- 証拠が `air_balance_config_sha256` / `fan_hardware_config_sha256` を持たない昇格（journal v1 の event。
  0073 §2.6）は、その2ファイルについて**一致を言えない**ので不一致として扱う（§5 の 2）
- 部分一致（例: SHADOW→LIMITED は A、LIMITED→EXPANDED は B）も Baseline にする（0089 §2.2 と同じ理由）

実効 stage は `min(journal の stage, 設定の上限, 書き残せていない降格の上限, artifact の上限, config の上限)` になる。

- **上げる方向には決して働かない。** 上限を外しても journal の stage を超えない
- artifact の上限（0089）とは独立に計算し、両方を掛ける

### 2.3 照合する時点

0089 §2.3 と同じ。起動時（journal を読んだ直後）と、journal を読み直したとき（毎 tick の `stat` の点検、
降格を書き残した直後、`reload()`）に計算し直す。loaded config は再起動まで変わらない。

### 2.4 journal を書かない

0089 §2.4 と同じ。config の上限は**実行時の上限**で、journal へ降格 event を書かない。
食い違いは fand 側の事情で、承認そのものは正しい。承認どおりの設定で再起動すれば journal どおりの stage に戻る。
自動降格（0057 §2.6）は実効 stage を見るので、config の上限で Baseline にいる間は起きない。

### 2.5 観測

0089 §2.5 に倣う。

- 上限が掛かった・外れたとき（起動時を含む）に、構造化ログを1行出す（`event`:
  `authority_config_mismatch` / `authority_config_matched`、journal の stage と revision、
  loaded config の4つの hash、食い違った昇格の revision、承認側の4つの hash、食い違ったファイルの名前）
- `AuthorityRuntime.trace_metadata()` に `authority_config_binding_ceiling`（上限だけ）を足す。
  照らした hash は載せない（設定の hash は trace の `sources` が既に持つ）。
  既にある `authority_config_ceiling` は `fan-policy.yaml` の `authority_stage`（設定が許す上限。#103）で、
  本記録の上限とは別物なので、名前を分ける
- `ControlTick` の `authority`（`AuthorityRecord` v1）には**欄を足さない**。0089 §6 の判断点 12 と同じく、
  trace の次の版上げのときに `AuthorityRecord` v2 で足す（§5 の 3）

## 3. Consequences

良くなること。

- B のために承認された authority を、A の設定で動いている fand が得ることがなくなる（逆も同じ）
- 設定を差し替えた後、fand を再起動するまで、および再起動しても新しい設定に対する承認が無い間は、
  Learned MPC の authority は Baseline になる。「承認は証拠を取った設定に束縛される」（0057 §2.4）を、
  書く時点だけでなく使う側でも満たす

悪くなること。

- **設定を差し替えたら、新しい設定については SHADOW から上げ直す**ことになる（コメントや空白だけの
  変更でも hash が変わるので同じ）。緩和: 差し替えの前に `rollback` し、新しい設定の Shadow の証拠で
  1段ずつ上げ直す。運用手順は `docs/authority-rollout.md` に、artifact の入れ替え（0089 §3）と並べて書く
- journal v1 の昇格を含む連なりは、設定が変わっていなくても Baseline になる（§5 の 2）
- trace の `AuthorityRecord` が config の上限を表さない（§2.5 / §5 の 3）

## 4. 却下した代替案

- **CLI 側で fand の loaded config を確かめる**（fand に問い合わせる）: 0089 §4 と同じ。問い合わせた後に
  fand が再起動すれば同じ穴が開く。照合は authority を**使う側**で毎回行うほうが閉じる
- **fand が4ファイルを毎 tick 読み直して合わせる**: 走行中に Safety の設定が変わることになり、
  「Control Config は起動時に1回検証して使う」前提（0028 / 0073 §2.2）を崩す
- **4ファイルをまとめた1つの hash で照らす**: 証拠はファイルごとの hash を持つので、まとめると
  CLI と証拠の照合（0057 §2.4）と単位がずれ、どのファイルが違うかもログに出せない
- **食い違いを journal へ降格として書く**・**部分一致は一致した段まで有効にする**・**fand を止める**:
  0089 §4 と同じ理由で却下する

## 5. 未決事項

所有者の判断が要る点は、すべて**安全側（Baseline）を推奨案として実装した**。

| # | 内容 | 推奨案（実装） | 決める場所 |
|---|---|---|---|
| 1 | 照らす単位。ファイルの bytes の hash にすると、コメント・空白・キーの順だけの変更でも Baseline になる | **ファイルの bytes の hash（4つ）のまま**。`raise_stage()` と証拠が照らす単位と同じで、正規化の規則を新しく持たない。意味の同じ変更でも上げ直しになるのは安全側の不便として受け入れる | PR #216 の人間レビュー |
| 2 | 証拠に `air_balance_config_sha256` / `fan_hardware_config_sha256` が無い昇格（journal v1 の event）を含む連なり | **Baseline**（一致を言えないものを一致としない）。v1 の event はもう新しく書けず（0073 §2.6）、該当する journal は rollback してから上げ直す | PR #216 の人間レビュー |
| 3 | 「実効 stage が journal より低い理由」（config の上限）を trace に載せるか | **0089 §5 の 1 と同じ**: trace の次の版上げのときに `AuthorityRecord` v2 で欄を足す。それまでは構造化ログ（`authority_config_mismatch` / `_matched`）で見る。本 PR では `ControlTick` の版を上げない | trace の次の版上げの PR |
| 4 | 4ファイル以外の入力（metric catalog・quality rules・`control-admin.yaml` 等）を照らすか | **照らさない**。証拠がそれらに束縛されていない（0057 §2.4 の対象外）ので、照らす値が無い。束縛を足すなら証拠の側から先に変える | 必要になったとき |
| 5 | `raise` の前に、fand が使っている設定と `--config-dir` が違うことを人へ知らせる手段（`coldaisle-control status` に loaded config の hash を出す等） | 運用の後（0089 §5 の 3 と一緒に） | 運用の後 |

## 6. 承認記録

**2026-10-05、リポジトリ所有者が方針を承認した**（PR #216 の Codex P1 への対応として、推奨案「0089 と同じく
照合する。新しい決定記録 0090 を立て、この PR で実装する」）。§5 の 1・2 は PR #216 の人間レビューで確かめる。
