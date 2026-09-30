# 管理ソケット（control-admin）: 運転モードの切り替え

走っている `coldaisle-fand` へ、人が運転モード（`AUTO` / `MANUAL` / `MAX`）と Authority Stage の
降格を届ける入口です。決定記録 [0072](decisions/0072-control-admin-entry.md) の §2.10 段階 1（#74）と
段階 2（#92。`lower_authority` / `rollback_authority` と journal の配線）を実装しています。

- 読み取り API（`coldaisle.api` / `coldaisle.server`）とも、`coldaisle-eventd`（決定記録 0045）とも別の入口です
- LLM のツールからは到達できません（`coldaisle.ai` / `coldaisle.api` / `coldaisle.server` /
  `coldaisle.event_entry` は `coldaisle.control_admin` を import しない。`tests/test_control_admin.py` が走査）
- **制御権を増やせません。** authority の昇格の操作はありません（`raise_authority` は `unknown_op`）。
  昇格は段階 3 の `coldaisle-authority raise`（人が実行する CLI。未実装）だけが行います
- `CALIBRATION` は #75 の測定計画の形が決まるまで `unsupported_mode` で拒否します（段階 4）

## 使い方

```bash
uv run coldaisle-control status
uv run coldaisle-control max --reason "GPU 負荷試験の前に全開にする"
uv run coldaisle-control manual --front 0.6 --rear 0.5 --top 0.7 --lease 30m --reason "騒音の比較"
uv run coldaisle-control auto --reason "比較の終了"
uv run coldaisle-control lower-authority --to-stage limited --reason "夜間の OOD が多い"
uv run coldaisle-control rollback-authority --reason "新しい artifact の挙動を見直す"
```

終了コードは 0 = 受理 / 1 = 拒否 / 2 = 接続できない・応答が壊れている・設定を読めない。

| モード | 意味 | 期限 |
|---|---|---|
| `max` | Critical Safety の `forced_max`（Manual safety override）。Guard の ceiling でも `ramp_down` でも下がらない | なし（勝手に下がる経路を作らない） |
| `manual` | zone ごとの demand（`0.0..1.0`。PWM は受け取らない）を requested にする。Guard の floor・Safety の floor・`forced_max`・`ramp_down` はすべて掛かる | `--lease` 必須。`manual.max_lease_s` 以下。期限が来たら `AUTO` へ戻る |
| `auto` | Gate が選んだ制御器（いまは Fallback）の値 | — |

- 指令は**次の tick の先頭**で効きます。応答は適用を確かめてから返り（`applied: true` と `applied_tick_id`）、
  `apply_ack_timeout_ms` までに適用されなければ `pending: true` を返します（適用は取り消しません）
- 後から来た指令に置き換えられた指令は `superseded_by` を返します
- `coldaisle-fand` を再起動すると `AUTO` に戻ります（モードは永続化しない）
- `MANUAL` の期限は `coldaisle-fand` の**単調時計**だけで数えます（壁時計が動いても変わらない）

## Authority Stage の降格（段階 2。#92）

| 操作 | 意味 |
|---|---|
| `lower-authority --to-stage shadow\|limited\|expanded` | 実効 stage をその stage 以下にする上限を入れる（`full` は行き先に取らない） |
| `rollback-authority` | Baseline（`shadow`）へ戻す |

- どちらも**安全側の指令**です。受付スレッドは採番したら監査の書き込みを待たずに受け渡し口の
  **authority の枠**へ置き、そのあとで受付の行を依頼します（監査を書けなくても取り消さない）
- 受付の時点の stage とは比べません。loop が次の tick の先頭で `AuthorityRuntime` へ**無条件に**
  上限として入れ、その tick の Gate から下がった stage が効きます（応答の `applied_tick_id`）
- journal（`authority.json`）へは heartbeat と decision trace の保存の後に、人の変更
  （`actor = uid.<数値>`、`trigger = human`、理由に `command_id` と入力した理由）として書きます。
  書けなければ memory 上の上限を持ち続け、次の tick で書き直します（`status` の `persist_failure`）
