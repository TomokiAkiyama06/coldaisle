# 決定記録 0072: 制御デーモンの管理操作の入口（運転モードと Authority Stage の切替）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-29
- **Supersedes**: なし
- **関連**: [0009](0009-read-api.md) §3（GET-only） /
  [0015](0015-llm-tools.md) / [0018](0018-tool-exposure.md) /
  [0027](0027-fan-control-architecture.md) §2.3（Manual safety override は Critical Safety の持ち物） /
  [0028](0028-fan-control-contracts.md) §2.2 / §2.4 / §2.5 / §2.7 / §2.8 / §2.9、未決 3 /
  [0045](0045-local-socket-write-entry.md) §2.2 / §2.3 / §2.4 / §2.7（`SO_PEERCRED` の前例） /
  [0057](0057-authority-rollout-stage-changes.md) §2.3 / §2.6、§5 の「外の process が下げたこと」「管理操作の入口」 /
  [0060](0060-control-loop-runtime.md) §2.3 / §2.6 / §2.7、未決 4 / 7 /
  [0064](0064-workload-hint-entry-and-supervisor-prior.md) §2.2（`coldaisle-eventd` は「指令」を受けない） /
  [0066](0066-workload-hint-stage-b-conditions.md)（制御デーモンが自分の表を別の接続で書く前例） /
  AGENTS.md「絶対に守るルール」1〜3・5・6・9・10
- **対象 Issue**: #92（Authority Stage 切替の管理操作）、#74（運転モードの入口）

---

## 1. Context

運転モード（`AUTO` / `MANUAL` / `MAX` / `CALIBRATION`）と Authority Stage は、どちらも
**人だけが変える**軸である（0028 §2.5 (a) / (b)、0057 §2.3）。ところが、走っている
`coldaisle-fand` へ人の操作を届ける入口がまだ無い。

| 既に決まっていること | 出典 |
|---|---|
| モード変更の入口は **`coldaisle-fand` のローカル Unix ソケットだけ**。読み取り API に書き込みを足さない。LLM のツールと `server.py` から到達できない | 0028 §2.2 |
| loop はモードを `OperatingModeSource.current()` から**待たずに**読む。再起動で `AUTO`。読めない tick は直前のモードを保つ | 0060 §2.3 |
| stage の正本は `authority.json`（`AuthorityStore`）。昇格は人の承認だけ、降格は承認なしで即時 | 0057 §2.1 / §2.3 / §2.6 |
| control runtime から使う `AuthorityStore` は lock の待ち上限を必ず持つ。人の昇格は待ってよい | 0060 §2.7 |
| `coldaisle-eventd` のソケット（0045）が受けるのは「記録と文脈」であって「指令」ではない | 0064 §2.2 |

決まっていないのは次である。

| 出典 | 未決 |
|---|---|
| 0028 未決 3 / 0060 §2.3・未決 4 | モード変更のソケットのプロトコルと認可 |
| 0057 §5 | 昇格・rollback の管理操作の入口（CLI か、ソケットか） |
| 0057 §5 | **外の process が journal を下げたことを、動いている制御ループがいつ知るか**（`AuthorityRuntime` は構築時と `reload()` でしか読まない） |
| #92 受入基準 | Stage 切替を管理操作で明示的に行える / Rollback で Baseline へ安全に戻れる / 理由と時刻を記録する |
| #74 受入基準 | Manual / Max / Calibration / Auto の mode を持つ / LLM から actuation できない |

いまの `coldaisle-fand` は `StaticOperatingMode()`（常に `AUTO`）と `StaticAuthority()`
（常に `SHADOW`）を配線している（`src/coldaisle/control_daemon.py`）。**この入口が
決まらないと、#74 の M8 運転は `AUTO` 固定のまま、#92 は Shadow から一歩も動けない。**

この入口は Fan の実効 Demand を人が変える経路なので、AGENTS.md の安全系に当たる。
本記録は **Proposed** であり、§2 は推奨案、§4 は主な代替案とその得失である。
所有者が選び直してよい。

---

## 2. Decision（推奨案）

### 2.1 入口は向きで2つに分ける

