# 決定記録 0045: 書き込み専用のローカル Unix ソケット入口（GPU Mode イベント / Workload Hint）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-18、リポジトリ所有者が承認）
- **Date**: 2026-09-18
- **Superseded by**: [0066](0066-workload-hint-stage-b-conditions.md)（§2.5 の `events` の表の列の定義のみ。boot id と `CLOCK_BOOTTIME` の値の列を足す。他の節は有効）
- **Supersedes**: [0002](0002-metric-naming.md) §2.6「用語の区別」の GPU Mode 行のうち
  「coldaisle が知る必要: なし」と、[0006](0006-gpu-mode-and-mixed-state.md) §2.1 の
  「coldaisle のデータモデルには現れない」の一文のみ（§2.8）。両記録の他の節は有効
- **関連**: [0002](0002-metric-naming.md) §2.6 /
  [0006](0006-gpu-mode-and-mixed-state.md) /
  [0009](0009-read-api.md) §3（GET-only の固定） /
  [0015](0015-llm-tools.md) / [0018](0018-tool-exposure.md) /
  [0027](0027-fan-control-architecture.md) / [0028](0028-fan-control-contracts.md) /
  [0040](0040-server-health-api.md)（`gpu.mode` の読み出し） /
  [0041](0041-supervisor-proposal-freshness.md) /
  AGENTS.md ルール1 / 2 / 9
- **対象 Issue**: #67（関連: #107 は入口の設計だけを共有する）

---

## 1. Context

#67 は Workspace の GPU Manager が AI Mode / Compute Mode を切り替えた時刻を
coldaisle に残し、温度・電力の時系列と重ねたいという要求である。
#107 は学習ジョブ等から Workload Hint を受け取り、Supervisor の prior にしたいという要求である。
**どちらも外から coldaisle へ書き込む入口を必要とする。**

一方で、読み取り API は書き込み系を1つも持たないことを OpenAPI とテストで固定している
（決定記録 0009 §3 / FR-307）。Workspace から状態を変えられないことが、2つのリポジトリを
分けている前提である。AI 層は読み取り専用ツールしか持たない（AGENTS.md ルール1）。

所有者は 2026-09-18 に、この入口を**読み取り API とは別の、ローカル Unix ドメインソケットの
書き込み専用入口**とすることをセッション内で直接決定した。本記録はその契約を定める。

---

## 2. Decision

### 2.1 入口は読み取り API と別プロセスの Unix ソケット（`coldaisle-eventd`）

- 書き込みの入口は **Unix ドメインソケット1本だけ**。TCP / UDP / HTTP では待ち受けない
- 読み取り API（`coldaisle.api` / `coldaisle.server`）には **POST 等を足さない**。
  OpenAPI が `get` だけであるという試験（0009 §3）はそのまま残す
- 待ち受けるのは専用の常駐プロセス `coldaisle-eventd`（`coldaisle.event_entry.server`）。
  取り込みデーモン（`coldaisle-daemon`）には組み込まない。理由:
  - 取り込みデーモンは `--speed` の圧縮再生や `replay` で **SimulatedClock** で動く。
    イベントの時刻はホストの実時刻で確定する必要があり（§2.5）、時計が混ざる
  - 取り込みデーモンはシリアルポートを持つ唯一のプロセスで（ルール6）、止まる理由を増やさない。
    ソケットの読み取りで詰まっても取り込みは止まらない
  - ソケットの所有者・グループ・権限を、取り込みとは別の systemd unit で絞れる
  - 書き手が増えるが、SQLite は WAL + `busy_timeout` で複数の書き手を既に扱っている
    （telemetry daemon / rollup / report と同じ）
- 人とスクリプトのためのクライアントは `coldaisle-event`（`coldaisle.event_entry.client`）。
  例: `coldaisle-event gpu-mode ai` / `coldaisle-event gpu-mode compute`

### 2.2 ソケットの場所・権限は設定から（`config/event-entry.yaml`）

