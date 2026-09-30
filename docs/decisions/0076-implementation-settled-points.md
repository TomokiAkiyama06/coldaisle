# 決定記録 0076: 0071 / 0072 / 0073 の実装で決着した点（版番号・監査の表・暫定値・応答・受付の失敗）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-30、リポジトリ所有者が承認）
- **Date**: 2026-09-30
- **Supersedes**: なし（各記録が実装 PR へ委ねた点と、実装で記録の文面と異なった事実を記録する。
  どの記録の決定も置き換えない）
- **関連**: [0060](0060-control-loop-runtime.md) §5 /
  [0071](0071-control-trace-read-api.md) §2.6 / §5 #1 /
  [0072](0072-control-admin-entry.md) §2.2 / §2.3 / §2.4 / §2.5 / §2.7 / §2.8 / §5 #3 / #4 / #6 /
  [0073](0073-air-balance-control-config-integration.md) §2.5 / §5（版番号の衝突） /
  [0075](0075-trace-registry-block-reason-digest.md) §2.3 /
  `src/coldaisle/control/schema.py` / `src/coldaisle/control/operating_mode.py` /
  `src/coldaisle/control/safety/critical.py` / `src/coldaisle/control_admin/` /
  `src/coldaisle/store/migrations/0008_control_admin_audit.sql` /
  `config/control-admin.yaml` / `config/control-admin.dev.yaml` / `config/airflow-ui.yaml`
- **対象 Issue**: #74 / #81 / #106

## 1. Context

0071・0072・0073 は、いくつかの点（版番号・監査の表の DDL・設定の値・画面の閾値）を
実装 PR に委ねた。それらの PR（#185 / #186 / #187）はマージ済みである。

実装の中で決まった点と、記録の文面と実装が異なる点が、PR の説明とコードの注釈にしか
残っていない。決定記録は追記のみで（`docs/decisions/README.md`）、0072 / 0073 の本文には
手を入れられない。記録だけを読んだ人が、実装と違う版番号や未決のままの項目を前提に
しないよう、ここに1か所で残す。

リポジトリ所有者は 2026-09-30 の Claude Code のセッションで、以下の §2.1〜§2.8 を確認し、承認した。

## 2. Decision

### 2.1 Air Balance の統合は `ControlTick` v11（0073 §2.5 の「v10」ではない）

0073 §2.5 は、Air Balance の記録（`air_balance`）と `applied_demand` / `estimated_flow` を
加える版を「9 → 10」と書いた。実装（PR #186、#81）では **v11** にした。
先に #104（PR #182、0071 §2.5 / 0075 §2.3）が v10 を registry の塊に使っていたためである。

- 0073 §5 の「版番号の衝突」の行（「別の記録・実装が先にその番号を使った場合は、実装の時点で
  次の空いた番号を使う。欄の意味は本記録のとおり」）に従った結果であり、欄の意味は 0073 のまま
- 0073 の本文の「v10」は、`air_balance` を持つ版としては **v11 と読み替える**。
  v10 の tick は registry の塊（`registry`）を持つ版で、`air_balance` を持たない
- 0073 §2.5 (c) の `ControlTickRuntime` v2 と、0073 §5 に挙げた他の版（束ねた版 11・
  `AuthorityJournal` v2）は記録どおりの番号で入った
  （`src/coldaisle/control/schema.py` の `CONTROL_TICK_RUNTIME_SCHEMA_VERSION`、
  `src/coldaisle/control/config.py` の `CONTROL_CONFIG_VERSION`、
  `src/coldaisle/control/authority.py` の `AUTHORITY_JOURNAL_SCHEMA_VERSION`）
- 根拠: `src/coldaisle/control/schema.py` の `SCHEMA_VERSION` の版の履歴（v10 は #104 / 0071 §2.5、
  v11 は #81 / 0073 §2.5）

