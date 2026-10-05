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
| `AuthorityRuntime` が下げた上限 | この process が自動降格で下げた分。**永続化できなくても保持する**（journal を置き換えた後のディレクトリの `fsync` に失敗した降格も、毎 tick `fsync` をやり直し、通るまで保持する） |
| artifact の上限（決定記録 0089） | journal の承認が、この process が使っている artifact のものでなければ Baseline |
| config の上限（決定記録 0090） | journal の承認が、この process が起動時に読んだ Control Config のものでなければ Baseline |

実効 stage は5つのうち**もっとも低いもの**である。`ControllerGate` 側でも上限を掛ける。

### いま使っている artifact との照合（決定記録 0089）

`coldaisle-fand` は Registry を起動時に1回だけ読み、その artifact を再起動まで使う。一方
`coldaisle-authority raise` は**実行した時点の** Production の artifact の証拠で journal を上げる。
走行中に Production が A → B へ入れ替わると、B の証拠で上げた authority を A が得てしまうので、
`AuthorityRuntime` は journal を読むたびに照合する。

- 照らす値は、fand が束縛した artifact（`loaded_artifact_sha256`。Controller Gate の
  `expected_artifact_sha256` と同じ変数から渡す。既定値は無い）
- journal が最後に Baseline にいた後の昇格**すべて**の `approval.evidence.artifact_sha256` が一致すれば
  上限を掛けない。1件でも違えば、また artifact を持たない構成では、実効 stage の上限を Baseline にする
- **journal は書かない**（承認は正しい）。正しい artifact で再起動すれば journal どおりに戻る
- 上限が変わったとき（起動時を含む）に構造化ログ（`authority_artifact_mismatch` /
  `authority_artifact_unbound` / `authority_artifact_matched`）を1行出す
- **いまの `coldaisle-fand` は Learned MPC の worker を配線しておらず artifact を持たないので、
  journal が上がっていても常に Baseline で動く**（提案が来ないので制御の結果は変わらない）

Production を入れ替えたら、新しい artifact について SHADOW から1段ずつ上げ直す（入れ替えの前に
`rollback` しておくと、journal と実効 stage が食い違わない）。

### 起動時に読んだ Control Config との照合（決定記録 0090）

`coldaisle-fand` は Control Config の4ファイル（`fan-hardware.yaml` / `safety.yaml` / `fan-policy.yaml` /
`air-balance.yaml`）を起動時に1回だけ読む。一方 `coldaisle-authority raise` は**実行した時点の**
`--config-dir` の4ファイルで証拠を検証する。走行中に4ファイルを A → B へ差し替えると、B の証拠で
上げた authority を A の設定で動く fand が得てしまうので、artifact と同じく照合する。

- 照らす値は、fand が制御に使う `ControlConfig.sources`（4ファイルそれぞれの bytes の SHA-256。
  `loaded_config_sources`。既定値は無い）
- journal が最後に Baseline にいた後の昇格**すべて**の証拠の4つの hash（`fan_policy_config_sha256` /
  `safety_config_sha256` / `air_balance_config_sha256` / `fan_hardware_config_sha256`）が一致すれば上限を
  掛けない。1件・1ファイルでも違えば、また証拠に hash が無ければ（journal v1 の event）、Baseline
- **journal は書かない**。承認どおりの設定で再起動すれば journal どおりに戻る
- 上限が変わったとき（起動時を含む）に構造化ログ（`authority_config_mismatch` /
  `authority_config_matched`）を1行出す。食い違ったファイルは `mismatched_files` に出る

### artifact・設定を入れ替えるときの手順

| 入れ替えるもの | 手順 |
|---|---|
| Production の artifact（0089） | 入れ替えの前に `rollback` し、新しい artifact について SHADOW から1段ずつ上げ直す |
| Control Config の4ファイル（0090） | **差し替えの前に `rollback` し**、fand を再起動してから、新しい設定で取った Shadow の証拠で SHADOW から1段ずつ上げ直す。コメントや空白だけの変更でも hash が変わるので同じ |

rollback しないまま入れ替えても、fand は Baseline より上を有効にしない（安全側）。ただし journal の
stage と実効 stage が食い違ったままになり、構造化ログを見ないと理由が分からない。

`ControllerGate` と `LearnedMpcController` は stage の供給元（`AuthorityStageSource`）を
**必須の引数**にしている。既定値を置くと、配線を忘れた起動が設定の**上限**を
そのまま制御権にしてしまうためである。試験や移行で固定したいときは
`StaticAuthorityStage` を明示的に渡す。

Registry の互換 stage（`MpcModelBinding.authority_stage`）との照合も**実効 stage**と行う。
実効 stage がそれを超えたら拒み、下回るぶんは通す。`load_production()` /
`MpcModelBinding.for_control()` へ渡す stage も実効 stage である（上限を渡すと、
journal がまだ SHADOW の初日に SHADOW 互換の artifact が拒まれ、昇格の証拠を集められない）。

## 上げる（人の承認が要る）

### `coldaisle-authority raise`（決定記録 0086）

