# 決定記録 0089: fand は、いま使っている artifact に対して承認された分だけ authority を有効にする

- **種別**: Decision Record
- **Status**: FINAL（2026-10-01、リポジトリ所有者が方針を承認。2026-10-05、PR #216 の判断点 10〜12 を推奨案で承認。§6）
- **Date**: 2026-10-01
- **Supersedes**: なし（0057 / 0072 / 0086 の規定は変えない。実効 stage の上限を1つ足すだけ）
- **関連**: [0057](0057-authority-rollout-stage-changes.md) §2.2 / §2.3 / §2.7 /
  [0059](0059-decision-trace-model-artifact.md) §2.1 /
  [0072](0072-control-admin-entry.md) §2.6 /
  [0077](0077-learned-proposal-handoff.md) §2.6（起動時に1回読む registry と `registry_superseded`） /
  [0086](0086-authority-approver-uid.md)（`coldaisle-authority raise`） /
  `docs/model-registry.md`（制御 loop は registry を読み直さない） /
  AGENTS.md「絶対に守るルール」2・3・4・5
- **対象 Issue**: #92（PR #216 の Codex P1「Bind authority raises to fand's loaded artifact」）

---

## 1. Context

`coldaisle-authority raise`（PR #216）は、**CLI を実行した時点の** Registry の production pointer を
`ModelRegistry.pinned()` で読み、その artifact の証拠で journal を上げる（0057 §2.3）。一方
`coldaisle-fand` は registry を**起動時に1回だけ**読み、その artifact を再起動まで使い続ける
（`docs/model-registry.md`、0077 §2.6）。journal の変化は毎 tick の `stat` で読み直す（0072 §2.6）。

このため次が起こりうる（Codex P1、PR #216）。

1. fand が artifact A で起動する
2. 人が Registry の production を B へ入れ替える（`coldaisle-registry promote`。journal は動かない。0057 §2.3）
3. 人が B の評価報告で `raise` する。CLI は「いま production の B」に対して正しく承認を検証して journal を上げる
4. fand は journal の変化を次の tick で読み、**動いている A が B のために承認された authority を得る**

0057 §2.3 の「承認を artifact に束縛する」は、書く瞬間の Registry については守っているが、
**承認された artifact と実際に制御している artifact が同じであること**は誰も確かめていない。
0057 の不変条件「Production の入れ替えで authority は動かない」（journal を書かない）も、
この食い違いの下では「入れ替わった後の artifact へ、前の artifact の authority がそのまま渡る」と
読めてしまう（逆向きも同じ）。

2026-10-01、所有者は次を決めた。**fand の側で照合し、不一致は Baseline として扱う。**
これは 0057 / 0072 / 0086 のどれにも無い安全側の挙動の追加なので、実装の前にここへ記録する。

## 2. Decision

### 2.1 照合の対象

- **承認側**: journal の昇格 event の `approval.evidence.artifact_sha256`（`RolloutEvidence`。0057 §2.4 で
  `raise_stage()` が「いま Production の artifact」と一致を確かめて書いたもの）
- **実行側**: fand が起動時に束縛し、使い続けている artifact の SHA-256（以下「loaded artifact」）。
  Controller Gate の `expected_artifact_sha256`（0059 §2.1）と**同じ値・同じ出どころ**から渡す
  （fand の組み立てで1つの変数から両方へ渡す。片方だけを更新できない形にする）
- 対象の kind は `raise_stage()` の既定と同じ `thermal_model`（Learned MPC の artifact）。
  RL Supervisor の policy artifact へ authority を渡す経路はまだ無いので、ここでは扱わない（§5）

### 2.2 規則

`AuthorityRuntime` は、実効 stage の上限にもう1つ **artifact の上限**を掛ける。