| 操作 | 入口 | 走っている loop への反映 |
|---|---|---|
| 運転モードの設定（`AUTO` / `MANUAL` / `MAX`。`CALIBRATION` は §2.3 で予約） | **`coldaisle-fand` が持つ管理ソケット**（本記録で `control-admin` と呼ぶ） | 次の tick の先頭（§2.5） |
| Authority の**降格**・Baseline への rollback | 同じ管理ソケット | 次の tick の先頭（§2.5） |
| いまの状態の照会（`status`） | 同じ管理ソケット | —（読むだけ） |
| Authority の**昇格** | **ソケットでは受けない。** 人が実行する CLI（`coldaisle-authority raise`）が `AuthorityStore.raise_stage()` を直接呼ぶ | journal の変化を loop が検知して読み直す（§2.6） |
| fand が止まっているときの rollback | 同じ CLI の `coldaisle-authority rollback`（`AuthorityStore.rollback_to_baseline()`） | 次に起動したときの journal、または動いていれば §2.6 |

**管理ソケットは制御権を増やせない。** 受理する操作は「モードを選ぶ」「authority を下げる」
「読む」の3種類だけで、authority を上げる型も操作も持たない。昇格は 0057 §2.3 の検証
（Registry の lock を握ったまま報告・設定・artifact を突き合わせる）を丸ごと必要とし、
その待ちは deadman の無い人の経路に置くべきものである（0060 §2.7）。制御プロセスの中へ
持ち込むと、Registry の lock 待ちや数 MB の報告の検証が control process に入る。

`coldaisle-eventd`（0045）は**再利用しない**。0064 §2.2 が「このソケットで受理する種類は
いずれも記録と文脈であって指令ではない」と固定しており、そのグループには Workspace の
GPU Manager などの書き手が入る。モード変更を足すと、その書き手全員が Fan を動かせる。

### 2.2 管理ソケットは `coldaisle-fand` のプロセス内の別スレッドが持ち、loop は待たない

- 待ち受けは `coldaisle-fand` の中の**受付スレッド1本**。受付スレッドは loop の状態
  （`ControlLoop` / `ControllerGate` / `AuthorityRuntime`）に**一切触れない**
- 受付スレッドは検証済みの指令を**受け渡し口**（mailbox）へ置くだけにする。受け渡し口は
  **状態の軸ごとに1枠**（モードの枠と authority の枠の2枠）を持つ。loop は tick の先頭で1回だけ
  受け渡し口を**非ブロッキングで**覗き、2枠を同じ lock の中で取り出す。lock が取れなければ
  その tick は直前のモード・stage のまま進む（0060 §2.3「待たない」「読めない tick は直前を保つ」）
- 次の tick までに**同じ軸**の指令が複数来たときだけ置き換える。**別の軸の指令は互いに消さない**
  （例: `set_mode(max)` のあとに `rollback_authority` が来ても、両方が同じ tick で効く。
  モードの軸の安全側の指令が authority の軸の指令で失われない）
  - モードの枠: **最後の1件**を採る（人の最新の意図。`MAX` のあとの `set_mode(auto)` は、
    人が弱めると決めた指令として §2.7 の監査を経て受けたものである）
  - authority の枠: 置き換えずに**合成する**。`rollback_authority` と `lower_authority` は
    どちらも下げる向きなので、届いた指令のうち**最も低い行き先**を採る（後から来た「浅い降格」が
    先の rollback を打ち消さない）
  - 採られなかった指令は `superseded` として応答・監査に残す（§2.7）
- loop は適用した指令の `command_id` と `tick_id` を受け渡し口へ返す。受付スレッドは
  それを `apply_ack_timeout_ms` まで待って応答する（**待つのは受付スレッドで、loop ではない**）。
  時間内に適用されなければ `{"ok": true, "applied": false, "pending": true}` を返し、
  適用は取り消さない（遅れて効いたことは監査と decision trace に残る）
- **受付スレッドが死んでも制御は止めない。** error を構造化ログに残し、loop は直前の
  モードで運転を続ける（冷却の継続を入口の可用性より優先する）。入口が死んだ状態で
  人が冷却を強めたいときは、service を止めれば 0028 §2.7 の引き継ぎが効く
