# Authority Rollout

#92 は **Production の model / policy artifact へ、どこまで実 Fan 制御権を渡すか**を持つ。
どの artifact を Production にするかは Model Registry（#104 / `docs/model-registry.md`）の
責務で、**Model を Production へ昇格させても authority は動かない。**

設計上の決定は決定記録 0057（Proposed。所有者の承認が要る）。ここは使い方をまとめる。

## Stage

```text
SHADOW  →  LIMITED  →  EXPANDED  →  FULL
```

- `SHADOW`: Learned MPC は提案だけ。実 Fan は Fallback が作る（#90 の counterfactual 記録は続く）
- `LIMITED` / `EXPANDED`: `authority_limits` の帯と許可 zone の中でだけ Learned の値を採る
- `FULL`: 帯を掛けない。Reactive Guard と Critical Safety は**全 stage で同一**に効く

昇格は隣へ1段ずつで、**段跳びはできない**。

## いまの stage はどこにあるか

| 場所 | 役割 |
|---|---|
| `authority.json`（`AuthorityStore`） | 与えている制御権の正本。人の承認で上がり、自動降格で下がる |
| `fan-policy.yaml` の `authority_stage`（#103） | 設定が許す**上限**。下げれば再起動後に効き、上げても journal は上がらない |
| `AuthorityRuntime` が下げた上限 | この process が自動降格で下げた分。**永続化できなくても保持する** |

実効 stage は3つのうち**もっとも低いもの**である。`ControllerGate` 側でも上限を掛ける。

`ControllerGate` と `LearnedMpcController` は stage の供給元（`AuthorityStageSource`）を
**必須の引数**にしている。既定値を置くと、配線を忘れた起動が設定の**上限**を
そのまま制御権にしてしまうためである。試験や移行で固定したいときは
`StaticAuthorityStage` を明示的に渡す。

Registry の互換 stage（`MpcModelBinding.authority_stage`）との照合も**実効 stage**と行う。
実効 stage がそれを超えたら拒み、下回るぶんは通す。`load_production()` /
`MpcModelBinding.for_control()` へ渡す stage も実効 stage である（上限を渡すと、
journal がまだ SHADOW の初日に SHADOW 互換の artifact が拒まれ、昇格の証拠を集められない）。

## 上げる（人の承認が要る）

```python
store = AuthorityStore(Path("/srv/coldaisle/authority"), clock)
store.raise_stage(
    approval=approval,  # 承認者・理由・時刻・遷移・revision を持つ
    evaluation_report=report_bytes,  # #91 の報告そのもの（bytes）
    config=config,  # 検証済み ControlConfig（#103）。設定の checksum はここから
    registry=registry,  # Model Registry（#104）。artifact は書く直前に読み直す
)
```

**承認者は値を持ち込めない。** artifact の hash も設定の checksum も「いま」も、
発行済みの attestation さえも受け取らない。artifact の identity は
`ModelRegistry.pinned()` で **registry の lock を握ったまま**決め、その lock を
authority journal を書き終えるまで手放さない。発行時点の写し（`production_active`）や
lock を手放す `inspect()` だけでは、A の証拠を持ったまま B が production になったあとに
昇格できてしまう。

**lock の順序は Registry → Authority で固定**（逆順の経路が無いので deadlock しない）。
**降格は registry の lock を取らない**ので、registry が使えなくても安全側へは常に動ける。

**#104 と #92 の境界**: #92 は #104 の state を**読む**だけで、**書かない**。
逆向き（#104 が authority journal を触ること）も無い（試験で走査）。

次のどれか1つでも満たさなければ昇格は通らない（**判定できないことを合格にしない**）。

- 承認が別の revision / 別の遷移のものである、または期限切れ・未来である
- 承認した stage が設定の上限を超える
- 渡した報告が承認の指した報告と違う、比較条件（`conditions_sha256`）が違う
- 報告が別の設定（`fan-policy.yaml` / `safety.yaml`）で取られている
- その kind の production pointer が無い、または指す先が production artifact でない
- 検証している間に Registry の production が動いた（やり直す）
- `to_stage` が Registry の `authority_compatibility` に含まれない
- 報告に現れた artifact が、いま Production の artifact ちょうど1つでない
- 報告に現れた authority stage が、いまの stage より高い / いまの stage を含まない
- **名指した arm 自身の `last_ts_ms`** が `evidence_max_age_ms` より古い
  （報告全体の run でも segment の終わりでも測らない。Fallback だけで回した続きを
  足しても新鮮にならない）
- 名指した arm が holdout の実績に無い、または**制御器が Learned MPC でない**
  （適用された Fallback の arm を名指して昇格できない）
- 名指した arm の stage が、いまの stage と違う
- 報告に現れた **Learned MPC の arm のどれか**に gate 判定が無い、または1つでも `blocked` である

同じ承認は2回使えない（`expected_revision` に束縛する）。

## 下げる（承認は要らない）

`AuthorityRuntime.observe()` を制御ループの毎 tick で呼ぶ。

| 条件 | 下げ先 |
|---|---|
| Critical Safety が `EMERGENCY` | `SHADOW` |
| Gate の降格推奨（#79。`demote_window_ms` / `demote_after`） | 1段下 |
| `unhealthy_window_ms` の中で OOD が `ood_after` 件 | 1段下 |
| 同じ窓で LOW confidence が `low_confidence_after` 件 | 1段下 |

降格推奨は**立ち上がりだけ**を消費する（1回の閾値超えで1段だけ下げる）。

`rollback_to_baseline()` は1手で `SHADOW` へ戻す。**下げるのは先、書き残すのは後**で、
適用は disk にも他 process の lock にも待たない。書けなければ理由を `persist_failure` として
残す（0057 §2.6 / §3）。
書けた降格は journal が表すので、そのあと承認された昇格は `reload()` でそのまま効く。
**記録の無い降格の上限だけ**が `reload()` でも外れない（process を作り直すまで残る）。
下がったあとに自動で戻る経路は無い。戻すには新しい承認が要る。

## 記録

- `ControllerSelection.authority_stage`: 提案の無い tick にも残る
- `MpcProposal.binding_authority_stage` → `LearnedControlStatus`: worker が照合した stage を
  Gate まで運ぶ。覆っていなければ `binding_authority_not_covered` で Fallback にする
- `AuthorityRuntime.trace_metadata()`: 実効 stage・journal の stage・設定の上限・
  直近の変更（種別・主体・理由・時刻）・永続化の失敗。**model version を含めない**

## まだ無いもの

- 昇格・rollback の管理操作の入口（CLI / ソケット）。読み取り API（#23）は制御を変えない
- 各段に必要な運転期間の下限
- 実機での rollout。GPU サーバーが要る（#92 の `requires:server`）