0073 の本文は書き換えない。0073 には `Superseded by` も付けない（本記録は 0073 の決定を
置き換えず、0073 §5 が許した番号の繰り上げを記録するだけで、`Supersedes` に当たらない。
README が旧記録へ許す追記は `Superseded by` だけである）。0073 から本記録へは README の索引でたどる。

### 2.2 0072 §5 #6: 監査の表・`run_id`・`ControlTick` の版と `admin_receiver_dead`

段階 1（PR #187、#74）で次のとおり決めた。

**(a) 監査の表**: `src/coldaisle/store/migrations/0008_control_admin_audit.sql` の `control_admin_audit`。

- **追記のみを制約で持つ。** `BEFORE UPDATE` / `BEFORE DELETE` のトリガ
  （`control_admin_audit_no_update` / `control_admin_audit_no_delete`）で拒否する
- 1つの指令の経過は、行を書き換えずに事象の行を足す（0072 §2.7）。`event` 列は
  `accepted` / `applied` / `superseded` / `lease_expired` の4値（`CHECK` で閉じる）
- 列: `run_id` / `command_id` / `event` / `ts_ms` / `peer_uid` / `op` / `body` / `tick_id` / `superseded_by`。
  `peer_uid` / `op` / `body` は `accepted` の行だけが持ち、`tick_id` は `applied` / `lease_expired` だけ、
  `superseded_by` は `superseded` だけが持つ（いずれも `CHECK`）。`body` は JSON の object、
  `superseded_by` は自分の `command_id` より大きい
- **`(run_id, command_id)` の部分一意索引を3つ**置く。
  `accepted` の行に1つ（`ux_control_admin_audit_accepted`）、結末の行（`applied` / `superseded`）に
  合わせて1つ（`ux_control_admin_audit_outcome`。1つの指令に結末は高々1つ）、
  `lease_expired` に1つ（`ux_control_admin_audit_lease`。結末とは別の一意制約）

**(b) `run_id`**: `coldaisle-fand` の**起動ごと**の識別子で、`secrets.token_hex(16)` による
32桁の16進の乱数（`src/coldaisle/control_admin/runtime.py` の `new_run_id()`）。
個体識別子を含まない。表の `CHECK` でも32桁の小文字16進に閉じる。`command_id` は起動ごとに
1 から振り直すので、一意性は `run_id` との組で持つ。

**(c) `ControlTick` v12 と `mode_command`**: v12 に `mode_command`（`ModeCommandRecord` schema version 1）を
**必須**で加えた（`src/coldaisle/control/schema.py`）。保存済みの v1〜v11 は欄なしのまま読める。

- 欄: `entry`（`none` / `control_admin`）・`run_id`・`command_id`・`manual_lease_expired_command_id`・
  `admin_receiver_dead`
- **受付スレッドの死（0072 §2.2）は真偽値 `admin_receiver_dead` で表す。** 死んだ後は再起動まで毎 tick
  真のままで、`command_id` と `manual_lease_expired_command_id` を持たない（検証で拒否する）
- 死を検知したときは `src/coldaisle/control/operating_mode.py` が `MANUAL` を解除してモードを `MAX` にし、
  `reason: admin_receiver_dead` を付けた error の構造化ログを1回出す

### 2.3 0072 §5 #4: `config/control-admin.yaml` の値は暫定値として受け入れる

段階 1 の実装が置いた次の値を、所有者は**暫定値**として受け入れた。運用後に見直す。

| 設定 | 値 |
|---|---|
| `limits.read_timeout_s` | 0.5 |
| `limits.max_connections` | 8 |
| `limits.max_pending_commands` | 8 |
| `limits.audit_queue_max` | 64 |
| `apply_ack_timeout_ms` | 3000 |
| `manual.max_lease_s` | 14400（`status: provisional`、`basis` 付き） |

- `read_timeout_s * 1000 <= tick_ms` と `apply_ack_timeout_ms >= tick_ms + tick_deadline_ms` の照合
  （0072 §2.8）は起動時に行う。値を変えるときもこの照合が先に効く