- Critical Safety・合成は受付スレッドの例外を見ない。受付スレッドの例外で
  `coldaisle-fand` を終わらせない（0060 §2.6 の「捕まえない」は Critical Safety・合成だけ）

実装の置き場所は `coldaisle.control_admin`（`coldaisle.event_entry` と同じ「合成の起点」の
位置）とし、`OperatingModeSource` の実装はここに置く。`coldaisle.control` 配下は
`coldaisle.control_admin` を import しない（下位層が上位を import しない。AGENTS.md
コード規約）。`control_daemon.py` が束ねる。

### 2.3 プロトコル：版付きの JSON Lines、操作はホワイトリスト

0045 §2.4 と同じ枠組み（**1接続につき1行の要求と1行の応答**、UTF-8 の JSON object を
`\n` で終える、`extra = forbid`、strict、`v` は `1` のみ、入力を応答に反射しない）を使う。
**メッセージの名前空間は 0045 と共有しない**（`type` ではなく `op` で区別する。
取り違えて別の入口へ送っても、どちらも未知として拒否する）。

```json
{"v": 1, "op": "set_mode", "mode": "max", "reason": "GPU 負荷試験の前に全開にする"}
{"v": 1, "op": "set_mode", "mode": "manual", "requested": {"front": 0.6, "rear": 0.5, "top": 0.7}, "lease_s": 1800, "reason": "騒音の比較"}
{"v": 1, "op": "set_mode", "mode": "auto", "reason": "比較の終了"}
{"v": 1, "op": "lower_authority", "to_stage": "limited", "reason": "夜間の OOD が多い"}
{"v": 1, "op": "rollback_authority", "reason": "新しい artifact の挙動を見直す"}
{"v": 1, "op": "status"}
```

| フィールド | 規則 |
|---|---|
| `op` | `set_mode` / `lower_authority` / `rollback_authority` / `status` のみ。**`raise_authority` は存在しない** |
| `mode` | `auto` / `manual` / `max`。`calibration` は #75 の測定計画の参照形が決まるまで**拒否する**（予約） |
| `requested` | `manual` のときだけ必須。`front` / `rear` / `top` の**3つすべて**を `0.0..1.0` の demand で持つ（PWM は受け取らない。0028 §2.3）。他のモードでは拒否 |
| `lease_s` | `manual` のときだけ必須。`1..manual.max_lease_s`（§2.8）。期限が来たら `AUTO` へ戻す（§2.4）。期限は `coldaisle-fand` の**単調時計**で数える（§2.4） |
| `to_stage` | `lower_authority` のときだけ必須。**いまの実効 stage より低い値だけ**を受ける。同じか高ければ `changed: false` で何もしない |
| `reason` | 状態を変える操作では必須。1〜200 文字、制御文字を含まない |

- **時刻・操作者名・設定値・path を受け取らない。** 時刻は受付時の `coldaisle-fand` の時計で
  確定し（0045 §2.5 と同じ理由）、操作者は peer credential から決める（§2.4）
- 応答: 受理は `{"ok": true, "command_id": <int>, "applied": <bool>, "applied_tick_id": <int|null>}`、
  拒否は `{"ok": false, "error": "<理由の code>"}`。`status` は §2.7 の欄を返す

### 2.4 モードの意味は 0028 §2.5 (a) のまま。**Critical Safety と Manual safety override を弱める経路を作らない**

入口が増えても、合成（0028 §2.4）とモードの意味は変えない。確認として固定する。

- `MANUAL` の `requested` は **requested までしか作れない**。Reactive Guard の floor・
  Critical Safety の floor・`forced_max`・`ramp_down` の制限はすべて掛かる（0028 §2.4）。
  Guard の ceiling だけは人の値に掛けない（0028 §2.4 のまま）
- `MAX` は Critical Safety の `forced_max`（Manual safety override。0027 §2.3）として表す。
  モードは `forced_max` を**足せるだけで、消せない**。Critical Safety 自身が出した
  `forced_max`（`EMERGENCY`・`config_invalid`・`STARTUP` など）は、どのモードを選んでも残る
- **Safety の状態を解除・確認済みにする操作を作らない**（fault の ack、`config_invalid` の解除、
  `safety.yaml` の値の変更、Guard の無効化、hardware mapping の変更）。管理ソケットは設定を
  読み直さない（0028 §2.8 の「live reload を行わない」）
