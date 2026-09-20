# 決定記録 0057: Authority Rollout の stage 変更（承認・証拠・自動降格・設定の上限）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-20
- **Supersedes**: なし
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md)、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.5 / §2.8 / §2.9、
  [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md)、
  [`0037-model-registry-rollback-target.md`](0037-model-registry-rollback-target.md)、
  [`0050-model-confidence-ood-and-authority.md`](0050-model-confidence-ood-and-authority.md) §2.4 / §2.5、
  [`0053-control-shadow-mode-and-counterfactual-logging.md`](0053-control-shadow-mode-and-counterfactual-logging.md)、
  [`0054-offline-evaluation-attribution-and-gates.md`](0054-offline-evaluation-attribution-and-gates.md) §2.4 / §2.5 / §2.7、
  GitHub #78 / #79 / #85 / #90 / #91 / #103 / #104
- **対象 Issue**: #92

## 1. Context

0028 §2.5 (b) は「authority stage の昇格は人だけ」と決めたが、**その人の承認を
何に束縛するか**と、**系が自分で下げてよい条件**を決めていない。#103 は
`fan-policy.yaml` に `authority_stage` を置いたが、設定は再起動時にしか反映されない
（0028 §2.8「live reload を行わない」）。**異常時に即座に制御権を下げる**という
Issue #92 の原則は、設定だけでは満たせない。

加えて、#104 が Production artifact の昇格を実装したことで境界が問題になる。
Model を Production にすることと、その Model に**どれだけの実 Fan 制御権を渡すか**は
別の判断である。片方がもう片方を動かすと、「評価の通った新しいモデルに入れ替えたら、
気づかないうちに Full Authority になっていた」が起こりうる。

そして #85 / #90 / #91 のレビューで繰り返し見つかった失敗の型がある。**同じ穴を開けない。**

- 束縛していない証拠を「記録されているから」として通す
- 完全でない証拠（一部の条件・一部の区間）を全体の合格として扱う
- 証拠の時刻ではなく処理した時刻を使い、古い証拠で判断する
- 承認・証拠を使い回せる形にしてしまう
- 黙って上限を広げる

未決だったのは6点である。

1. stage の**正本**をどこに置くか（設定か、別の状態か）
2. 昇格の承認を何に束縛するか
3. 昇格の証拠（#91 の rollout gate）をどこまで要求するか
4. 段跳び（Shadow → Full）を許すか
5. 系が自分で下げてよい条件と、その下げ幅
6. 降格を書き残せなかったときに、下げたままにするか戻すか

## 2. Decision

### 2.1 Authority stage は Model Registry と**別の状態**として持つ

`AuthorityStore` が `authority.json` を1つ持つ。中身は `AuthorityJournal`
（`revision` / `stage` / `events`）で、**stage は event から再現できなければ読まない**。
Model Registry（#104）の `registry.json` とは別 file・別 revision にする。
同じ file に同居させると、artifact の promotion と authority の変更が1つの revision を
共有し、片方の操作がもう片方の状態を動かせてしまう。

**既定は `SHADOW`。** journal が無ければ Shadow から始まる。壊れた journal は
「記録の無い状態」と読み替えず、`AuthorityStateError` で止める（壊すだけで静かに
rollback できてしまうため）。

### 2.2 `fan-policy.yaml` の `authority_stage` は「いまの stage」ではなく**上限**にする

実効 stage は次の3つのうち**最も低いもの**とする。

1. journal の stage（人の承認で上がり、自動降格で下がる）
2. 検証済み設定の `authority_stage`（#103）
3. この process が自分で下げた上限（2.6）

設定を下げれば再起動後に実効 stage が下がり、設定を上げても journal は上がらない。
**設定を編集するだけで制御権が増えることはない。** Controller Gate 側でも同じ上限を
掛ける（配線の誤りを1箇所だけで止めない）。

### 2.3 stage を上げられるのは人の承認だけ。**Model promotion は stage を動かさない**

`StageApproval` は「上げる」ためだけの型で、**降格の承認は型として存在しない。**
型を作らないことで「降格にも承認が要る」実装を書けないようにする。

承認は次のすべてに束縛する。1つでも合わなければ昇格は通らない。