- `config/control-admin.dev.yaml` も同じ値を持つ（違いは §2.6 の2項目だけ）
- `limits.max_message_bytes`（4096）は 0072 §5 #4 の列挙に無く、0045 と同じ意味の値として置いた

### 2.4 応答の `superseded_by` を 0072 §2.3 の表に加える

0072 §2.3 の受理の応答は `{"ok": true, "command_id", "applied", "applied_tick_id"}` だけを定めていた。
実装は、冷却を弱めうる指令が監査の受付の行を書けた時点で、モードの軸にそれより大きい `command_id` が
すでに置かれていたとき（0072 §2.2 / §2.7 の `superseded`）、次を返す。

```json
{"ok": true, "command_id": 5, "applied": false, "applied_tick_id": null, "superseded_by": 6}
```

この欄の追加を受け入れる（`src/coldaisle/control_admin/server.py` の `_superseded_body`）。
`applied: false` だけでは「確認待ちで時間切れ（`pending`）」と「後の指令に置き換えられた」を
クライアントが区別できないためである。値は監査の表の `superseded_by` と同じ `command_id` を指す。

### 2.5 受付スレッドの死による `MAX` は、Critical Safety では既存の `manual_max` として現れる

受付スレッドの死による `MAX` は、loop へは `OperatingMode.MAX` として渡る（§2.2 (c)）。
Critical Safety（`src/coldaisle/control/safety/critical.py`）では、人が `set_mode(max)` を送ったときと
同じ `manual_max` の `forced_max` として現れる。

- **Safety の理由の code を別に作らない。Critical Safety は変えない**（所有者の選択）
- 人の `MAX` と受付スレッドの死による `MAX` は、decision trace の `mode_command.admin_receiver_dead` と
  構造化ログ（`reason: admin_receiver_dead`）だけで区別する
- Critical Safety は `coldaisle.control_admin` を知らないまま（0072 §2.4）で、入口の事情を Safety の
  語彙に持ち込まない

### 2.6 出荷する `config/control-admin.yaml` は同じ uid を認めない。開発は `.dev.yaml` を明示する

- `config/control-admin.yaml`（`coldaisle-fand --admin-config` の既定）は
  `authorization.allow_same_user: false`、`socket.group: coldaisle-admin` とする
- **`coldaisle-admin` は仮の名前**で、実環境のグループ名ではない（AGENTS.md ルール 10）。
  実際のグループ名は 0072 §5 #3（0060 の未決の系列、#57）で決める。グループを解決できなければ
  管理ソケットは開かず、`coldaisle-fand` は `AUTO` で運転を続ける（0072 §2.8）
- 開発で同じ uid から操作するときは `--admin-config config/control-admin.dev.yaml` を**明示**する。
  このファイルは `socket.group: null` と `allow_same_user: true` の組だけが本番用と異なる（0072 §2.5）

### 2.7 `accept()` が続けて `OSError` を返すときの、上限付きの backoff

いまの受付スレッド（`src/coldaisle/control_admin/server.py` の `_accept`）は、`EMFILE` / `ENFILE` /
`ECONNABORTED` などの `OSError` を受けると warning を出してその回の accept を打ち切り、受付を続ける
（スレッドは死なない）。ただし原因が続く（fd の枯渇など）と、待ち受けのソケットが読み取り可能のまま
`select` が即座に戻り、同じ失敗とログを繰り返す。

所有者は次を決めた。

- 持続する `accept()` の `OSError` には、**上限のある backoff** を入れる（次の accept までの待ちを
  伸ばし、上限で頭打ちにする。ログも抑える）
- **backoff は `MAX` を強制しない。** accept の失敗は受付スレッドの死ではなく、`admin_receiver_dead` にも
  しない。loop と Critical Safety はこの間も通常どおり動き、Fan の Demand はこの失敗で変わらない
- backoff の間も、既に受け付けた接続の処理（応答・確認待ち・監査の完了）は止めない
- 実装は #74 の別の fix PR で行う（本記録の時点では `main` に入っていない）。待ちの上限などの値は、
  AGENTS.md ルール 9 に従いその PR で設定に置くかを決める