- `MAX` から他のモードへ移る操作、`MANUAL` から出る操作（`AUTO` へ戻すことを含む。`MANUAL` の値が
  Fallback より高いことがある）など、冷却を弱めうる操作も人の指令としては受けるが、
  下げ方は `ramp_down` の制限を通る。監査に書けなかったら受けない（§2.7）
- `MANUAL` には**期限（lease）を必須**にする。期限が来たら loop が `AUTO` へ戻し、
  `manual_lease_expired` として記録する。`MANUAL` は負荷に追従しない値で、人が戻し忘れたときに
  Fallback の自動追従を失ったまま運転し続けないためである。**`MAX` には期限を付けない**
  （勝手に下がる経路を作らない。0060 §2.3 と同じ考え方）
- **lease の期限は `coldaisle-fand` の単調時計（0028 §2.7「時計」の `monotonic_ms()`）だけで数える。**
  起点は loop がその `MANUAL` を適用した tick の単調時刻、期限は起点 + `lease_s`。期限切れの判定と
  `status` の残り期限はこの単調時刻から出す。監査の受付時刻（壁時計、§2.7）は**記録の時刻だけ**に使い、
  期限の計算に使わない（0028 §2.7「期限は単調時計だけで数える」。壁時計が戻ると、負荷に追従しない
  低い `MANUAL` が `lease_s` を過ぎても残りうるため）。再起動で `AUTO` に戻るので、単調時計を
  process をまたいで比べる必要はない
- 再起動で `AUTO` に戻る（0028 §2.5 (a)）。受け渡し口もモードも永続化しない
- `coldaisle.control.safety` は `coldaisle.control_admin` を import しない。Critical Safety は
  モードを `OperatingMode` の値としてだけ受け取り、どの入口から来たかを知らない（試験で走査する）

### 2.5 認可：ファイル権限と `SO_PEERCRED`。**同じ uid も root も暗黙には認めない**

0045 §2.2 / §2.3 の2つの門をそのまま使う。起動時の検査（other が書ける親ディレクトリ、
ソケット以外の既存ファイル、既に応答するソケット、長さ上限）と `umask` を絞った作成も同じにする。
実装は 0045 の検査を共通の部品にして使う（部品の置き場所は実装 PR で決める）。

違いは次の3点で、いずれも「Fan を動かせる」入口であることによる。

| 項目 | 0045（`coldaisle-eventd`） | 本記録（`control-admin`） | 理由 |
|---|---|---|---|
| 同じ uid の接続 | 既定で認める | **既定で認めない**（`allow_same_user: false`） | `coldaisle-fand` は hwmon へ書くために高い権限で動きうる。同じ uid を暗黙に通すと、fand と同じ uid で動く別のサービスが Fan を動かせる |
| 専用グループ | 任意（`null` で開発用） | **本番は必須**。`group: null` では、`allow_same_user: true` を明示した開発用の起動しか認めない | 誰がモードを変えられるかを、グループのメンバーという1つの事実に集める |
| 権限の範囲 | 注釈・文脈 | モードの設定と authority の降格 | — |

- 認可は接続のたびに判定する。root を暗黙に認めない（0045 §2.3 と同じ）
- `SO_PEERCRED` が無いプラットフォームでは**管理ソケットを開かない**。このとき
  `coldaisle-fand` 自体は起動し、`AUTO` と journal の stage で運転する（入口が無いことを
  error として残す）。冷却を入口の有無に依存させない
- 操作者名は `uid.<数値>` とする（`AuthorityEvent.actor` の形 `^[a-z][a-z0-9_.-]*$` に収まる）。
  名前解決の結果を記録に使わない
- **配置の必須条件**: 読み取り API（`coldaisle.api` / `coldaisle.server`）と AI 層を動かすユーザーを
  管理グループに入れない。`authority.json` のディレクトリは `coldaisle-fand` の実行ユーザーと
  昇格を行う人だけが書ける権限にする。**API / AI サーバを `coldaisle-fand` と同じユーザーで
  動かさない**（同じ uid は journal もソケットも守れない。0045 §2.3 と同じ限界）。
  具体的なユーザー・グループ名・`RuntimeDirectory` は unit の記録（0060 未決 7）で決める