| 設定 | 既定 | 規則 |
|---|---|---|
| `socket.path` | `var/run/coldaisle-events.sock` | 相対パスは作業ディレクトリ基準。本番は systemd の `RuntimeDirectory` 配下を設定する |
| `socket.mode` | `0660` | **other のビットを1つでも含む値は起動時に拒否する**（world-writable にしない）。setuid / setgid / sticky も拒否 |
| `socket.group` | `null` | 専用グループ名。設定すればソケットをそのグループへ `chown` する。解決できなければ起動しない |
| `authorization.allow_same_user` | `true` | サーバと同じ uid の接続を認める |
| `limits.max_message_bytes` | `4096` | 1行の上限。超えたら拒否して切断する |
| `limits.read_timeout_s` | `2.0` | 1接続が1行を送り切るまでの上限 |

本番の想定は「`coldaisle-eventd` を専用ユーザーで動かし、ソケットを `0660` +
専用グループにし、書いてよい利用者（Workspace の GPU Manager を動かすユーザー）だけを
そのグループへ入れる」である。**読み取り API / AI サーバを動かすユーザーはグループに入れない。**

起動時の検査（どれかに当たれば起動しない。fail closed）:

- 親ディレクトリの **other が書ける**（他人がソケットを差し替えられる）。
  親ディレクトリが無ければ `0750` で作る（other からは辿れない）
- パスに既にソケット以外のものがある（**消さない**。設定の誤りで任意のファイルを消さないため）
- 既にソケットがあり、接続できる（別のサーバが動いている）
- パスが Unix ソケットの長さ上限を超える

ソケットは `umask` を絞った状態で作り、その後 `chmod` / `chown` する。
作った瞬間に広い権限で開いている時間を作らない。

### 2.3 接続ごとの認可は peer credential（`SO_PEERCRED`）

ファイルの権限（§2.2）が第一の門、**接続相手の uid の確認が第二の門**である。
権限の設定を誤っても、認可されていない利用者の書き込みは受理しない。

- 接続を受けたら `SO_PEERCRED` で相手の uid を取る
- 認める条件: (a) `allow_same_user` が真でサーバと同じ uid、または
  (b) `socket.group` が設定されていて、相手の uid の利用者がそのグループの
  メンバー（主グループまたは補助グループ）である
- 認可の判定は**接続のたびに**行う（グループから外した利用者はすぐ書けなくなる）
- `SO_PEERCRED` が無いプラットフォームでは**起動しない**。本番は Linux であり、
  確かめられない認可で受理するより止まるほうを選ぶ
- root を暗黙に認めない。root から書くなら上の条件を満たすこと
- 受理・拒否はどちらも uid 付きで JSON Lines に記録する。受理したイベントには
  相手の uid を `peer_uid` として保存する（誰が書いたかを後から辿れるように）

**このソケットの認可は「別の利用者」から守るもので、同じ利用者からは守らない。**
同じ uid のプロセスは SQLite のファイルを直接書けるため、ソケットで拒否しても意味が無い。
AI 層を守るのは §2.7 の構造と、AI サーバを別ユーザーで動かす配置である。

### 2.4 メッセージは版付きの JSON Lines、種類はホワイトリスト

1接続につき**1行の要求と1行の応答**。UTF-8 の JSON object を `\n` で終える。

```json
{"v": 1, "type": "gpu_mode", "mode": "compute", "source": "workspace-gpu-manager", "note": "kaggle run"}
```

| フィールド | 型 | 規則 |
|---|---|---|
| `v` | int | **`1` のみ**。版を上げるときは新しい決定記録で定める |
| `type` | str | **`gpu_mode` のみ受理する**（#67）。`workload_hint` は #107 のために予約し、実装するまで拒否する |
| `mode` | str | `gpu_mode` のとき必須。`ai` / `compute`（0006 §2.1 の2段階） |
| `source` | str | 任意。書き手の名前。`[a-z0-9][a-z0-9._-]{0,63}` |
| `note` | str | 任意。人間向けの補足。200文字以内、制御文字を含まない |

- **未知のフィールドは拒否する**（`extra = forbid`）。`ts` / 時刻は受け付けない（§2.5）
- 型を暗黙に変換しない（`"1"` を `1` にしない。strict）
- 空行・JSON でない行・object でない値・上限超過・タイムアウトは拒否する
- 応答: 受理は `{"ok": true, "event_id": <int>, "ts_ms": <int>}`、
  拒否は `{"ok": false, "error": "<理由>"}`。拒否の理由に入力をそのまま反射しない

