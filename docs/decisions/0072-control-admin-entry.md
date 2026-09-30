# 決定記録 0072: 制御デーモンの管理操作の入口（運転モードと Authority Stage の切替）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-29、リポジトリ所有者が承認）
- **Date**: 2026-09-29
- **Supersedes**: なし
- **Superseded by**: [0076](0076-implementation-settled-points.md)（§2.3 の「応答」の項のうち、受理の応答の形のみ。§2.2 の `superseded` になった指令の受理の応答に `superseded_by`（置き換えた指令の `command_id`）を加える。§2.3 の残りと他の節は有効） / [0081](0081-drain-authority-on-receiver-death.md)（§2.2 の「死んだあとは受け渡し口を読まない」のうち authority の枠に残った降格のみ。死を検知したら authority の枠だけを取り出せるまで試して journal へ書き残す。モードの枠と他の節は有効）
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
- **対象 Issue**: #92（Authority Stage 切替の管理操作。本記録の PR はこの Issue に属する）
- **関連 Issue**: #74（運転モードの入口）。本記録は入口の設計を1つに決めるために #74 の受入基準も参照するが、
  **実装は Issue ごとに別の PR に分ける**（AGENTS.md「1 Issue = 1 ブランチ = 1 PR」。§2.10 の「担当 Issue」列）

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
本記録は 2026-09-29 に所有者が承認した（FINAL）。§2 が決定、§4 は検討した代替案とその得失である。

---

## 2. Decision

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

- 待ち受けは `coldaisle-fand` の中の**受付スレッド1本**。ただし受付スレッドは接続を1つずつ
  直列に処理しない。**I/O 多重化（`selectors`）で複数の接続を同時に持ち**、ある接続の要求の
  受信・適用の確認待ち（下）・応答の送信の途中でも、新しい接続を受け付けて次の指令を
  受け渡し口へ置ける。直列にすると、1件の確認待ちや遅いクライアントの読み取り
  （`limits.read_timeout_s`）の間、後から来た `set_mode(max)` や rollback が次の tick の
  受け渡し口に入れず、下の同じ軸の置き換え・合成の規則も働かない
  （接続の同時数の上限は下の2つに分ける。上限を超えたときの扱いは段階ごとに下で決め、閉じた接続は接続ごとのログに残す）
- **安全側の指令が受け渡し口へ届く枠を、待っている接続に食わせない。** 接続を2つの段階に分けて
  別々に数える
  - **受信中の接続**（要求の1行を読み終えていない）: 上限 `limits.max_connections`。
    枠が埋まっているときに新しい接続が来たら、**新しい接続を閉じるのではなく、受信中の接続のうち
    最も長く受信を続けているものを閉じて**新しい接続を受ける（接続ごとのログに `evicted` を残す）。
    新しい接続を閉じる方式では、認可済みのクライアントが要求の1行を送り切らない接続を
    `max_connections` 本持ち続けるだけで、後から来た `set_mode(max)` や rollback が
    `limits.read_timeout_s` のあいだ読まれず、次の tick に届く約束（§2.1 の表）が
    `read_timeout_s` の設定しだいで何 tick も崩れる。要求は `limits.max_message_bytes` 以下の
    1行で、正しいクライアントは接続の直後に送り切るので、最も古い受信中の接続を追い出しても
    正しい要求はほぼ失われない（追い出されたクライアントには応答が返らないので、再送できる）。
    認可（§2.5）は受け付けた時点で先に判定し、認可されなかった接続はすぐ閉じてこの枠に数えず、
    他の接続を追い出す理由にもしない。受信中の接続は `limits.read_timeout_s` でも必ず閉じる
  - さらに `limits.read_timeout_s` は **`tick_ms` 以下**（`read_timeout_s * 1000 <= tick_ms`）でなければならない（起動時に
    `safety.yaml` と照合し、満たさなければ §2.8 の「不正な設定」として扱う）。追い出しに加えて
    時間でも、受信中の接続が1 tick を超えて枠を占めないようにする
  - **受信後の接続**（弱めうる指令の監査の書き込み待ち、適用の確認待ち、応答の送信中）:
    要求を読み終えた時点で受信中の枠から外し、別の上限 `limits.max_pending_commands` で数える。
    監査の DB の lock で書き込み待ちが積み上がっても、受信中の枠は減らないので、後から来た
    `set_mode(max)` や rollback は読まれて受け渡し口へ置かれる
  - 受信後の枠が埋まっているときは、**向きで分ける**（§2.7）。
    冷却を弱めうる指令は受け渡し口へ置かず、監査も依頼せずに `busy` で拒否する（接続ごとのログに残す）。
    安全側の指令は**先に受け渡し口へ置き**（監査の依頼も §2.7 のとおり行う）、適用の確認を待たずに
    `{"ok": true, "applied": false, "pending": true}` を返して閉じる。安全側の指令が受け渡し口へ
    届くかどうかは、受信後の枠の空きに依存しない