### 2.6 反映の時点と、外の process による降格（0057 §5）

**モード**

- 受け渡し口に置かれた指令は、**次の tick の先頭の1回の読み取り**で loop に入る。
  tick の途中でモードは変わらない（1 tick は1つの snapshot・1つのモード。0060 §2.5）
- 遅れの上限は `tick_ms + tick_deadline_ms`（次の tick が始まるまでと、その tick の処理）。
  `apply_ack_timeout_ms` はこれ以上でなければならない（起動時に `safety.yaml` と突き合わせる）

**管理ソケットからの降格**

- 受付スレッドは `AuthorityRuntime` を呼ばない（スレッド安全でない状態を2つのスレッドから触らない）。
  指令を受け渡し口へ置き、loop が次の tick の先頭で `AuthorityRuntime` の降格
  （0057 §2.6 の「先に in-memory、あとで journal」。lock の待ち上限つき。0060 §2.7）を行う
- 降格は Gate がその tick の stage を読む前に効く。書き残せなかった降格は 0057 §2.6 のとおり
  in-memory の上限として残り、`persist_failure` が立つ

**外の process が journal を変えたとき（CLI の rollback / 昇格）**

- loop は**毎 tick、heartbeat の後**（0060 §2.7 の境目の後。decision trace の保存と同じ側）に
  `authority.json` の `stat`（inode・大きさ・`mtime_ns`）を見て、変わっていたら `reload()` する。
  `AuthorityStore.read()` は flock を取らない（原子置換された file を読むだけ）ので、他 process の
  lock を待たない。効くのは**次の tick から**
- 外で下げられた stage は次の tick から効く。外で上げられた stage も同じ経路で入るが、
  実効 stage は 0057 §2.2 の3つ（journal・設定の上限・この process が下げた上限）の最小のままで、
  Gate は tick ごとの束縛照合（0057 §2.2）と復帰の hold（0028 §2.5 (c)）を経て ML を使う
- **走行中に journal が読めない・壊れているときは、その process の in-memory の上限を
  `SHADOW` に下げる**（`authority_journal_unreadable`）。0057 §2.1 は壊れた journal を起動時に
  `AuthorityStateError` で止めるが、走行中に止めると冷却が止まる。止めずに、**読めない間は
  制御権を最小にする**。次に正しく読めたら 0057 §2.6 の規則どおり（書き残した降格は journal が
  表すので、in-memory の上限はこの理由の分だけ外す）

### 2.7 監査：全接続をログに、状態を変えた指令を追記専用の表に残す

- **接続ごと**（受理・拒否とも）に JSON Lines で `peer_uid`・`op`・結果・理由の code を残す
  （0045 §2.3 と同じ。入力の値そのものは拒否の理由に反射しない）
- **状態を変える指令**（`set_mode` / `lower_authority` / `rollback_authority`）は、
  `coldaisle-fand` が**自分の表にだけ書く別の接続**（0066 の前例）で、追記専用の表へ残す。
  更新・削除はトリガで拒否する（0045 §2.5 と同じ）。表の DDL と migration は実装 PR で決める
- **1つの指令の経過は、行を書き換えずに「事象の行」を足して表す。** 受付の時点では結果も
  適用した tick も分からないため、結果の列を後から埋める形にすると追記専用のトリガと両立しない。
  - 受付の行（`event = accepted`）: `command_id`・受付時刻（壁時計）・`peer_uid`・`op`・
    検証済みの本文。**受け渡し口へ置く前に**書く（弱めうる指令は、この行が書けたときだけ置く）
  - 結末の行（`event = applied` / `superseded` / `expired`）: 同じ `command_id`・記録時刻・
    `applied` なら適用した `tick_id`、`superseded` なら置き換えた `command_id`、`expired` は
    `MANUAL` の lease 切れ。1つの `command_id` に結末の行は**高々1つ**（一意制約）
  - 拒否（検証の失敗・認可の失敗・弱めうる指令で受付の行を書けなかった）は受付の行を作らず、
    接続ごとのログ（上）にだけ残す
  - 受付の行があって結末の行が無い指令は「受理済み・未確定」（応答の `pending` と同じ状態）として
    読む。結末の行が書けなくても適用は取り消さない。適用の事実は decision trace の `command_id`（下）に
    残るので、弱めうる指令が「記録なしで効いた」状態にはならない（受付の行は適用より前に確定している）