### 2.5 保存は `events` 表への追記のみ

```sql
CREATE TABLE events (
    id        INTEGER PRIMARY KEY,
    ts_ms     INTEGER NOT NULL,   -- ホスト受信時刻 (Unix ms, UTC)。書き手は時刻を渡せない
    kind      TEXT    NOT NULL,   -- 'gpu_mode'
    payload   TEXT    NOT NULL,   -- 検証済みメッセージの JSON（v / type を含む）
    peer_uid  INTEGER,            -- 書き込んだ接続の uid（SO_PEERCRED）
    CHECK (ts_ms >= 0),
    CHECK (kind GLOB '[a-z]*' AND kind NOT GLOB '*[^a-z0-9_]*'),
    CHECK (json_valid(payload)),
    CHECK (json_type(payload) = 'object')
);

CREATE INDEX ix_events_ts ON events (ts_ms);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;
```

- **時刻はサーバが確定する**（実時計のホスト受信時刻。決定 D-05 と同じ）。
  書き手に時刻を渡させると、過去を書き換えたのと同じ見え方の記録が作れてしまう
- **更新・削除はトリガで拒否する。** 追記のみを規約ではなく制約として持つ
- 保持は無期限（`system_state` と同じく、行数は切り替えの回数しかない）
- `kind` の許可リストは DB の CHECK に入れない。#107 で種類を足すたびに表の作り直しが
  要るため。許可リストはコード（§2.4）で持ち、DB は形だけを縛る
- `gpu_mode` を受理したら、**同じトランザクションで** `system_state` の `sys.gpu_mode` を
  変化時だけ書く（0002 §2.6 の規則）。Server Health の `gpu.mode`（0040）はこれを読む
- 同じ mode の通知が続いてもイベントは毎回残す（通知が届いた事実も記録）。
  `sys.gpu_mode` は変化時だけ

`PROBE_CHANGED` やデバイスの再起動など既存の事象（決定記録 0007 §2.4）も、将来この表の
`kind` として統合できる形にしてある。統合は本記録の範囲外とする。

### 2.6 この入口から Fan Demand・設定へ至る経路を作らない

- `coldaisle.event_entry` は `coldaisle.control` / `config` の書き込み・
  `coldaisle.calibrate` / `coldaisle.memory` を import しない（試験で固定）
- `sys.gpu_mode` と `events` は**説明と注釈のための記録**であり、閾値の選択にも制御にも使わない。
  閾値の選択は引き続き観測値の `sys.gpu_state` による（0002 §2.6 / 0006 §2.2）
- #107 の Workload Hint を受理するようになっても、ヒントは Supervisor の文脈にだけ入る
  （#107 の原則）。Fan Demand / Reactive Guard / Critical Safety へ直接の経路を作らない
  （AGENTS.md ルール2、0028）

### 2.7 AI 層はこの入口へ構造的に到達できない

- `coldaisle.ai` 配下・`coldaisle.api` 配下・`coldaisle.server` は `coldaisle.event_entry` を
  **import しない**。AST で走査する試験と、`coldaisle.server` を import しても
  `coldaisle.event_entry` が読み込まれないことを確かめる試験で固定する
- AI 向けツール（0015 / 0018）に書き込みやイベント送信のツールを足さない
  （既存の「定義は読み取りの5つだけ」の試験がそのまま効く）
- 記録されたイベントの**読み出し**は読み取り API の `GET /api/v1/events` で行う。
  これは他の GET と同じく読み取り専用で、AI 層から読めても書き込みには繋がらない

### 2.8 GPU Mode を coldaisle のデータに持つ（0002 §2.6 / 0006 §2.1 の一部を置き換え）

0002 §2.6 の「用語の区別」は GPU Mode について「coldaisle が知る必要: なし」とし、
0006 §2.1 は「coldaisle のデータモデルには現れない」としていた。当時の理由は
**閾値の選択に指令値を使わない**ことであり、その理由は変わらない。

