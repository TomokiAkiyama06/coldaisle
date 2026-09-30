# Authority Rollout

#92 は **Production の model / policy artifact へ、どこまで実 Fan 制御権を渡すか**を持つ。
どの artifact を Production にするかは Model Registry（#104 / `docs/model-registry.md`）の
責務で、**Model を Production へ昇格させても authority は動かない。**

設計上の決定は決定記録 0057（FINAL、2026-09-20 所有者承認）。ここは使い方をまとめる。

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
- `air-balance.yaml` / `fan-hardware.yaml` の hash が、承認の証拠・報告の provenance・
  いま動いている設定の3つで一致しない（#81 / 決定記録 0073 §2.6）。Air Balance の曲線・目標帯と
  profile の `minimum_stable_demand` は Learned MPC の cost を変えるため
- 報告の `air_balance_trace_binding` / `fan_hardware_trace_binding` が、消費した tick が1件以上で
  すべて一致、を満たさない（別の characterization・別の profile で記録された tick や、hash を持たない
  v10 以前の tick が1件でも混ざった評価は、丸ごと証拠にしない）
- 報告の版が 3 未満（`MIN_EVIDENCE_REPORT_SCHEMA_VERSION`。v2 以前は Air Balance の設定を言えない）
- その kind の production pointer が無い、または指す先が production artifact でない
- 検証している間に Registry の production が動いた（やり直す）
- `to_stage` が Registry の `authority_compatibility` に含まれない
- 報告に現れた artifact が、いま Production の artifact ちょうど1つでない
- 報告に現れた authority stage が、いまの stage より高い / いまの stage を含まない
- **名指した arm の `last_attested_ts_ms`**（裏づけのある提案を最後に出した時刻）が
  `evidence_max_age_ms` より古い、または裏づけのある提案が1つも無い
  （報告全体の run でも、segment の終わりでも、arm の `last_ts_ms` でも測らない。
  Fallback だけで回した続きや、いまの読み込み失敗を足しても新鮮にならない）
- 名指した arm が holdout の実績に無い、または**制御器が Learned MPC でない**
  （適用された Fallback の arm を名指して昇格できない）
- 名指した arm が**適用側**で、その arm が適用した artifact が記録されていない
  （v1〜v6 の tick だけの区間。推測で埋めない）、または記録された artifact が
  いまの Production の artifact ちょうど1つでない（決定記録 0059 §2.3 が 0057 §3 の
  禁止を置き換えた。`_check_learned_arms()`）
- 報告に現れた **Learned MPC の適用 arm のどれか**に、artifact を言えない適用 tick
  （`unbound_attested_ticks`）が1件でもある
- 名指した arm の stage が、いまの stage と違う
- 報告に現れた **Learned MPC の arm のどれか**に gate 判定が無い、または1つでも `blocked` である

同じ承認は2回使えない（`expected_revision` に束縛する）。

`AuthorityJournal` は v3 である。v2 から、新しく書く昇格の証拠（`RolloutEvidence`）は
`air_balance_config_sha256` と `fan_hardware_config_sha256` を必ず持つ。既に残った v1 の event は
書き換えずに読むが、新しい昇格の根拠にはならない。v3（#92 / 決定記録 0072 §2.6）は自動降格の
理由に `authority_journal_unreadable` を足した版で、この理由の event は v3 の journal にだけ置ける
（v2 までの reader には「知らない journal」として拒ませる）。v1 / v2 の journal はそのまま読み、
次に書くときに v3 で書く（新しい原因を含まない降格・昇格でも v3 で書く）。

**切り戻しの注意:** v3 を書く `coldaisle-fand` が1回でも journal へ書くと、#92 より前のバイナリは
その journal を読めず、起動を拒む（終了コード 5。引き継ぎの Max のまま止まる）。旧版へ戻すときは、
先に `authority.json` を退避し、旧版では journal の無い状態（`SHADOW`）から始める。昇格はやり直しになる
（旧 reader は未知の `cause` を enum の検証でどのみち拒むので、原因を含む event だけ v3 にしても
切り戻しの安全は変わらない。版を常に上げるのは「知らない journal」として一律に拒ませるためである）。
Air Balance が無効（`uncalibrated`）の間に
集めた証拠もその未校正ファイルに束縛されるので、`calibrated` へ差し替えた後は使えない。

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

## `coldaisle-fand` での配線（決定記録 0072 §2.6 / §2.10 段階 2）