- **書けなかったときは向きで分ける。** 冷却を強める・制御権を下げる指令（`MAX` への移行、
  `lower_authority`、`rollback_authority`）は、監査の書き込みに失敗しても**適用する**
  （安全側は記録を待たない。0057 §2.6 と同じ考え方）。**それ以外の状態を変える指令はすべて
  冷却を弱めうるものとして扱い**、受付の行を書けなければ受理しない（記録の無い弱化を作らない）。
  これには `MAX` から出る、`MANUAL` に入る・値を変える、**`MANUAL` から出る（`AUTO` へ戻すことを
  含む）**が入る。`MANUAL` の値が Fallback の出力より高いとき、`AUTO` へ戻すと冷却が下がるためである。
  向きを実際の demand の比較で決めることはしない（比較の時点と適用の tick で Fallback の出力が
  変わりうるため、指令の種類だけで保守的に決める）
- authority の変更は 0057 の journal にもそのまま残る（`actor = uid.<数値>`、`trigger = human`）
- decision trace の各 tick にはすでに `operating_mode` と `authority_stage` が残る。
  加えて、その tick に効いていたモードを決めた `command_id` と、`MANUAL` の期限切れを
  残す（`ControlTick` の版を上げる。番号は実装 PR が他の PR と取り合わないように決める）
- lease 切れ（`expired`）と、loop が適用したときの結末の行は、loop の tick の中では書かない。
  loop は結果を受け渡し口へ返すだけで、書くのは受付スレッドの側（loop は DB を待たない。0060 §2.3）
- `status` は、いまのモード・`command_id`・`MANUAL` の残り期限・実効 stage・journal の stage・
  設定の上限・`persist_failure`・入口の起動状態を返す。**読み取り API には出さない**
  （出すかは §5）

### 2.8 設定：`config/control-admin.yaml`

| 設定 | 規則 |
|---|---|
| `socket.path` / `socket.mode` / `socket.group` | 0045 §2.2 と同じ規則（other のビット・setuid / setgid / sticky は拒否） |
| `authorization.allow_same_user` | 既定 `false`。`true` は `socket.group: null` の開発用のときだけ許す |
| `limits.max_message_bytes` / `limits.read_timeout_s` | 0045 と同じ意味 |
| `apply_ack_timeout_ms` | `>= tick_ms + tick_deadline_ms`（起動時に `safety.yaml` と照合） |
| `manual.max_lease_s` | `MANUAL` の期限に書ける上限。`status` / `basis` 付きの暫定値 |

- 値はすべて設定に置き、コードに既定値を置かない（AGENTS.md ルール9）
- **制御の3ファイル（0028 §2.8）には入れない。** 入口の形であって制御の閾値ではなく、
  `safety.yaml` の schema を変えると 0028 §2.9 の承認点 2 に当たるためである
- 起動時に1回だけ読む。不正なら**管理ソケットを開かず**、`coldaisle-fand` は `AUTO` と
  journal の stage で運転を続ける（0060 §2.7 の終了コードの表には足さない。
  入口の設定が壊れているだけで Max を書いたり制御を手放したりしない）

### 2.9 LLM・読み取り API・`coldaisle-eventd` から構造的に到達できない

- `coldaisle.ai` / `coldaisle.api` / `coldaisle.server` / `coldaisle.event_entry` は
  `coldaisle.control_admin` を import しない。AST の走査と、`coldaisle.server` を import しても
  `coldaisle.control_admin` が読み込まれないことの試験で固定する（0045 §2.7 と同じ方式）
- AI 向けツール（0015 / 0018）に書き込み・モード・authority のツールを足さない
  （既存の「定義は読み取りの5つだけ」の試験がそのまま効く）
- 読み取り API は GET のみのまま（0009 §3）。管理ソケットは TCP / HTTP で待ち受けない
- 管理ソケットのグループに API / AI サーバの実行ユーザーを入れない（§2.5 の配置の必須条件）

