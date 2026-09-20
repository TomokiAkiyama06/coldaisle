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
store = AuthorityStore(Path("/srv/coldaisle/authority"))
store.raise_stage(
    approval=approval,  # 承認者・理由・時刻・遷移・revision を持つ
    evaluation_report=report_bytes,  # #91 の報告そのもの（bytes）
    policy=config.policy,
    fan_policy_config_sha256=config.sources.policy.sha256,
    safety_config_sha256=config.sources.safety.sha256,
    production_artifact_sha256=attestation.artifact_sha256,  # #104 の attestation から
    now_ms=clock.now_ms(),
)
```

次のどれか1つでも満たさなければ昇格は通らない（**判定できないことを合格にしない**）。

- 承認が別の revision / 別の遷移のものである、または期限切れ・未来である
- 承認した stage が設定の上限を超える
- 渡した報告が承認の指した報告と違う、比較条件（`conditions_sha256`）が違う
- 報告が別の設定（`fan-policy.yaml` / `safety.yaml`）で取られている
- 報告に現れた artifact が、いま Production の artifact ちょうど1つでない
- 報告に現れた authority stage が、いまの stage より高い / いまの stage を含まない
- 報告の run の最終観測が `evidence_max_age_ms` より古い
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

`rollback_to_baseline()` は1手で `SHADOW` へ戻す。**降格は先に効き、そのあとで書き残す。**
書けなくても下げたままにし、理由を `persist_failure` として残す（0057 §2.6 / §3）。
下がったあとに自動で戻る経路は無い。戻すには新しい承認が要る。

## 記録

- `ControllerSelection.authority_stage`: 提案の無い tick にも残る
- `AuthorityRuntime.trace_metadata()`: 実効 stage・journal の stage・設定の上限・
  直近の変更（種別・主体・理由・時刻）・永続化の失敗。**model version を含めない**

## まだ無いもの

- 昇格・rollback の管理操作の入口（CLI / ソケット）。読み取り API（#23）は制御を変えない
- 各段に必要な運転期間の下限
- 実機での rollout。GPU サーバーが要る（#92 の `requires:server`）