- journal の stage が Baseline（`SHADOW`）なら、上限は掛けない（Baseline より下は無い）
- loaded artifact が無い（`None`。worker 未配線・束縛する artifact が無い構成）なら、上限は **Baseline**
- それ以外は、**journal が最後に Baseline にいた時点より後の昇格 event すべて**（いまの stage へ至る
  昇格の連なり）の `approval.evidence.artifact_sha256` が loaded artifact と一致するときだけ上限を掛けない。
  1件でも違えば上限は **Baseline**

実効 stage は `min(journal の stage, 設定の上限, 書き残せていない降格の上限, artifact の上限)` になる。

- **上げる方向には決して働かない。** 上限を外しても journal の stage を超えない
- 一部だけ一致する連なり（例: SHADOW→LIMITED は A、LIMITED→EXPANDED は B）も Baseline にする。
  「A に対して承認された LIMITED までは有効」とはしない。B の証拠は B が LIMITED で走った実績に
  束縛されている前提で集められ、A の LIMITED の上に積んだ判断ではないため（所有者の指示「違えば
  Baseline に倒す」を、部分一致にも適用する）。**2026-10-05、所有者が承認した**（PR #216 の判断点 10。§6）

### 2.3 照合する時点

- 起動時（journal を読んだ直後）と、journal を読み直したとき（0072 §2.6 の毎 tick の `stat` の点検、
  降格を書き残した直後、`reload()`）に計算し直す。journal が変わらない限り結果は変わらないので、
  「毎 tick の journal の変化検知と同じ所」で照合したことになる
- loaded artifact は再起動まで変わらない（registry を読み直さない。`docs/model-registry.md`）。
  Registry の production が走行中に変わったことの検知と worker の役割の停止は 0077 §2.6 の
  `registry_superseded` が受け持つ。本記録は authority の側だけを閉じる

### 2.4 journal を書かない

artifact の上限は**実行時の上限**で、journal へ降格 event を書かない（設定の上限と同じ扱い）。

- 書くと、A の fand が一時的に動いているだけで B のための承認が journal から失われ、B で再起動しても
  承認をやり直すことになる。食い違いは fand 側の事情で、承認そのものは正しい
- 書かないので、正しい artifact で再起動すれば journal どおりの stage に戻る（上げるのではなく、
  journal が既に表している承認がその artifact に対して有効になる）
- 自動降格（0057 §2.6）は実効 stage を見るので、artifact の上限で Baseline にいる間は起きない

### 2.5 観測

- 上限が掛かった・外れたとき（起動時を含む）に、構造化ログを1行出す（`event`:
  `authority_artifact_mismatch` / `authority_artifact_unbound` / `authority_artifact_matched`、
  journal の stage と revision、loaded artifact、食い違った昇格の revision と `artifact_sha256`）
- `AuthorityRuntime.trace_metadata()` に `authority_artifact_ceiling`（上限だけ）を足す。照らした artifact の
  hash は載せない（0057 §2.7「stage と model は独立に残す」。artifact は Gate と registry の記録が持つ）
- `ControlTick` の `authority`（`AuthorityRecord` v1）には**欄を足さない**。足すと `AuthorityRecord` と
  `ControlTick` の版上げになり、本記録の範囲（#216）を超える。いまの trace でも実効 stage
  （`state.authority_stage`）は記録した上限の最小**以下**であればよい（schema が拒むのは超えたときだけ）ので、
  矛盾はしない。ただし trace だけからは「なぜ journal より低いか」を言えない（§5）

## 3. Consequences

良くなること。

- B のために承認された authority を、動いている A が得ることがなくなる（逆も同じ）
- Production を入れ替えた後、fand を再起動するまで、および再起動しても新しい artifact に対する承認が
  無い間は、Learned MPC の authority は Baseline になる。0057 §2.3 の「Model の入れ替えで authority が
  暗黙に上がらない」を、実行側でも満たす
- 束縛する artifact を持たない fand（いまの `coldaisle-fand`）は、journal が上がっていても Baseline より
  上を有効にしない。提案が来ない構成で authority の数字だけが上がって見える状態が無くなる

悪くなること。