### 2.10 実装の段階

| 段階 | 内容 | 前提 |
|---|---|---|
| 1 | 管理ソケット（`AUTO` / `MAX` / `MANUAL` と lease、`lower_authority` / `rollback_authority`、`status`）・監査の表・`coldaisle-control` クライアント・§2.9 の試験 | 本記録の承認 |
| 2 | `AuthorityRuntime` を `coldaisle-fand` へ配線し、journal の変化を毎 tick 検知する（§2.6） | 段階 1 |
| 3 | `coldaisle-authority raise` / `rollback` の CLI | 段階 2（昇格が走行中の loop に届くため） |
| 4 | `CALIBRATION` の受理 | #75 の測定計画の形 |

実機で初めてこの入口からモードを変えるのは、0028 §2.9 の承認点 3（実機の制御を初めて取る）の
確認項目に含める。

---

## 3. Consequences

### 良くなること

- 0028 未決 3・0060 未決 4・0057 §5 の2項（入口、外からの降格）が閉じ、#74 はモードを、
  #92 は stage を、走っている `coldaisle-fand` に対して変えられるようになる
- 管理ソケットが「上げられない」ので、ソケットの認可を誤っても制御権は増えない。
  増やせるのは人が CLI で行う昇格だけで、0057 §2.3 の検証をすべて通る
- loop はどの入口も待たない。入口が壊れても、止まっても、冷却と deadman は影響を受けない
- 誰が・いつ・どのモードにしたかが、追記専用の表・journal・decision trace の3か所で辿れる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| `coldaisle-fand` にスレッドが1本増える | 受付スレッドは loop の状態に触れず、受け渡し口は軸ごとに1枠（モードと authority の2枠）。loop 側は非ブロッキングで覗くだけ（§2.2） |
| 管理ソケットが死ぬとモードを変えられない | 直前のモードで運転を続け、error を残す。冷却を強めたいときは service の停止で 0028 §2.7 の引き継ぎが効く |
| 昇格が走行中の loop に効くまで最大 1 tick 遅れる | 安全側ではないので遅れてよい。降格は受け渡し口経由で次の tick から効く |
| 毎 tick の `stat` が1回増える | heartbeat の後に置き、flock を待たない（§2.6） |
| `MANUAL` の lease が切れると勝手に `AUTO` へ戻る | 戻り先は Fallback の自動追従で、Guard / Safety はそのまま効く。`MAX` には lease を付けない |
| 監査を書けないと `MANUAL` に入れず、`MANUAL` から `AUTO` へも戻せない | 安全側（`MAX`・降格）は書けなくても効く。弱める向きだけを止める。`MANUAL` は lease 切れで `AUTO` へ戻る（loop が決めるので監査の可否に依存しない） |
| 同じ uid は守れない | 0045 §2.3 と同じ限界。API / AI サーバを別ユーザーで動かすことを配置の必須条件にする（§2.5） |
| 設定ファイルが1つ増える | 入口の形だけを持ち、制御の閾値を持たない。壊れていても入口が閉じるだけ（§2.8） |

---

## 4. 却下した代替案（所有者が選び直せるよう、得失を残す）