- `expected_revision` が journal のいまの revision と一致する（**使い回せない**）
- `from_stage` が journal のいまの stage と一致する
- `approver` / `reason` / `approved_at_ms` を持ち、journal の event と一致する
- `approved_at_ms` が未来でなく、`authority_rollout.approval_max_age_ms` 以内である
- `to_stage` が設定の上限（2.2）を超えない

`ModelRegistry` は `AuthorityStore` を参照しない。Production の入れ替えは journal を
1行も書かない。

### 2.4 昇格の証拠は**完全・新鮮・束縛済み**でなければならない

承認は Offline Evaluation の報告（#91 / 0054）を `RolloutEvidence` で指す。
**数値を写さない。** 識別子だけを持ち、判定は報告そのものを読み直して行う。
昇格時に次をすべて確かめる。

- 報告の bytes の digest が `report_sha256` と一致する
- `provenance.conditions_sha256` が承認の指した条件と一致する（同じ条件で比べている）
- `provenance.fan_policy_config_sha256` / `safety_config_sha256` が**いま動いている設定**と一致する
- 報告に現れた model artifact が、**いま Production の artifact ちょうど1つ**である
  （複数混ざった報告は帰属が決まらないので使わない）
- 報告に現れた authority stage が、**いまの stage 以下**で、かつ**いまの stage を含む**
- 証拠の新しさは**報告の run の `end_ms` の最大**で測り、`evidence_max_age_ms` 以内である。
  0054 §2.7 により報告は生成時刻を持たないので、**自己申告ではなく中に記録された観測時刻**を使う
- その arm の gate 判定が**存在し、すべて `pass`** である（判定していないことを合格にしない）

`approval_max_age_ms <= evidence_max_age_ms` を設定検証で強制する。承認のほうが
長生きすると、承認だけ取って書き込みを遅らせることで期限切れの証拠での昇格が通る。

### 2.5 昇格は1段ずつ

`SHADOW → LIMITED → EXPANDED → FULL` の隣へしか上げられない。`StageApproval` の型が
1段しか表現できないので、**段跳びの承認は作れない。** 各段で「その段で運転した証拠」
（2.4）を求めるため、飛ばすと根拠の無い段が生まれる。

### 2.6 降格は承認なしで、**その場で**効く

`AuthorityRuntime` が毎 tick 観測し、次のときに下げる。

| 条件 | 下げ先 |
|---|---|
| Critical Safety が `EMERGENCY`（`safety_emergency`） | `SHADOW`（Baseline） |
| Gate の降格推奨（#79 の `demote_window_ms` / `demote_after`。`repeated_fallback`） | 1段下 |
| `unhealthy_window_ms` の中で OOD が `ood_after` 件（`persistent_ood`） | 1段下 |
| 同じ窓で LOW confidence が `low_confidence_after` 件（`persistent_low_confidence`） | 1段下 |
| 人の rollback（`rollback_to_baseline`） | `SHADOW` |

**適用は永続化の成否に依存しない。** 先に in-memory の上限を下げ、そのあとで journal へ
書く。書けなくても下げたままにして、失敗の理由を trace と runtime の状態に残す。
書けなかったら元へ戻す実装だと、disk が一杯な間ほど高い authority で回り続ける。
**読み直し（`reload()`）でも戻らない。** 戻るなら、降格を「読むだけ」で取り消せてしまう。

下がったあとに自動で戻る経路は無い。戻すには 2.3 の承認が要る。

### 2.7 stage は model version と**独立に** decision trace へ残す

- `ControllerSelection.authority_stage`: **提案の無い tick にも残す。**
  `model_gate` は Learned 提案があった tick にしか出ないので、そこにだけ書くと
  「Fallback で回っていた区間の stage」が trace から消える
- `model_gate.authority_stage` と食い違う値は schema が拒む
- `AuthorityRuntime.trace_metadata()`: 実効 stage・journal の stage・設定の上限・
  直近の変更（種別・主体・理由・時刻）・永続化の失敗。**model version を含めない**

Critical Safety は全 stage で同一である。`src/coldaisle/control/safety/` は
authority stage を読まない（試験で走査する）。

### 2.8 設定（`fan-policy.yaml` v9）