- **いまの `coldaisle-fand` は常に Baseline で動く**（worker 未配線で loaded artifact が無い）。
  Learned の提案が来ないので制御の結果は変わらないが、`state.authority_stage` は journal より低く出る。
  緩和: 起動時と変化時の構造化ログで理由を出す。worker を配線するとき（0077 段階 2）に loaded artifact を渡す
- Production を入れ替えた後は、新しい artifact について SHADOW から承認をやり直すことになる
  （0057 の「Production の入れ替えで journal は動かない」は変わらないが、journal の stage が新しい artifact に
  対して有効でなくなる）。緩和: 入れ替えの前に rollback するか、入れ替えた artifact の Shadow の証拠で
  1段ずつ上げ直す。運用手順は `docs/authority-rollout.md` に書く。**2026-10-05、所有者がこの運用を
  承認した**（PR #216 の判断点 11。§6）
- trace の `AuthorityRecord` が artifact の上限を表さない（§2.5 / §5）

## 4. 却下した代替案

- **CLI 側で fand の loaded artifact を確かめる**（fand に問い合わせる・trace を読む）: CLI から fand へ
  問い合わせる経路を増やすことになり、問い合わせた後に fand が再起動すれば同じ穴が開く。照合は
  authority を**使う側**で毎回行うほうが閉じる
- **食い違いを journal へ降格として書く**: §2.4。承認が正しいのに journal から失われる
- **部分一致は一致した段まで有効にする**: §2.2。証拠の積み上げの前提が崩れるうえ、規則が複雑になる。
  所有者の指示は「違えば Baseline」
- **fand を止める・Max にする**: artifact の食い違いは安全の問題ではなく制御権の問題で、Fallback で
  冷却は続けられる（AGENTS.md ルール4。0077 の「worker の異常はすべて Fallback で Max にしない」と同じ考え方）

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | **方針は決着**（2026-10-05 所有者承認。PR #216 の判断点 12）: 「実効 stage が journal より低い理由」（artifact の上限）は、trace の次の版上げのときに `AuthorityRecord` v2 で欄を足す。それまでは構造化ログ（`authority_artifact_mismatch` / `_unbound` / `_matched`）で見る。本記録の PR（#216）では `ControlTick` の版を上げない。以下は判断前の記録。`AuthorityRecord` に artifact の上限（と loaded artifact）を足すか（`AuthorityRecord` v2・`ControlTick` の版上げ） | **後続**: trace の次の版上げの PR。欄の形（上限だけか、loaded artifact も載せるか。§2.5 の 0057 §2.7 との関係）はそのときに決め、必要なら別の記録 |
| 2 | RL Supervisor の policy artifact へ authority を渡すときの照合（kind ごとの loaded artifact） | RL Supervisor を active にする記録（0061 の側） |
| 3 | `raise` が「fand が今使っている artifact」でないことを、承認の前に人へ知らせる手段（`coldaisle-control status` に loaded artifact を出す等） | 運用の後 |

## 6. 承認記録

**2026-10-05、リポジトリ所有者は PR #216 の「人間レビューが必要な点」10〜12 を、すべて推奨案（実装どおり）で決めた。**

| PR #216 の判断点 | 決定 | 本記録 |
|---|---|---|
| 10 | 昇格の連なりの一部だけが loaded artifact と一致する場合も、実効 stage の上限を Baseline にする（実装のまま） | §2.2 |
| 11 | Production の artifact を入れ替えたら、新しい artifact については SHADOW から上げ直す運用にする（実装と `docs/authority-rollout.md` の手順のまま。入れ替えの前の rollback を勧める） | §3 |
| 12 | 「実効 stage が journal より低い理由」を trace に載せるのは、trace の次の版上げのときに `AuthorityRecord` v2 で欄を足す。それまでは構造化ログで見る。本 PR では `ControlTick` の版を上げない | §2.5 / §5 #1 |

これにより本記録を FINAL とした。§5 の 2・3 は開いたまま。