昇格は**人が自分の uid で実行する CLI** だけが行う。`coldaisle-fand`・管理ソケット・API・AI・
eventd には昇格の経路が無い（0072 §2.1。`tests/test_authority_cli.py` が走査する）。

```bash
uv run coldaisle-authority raise \
    --authority-root /var/lib/coldaisle-authority \
    --approval var/stage-approval.json \
    --report var/evaluation.json \
    --config-dir var/control-config \
    --registry-root var/model-registry
```

（path は仮の値。導入先の値に置き換える。`--registry-limits` は `model-registry.yaml` の
ディレクトリで、既定は `config`）

**本番（`docs/ubuntu-deploy.md` の導入先）での `raise` は、操作者に制御設定の読み取りと Model Registry の
lock の最小権限を与える設計が決まるまで使えない（#217。`rollback` は使える）。** 承認者は自分の uid で
制御設定（`/etc/coldaisle/control-config`。`root:coldaisle-fan`・`0640`）を読み、Model Registry の lock を
取る必要があるが、導入手順はその権限を与えていない（決定記録 0086 §5 の未決 3。2026-10-01 所有者の判断で、
#216 では文書で制限し、権限の設計は #217 で行う）。権限を個別に足して回避しない。
`rollback` は authority のディレクトリ（`authority.json` と lock）だけを使い、制御設定も Registry も
読まないので、導入手順のままで使える。

- **承認者は実行した uid（`uid.<os.getuid()>`）。** `--approver` は無い。承認ファイルに
  `approver` / `approver_binding` があれば拒む（0086 §2.5）。`SUDO_UID` などの環境変数は読まない（§2.1）
- 承認ファイルは `StageApproval` から承認者の欄を除いたもの（`from_stage` / `to_stage` /
  `expected_revision` / `approved_at_ms` / `reason` / `evidence`）。CLI は中身を作らない・直さない
- 次の実行者は承認者になれない（`AuthorityStore` が journal を読む前に拒む。0086 §2.3）:
  root、authority のディレクトリの所有者（= `coldaisle-fand` の実行ユーザー）、`uid != euid`
  （setuid のラッパー経由）。**CLI を通さず `raise_stage()` を直接呼んでも同じく拒む**
- 承認者のグループ（仮の名前 `coldaisle-authority`）に入った人が実行する。ディレクトリは
  導入手順で `2770`（setgid）で作る（`docs/ubuntu-deploy.md`）。**CLI はディレクトリを作らず**、
  書く前に「ディレクトリ・other に権限が無い・setgid 付き」を確かめる。journal と新しく作る lock は
  `umask` に依らず `0660`（0086 §2.4）
- stdout に結果を1件の JSON で出す（`actor` は名前を引けたら `uid.<数値>（<名前>）`。**名前は記録に
  書かない**）。stderr に JSON Lines の構造化ログを1行出す（`event`・`uid`・`euid`・`from_stage` /
  `to_stage`・`revision`・`report_sha256`。失敗は `code` 付き）。DB は開かない（0086 §2.8）

| 終了コード | 意味 | `code` |
|---|---|---|
| 0 | 書いた（rollback で既に Baseline だったときも 0） | —（下の注記） |
| 1 | 読めない・書けない（ディレクトリが無い／形が違う・壊れた journal・設定・Registry・I/O） | `store_error` / `journal_invalid` / `registry_error` / `input_too_large` / `io_or_config_error` |
| 2 | 引数の誤り（argparse） | — |
| 3 | 実行者を承認者として認めない（0086 §2.3 / §2.5） | `approver_is_root` / `approver_owns_authority_root` / `uid_differs_from_euid` / `invalid_uid` / `approval_not_bound_to_process` / `approver_is_not_the_process_uid` |
| 4 | 承認・証拠を受け入れない（下の一覧） | `invalid_approval` / `approval_rejected` / `evidence_rejected` |
| 5 | **書いた（他の process に見えている）が、ディレクトリの `fsync` に失敗し、永続化を確かめられない**（raise / rollback とも） | —（結果の `durable: false`・warning のログ） |

- journal を置き換えた**後**の失敗は、変更しなかったこと（1）にしない。
  ディレクトリの `fsync` に失敗したときは**終了コード 5**で、結果の `durable` を `false` にし、構造化ログを
  warning で出す（変更は他の process に見えているが、電源断で失われうる。もう一度同じ操作をするか、
  journal を確かめる）。成功（0）と分けるのは、**失われた rollback は上げた authority を黙って元に戻す**ので、
  人もスクリプトも終了コードで気づけなければならないため（2026-10-01 所有者の決定。#216）。
  stdout に書けないとき（閉じた pipe など）は `result_not_written` の警告だけを残す（終了コードは変えない）

### `AuthorityStore.raise_stage()`

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

`AuthorityJournal` は v4 である（v4 の内容は下の「journal v4」）。v2 から、新しく書く昇格の証拠（`RolloutEvidence`）は
`air_balance_config_sha256` と `fan_hardware_config_sha256` を必ず持つ。既に残った v1 の event は
書き換えずに読むが、新しい昇格の根拠にはならない。v3（#92 / 決定記録 0072 §2.6）は自動降格の
理由に `authority_journal_unreadable` を足した版で、この理由の event は v3 の journal にだけ置ける
（v2 までの reader には「知らない journal」として拒ませる）。v1 / v2 の journal はそのまま読み、
次に書くときに現行の版（いまは v4）で書く（新しい原因を含まない降格・昇格でも同じ）。