`coldaisle-fand` は `--authority-root`（既定 `var/authority`）の `authority.json` を
`AuthorityRuntime` で読み、Controller Gate と control loop に同じ runtime を渡す。

- **起動時に journal を読めなければ制御を取らない**（終了コード 5。0057 §2.1）。journal が無ければ `SHADOW`
- control runtime の store の lock の待ち上限は `safety.yaml` の `tick_deadline_ms`（0060 §2.7）
- 管理ソケットの `lower_authority` / `rollback_authority`（`docs/control-admin.md`）は、次の tick の
  先頭で **memory 上の上限として無条件に**入る（いまの stage と比べない）。Gate はその tick から
  下がった stage を読む。journal へは heartbeat と decision trace の保存の後に、人の変更
  （`actor = uid.<数値>`、`trigger = human`）として書く。書けなければ上限を持ち続け、次の tick で書き直す
- 毎 tick、heartbeat と trace の保存の後に `authority.json` の `stat`（inode・大きさ・`mtime_ns`）を見て、
  変わっていたら読み直す（flock を取らない）。外の process の rollback・昇格は**次の tick から**効く。
  実効 stage は journal・設定の上限・この process が書き残せずに持っている上限の最小のまま
- **走行中に journal が読めない・壊れているときは止めずに `SHADOW` へ下げ**、`authority_journal_unreadable`
  の自動降格として journal へ書き残しを試みる。**読めるようになっただけでは戻さない。** 書けたら
  journal が `SHADOW` を表すので上限を手放す（authority は上がらない）。戻すには承認による昇格が要る
- **読み直した journal が既知の履歴を延長していない**（revision が戻った・event 列が差し替わった。
  古いバックアップの書き戻しなど）ときも、読めない journal と同じく `SHADOW` へ下げて
  `authority_journal_unreadable` を書き残す。承認を経ずに高い stage が戻ってくる経路にしない。
  追記で届いた昇格（`raise_stage`）だけがそのまま効く
- 予約した書き残しは、同じ主体（trigger・actor・cause）の同じ深さ以上のものを重ねない（主体 × stage の
  段数で抑えられる）。1 tick に書くのは1件で、**同じ tick で自動降格が既に lock を待っていれば次の tick に
  回す**（heartbeat の後の待ちを trace の保存と合わせて2回までに抑える。0060 §2.7）
- 停止（`SIGTERM`）の直前に、残った予約を1回だけ書き直す。それでも書けなければ行き先を error ログに残す
  （再起動すると journal の stage で運転が再開する）

**runtime に authority を上げる経路は無い。** 外の process（人の CLI）の昇格が journal に入っても、
それは 0057 §2.3 の承認を経たものだけである。

## 記録

- `ControllerSelection.authority_stage`: 提案の無い tick にも残る
- `MpcProposal.binding_authority_stage` → `LearnedControlStatus`: worker が照合した stage を
  Gate まで運ぶ。覆っていなければ `binding_authority_not_covered` で Fallback にする
- `AuthorityRuntime.trace_metadata()`: 実効 stage・journal の stage・設定の上限・
  直近の変更（種別・主体・理由・時刻）・永続化の失敗・journal を読めないこと。**model version を含めない**
- `ControlTick` v13 の `authority`（`AuthorityRecord`）: その tick の Gate が stage を読んだ時点の
  journal の stage と revision・設定の上限・書き残せずに持っている上限・`journal_unreadable`・
  その tick の先頭で入れた管理ソケットの降格の `command_id`・直近の永続化の失敗。
  実効 stage（`state.authority_stage`）はこれらの最小を超えない（schema が拒む）

## まだ無いもの

decision trace への、適用した tick の model artifact の記録は #159（PR #160 / 決定記録 0059）で入った。
走っている `coldaisle-fand` の authority stage を下げる・rollback する入口は、管理ソケット
（`coldaisle-control lower-authority` / `rollback-authority`。決定記録 0072 §2.10 段階 2。#92）で入った。

- authority stage を**上げる**入口（`coldaisle-authority raise`）と、`coldaisle-fand` が止まっている
  ときの rollback（`coldaisle-authority rollback`）。0072 §2.10 段階 3（#92）。
  読み取り API（#23）は制御を変えない
- 各段に必要な運転期間の下限
- 実機での rollout。GPU サーバーが要る（#92 の `requires:server`）