- 受付スレッドは loop の状態
  （`ControlLoop` / `ControllerGate` / `AuthorityRuntime`）に**一切触れない**
- 受付スレッドは検証済みの指令を**受け渡し口**（mailbox）へ置くだけにする。受け渡し口は
  **状態の軸ごとに1枠**（モードの枠と authority の枠の2枠）を持つ。loop は tick の先頭で1回だけ
  受け渡し口を**非ブロッキングで**覗き、2枠を同じ lock の中で取り出す。lock が取れなければ
  その tick は直前のモード・stage のまま進む（0060 §2.3「待たない」「読めない tick は直前を保つ」）
- 次の tick までに**同じ軸**の指令が複数来たときだけ置き換える。**別の軸の指令は互いに消さない**
  （例: `set_mode(max)` のあとに `rollback_authority` が来ても、両方が同じ tick で効く。
  モードの軸の安全側の指令が authority の軸の指令で失われない）
  - モードの枠: **`command_id` が最大の1件**を採る（人の最新の意図。`MAX` のあとの `set_mode(auto)` は、
    人が弱めると決めた指令として §2.7 の監査を経て受けたものである）。「最後に置かれた1件」ではない。
    §2.7 のとおり安全側の指令は監査を待たずに置き、弱めうる指令は監査の書き込みを待ってから置くので、
    **先に届いた弱めうる指令が、後から届いて先に置かれた `MAX` を上書きしうる**。これを防ぐため、
    受け渡し口はモードの軸で**これまでに置いた最大の `command_id`** を覚え（loop が取り出したあとも
    保つ）、それより小さい `command_id` の指令は置かずに `superseded` とする
  - authority の枠: 置き換えずに**合成する**。`rollback_authority` と `lower_authority` は
    どちらも下げる向きなので、届いた指令のうち**最も低い行き先**を採る（後から来た「浅い降格」が
    先の rollback を打ち消さない）
  - 採られなかった指令は `superseded` として応答・監査に残す（§2.7）
- loop は適用した指令の `command_id` と `tick_id` を受け渡し口へ返す。受付スレッドは
  それを `apply_ack_timeout_ms` まで待って応答する（**待つのは受付スレッドで、loop ではない**。
  待つ間も、上の多重化により他の接続の受付は止まらない）。
  時間内に適用されなければ `{"ok": true, "applied": false, "pending": true}` を返し、
  適用は取り消さない（遅れて効いたことは監査と decision trace に残る）