### 2.8 0071 §5 #1: 画面の「古い」の倍数は `stale_after_tick_periods: 3.0` で確定する

0071 §5 #1 が #106 の実装 PR に委ねた、画面が decision trace を「古い」と言う倍数は、
`config/airflow-ui.yaml` の `control_trace.stale_after_tick_periods: 3.0` で入った（PR #185。
`src/coldaisle/api/airflow.py` が `> 1.0` の有限値として検証し、`GET /api/v1/airflow/config` で返す）。
所有者はこの値を**確定**とした。

- 設定ファイルにはまだ `provisional: true` と「仮の値。実運用で見直す」の注記が残っている。
  これを確定の表記へ直すのは設定の変更なので、本記録の PR には含めない（別の PR で行う）

## 3. Consequences

### 良くなること

- 0073 を読んで `air_balance` を v10 に探す読み違いを、索引から本記録へたどって防げる
- 0072 §5 の #4 / #6 と 0071 §5 #1 が、どこで何に決まったかを記録から引ける
- 受付スレッドの死の区別の仕方（Safety の code ではなく trace とログ）が明文になり、
  Critical Safety へ入口の語彙を足す変更を後から「漏れ」と誤解して入れることを防げる

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 0073 の本文だけを読むと、まだ「v10」と読める | 追記のみの規則で本文は直せない。README の索引の本記録の行に 0073 の版の読み替えを書き、`schema.py` の版の履歴にも同じ注記がある |
| Safety の理由の code だけを見ると、人の `MAX` と受付スレッドの死による `MAX` が区別できない | trace の `mode_command.admin_receiver_dead` と error のログで区別する。エアフロー画面（`src/coldaisle/web/airflow-trace.js`）も `admin_receiver_dead` を読む |
| backoff の間は新しい管理操作（`set_mode(max)` を含む）の受付が遅れる | 上限を置く。受付が遅れても loop と Critical Safety は動き続け、自動の安全側（温度・tach・telemetry loss など）は入口に依存しない |
| §2.3 の値は運用の実績のない暫定値 | `manual.max_lease_s` は `status: provisional` を持つ。運用後に所有者が見直す |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| 0073 の本文の「v10」を「v11」に直す | README の「追記のみ」に反する。許される追記は `Superseded by` だけで、本記録は 0073 を置き換えない |
| 0073 に `Superseded by: 0076` を付ける | 0073 の決定は何も置き換わっていない（番号の繰り上げは 0073 §5 自身が許していた）。付けると 0073 が失効したと誤読される |
| 受付スレッドの死に Critical Safety の理由の code（例: `admin_receiver_dead_max`）を足す | Critical Safety が入口の事情を知ることになり、0072 §2.4 の「Safety は control_admin を知らない」を崩す。区別は trace とログで足りる（所有者の選択） |
| 持続する `accept()` の失敗で `MAX` に倒す（受付スレッドの死と同じ扱い） | 入口の資源不足で Fan を全開にし、再起動まで戻せなくなる。受付スレッドは生きており、失敗は回復しうる |
| backoff を入れず、いまの「打ち切って次の select で再試行」のまま | 原因が続くと受付スレッドが空回りし、ログが溢れる |
| 同じ uid を既定で認める（開発の手間を減らす） | 同じ uid で動く別のサービス（API / AI 層）が Fan を動かせる。0072 §2.5 の「同じ uid も root も暗黙には認めない」に反する |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | §2.3 の暫定値の確定 | 運用後に所有者（0072 §5 #4 のまま） |
| 2 | 管理グループの実際の名前（`coldaisle-admin` は仮） | 0072 §5 #3 / 0060 の未決の系列 / #57 |
| 3 | §2.7 の backoff の初期値・上限・ログの抑え方、それを設定に置くか | #74 の fix PR |
| 4 | `config/airflow-ui.yaml` の `stale_after_tick_periods` の `provisional: true` と注記を確定の表記へ直す | 別の PR（#106 の続き） |