- **journal に人の event が残らない場合があります。** 書く時点で journal が既にその stage 以下
  （例: 読めない journal による自動降格が先に `shadow` を書いた）なら、journal の上では何も変わらないので
  event を足しません。人の指令は監査の表（`accepted` / `applied`）に残ります
- 同じ tick までに降格が複数届いたら、**最も低い行き先**を採ります。採らなかった指令は `superseded_by`
  を返します（後から届いた浅い降格は、先に届いた深い降格に置き換えられる）
- モードの枠とは別の枠なので、`max` と `rollback-authority` は同じ tick で両方効きます
- 走っている `coldaisle-fand` は、外の process（人の CLI）が journal を変えたことを毎 tick の `stat` で知り、
  次の tick から効かせます。走行中に journal が読めなければ `shadow` へ下げ、読めるようになっただけでは
  戻しません（`docs/authority-rollout.md`）

## 設定（`config/control-admin.yaml`）

`coldaisle-fand` は `--admin-config` を省くと `config/control-admin.yaml` を読みます。**この既定の設定は
同じ uid の接続を認めません**（`allow_same_user: false`。0072 §2.5）。同じ uid で動く別のサービス
（読み取り API・AI 層など）から `manual` / `max` を送れないようにするためです。

- `socket.group` の値は**仮の名前**です。配置先で作った専用グループ名に置き換え、操作する人だけをそのグループに
  入れてください。グループを解決できなければ入口は開かず、`coldaisle-fand` は `AUTO` で運転を続けます
- 開発で同じ uid から操作するときだけ、開発用の設定を**明示して**渡します（本番では使わない）

  ```bash
  uv run coldaisle-fand --admin-config config/control-admin.dev.yaml
  ```

  `control-admin.dev.yaml` は `socket.group: null` + `allow_same_user: true` で、それ以外の値は既定の設定と
  揃えています（`tests/test_control_admin.py` が確かめる）。ソケットの場所も同じなので、
  `coldaisle-control` は `--config` を省いたままで接続できます

入口の形だけを持ちます。制御の4ファイル（`fan-hardware.yaml` / `safety.yaml` / `fan-policy.yaml` /
`air-balance.yaml`）には入れません（0072 §2.8）。

| 設定 | 規則 |
|---|---|
| `socket.path` / `socket.mode` / `socket.group` | `event-entry.yaml` と同じ規則（other のビット・setuid / setgid / sticky は拒否） |
| `authorization.allow_same_user` | 既定（`config/control-admin.yaml`）は `false`。`true` は `socket.group: null` の開発用のときだけ |
| `limits.read_timeout_s` | `read_timeout_s * 1000 <= tick_ms`（`safety.yaml` と起動時に照合） |
| `limits.max_connections` | 受信中の接続の上限。埋まれば最も長く受信を続けている接続を閉じる（`evicted`） |
| `limits.max_pending_commands` | 要求を読み終えた後の接続の上限。埋まれば弱めうる指令は `busy`、`max` は先に置いて `pending` |
| `limits.audit_queue_max` | 監査書き込みスレッドの FIFO の上限 |
| `apply_ack_timeout_ms` | `>= tick_ms + tick_deadline_ms`（起動時に照合） |
| `manual.max_lease_s` | `status` / `basis` 付きの暫定値 |

**設定が不正・無い、`SO_PEERCRED` が無い、ソケットを作れない、受付・監査のスレッドを起動できない**ときは入口を開かず、`coldaisle-fand` は
`AUTO` で運転を続けます（error を構造化ログに残す）。`--no-admin` で明示的に開かないこともできます。

認可はファイル権限と `SO_PEERCRED` の2つの門です（0072 §2.5）。**同じ uid も root も暗黙には認めません。**
配置の必須条件: 読み取り API と AI 層を動かすユーザーを `socket.group` に入れない。API / AI サーバを
`coldaisle-fand` と同じユーザーで動かさない。