- **受付スレッドが死んだら、loop が自分で Max に倒す。** loop は**毎 tick の先頭**（受け渡し口を
  覗く前）に、受付スレッドが生きているかを **lock を取らずに**確かめる（`OperatingModeSource` の
  非ブロッキングな照会。実装は `coldaisle.control_admin` にあり、`coldaisle.control` は
  その Protocol だけを知る）。死んでいたら、その tick で
  1. 効いている `MANUAL` を解除する（lease の残りも捨てる）
  2. モードを `MAX` にする。`MAX` は Critical Safety の `forced_max`（§2.4、0028 §2.4）として
     表すので、Guard の ceiling でも `ramp_down` でも下がらない
  3. decision trace の tick に `admin_receiver_dead` を残し、error を構造化ログに残す

  この `MAX` は **`coldaisle-fand` の再起動まで保つ**。死んだあとは受け渡し口を読まない
  （受付スレッドの死後に残った指令も適用しない）ので、人の指令で下げる経路も無い。
  lease も付けない（勝手に下がる経路を作らない。§2.4 の `MAX` と同じ）。
  **これは 0028 §2.7 の SIGTERM での引き継ぎに依存しない。** 通常の停止での引き継ぎは
  元の `pwmN_enable` が `0` / `2+` なら自動制御へ戻すので、低い `MANUAL` が効いていた
  ときに service を止めても冷却が強まるとは限らない。入口が死んだ状態では、人が冷却を
  強める手段（`set_mode(max)`）も `MANUAL` から出る手段も失われるため、loop の中で決定論的に
  安全側へ倒す。
  ただし、**入口を最初から開かなかった場合**（§2.5 の `SO_PEERCRED` が無い、§2.8 の設定が不正）は
  死んだとは扱わない（`MANUAL` に入る経路が無く、`AUTO` と journal の stage で運転する）。
  `coldaisle-fand` 自身の停止の手順で受付スレッドを止めるときも死んだとは扱わない
  （loop が止まった後に止める）
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
| `to_stage` | `lower_authority` のときだけ必須。受付スレッドは実効 stage と比べて拒否しない（受付時点の stage は古いことがある）。適用するかどうかは loop が §2.6 で決める |
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
- **降格は古い snapshot で no-op と判断しない。** loop は `lower_authority` の `to_stage` を、その時点の
  実効 stage と比べずに**無条件で** in-memory の上限として入れ、journal への降格を試みる。
  外の process（CLI の昇格）が直前の `stat` の後に journal を上げていても、この上限は `reload()` で
  外れないので、後から届いた降格が捨てられて authority が上がることはない。結果の `changed` は、
  上限を入れた後の実効 stage が入れる前より下がったかどうかで返す（下がらなくても上限は残る）

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
  制御権を最小にする**。
- **この上限は、正しく読めるようになっただけでは外さない。** これは 0057 §2.6 の自動降格の
  1つ（原因は `authority_journal_unreadable`）として扱い、同じ規則に従う。すなわち
  「先に in-memory、あとで journal」で、loop は次の tick 以降（heartbeat の後、lock の待ち上限つき。
  0060 §2.7）に `lower_stage(to_stage=SHADOW)` で journal へ書き残しを試みる。
  **書けたら** journal が `SHADOW` を表すので in-memory の上限を手放す（0057 §2.6 の「書けたら
  手放す」。下げ先は journal と同じなので authority は上がらない）。**書けなければ**上限を
  持ち続け、`reload()` でも外れない（0057 §2.6）。読めたときの journal が高い stage を
  表していても、それを理由に戻さない。一時的な読み取りの失敗のあとで、状態の整合性が
  崩れた可能性のある journal を「読めたから」と信じて制御権を自動で戻すと、0057 §2.6
  「下がったあとに自動で戻る経路は無い」に反する。戻すには 0057 §2.3 の承認による昇格
  （`SHADOW` から1段ずつ）が要る
- `AutomaticCause` に `authority_journal_unreadable` を足す（journal の版の扱いは段階 2 の実装 PR で決める）

### 2.7 監査：全接続をログに、状態を変えた指令を追記専用の表に残す

- **接続ごと**（受理・拒否とも）に JSON Lines で `peer_uid`・`op`・結果・理由の code を残す
  （0045 §2.3 と同じ。入力の値そのものは拒否の理由に反射しない）
- **状態を変える指令**（`set_mode` / `lower_authority` / `rollback_authority`）は、
  `coldaisle-fand` が**自分の表にだけ書く別の接続**（0066 の前例）で、追記専用の表へ残す。
  更新・削除はトリガで拒否する（0045 §2.5 と同じ）。表の DDL と migration は実装 PR で決める
- **監査の表へ書くのは、専用の監査書き込みスレッド1本だけ**にする。受付スレッドは書き込みの
  依頼を **FIFO の queue** へ入れるだけで、自分では DB を開かない。書き込みは依頼の順に1件ずつ行う
  （同じ指令の受付の行が結末の行より先に書かれる）。queue には上限（`limits.audit_queue_max`）を
  置き、溢れた依頼は書き込みの失敗として扱う（下の「向きで分ける」）。監査の DB が lock されて
  いても、待つのはこのスレッドだけで、受付スレッドの多重化（§2.2）も loop も止まらない。
  loop はこのスレッドにも queue にも触れない（0060 §2.7 の「保存を別スレッドへ逃がさない」は
  loop の tick の中の decision trace の話で、loop の外の監査には当たらない）
