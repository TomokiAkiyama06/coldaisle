# 管理ソケット（control-admin）: 運転モードの切り替え

走っている `coldaisle-fand` へ、人が運転モード（`AUTO` / `MANUAL` / `MAX`）を届ける入口です。
決定記録 [0072](decisions/0072-control-admin-entry.md) の §2.10 段階 1（#74）を実装しています。

- 読み取り API（`coldaisle.api` / `coldaisle.server`）とも、`coldaisle-eventd`（決定記録 0045）とも別の入口です
- LLM のツールからは到達できません（`coldaisle.ai` / `coldaisle.api` / `coldaisle.server` /
  `coldaisle.event_entry` は `coldaisle.control_admin` を import しない。`tests/test_control_admin.py` が走査）
- **制御権を増やせません。** authority の昇格の操作はありません。段階 1 では
  `lower_authority` / `rollback_authority` も `unsupported_op` で拒否します（段階 2 の #92 で受理）
- `CALIBRATION` は #75 の測定計画の形が決まるまで `unsupported_mode` で拒否します（段階 4）

## 使い方

```bash
uv run coldaisle-control status
uv run coldaisle-control max --reason "GPU 負荷試験の前に全開にする"
uv run coldaisle-control manual --front 0.6 --rear 0.5 --top 0.7 --lease 30m --reason "騒音の比較"
uv run coldaisle-control auto --reason "比較の終了"
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
| `accept_backoff.initial_ms` / `accept_backoff.max_ms` | `accept()` が失敗し続けるときに待ち受けを休む間隔。`initial_ms <= max_ms <= tick_ms`（`safety.yaml` と起動時に照合）。`status` / `basis` 付きの暫定値 |

**設定が不正・無い、`SO_PEERCRED` が無い、ソケットを作れない、受付・監査のスレッドを起動できない**ときは入口を開かず、`coldaisle-fand` は
`AUTO` で運転を続けます（error を構造化ログに残す）。`--no-admin` で明示的に開かないこともできます。

認可はファイル権限と `SO_PEERCRED` の2つの門です（0072 §2.5）。**同じ uid も root も暗黙には認めません。**
配置の必須条件: 読み取り API と AI 層を動かすユーザーを `socket.group` に入れない。API / AI サーバを
`coldaisle-fand` と同じユーザーで動かさない。

## 接続の受け付けが失敗し続けるとき

fd の枯渇（`EMFILE` / `ENFILE`）や `ECONNABORTED` などで `accept()` が失敗しても、受付スレッドは死なせません。
待ち受けのソケットは読める状態のままなので、そのまま監視を続けると受付スレッドが空回りしてログを溢れさせます。
そこで失敗したら**待ち受けのソケットだけ**を selector から外し、単調時計の期限が来たら戻します。

- 休む長さは最初が `accept_backoff.initial_ms`、失敗のたびに倍にして `accept_backoff.max_ms` で頭打ち。
  `accept()` が1回でも成功したら戻します（回復を info で1行残す）
- **`sleep` しません。** 休んでいる間も接続済みの接続は読み続け、`max` は遅れずに受け渡し口へ置きます。
  待たされるのは新しい接続だけで、その待ちは `max_ms`（`tick_ms` 以下）を超えません
- **`MAX` には倒しません**（入口の不調で Fan を動かさない。所有者の判断）。受付スレッドが死んだときとは扱いが違います
- ログ（warning。`reason: admin_accept_failed`）は最初の失敗と、休みが伸びたとき、頭打ちの後は連続の失敗の回数が
  2 の冪に届いたときだけ出します。`consecutive_failures`・`suppressed_since_last_log`（出さなかった回数）・
  `backoff_ms`・`errno` を持ちます。スタックトレースは最初の1回だけです
- `initial_ms = 100` / `max_ms = 1000` は暫定値です（`status: provisional`）

## 受付スレッドが死んだとき

loop は毎 tick の先頭で受付スレッドの生存を lock を取らずに確かめます。死んでいたら、その tick から

1. 効いている `MANUAL` を解除し（lease の残りも捨てる）
2. `MAX`（`forced_max`）にし
3. decision trace の `mode_command.admin_receiver_dead` を真にして、error を構造化ログに残します

この `MAX` は **`coldaisle-fand` の再起動まで**保ちます（受け渡し口も読まない）。入口を最初から
開かなかった場合は対象外です。

## 監査（`control_admin_audit` 表。migration 0008）

状態を変える指令（`set_mode`）の経過を、行を書き換えずに事象の行として追記します。更新・削除はトリガで拒否します。

| `event` | 持つ欄 | いつ |
|---|---|---|
| `accepted` | `peer_uid`・`op`・`body`（検証済みの本文） | 受付。`max` は受け渡し口へ置いた後、弱めうる指令は置く前 |
| `applied` | `tick_id` | loop が適用した |
| `superseded` | `superseded_by` | 後の指令に置き換えられた |
| `lease_expired` | `tick_id` | `MANUAL` の期限切れで `AUTO` へ戻した |

- `(run_id, command_id)` で一意です。`run_id` は起動ごとの 32 桁の16進の乱数で、`command_id` は起動ごとに 1 から振り直します
- 書くのは `coldaisle-fand` の**監査書き込みスレッド1本**だけで、別の SQLite 接続を使います
- `max` は監査の書き込みを待たずに効きます。書けなかったときは構造化ログと `status` の `audit_failures` に残ります
- 弱めうる指令（`auto`・`manual`）は、受付の行を書けなければ受理しません（`audit_unavailable`）
- 受付の時刻（`ts_ms`）は壁時計で、記録にだけ使います（lease の計算には使わない）

## decision trace（`ControlTick` v12）

各 tick に `mode_command` を残します。

| 欄 | 意味 |
|---|---|
| `entry` | `control_admin`（管理ソケットから読む構成）/ `none`（入口を開いていない構成） |
| `run_id` | 監査の表の行と突き合わせる起動の識別子 |
| `command_id` | その tick のモードを決めた指令。起動直後の `AUTO`・lease 切れの `AUTO`・受付の死による `MAX` では null |
| `manual_lease_expired_command_id` | その tick で期限が切れた `MANUAL` の指令 |
| `admin_receiver_dead` | 受付スレッドの死による `MAX`（再起動まで毎 tick 真） |

エアフロー画面は「モードの出どころ」として表示します（受付の停止による最大は `bad` の色）。

## `status` の欄

`mode`・`command_id`・`manual_lease_remaining_ms`・`authority_stage`（実効）・`authority_journal_stage`
（段階 1 では null）・`authority_ceiling`（`fan-policy.yaml` の上限）・`persist_failure`（段階 1 では null）・
`entry`・`audit_failures`・`run_id`・`tick_id`。**読み取り API には出しません。**