## 受付スレッドが死んだとき

loop は毎 tick の先頭で受付スレッドの生存を lock を取らずに確かめます。死んでいたら、その tick から

1. 効いている `MANUAL` を解除し（lease の残りも捨てる）
2. `MAX`（`forced_max`）にし
3. decision trace の `mode_command.admin_receiver_dead` を真にして、error を構造化ログに残します

この `MAX` は **`coldaisle-fand` の再起動まで**保ちます（受け渡し口も読まない）。入口を最初から
開かなかった場合は対象外です。

## 監査（`control_admin_audit` 表。migration 0008）

状態を変える指令（`set_mode` / `lower_authority` / `rollback_authority`）の経過を、行を書き換えずに
事象の行として追記します。更新・削除はトリガで拒否します。

| `event` | 持つ欄 | いつ |
|---|---|---|
| `accepted` | `peer_uid`・`op`・`body`（検証済みの本文） | 受付。`max` は受け渡し口へ置いた後、弱めうる指令は置く前 |
| `applied` | `tick_id` | loop が適用した |
| `superseded` | `superseded_by` | 採られなかった（モードの枠は後の指令、authority の枠はより低い行き先の指令に置き換えられた） |
| `lease_expired` | `tick_id` | `MANUAL` の期限切れで `AUTO` へ戻した |

- `(run_id, command_id)` で一意です。`run_id` は起動ごとの 32 桁の16進の乱数で、`command_id` は起動ごとに 1 から振り直します
- 書くのは `coldaisle-fand` の**監査書き込みスレッド1本**だけで、別の SQLite 接続を使います
- `max`・`lower_authority`・`rollback_authority` は監査の書き込みを待たずに効きます。書けなかったときは構造化ログと `status` の `audit_failures` に残ります
- migration 0009 で `superseded_by` の条件を「自分自身ではない」に緩めました（authority の枠では、
  先に受け付けた深い降格が後の浅い降格を置き換えるため）
- 弱めうる指令（`auto`・`manual`）は、受付の行を書けなければ受理しません（`audit_unavailable`）
- 受付の時刻（`ts_ms`）は壁時計で、記録にだけ使います（lease の計算には使わない）

## decision trace（`ControlTick` v12 / v13）

各 tick に `mode_command`（v12）と `authority`（v13）を残します。

| 欄 | 意味 |
|---|---|
| `entry` | `control_admin`（管理ソケットから読む構成）/ `none`（入口を開いていない構成） |
| `run_id` | 監査の表の行と突き合わせる起動の識別子 |
| `command_id` | その tick のモードを決めた指令。起動直後の `AUTO`・lease 切れの `AUTO`・受付の死による `MAX` では null |
| `manual_lease_expired_command_id` | その tick で期限が切れた `MANUAL` の指令 |
| `admin_receiver_dead` | 受付スレッドの死による `MAX`（再起動まで毎 tick 真） |

v13 の `authority` は `entry`（`journal` / `static`）・`journal_stage` / `journal_revision`・`config_ceiling`・
`unpersisted_ceiling`（書き残せずに持っている上限）・`journal_unreadable`・`command_id`（その tick の先頭で
入れた降格）・`persist_failure` です。`state.authority_stage` はこれらの上限の最小を超えません。

エアフロー画面は「モードの出どころ」と「制御権の出どころ」として表示します（受付の停止による最大、
journal を読めない・書き残せないことは `bad` の色）。

## `status` の欄

`mode`・`command_id`・`manual_lease_remaining_ms`・`authority_stage`（実効）・`authority_journal_stage`・
`authority_journal_revision`・`authority_ceiling`（`fan-policy.yaml` の上限）・`authority_unpersisted_ceiling`・
`authority_journal_unreadable`・`persist_failure`・`entry`・`audit_failures`・`run_id`・`tick_id`。
authority の欄は loop が最後に置いた写しです（まだ1 tick も回っていなければ null）。**読み取り API には出しません。**