変わったのは、#67 が**温度変化の原因を後から説明するために指令値の履歴を要る**と
したことである（要件 §5.1 の `sys.gpu_mode`、0040 の `gpu.mode` も既に前提にしている）。
したがって本記録は次の範囲だけを置き換える。

- GPU Mode（指令値）を `events` と `system_state.sys.gpu_mode` に**記録する**
- **閾値の選択・制御には使わない**（0002 §2.6 の区別の趣旨はそのまま）

### 2.9 #107 の時期は据え置き

#107 と共有するのは**入口の方式（本記録の §2.1〜§2.4、§2.6〜§2.7）だけ**である。
Workload Hint のメッセージ形（`type: workload_hint` の中身）、Supervisor への渡し方、
実装する時期は #107 の判断（Telemetry だけの Regime 推定で不足が見えてから）に従い、
本記録では決めない。それまで `workload_hint` は未知の種類として拒否する。

---

## 3. Consequences

### 良くなること

- 読み取り API の GET-only が保たれる。Workspace の既存連携は何も変わらない
- Mode 遷移が時系列に残り、`GET /api/v1/events` とダッシュボードの縦線で
  温度・電力と重ねて見られる
- 書き込みの経路が1本・1プロセス・1ソケットに限られ、権限・認可・検証が1箇所に集まる
- 誰がいつ書いたか（`peer_uid` / `ts_ms`）が残り、後から改変できない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 常駐プロセスが1つ増える | 小さく、SQLite に1行書くだけ。止まっても取り込み・制御・API に影響しない |
| Linux 以外では書き込みの入口が起動しない | 本番は Linux。確かめられない認可で受理しない（§2.3）。読み取り側は影響なし |
| 同じ uid のプロセスは守れない | DB ファイル自体を同じ uid が書けるため、ソケットでは守れない。AI サーバを別ユーザーで動かす（§2.3） |
| `events` を消せない | 行数は切り替えの回数しかない。消す必要が出たら新しい決定記録でトリガを外す |
| 書き手が時刻を指定できない | Workspace の切り替え時刻との差は通知の遅延ぶんだけ。遅延を持ち込むより改ざんできないほうを採る |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 読み取り API に `POST /api/v1/events` を足す | GET-only の契約（0009 §3 / FR-307）を破る。AI サーバと同じプロセスに書き込み経路ができる |
| v2 HTTP API として書き込み契約を導入する | TCP で待ち受けることになり、認可を HTTP の層で作り直す必要がある。同じプロセスに読み取りと書き込みが同居し、ツール窓口（`coldaisle.server`）から近くなる |
| localhost TCP の書き込み専用ポート | 同じホストの全利用者が接続でき、相手の uid が分からない。ファイル権限という門も持てない |
| ファイル（spool ディレクトリや YAML）を書かせる | 書き手の認可が「ディレクトリに書けるか」だけになり、検証・応答を返せない。取り込み側の監視・消し込み・部分書き込みの扱いが要る。#107 の候補でもあったが同じ理由で採らない |
| 取り込みデーモンにソケットを持たせる | 時計が SimulatedClock になりうる。シリアルを持つ唯一のプロセスに停止理由を足す（§2.1） |
| 書き手が `ts` を指定できるようにする | 過去の時刻で記録を作れてしまう（§2.5） |
| `kind` の許可リストを DB の CHECK に入れる | 種類を足すたびに表の作り直しが要る（§2.5） |
| `mixed` 検出の警告も本入口で扱う | `sys.gpu_state` は観測値で、書き込みの入口とは無関係。ルールエンジン側の話（0006 §2.4） |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | Workload Hint のメッセージ形・Supervisor への渡し方・実装時期 | #107 |
| 2 | `PROBE_CHANGED` / デバイス再起動などの既存事象を `events` へ統合するか | 別 Issue |
| 3 | `sys.gpu_state = mixed` の警告（`rule_id`・継続時間） | #67 の残り / #18（0006 §5） |
| 4 | 本番の systemd unit（専用ユーザー・グループ名・`RuntimeDirectory`） | 実機導入時 |