| 案 | 得 | 失（却下の理由） |
|---|---|---|
| **A. `coldaisle-eventd`（0045）に `set_mode` などの種類を足し、fand が `events` 表を読む** | 入口・認可・監査が1本のまま。fand にスレッドが増えない | 0064 §2.2「指令ではない」を Supersede する必要がある。eventd のグループの書き手（Workspace の GPU Manager など）が Fan を動かせるようになる。0028 §2.2「fand のソケットだけ」とも食い違う。反映が DB のポーリング周期に縛られる |
| **B. 別の管理デーモン（`coldaisle-controld`）が状態 file を書き、fand が読む** | fand が socket を持たない | file が「別の uid が書ける制御入力」になる。プロセスと file の受け渡しが1つずつ増える。0028 §2.2 と食い違う |
| **C. tick の中で socket を非ブロッキングに読む（スレッドを使わない）** | スレッドが増えない。状態の受け渡しが要らない | 安全の経路（1 tick）に socket の I/O と部分受信の扱いが入る。遅いクライアントの読み取り時間を tick の予算から削ることになる |
| **D. 昇格も管理ソケットで受ける** | 入口が1つに揃う | ソケットの権限に「制御権を増やす」が加わり、認可の誤りがそのまま authority の増加になる。Registry の lock 待ちと報告（上限 4096 byte を大きく超える）の検証が control process に入る（0060 §2.7） |
| **E. モードを設定ファイルに置き、再起動で反映する** | 入口を作らなくてよい | 再起動で `AUTO` に戻る規則（0028 §2.5 (a)）と矛盾し、毎回 `STARTUP` の Max を通る。`MANUAL` を表せない |
| **F. 読み取り API に `POST` を足す** | Workspace から扱いやすい | 0009 §3 / 0028 §2.2 / §4 で却下済み。AI サーバと同じプロセスに書き込み経路ができる |
| **G. 外からの降格を SIGHUP で伝える** | `stat` が要らない | Python の signal handler は tick の途中に割り込む。systemd の `reload` は「設定の読み直し」を意味し、0028 §2.8 の live reload 禁止と紛らわしい |
| **H. 外からの降格を inotify で伝える** | 変化がすぐ分かる | Linux 固有の依存が増え、監視の失敗という故障の種類が増える。毎 tick の `stat` で足りる |
| **I. 管理ソケットで同じ uid を既定で認める（0045 と同じ）** | 開発・単一ユーザー運用が楽 | fand が root 等で動くと、同じ uid の全サービスが Fan を動かせる。root を暗黙に認めない 0045 の原則とも食い違う |
| **J. 権限を2グループに分ける（安全側だけ：`MAX`・降格 / 弱めうる：`MANUAL`・`MAX` 解除）** | 監視系や Workspace に「全開にする」だけを渡せる | 認可面が2つに増え、設定の誤りの種類が増える。最初は人だけが使うので1グループで足りる。Workspace に渡す段階（§5）で再検討する |
| **K. `MANUAL` に期限を付けない** | 操作が1つ減る | 戻し忘れた固定値が負荷に追従しないまま残る。Guard / Safety の floor は掛かるが、Fallback の追従を失う |
| **L. `MAX` にも期限を付ける** | 戻し忘れの騒音を防げる | 人が全開にした運転が勝手に下がる。安全でない側への自動遷移になる（0060 §2.3） |
| **M. 走行中に journal が壊れたら process を止める（0057 §2.1 の起動時と同じ）** | 状態が単純 | 冷却の制御が止まり、引き継ぎで Max になる。`SHADOW` へ下げて運転を続けるほうが、制御権を増やさずに冷却を保てる |
| **N. モードを永続化し、再起動後も保つ** | 再起動をまたいで `MAX` を保てる | 0028 §2.5 (a) に反する。`MANUAL` の低い値を持ち越す |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | `CALIBRATION` の指令の形（#75 の測定計画をどう参照し、いつ終わるか） | #75 |
| 2 | Workspace / GUI（#60）に管理ソケットの権限を渡すか。渡すなら §4 の J（安全側だけのグループ）を含めて決める | #60（本記録では渡さない） |
| 3 | `coldaisle-fand` の実行ユーザー・管理グループ名・`RuntimeDirectory`・`authority.json` のディレクトリの所有者 | 0060 未決 7 / #57（0069 の後続） |
| 4 | `apply_ack_timeout_ms`・`manual.max_lease_s`・`limits` の値 | 段階 1 の実装で暫定値、運用後に所有者 |
| 5 | 昇格の承認者（`StageApproval.approver`）を CLI の実行 uid に束縛するか。束縛するなら journal の版を上げる | #92 の段階 3 |
| 6 | 監査の表の DDL・migration、`ControlTick` の版番号 | 段階 1 の実装 PR |
| 7 | いまのモードと stage を読み取り API（Server Health など）に出すか | 別の決定記録（0009 / 0040 の拡張） |
| 8 | 0057 §5「降格の永続化に失敗したまま再起動したときの扱い」 | 0057 のまま（本記録では扱わない） |
| 9 | 0060 §2.3 は「プロトコルと認可は #60 で決める」としていたが、本記録が #74 / #92 で決める。0060 側への注記を足すか | 本記録の承認時に所有者 |