- **`command_id` は受け渡し口へ置く前に、受付スレッドが採番する。** プロセスの中で単調に増える
  整数で、DB を待たずに決まる。監査の行は起動ごとの識別子（`run_id`）との組で一意にする
  （再起動で番号が戻っても行が衝突しないため。形は実装 PR で決める）。受付の順と
  `command_id` の順は一致し、§2.2 のモードの枠の比較はこの番号で行う
- **置く順番は向きで分ける。**
  - **安全側の指令**（`MAX` への移行、`lower_authority`、`rollback_authority`）は、
    採番したら**先に受け渡し口へ置き**、そのあとで受付の行の書き込みを依頼する。
    監査の書き込みの成否を待たず、失敗しても**取り消さない**（安全側は記録を待たない。
    0057 §2.6 と同じ考え方）。操作者の Manual safety override が監査の DB の lock
    （いまの `Store` の busy timeout は 5 秒）で何 tick も遅れることを避ける
  - **冷却を弱めうる指令**（下の「向きで分ける」）は、受付の行の書き込みを依頼し、
    **書けたと分かってから**受け渡し口へ置く。完了の知らせは受付スレッドの多重化の中で受け取り、
    待つ間も他の接続（後から来た `set_mode(max)` など）の受付は止めない。書けなければ置かずに拒否する。
    書けた時点で、モードの軸にそれより大きい `command_id` がすでに置かれていたら置かずに
    `superseded` とする（§2.2）
- **1つの指令の経過は、行を書き換えずに「事象の行」を足して表す。** 受付の時点では結果も
  適用した tick も分からないため、結果の列を後から埋める形にすると追記専用のトリガと両立しない。
  - 受付の行（`event = accepted`）: `command_id`・受付時刻（壁時計）・`peer_uid`・`op`・
    検証済みの本文。弱めうる指令では**受け渡し口へ置く前に**確定し、安全側の指令では
    **置いた後に**書く（上の「置く順番」）
  - 結末の行（`event = applied` / `superseded`）: 同じ `command_id`・記録時刻・
    `applied` なら適用した `tick_id`、`superseded` なら置き換えた `command_id`。
    1つの `command_id` に結末の行は**高々1つ**（一意制約）
  - lease 切れの行（`event = lease_expired`）: **結末の行とは別の事象**として足す。適用された
    `MANUAL` はまず `applied` の結末を持ち、そのあと人がモードを変えないまま期限が来たときに
    この行が加わる（同じ `command_id`・記録時刻・期限切れを判定した `tick_id`）。
    1つの `command_id` に**高々1つ**（結末の行とは別の一意制約）。`applied` の結末を持つ
    `MANUAL` の指令にだけ書く
  - 拒否（検証の失敗・認可の失敗・弱めうる指令で受付の行を書けなかった）は受付の行を作らず、
    接続ごとのログ（上）にだけ残す
  - 受付の行があって結末の行が無い指令は「受理済み・未確定」（応答の `pending` と同じ状態）として
    読む。結末の行が書けなくても適用は取り消さない。適用の事実は decision trace の `command_id`（下）に
    残るので、弱めうる指令が「記録なしで効いた」状態にはならない（受付の行は適用より前に確定している）
  - 受付の行が無く結末の行だけがある `command_id` は、受付の行を書けなかった安全側の指令として読む
    （結末の行は受付の行の存在を前提にしない）
- **書けなかったときは向きで分ける。** 冷却を強める・制御権を下げる指令（`MAX` への移行、
  `lower_authority`、`rollback_authority`）は、監査の書き込みに失敗しても（queue が溢れた、
  監査書き込みスレッドが死んだ場合を含む）**適用を妨げず、取り消さない**。失敗は構造化ログに
  `command_id` 付きで残し、`status` の監査の失敗数に数える。**それ以外の状態を変える指令はすべて
  冷却を弱めうるものとして扱い**、受付の行を書けなければ受理しない（記録の無い弱化を作らない）。
  これには `MAX` から出る、`MANUAL` に入る・値を変える、**`MANUAL` から出る（`AUTO` へ戻すことを
  含む）**が入る。`MANUAL` の値が Fallback の出力より高いとき、`AUTO` へ戻すと冷却が下がるためである。
  向きを実際の demand の比較で決めることはしない（比較の時点と適用の tick で Fallback の出力が
  変わりうるため、指令の種類だけで保守的に決める）