**切り戻しの注意:** v3 を書く `coldaisle-fand` が1回でも journal へ書くと、#92 より前のバイナリは
その journal を読めず、起動を拒む（終了コード 5。引き継ぎの Max のまま止まる）。旧版へ戻すときは、
先に `authority.json` を退避し、旧版では journal の無い状態（`SHADOW`）から始める。昇格はやり直しになる
（旧 reader は未知の `cause` を enum の検証でどのみち拒むので、原因を含む event だけ v3 にしても
切り戻しの安全は変わらない。版を常に上げるのは「知らない journal」として一律に拒ませるためである）。
### journal v4（決定記録 0086 §2.7）

- 昇格の承認に `approver_binding: "process_uid"` が付く。付いた承認の `approver` は `uid.<1..4294967294>`
  （`uid.0` は書けない）で、v4 の journal にだけ置ける。束縛した昇格のあとに、束縛の無い昇格は記録できない
- v1〜v3 の journal はそのまま読み、次に書くとき（CLI の昇格・rollback、fand の降格のどれでも）に
  v4 で書き直す。既存の event（自己申告の承認者を含む）は書き換えない
- **新しい reader を先に配る。** 順序は「パッケージを更新 → `coldaisle-fand` を再起動 → それから CLI を使う」。
  v4 を読めない fand は、走行中なら `authority_journal_unreadable` で `SHADOW` へ下がり、起動時なら
  終了コード 5 で止まる（安全側だが制御を取れない）
- **切り戻し:** v4 の journal は旧版（v3 まで）の fand が読めない。旧版へ戻すときは v3 と同じく、先に
  `authority.json` を退避し、旧版では journal の無い状態（`SHADOW`）から始める。昇格はやり直しになる
- 置き場所の移行（`/var/lib/coldaisle-fand` から authority 専用のディレクトリへ）は `docs/ubuntu-deploy.md`

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

`coldaisle-fand` が止まっているときは、人が CLI で戻す（0072 §2.1 / 0086 §2.6）。

```bash
uv run coldaisle-authority rollback --authority-root /var/lib/coldaisle-authority --reason "挙動を見直す"
```

承認は要らず、**uid で拒まない**（root・fand の実行ユーザーでも通る。障害時に root しか残っていなくても
戻せるようにする）。`actor` は `uid.<os.getuid()>`。既に `SHADOW` なら何も書かずに終了コード 0。
CLI の store なので、ディレクトリが無い・形が違うときは書かない（終了コード 1）。
fand が動いているときは管理ソケットの `coldaisle-control rollback-authority` を使う（次の tick で効く）。
CLI で書いた rollback も、動いている fand は次の tick で journal の変化として読む。

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
  直近の変更（種別・主体・理由・時刻）・永続化の失敗・journal を読めないこと・artifact の上限
  （`authority_artifact_ceiling`。0089）・config の上限（`authority_config_binding_ceiling`。0090。
  `authority_config_ceiling` は `fan-policy.yaml` の上限で別物）。**model version も照らした artifact や
  設定の hash も含めない**
- `ControlTick` v13 以降（いまは v14）の `authority`（`AuthorityRecord`）: その tick の Gate が stage を読んだ時点の
  journal の stage と revision・設定の上限・書き残せずに持っている上限・`journal_unreadable`・
  その tick の先頭で入れた管理ソケットの降格の `command_id`・直近の永続化の失敗。
  実効 stage（`state.authority_stage`）はこれらの最小を超えない（schema が拒む）。
  **artifact の上限（0089）は `AuthorityRecord` に欄が無い**（足すと版上げになる）。所有者の判断（2026-10-05。
  0089 §5 の 1 / §6）により、trace の次の版上げのときに `AuthorityRecord` v2 で欄を足す。それまで
  journal より低い理由は fand の構造化ログ（`authority_artifact_mismatch` / `_unbound` / `_matched`）で見る。
  config の上限（0090）も同じ扱い（`authority_config_mismatch` / `_matched`。0090 §5 の 3）

## まだ無いもの

decision trace への、適用した tick の model artifact の記録は #159（PR #160 / 決定記録 0059）で入った。
走っている `coldaisle-fand` の authority stage を下げる・rollback する入口は、管理ソケット
（`coldaisle-control lower-authority` / `rollback-authority`。決定記録 0072 §2.10 段階 2。#92）で入った。

authority stage を**上げる**入口（`coldaisle-authority raise`）と、`coldaisle-fand` が止まっている
ときの rollback（`coldaisle-authority rollback`）は、0072 §2.10 段階 3 / 0086 段階 3b（#92）で入った。
読み取り API（#23）は制御を変えない。

- journal の改ざん検知（承認者のグループは `authority.json` を直接書き換えられる。0086 未決 1）
- 各段に必要な運転期間の下限
- 実機での rollout。GPU サーバーが要る（#92 の `requires:server`）