`authority_rollout` に `approval_max_age_ms` / `evidence_max_age_ms` /
`unhealthy_window_ms` / `low_confidence_after` / `ood_after` を `status` / `basis` 付きで置く。
値はすべて実測前の暫定値として扱い、コードに既定値を置かない。
v8 からの移行は自動補完せず、v1〜v8 は起動前に拒否する。

## 3. Consequences

良くなること。

- Model の入れ替えと制御権の大きさが**別々に**動く。新しい Production へ切り替えても
  authority は暗黙に上がらない
- 異常時の降格が、設定の再起動を待たずに効く。Baseline への rollback は常に1手
- 昇格の根拠が journal に残り、**誰が・いつ・何を見て**上げたかを後から読める
- 承認も証拠も使い回せない。貯めた承認や古い報告で上げられない

悪くなること。

- **書き残せなかった降格は、再起動で戻る。** in-memory の上限は process の寿命しか
  持たない。緩和として、永続化の失敗は `persist_failure` として trace と runtime に残し、
  運用者が気づけるようにする。再起動後も Gate 側の1 tick ごとの退避（#79 / #85）と
  Critical Safety（#78）はそのまま効くため、危険側へ倒れるのは「高い stage で
  ML の提案を採りうる」ところまでである
- 段跳びができないので、Full までに3回の承認が要る。学習初期の運用としては意図どおりだが、
  実機の rollout は #92 の受入基準どおり GPU サーバーが要る
- `unhealthy_window_ms` の中で不健全が**散発**するだけでは下がらない。連続でなく件数で
  数えるため、窓と件数の値の決め方が効く。実測前は保守側の暫定値で運用する

## 4. 却下した代替案

- **設定（`fan-policy.yaml`）だけで stage を持つ。** 自動降格が次の再起動まで効かない。
  live reload を入れれば効くが、0028 §2.8 が「設定変更は必ず `STARTUP` の Max を通す」と
  決めた理由（検証していない組み合わせで運転しない）を崩す
- **Model Registry の snapshot に authority stage を入れる。** 1つの revision を共有すると、
  promotion の CAS が authority の変更と競合し、逆に authority の変更が promotion を
  弾く。境界（#104 と #92）が実装の中で溶ける
- **rollout gate が通ったら自動で昇格する。** Issue #92 の原則「Stage を自動で勝手に
  上げない」に反する。gate は助言であり（0054 の module docstring）、判断は人が行う
- **降格も承認制にする。** 異常時に人を待つことになる。安全側への移動に承認を求めない
- **証拠の新しさを報告の生成時刻で測る。** 0054 §2.7 が「報告は生成時刻を持たない」と
  決めており、持たせると同じ入力から同じ bytes が出なくなる。run の `end_ms` を使う
- **別 stage で取った証拠も認める。** 降格後の再昇格が、降格前の高い authority の
  実績で素通りする

## 5. 未決事項

- **`approval_max_age_ms` / `evidence_max_age_ms` / `unhealthy_window_ms` /
  `low_confidence_after` / `ood_after` の値。** 実データが無いので `provisional` のまま
  置く。Shadow 期間（#90）と Offline Evaluation（#91）の実績で所有者が決める
- **各段に必要な運転期間**（Shadow で何日、LIMITED で何日）。いまは「その段で運転した
  証拠があること」しか求めていない。期間の下限を gate 条件として 0054 の設定へ足すか、
  ここで持つかは、実運用の1周目を見てから決める
- **降格の永続化に失敗したまま再起動したときの扱い。** いまは trace と runtime の状態に
  残すだけで、起動時に「前回書けなかった」を知る手段が無い。#82 の decision trace から
  読み取って起動時に Baseline から始める案があるが、trace の保存先（0030）が Proposed の
  ままなので、そちらの確定後に別の記録で決める
- **管理操作の入口。** いまは `AuthorityStore` の API だけで、CLI も API も無い。
  読み取り API（#23）は制御を変えられないので、昇格・rollback の入口を
  どこに置くか（CLI か、0045 の書き込み専用ソケットか）は別 Issue で決める

**この記録は Proposed である。安全系の設計変更なので、リポジトリ所有者の承認を
マージの条件とする（AGENTS.md「迷ったら」）。**