- authority の変更は 0057 の journal にもそのまま残る（`actor = uid.<数値>`、`trigger = human`）
- decision trace の各 tick にはすでに `operating_mode` と `authority_stage` が残る。
  加えて、その tick に効いていたモードを決めた `command_id` と、`MANUAL` の期限切れと、
  受付スレッドの死による `MAX`（`admin_receiver_dead`、§2.2）を残す（`ControlTick` の版を上げる。番号は実装 PR が他の PR と取り合わないように決める）
- lease 切れの行（`lease_expired`）と、loop が適用したときの結末の行は、loop の tick の中では書かない。
  loop は結果を受け渡し口へ返すだけで、受付スレッドがそれを監査書き込みスレッドへ依頼する
  （loop は DB を待たない。0060 §2.3）
- `status` は、いまのモード・`command_id`・`MANUAL` の残り期限・実効 stage・journal の stage・
  設定の上限・`persist_failure`・入口の起動状態・監査の失敗数を返す。**読み取り API には出さない**
  （出すかは §5）

### 2.8 設定：`config/control-admin.yaml`

| 設定 | 規則 |
|---|---|
| `socket.path` / `socket.mode` / `socket.group` | 0045 §2.2 と同じ規則（other のビット・setuid / setgid / sticky は拒否） |
| `authorization.allow_same_user` | 既定 `false`。`true` は `socket.group: null` の開発用のときだけ許す |
| `limits.max_message_bytes` / `limits.read_timeout_s` | 0045 と同じ意味。ただし `read_timeout_s * 1000 <= tick_ms`（起動時に `safety.yaml` と照合。§2.2） |
| `limits.max_connections` | 受付スレッドが同時に持つ**受信中**の接続の上限。埋まっているときは最も長く受信を続けている接続を閉じて新しい接続を受ける（§2.2） |
| `limits.max_pending_commands` | 要求を読み終えた後（監査の書き込み待ち・適用の確認待ち・応答の送信中）の接続の上限。受信中の枠とは別に数える（§2.2） |
| `limits.audit_queue_max` | 監査書き込みスレッドの FIFO の上限（§2.7） |
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

| 段階 | 担当 Issue（1段階ずつ別の PR） | 内容 | 前提 |
|---|---|---|---|
| 1 | #74 | 管理ソケット（`AUTO` / `MAX` / `MANUAL` と lease、`status`）・受け渡し口のモードの枠・監査の表と監査書き込みスレッド（§2.7。`MAX` は監査の前に置く）・受付スレッドの生存確認と `admin_receiver_dead` の `MAX`（§2.2。受付スレッドを止めた試験で、次の tick から `MANUAL` が解除されて `forced_max` になり、再起動まで保たれることを確かめる）・`coldaisle-control` クライアント・§2.9 の試験。**この段階では `lower_authority` / `rollback_authority` を受理しない**（`unsupported_op` で拒否） | 本記録の承認 |
| 2 | #92 | `AuthorityRuntime` を `coldaisle-fand` へ配線し、受け渡し口の authority の枠と `lower_authority` / `rollback_authority` の受理を足し、journal の変化を毎 tick 検知する（§2.6） | 段階 1 |
| 3 | #92 | `coldaisle-authority raise` / `rollback` の CLI | 段階 2（昇格が走行中の loop に届くため） |
| 4 | #74 | `CALIBRATION` の受理 | #75 の測定計画の形 |

実機で初めてこの入口からモードを変えるのは、0028 §2.9 の承認点 3（実機の制御を初めて取る）の
確認項目に含める。

---

## 3. Consequences

### 良くなること

- 0028 未決 3・0060 未決 4・0057 §5 の2項（入口、外からの降格）が閉じ、#74 はモードを、
  #92 は stage を、走っている `coldaisle-fand` に対して変えられるようになる
- 管理ソケットが「上げられない」ので、ソケットの認可を誤っても制御権は増えない。
  増やせるのは人が CLI で行う昇格だけで、0057 §2.3 の検証をすべて通る
- loop はどの入口も待たない。入口が壊れても冷却と deadman は影響を受けず、受付スレッドが死んだら
  loop が自分で `MAX` に倒す（0028 §2.7 の停止時の引き継ぎに頼らない）
- 監査の DB が lock されていても、`MAX` と降格は待たずに次の tick で効く（§2.7）
- 誰が・いつ・どのモードにしたかが、追記専用の表・journal・decision trace の3か所で辿れる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| `coldaisle-fand` にスレッドが1本増える | 受付スレッドは loop の状態に触れず、受け渡し口は軸ごとに1枠（モードと authority の2枠）。loop 側は非ブロッキングで覗くだけ（§2.2） |
| 受付スレッドが死ぬと、再起動まで `MAX` で運転する（騒音が増え、`MANUAL` / `AUTO` に戻せない） | 入口が死ぬと人は冷却を強めることも `MANUAL` から出ることもできないので、安全側に固定する。decision trace の `admin_receiver_dead` と error のログで気づき、再起動で `AUTO` に戻す。入口を最初から開かなかった場合は対象外（§2.2） |
| スレッドがもう1本（監査書き込み）増える | loop は触れない。受付スレッドは queue に入れるだけ。queue は上限つき（§2.7） |
| 安全側の指令は、監査の行が無いまま効くことがある | 失敗は `command_id` 付きで構造化ログと `status` に残り、適用の事実は decision trace と authority の journal に残る。弱めうる指令は従来どおり監査が書けたときだけ置く |
| 昇格が走行中の loop に効くまで最大 1 tick 遅れる | 安全側ではないので遅れてよい。降格は受け渡し口経由で次の tick から効く |
| 毎 tick の `stat` が1回増える | heartbeat の後に置き、flock を待たない（§2.6） |
| `MANUAL` の lease が切れると勝手に `AUTO` へ戻る | 戻り先は Fallback の自動追従で、Guard / Safety はそのまま効く。`MAX` には lease を付けない |
| 監査を書けないと `MANUAL` に入れず、`MANUAL` から `AUTO` へも戻せない | 安全側（`MAX`・降格）は監査を待たずに先に置くので、書けなくても遅れずに効く。弱める向きだけを止める。`MANUAL` は lease 切れで `AUTO` へ戻る（loop が決めるので監査の可否に依存しない） |
| 同じ uid は守れない | 0045 §2.3 と同じ限界。API / AI サーバを別ユーザーで動かすことを配置の必須条件にする（§2.5） |
| 設定ファイルが1つ増える | 入口の形だけを持ち、制御の閾値を持たない。壊れていても入口が閉じるだけ（§2.8） |

---

## 4. 却下した代替案

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
| 4 | `apply_ack_timeout_ms`・`manual.max_lease_s`・`limits`（`max_connections`・`max_pending_commands`・`audit_queue_max` を含む）の値 | 段階 1 の実装で暫定値、運用後に所有者 |
| 5 | 昇格の承認者（`StageApproval.approver`）を CLI の実行 uid に束縛するか。束縛するなら journal の版を上げる | #92 の段階 3 |
| 6 | 監査の表の DDL・migration（`run_id` と `command_id` の組の形を含む）、`ControlTick` の版番号と `admin_receiver_dead` の表し方 | 段階 1 の実装 PR |
| 7 | いまのモードと stage を読み取り API（Server Health など）に出すか | 別の決定記録（0009 / 0040 の拡張） |
| 8 | 0057 §5「降格の永続化に失敗したまま再起動したときの扱い」 | 0057 のまま（本記録では扱わない） |
| 9 | 0060 §2.3 は「プロトコルと認可は #60 で決める」としていたが、本記録が #74 / #92 で決める。0060 側への注記を足すか | 0060 側には注記を足さない。0060 は FINAL で、追記は `Superseded by` だけが許される（docs/decisions/README.md「追記のみ」）。本記録は 0060 の決定を置き換えず、0060 が先送りした点を決めるものなので `Supersedes` に当たらない。0060 から本記録へは README の索引でたどる |
