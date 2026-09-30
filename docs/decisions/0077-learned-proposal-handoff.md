# 決定記録 0077: Learned MPC / Supervisor worker から制御ループへの提案の受け渡し（プロセスの置き場所・経路・書き手・snapshot への束縛・registry の版の記録・worker の異常時の扱い）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-30
- **Supersedes**: なし
- **関連**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md)（§2.2 / §2.3 / §2.5 (c) / §2.6） /
  [`0041-supervisor-proposal-freshness.md`](0041-supervisor-proposal-freshness.md) /
  [`0050-model-confidence-ood-and-authority.md`](0050-model-confidence-ood-and-authority.md)（§5 #5） /
  [`0052-learned-mpc-optimizer-and-hard-constraints.md`](0052-learned-mpc-optimizer-and-hard-constraints.md) /
  [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md) /
  [`0059-decision-trace-model-artifact.md`](0059-decision-trace-model-artifact.md) /
  [`0060-control-loop-runtime.md`](0060-control-loop-runtime.md)（§2.5 / §2.6 / §2.7、§5 #5） /
  [`0061-rl-supervisor-policy-artifact-and-binding.md`](0061-rl-supervisor-policy-artifact-and-binding.md)（§2.4 / §5） /
  [`0062-model-registry-operations.md`](0062-model-registry-operations.md)（§2.4） /
  [`0071-control-trace-read-api.md`](0071-control-trace-read-api.md)（§2.5 / §5 #7） /
  [`0072-control-admin-entry.md`](0072-control-admin-entry.md)（§2.2 / §2.5） /
  [`0074-supervisor-shadow-wiring-and-episode-evaluation.md`](0074-supervisor-shadow-wiring-and-episode-evaluation.md)（§5） /
  `AGENTS.md`「絶対に守るルール」1〜7・9
- **対象 Issue**: #86（Learned MPC の runtime）/ #104（runtime の部分: `coldaisle-fand` の registry 束縛）/ #89（後続: RL worker）

---

## 1. Context

0028 §2.2（FINAL）は「Learned MPC / RL Supervisor は**制御ループの外の worker プロセス**で動かし、
worker は提案を置き、ループは最新の提案を読むだけにする」と決めた。ループ側の**受け口**は main に
すでにある。

| いまあるもの | 場所 | 状態 |
|---|---|---|
| `LearnedProposalSource.poll() -> MpcProposal \| None` | `control/loop.py` | Protocol だけ。実装が無い |
| `SupervisorOutputSource.poll() -> SupervisorOutput \| None` | `control/loop.py` | Protocol だけ。識別（`SupervisorPolicyIdentity`）を運べない |
| 初めて見た結果にだけ受信時刻を押す・`result_digest()` で新旧を見分ける | `ControlLoop._poll_learned` | 実装済み（0060 §2.6） |
| この process が出した snapshot にだけ結果を結び付ける（`(tick_id, ts_ms, schema)`） | `ControlLoop._issued_snapshot` | 実装済み（0060 §2.6 / §2.7） |
| 受信から `mpc.valid_ms` で期限切れ・締め切り超過の tick は ML を通さない | `ControllerGate` / `ControlLoop._select` | 実装済み（0028 §2.6 / 0060 §2.6） |
| `expected_model_version` / `expected_artifact_sha256` との照合 | `ControllerGate` | 実装済み（0059 §2.1）。`coldaisle-fand` は `unconfigured` / `None` を渡している |
| `RegistryProvenance`（`ControlTick` v10 の `registry`） | `control/schema.py` | `coldaisle-fand` は**常に `unbound()`** を渡している |
| `LearnedMpcController.propose(snapshot, observed, supervisor, baseline, safety_floor, residual)` | `control/mpc/controller.py` | 実装済み。例外を外へ出さず構造化した失敗へ翻訳する |

0060 §5 #5 は「Learned MPC / RL Supervisor の worker プロセスと、提案を置く経路の実装」を
**#86 / #89 で決める**として残し、0050 §5 #5・0061 §5・0074 §5 も同じ穴を指している。
決まっていないのは次の6点である。

1. worker をどこで動かすか（別 unit か、`coldaisle-fand` の子か、スレッドか）
2. 何で運ぶか（socket・file・共有メモリ・SQLite）
3. **worker は何を入力にするか。** `propose()` は snapshot だけでなく、同じ tick の
   Fallback の提案・選ばれた Supervisor 出力・Critical Safety の floor・観測 window を要る。
   worker が自分で Telemetry を読むと「MPC だけが別時刻の Raw Telemetry を再取得しない」（#86）と
   「1 tick の snapshot は1つだけ」（0060 §2.5）が崩れる
4. 誰が書けるか（認可）
5. registry のどの版で判断したかを trace へどう載せるか（`RegistryProvenance` が常に unbound）
6. worker が遅い・死ぬ・壊れた出力を出すときにどう倒れるか

これが決まらないと、#86 の runtime（worker 実装と配線）、#104 の runtime 部分
（`coldaisle-fand` が registry を読んで Gate と trace に束縛する）、#89 の RL worker のいずれも
実装に入れない。安全系・制御系の設計であり、**実装より先に**所有者の承認を要する（AGENTS.md「迷ったら」）。

---

## 2. Decision

以下は**推奨案**である。所有者が選ぶべき分岐は §4 の代替案と §5 に並べた。

### 2.1 置き場所：役割ごとの別プロセス（別 systemd unit）。`coldaisle-fand` の子にしない

| プロセス | 役割 | 書けるもの |
|---|---|---|
| `coldaisle-fand` | 1 tick の Pipeline・合成・hwmon への書き込み（0028 §2.2 のまま） | hwmon（Hardware Backend だけ） |
| Learned MPC worker（仮称 `coldaisle-learnd --role mpc`） | 推論・Confidence / OOD の判定・optimizer | **`coldaisle-fand` への提案メッセージだけ** |
| RL Supervisor worker（仮称 `coldaisle-learnd --role supervisor`。#89） | RL policy の推論 | **`coldaisle-fand` への Supervisor 出力メッセージだけ** |

- worker は **systemd の別 unit** として、`coldaisle-fand` とは**別の、権限の低いユーザー**で動かす。
  hwmon・シリアル・NVML・管理ソケット・`authority.json` へ書く権限を持たせない
  （AGENTS.md ルール2・6、0001 D-07）。`coldaisle-fand` が worker を起動・監視しない
- `coldaisle-fand` は worker に**依存しない**。worker が無い・起動していない・落ちた構成でも、
  いまと同じく Fallback / RulePolicy で運転する（0028 §2.2「worker が落ちても、止まっても、
  ループは止まらない」、AGENTS.md ルール4）。GPU AI Service にも依存させない（AGENTS.md
  「制御アーキテクチャの固定前提」）
- 役割ごとに unit を分ける。MPC と RL の周期（`mpc.period_ms` / `supervisor.period_ms`）も
  失敗の仕方も違い、片方の異常（メモリの枯渇・無限ループ）でもう片方を止めない
- **RulePolicy と Supervisor の選択（`SupervisorCoordinator`）は `coldaisle-fand` の中に残す。**
  RL worker が出すのは候補の `SupervisorOutput` までで、Rule / RL のどちらを使うかは
  いまと同じくループ内の決定論的な Coordinator が決める（0041 / 0061 §2.4）。MPC worker が
  受け取るのは、その tick に**選ばれた** Supervisor 出力である（§2.3）

### 2.2 経路：`coldaisle-fand` が持つ Unix ドメインソケット（`SOCK_SEQPACKET`）と、受付スレッド＋1枠の受け渡し口

0072 §2.2 の管理ソケットと**同じ形**（別スレッドが I/O を持ち、ループは受け渡し口を非ブロッキングで
覗くだけ）にする。ソケットは管理ソケットとも `coldaisle-eventd`（0045）とも**別**にする。

- **待ち受けは `coldaisle-fand`、接続するのは worker。** `SOCK_SEQPACKET` を使い、
  メッセージの境界と「相手が死んだ（EOF）」を OS が教える形にする
- 受付スレッドが受信・長さの上限の確認・JSON の解釈・pydantic の検証（`MpcProposal` /
  §2.8 の封筒）までを行い、**検証を通った最新の1件だけ**を役割ごとの受け渡し口に置く
  （新しいものが古いものを置き換える。積まない）
- `LearnedProposalSource.poll()` / `SupervisorOutputSource.poll()` の実装は、受け渡し口を
  **lock を試すだけ**（取れなければ前回の値）で覗く。**ループは待たない**（0060 §2.3 と同じ規則）。
  重い検証をループの tick に入れないのは、`poll()` が Critical Safety の評価より前にあるからである
- 出す向き（`coldaisle-fand` → worker）も同じスレッドが送る。ループは tick の最後
  （**heartbeat の後**。0060 §2.7 の境目の後）に、その tick の入力（§2.3 の frame）を
  送り出し用の1枠へ置くだけにする。送れない（未接続・送信バッファが一杯）ときは**捨てる**。
  再送も待ちもしない
- 実装の置き場所は `coldaisle.learned_channel`（仮称。`coldaisle.control_admin` と同じ「合成の起点」の
  位置）。`coldaisle.control` 配下はこれを import しない（AGENTS.md コード規約の一方向の依存）。
  ループが知るのは `LearnedProposalSource` / `SupervisorOutputSource` と、新しく足す
  **送り出し用の Protocol**（仮称 `LearnedFrameSink.offer(frame) -> None`。待たない・失敗は例外で返してよい）だけである

**受付スレッドが死んだら、Learned の経路だけを閉じる（Max にはしない）。** ループは毎 tick、
受け渡し口を覗く前に受付スレッドの生存を lock なしで確かめ、死んでいたら
`coldaisle-fand` の再起動まで**提案を1件も読まない**（Gate は `learned_proposal_unavailable` で
Fallback、Supervisor は RulePolicy）。0072 §2.2 が受付スレッドの死で Max にするのは、人が
冷却を強める手段（`set_mode(max)`）を失うからであり、この経路はそうではない。
Learned の経路を失っても、残るのは設計どおりの Baseline（Fallback + Reactive Guard + Critical Safety）である。

### 2.3 worker の入力：`coldaisle-fand` が毎 tick 送る frame。**worker は Telemetry を読まない**

worker は SQLite も API も読まず、`coldaisle-fand` が送る **frame の列だけ**を入力にする。
frame は tick ごとに1つ、その tick の**同じ snapshot**から作る。

| frame の欄 | 出どころ（その tick の値） | 用途 |
|---|---|---|
| `run_id` | `coldaisle-fand` の起動ごとの乱数（管理ソケットの監査が使う `run_id`。`control_admin/runtime.py` の `new_run_id()` と同じ値） | 再起動をまたいだ取り違えを防ぐ（§2.4） |
| `snapshot` | その tick の `ControlStateSnapshot`（層をまたいで同じ object。0060 §2.5） | 推論の入力・観測 window の1 frame |
| `workload` | その tick の `WorkloadRegimeEstimate` | RL の入力（`SupervisorInput`） |
| `supervisor` | その tick に **選ばれた** `SupervisorOutput`（無ければ `null`） | MPC の目的関数の重み |
| `baseline` | その tick の Fallback の `ControllerProposal` | MPC の `baseline` |
| `safety_floor` | その tick の Critical Safety の zone ごとの floor | MPC の `safety_floor` |
| `applied` | その tick の effective demand（Hardware Backend へ渡した値） | 観測 window の action（`ObservedFanAction`） |
| `authority_stage` | その tick の実効 stage | `binding_authority_stage` の照合（0057 §2.2） |
| `expected_artifacts` | 起動時に読んだ registry の production の識別（§2.6） | worker が読み込む artifact の固定 |
| `config` | `ControlConfigDigest`（`runtime.config` と同じ値） | worker の設定との一致確認 |

- **観測 window は worker が frame の列から組み立てる。** `coldaisle-fand` はモデルの feature schema も
  window 長も知らない（知ると、モデルを変えるたびに制御デーモンの変更が要る）。
  **届かなかった frame（捨てられた・worker が止まっていた）を補間しない。** 欠けは欠けとして
  window に残し、Confidence / OOD（0050）と Gate が Fallback へ倒す
- worker は frame の `config` の hash が自分の読み込んだ設定と違えば、提案を作らずに
  失敗（`model_load_failure`、理由 `config_mismatch`）を返す。**設定の食い違った提案を作らない**
- MPC worker は `mpc.period_ms` ごとに、**最後に受け取った frame** に対して `propose()` を1回呼ぶ。
  optimizer の予算（`mpc.budget_ms`）は worker が持つ（#86 の実装要件。0028 §2.6 の表のまま）
- frame に含めてよいのは Telemetry の値と制御の状態だけで、path・個体識別子を入れない
  （AGENTS.md ルール10）。frame は LLM のプロンプトへ渡さない（ルール8。LLM と制御 ML を混同しない）

### 2.4 1 tick が提案を消費する規則（snapshot への束縛・新しさ・締め切り）

ループの側の規則は **0060 §2.6 / §2.7 をそのまま使い**、経路の都合で次を足す。

1. **`run_id` の一致を受付スレッドで確かめる。** worker の返信の封筒（§2.8）が運ぶ `run_id` が
   この process の値と違えば捨てる。0060 の `(tick_id, ts_ms, schema)` の照合は残す（二重にする）
2. **元 snapshot の特定**は 0060 §2.7 のまま（`ControllerProposal.seq` / `computed_at_ms` が
   この process の出した snapshot の窓にあるか）。特定できない結果は使わない
3. **受信時刻**は「ループが `poll()` で初めて見た tick の単調時刻」で、`result_digest()` で新旧を
   見分ける（0060 §2.6）。受付スレッドの受信時刻は使わない（ループの時計で数える一貫性を保つ）
4. **新しさ**は 0028 §2.6 の「受け取ってから `mpc.valid_ms`」に加えて、**元 snapshot の単調時刻から
   `mpc.max_source_age_ms`（新設。`fan-policy.yaml`、`status: provisional`）** を超えた提案も
   期限切れにする。受信起点だけだと、worker の中で長く滞留した提案が受信した瞬間に新しく見える
   （0041 が Supervisor 側で塞いだのと同じ穴）。いまは snapshot の窓の長さ
   （`max(supervisor.valid_ms, mpc.valid_ms)`）が暗黙の上限になっているだけで、Supervisor の
   期限が長いと MPC の元 snapshot が数分前でも通る。**条件を足すだけで 0028 §2.6 の
   `mpc.valid_ms` の意味は変えない**（Supersede しない）。不変条件
   `mpc.max_source_age_ms <= mpc.valid_ms` を読み込み時に確かめる（→ 所有者判断 6）
5. **締め切り**は 0060 §2.6 のまま。締め切りを過ぎた tick では ML を通さない。ループは
   worker を待たないので、worker の遅れが tick の所要時間に入る経路は無い
6. **採った提案でも出せるのは requested だけである。** Gate（authority の帯）→ 合成（Reactive Guard の
   floor / ceiling、Critical Safety の floor・`forced_max`・`ramp_down`）の順は変えない（0028 §2.4、
   AGENTS.md ルール2）。worker からの経路は `MpcProposal` / `SupervisorOutput` の型しか運べず、
   Demand の上書き・モード・authority を表す型を持たない

### 2.5 worker の異常：どれも「その tick は Fallback / RulePolicy」。ループを止めない・Max にもしない

| 起きたこと | 検出 | 扱い | trace に残るもの |
|---|---|---|---|
| worker が遅い（結果が来ない） | 受信・元 snapshot からの経過 | 期限切れで Fallback（既存） | `fallback_reason: proposal_expired` |
| worker が落ちた・接続が切れた | `SOCK_SEQPACKET` の EOF | **その tick から**受け渡し口を空にし Fallback。**識別子と受信時刻は捨てない**（0060 §2.6。再接続した worker が同じ結果を送り直しても新しい受信時刻を押さない） | `fallback_reason: learned_proposal_unavailable`（detail に `worker_disconnected`） |
| worker が固まった（接続したまま黙る） | 最後の受信から `worker_idle_timeout_ms`（新設。§2.7 の設定） | 受付スレッドが接続を閉じ、上の「落ちた」と同じ扱い | 同上（detail に `worker_idle`） |
| 壊れた出力（長さ超過・JSON 不正・型の検証失敗・別の `run_id`） | 受付スレッド | 捨てて数える。受け渡し口の値は変えない（直前の有効な結果が期限まで残るか、無ければ Fallback） | 構造化ログ（件数と理由の code）。§2.9 の選択肢 B を採れば trace にも |
| 提案の代わりに失敗を返した | `MpcProposal.failure` | Gate が Fallback（既存） | `fallback_reason: model_load_failure` / `optimizer_exception`（detail 付き） |
| 別の artifact・別の版の提案 | Gate の照合（0059 §2.1） | Fallback（既存） | `model_artifact_mismatch` / `model_version_mismatch` |
| 受付スレッドが死んだ | ループが毎 tick 確かめる | 再起動まで Learned を読まない（§2.2） | `learned_proposal_unavailable`（detail に `channel_dead`）と error ログ |
| RL worker の同様の異常 | 同上 | Coordinator が RulePolicy（既存。0041 / 0061） | `SupervisorDecision` の RL の `error` |

- **Max に倒さない理由。** Learned の経路が無いときの状態は、M8 から運転してきた Baseline そのもので、
  Critical Safety と Reactive Guard は ML と独立に毎 tick 効いている（AGENTS.md ルール3）。
  worker の不調で Max にすると、ML の失敗が騒音という形で安全系に混ざる
- 「落ちた」ときに直前の有効な提案を期限まで使わず**即 Fallback** にするのは、プロセスの死が
  直前の出力の健全さを疑わせるからである（→ 所有者判断 4）
- `LearnedFailure` に経路の失敗を表す値を足すか、既存の `learned_proposal_unavailable` の detail で
  表すかは段階 1 の実装で決める。`Reason.code` は閉じた語彙ではないので、**どちらでも
  `ControlTick` の版は上げない**（上げが要ると分かったら §2.9 の規則に従う）

### 2.6 registry の版の束縛：`coldaisle-fand` が起動時に1回だけ読み、同じ snapshot から Gate・trace・frame を作る

- `coldaisle-fand` に `--registry-root`（`--authority-root` と同じ形。#92 / PR #192）を足す。
  与えられたら、**起動時に1回だけ** `registry.json` を読み取り専用で読む（flock を取らない。
  原子置換された file を読むだけ。0062 §2.4 の起動時検証の報告を使う）
- **1つの `RegistrySnapshot` から次の3つを作る。** 別々に読むと、途中で promotion が起きたときに
  Gate と trace が別の版を名乗る
  - `RegistryProvenance`（`snapshot.trace_provenance()`）→ `ControlLoop(registry=...)`。毎 tick 同じ値
    （0071 §2.5。`unbound()` をやめる）
  - `thermal_model` の production の版と `artifact_sha256` → `ControllerGate(expected_model_version=...,
    expected_artifact_sha256=...)`。production が無ければ `None`（どの Learned 提案も採らない。0059 §2.1）
  - `supervisor_policy` の production の識別 → `SupervisorCoordinator(expected_rl_identity=...)`（0074 §5。
    文字列から作らない）
- **frame の `expected_artifacts` で worker が読む artifact を固定する。** worker は registry を自分で
  検証して読み込む（bytes の checksum・schema。0048 / 0062）が、**どれを読むかは
  `coldaisle-fand` が起動時に読んだ版に従う**。worker が独自に「いまの production」を追うと、
  promotion の直後に Gate と別の artifact で提案を作り続ける
- **registry の変更が効くのは `coldaisle-fand` の再起動から**（0071 §2.5 のまま。再起動は `STARTUP` の
  Max を通る）。走行中に rollback された（固定された artifact が production でなくなった）ことを
  worker が知ったら、worker は**提案をやめて** `model_load_failure` を返す。制御デーモンの再起動を
  待たずに Fallback へ倒れる向きにしか効かない。速く止めたいときの経路は、これまでどおり
  管理ソケットの authority の降格（0072）である
- **`--registry-root` を与えたのに読めない・壊れているときは、起動を止めずに Learned を無効にする。**
  `expected_*` を `None`、`registry` を `unbound()` にし、起動時に error を残す。
  registry の破損で Fan の制御まで手放す（終了コード 5 で BIOS に戻す）のは、Learned が
  任意の部品である以上、失うものが大きい（→ 所有者判断 7）。trace の `unbound()` は
  「読まなかった」と「読めなかった」を区別できないので、区別は起動ログで行う
  （trace で区別するなら §2.9 の選択肢 B に含める）
- `coldaisle-fand` は artifact の bytes を読まず、deserialize もしない（ML を制御プロセスへ入れない）。
  bytes の検証は worker の仕事で、ループ側の照合は Gate の導出された識別（0059 §2.1 の
  `prediction.artifact_sha256`）で行う

### 2.7 誰が書けるか：`SO_PEERCRED` で worker のグループだけ。役割ごとに1接続

0072 §2.5 の門（ファイル権限と `SO_PEERCRED`、起動時の検査、`umask`）をそのまま使う。

- 接続を認めるのは、設定で名指しした **worker 専用グループ**のメンバーだけ。**`coldaisle-fand` と
  同じ uid も root も暗黙には認めない**（0072 §2.5 と同じ理由）
- 役割（`mpc` / `supervisor`）は**接続の最初のメッセージ**で1回だけ名乗り、以後その接続では
  その役割の型しか受けない。役割ごとに**同時に1接続**だけ。既に生きた接続がある役割への
  新しい接続は拒否し、接続ごとのログに残す（固まった接続は `worker_idle_timeout_ms` で閉じるので、
  再起動した worker が長く締め出されることはない）
- 読み取り API・AI 層・`coldaisle-eventd` の実行ユーザーを worker グループに入れない。
  **LLM から到達できる経路を作らない**（AGENTS.md ルール1）。worker の package は `coldaisle.ai`・
  `coldaisle.api`・`coldaisle.control.hardware` の書き込み側・`serial` を import しない（試験で止める）
- 設定（ソケットの path・グループ・`max_message_bytes`・`worker_idle_timeout_ms`・接続数の上限）は
  新しい設定ファイル（仮称 `config/learned-channel.yaml`）に置く（AGENTS.md ルール9）。
  `safety.yaml` には置かない（安全の裁定に効く値ではない）。**設定が不正・ソケットを開けないときは
  Learned を無効にして起動を続ける**（0072 §2.8 と同じ。冷却を入口の有無に依存させない）

### 2.8 `LearnedProposalSource` / `SupervisorOutputSource` のつなぎ方

- `LearnedProposalSource` の形は**変えない**（`poll() -> MpcProposal | None`）。受付スレッドが
  封筒（`run_id` と役割）を剥がし、検証した `MpcProposal` だけを受け渡し口に置く
- `SupervisorOutputSource` は、RL の識別（`SupervisorPolicyIdentity`）を運べる形に**広げる**
  （例: `poll() -> DeliveredSupervisorOutput | None`、中身は `SupervisorOutput` と identity）。
  いまの形では `ReceivedSupervisorOutput.identity` が常に `None` になり、0074 の集計が
  `usable=False` のままになる。ループは identity を `ReceivedSupervisorOutput` へ写し、照合は
  いまどおり Coordinator が `expected_rl_identity`（§2.6）と行う
- **`origin` は `coldaisle-fand` が決め、worker に名乗らせない。** `SupervisorPolicyBinding.for_active`
  は閉じている（0061 §2.4）ので、経路から来た出力の `origin` は `shadow_binding` に固定する。
  `active_binding` を運ぶ形は、`for_active` の門を開く別の決定記録で決める（0061 §5 / 0074 §5）
- 経路の封筒の形（`schema_version`・`run_id`・`role`・本文）は版を持ち、知らない版は捨てる

### 2.9 trace の版：推奨案では `ControlTick` の版を上げない

- registry の束縛は既存の `registry` 欄（v10）、採否と理由は既存の `model_gate` / `fallback_reason` /
  `SupervisorDecision`（`policy_identity` は `SupervisorDecision` v2）で表せる。**段階 1〜4 では
  `ControlTick` の版を上げない**（選択肢 A、推奨）
- 選択肢 B として、経路の健全性（役割ごとの接続状態・捨てた件数と理由の code・`--registry-root` を
  読めなかったこと）を trace に載せる塊（仮称 `learned_channel`）を足す案がある（→ 所有者判断 9）。
  採るなら
  - **`ControlTick` の版上げは、ほかの版上げと直列にする。** いま main は v12、PR #192 が v13 に上げる。
    この塊は #192 が main に入った後の**次の空いた番号**を使う（0073 §5 の番号の繰り上げの規則。
    並行する記録・PR が先に取ったら、その次）
  - 同じ PR で `src/coldaisle/web/airflow-trace.js` の `KNOWN_VERSIONS` に新しい版を足し、
    画面の版の解釈（0071 §2.3）と試験を更新する
  - 省略可能な欄にせず、その版の tick には必ず載せる（0060 §4「版は自分の中身を表す」）

### 2.10 実装の段階

| 段階 | 担当（1段階ずつ別の PR） | 内容 | 前提 |
|---|---|---|---|
| 0 | 本記録 | 所有者の承認（§5 の判断を含む） | — |
| 1 | #86 | `coldaisle-fand` 側の経路：ソケット・受付スレッド・受け渡し口・§2.7 の認可と設定・`LearnedProposalSource` の実装・`LearnedFrameSink` と frame の送り出し（heartbeat の後）・§2.4 の `run_id` 照合・§2.5 の失敗の扱い・受付スレッドの死の扱い。**worker はまだ無い**ので、試験の偽 worker（`socketpair`）で確かめる。設定が無い起動の挙動はいまと変わらない | 段階 0 |
| 2 | #104（runtime の部分） | `--registry-root`・起動時に1回読む `RegistrySnapshot` から provenance・Gate の期待値・`expected_rl_identity` を作る（§2.6）・読めないときは Learned を無効にして起動・frame の `expected_artifacts` | 段階 1（frame に載せるため。provenance だけなら先行してよい） |
| 3 | #86 | MPC worker（`coldaisle-learnd --role mpc`）：frame の列から window を組み立てる・固定された artifact を registry で検証して読む・`propose()` を `mpc.period_ms` ごと・config の hash 照合・rollback の検知。`mpc.max_source_age_ms` を `fan-policy.yaml` に足す（schema の版上げ）。**現行の artifact はすべて `observational_replay` なので `MpcModelBinding.for_control` が拒み、`model_load_failure` で Fallback になるのが通常経路**（0052）。試験は試験用の反実仮想モデルで行う。authority は既定の `SHADOW`（0060 §2.8） | 段階 1・2 |
| 4 | #89 | RL worker（`--role supervisor`）・`SupervisorOutputSource` の形を広げる（§2.8）・`origin` を `shadow_binding` に固定。active は閉じたまま（0061 §2.4） | 段階 1・2。#89 の policy 側 |
| 5（選択肢 B を採る場合のみ） | #86 | `ControlTick` に `learned_channel` の塊を足す（§2.9）。**#192 の v13 の後に直列で**、`KNOWN_VERSIONS` と画面の解釈を同じ PR で更新 | 段階 1・2、#192 のマージ |
| 6 | #57 / 0060 未決 7 | worker の systemd unit のテンプレート（実行ユーザー・グループ・`RuntimeDirectory`・資源の上限）。**仮の値だけ**（AGENTS.md ルール10） | 段階 3 |

各段階は hardware なしで試験できる。

| 確かめること | 方法（実機不要） |
|---|---|
| ループが worker を待たない | 偽 worker が返信しない・送信バッファを埋める・受け渡し口の lock を握り続ける状態で、`SimulatedClock` / 試験用単調時計の上の tick の所要時間が変わらないこと |
| worker の死・固まり・壊れた出力・別 `run_id`・別 artifact で Fallback になる | `socketpair` の偽 worker と試験用の `MpcProposal`。§2.5 の表の各行を1試験ずつ |
| 受付スレッドの死で Learned だけが閉じ、Max にはならない | スレッドを止め、次の tick から `learned_proposal_unavailable`・`forced_max` が立たないこと |
| どの経路でも effective は Guard と Critical Safety を通る | 既存の合成の性質試験を、採った Learned 提案を含む入力で回す（requested に 0.0 を置いても floor が残る） |
| 再起動をまたいだ結果を使わない | 同じ `tick_id` / `ts_ms` を持つ前の `run_id` の結果を送る |
| 認可 | 0072 の試験と同じ（他 uid・同じ uid・root の接続を拒む。`SO_PEERCRED` を偽装した試験用の門） |
| worker が読む入力が frame だけ | worker の package の import 試験（`sqlite3`・`serial`・`coldaisle.store`・`coldaisle.ai` を import しない） |
| registry の束縛 | 一時ディレクトリの registry で、provenance・Gate の期待値・`expected_rl_identity` が同じ `revision` から来ること、壊れた registry で Learned が無効のまま起動すること |
| 再現性（#86 の受入基準） | 記録した frame の列・artifact・設定・seed から、同じ `result_digest()` が出ること |

---

## 3. Consequences

### 良くなること

- 0060 §5 #5・0050 §5 #5・0061 / 0074 §5 の「worker と経路」が1か所で決まり、#86 / #104 / #89 の
  runtime を同じ土台で実装できる
- worker の入力が `coldaisle-fand` の snapshot だけになり、「1 tick の snapshot は1つ」（0060 §2.5）と
  「MPC は Raw Telemetry を再取得しない」（#86）が経路の構造で守られる
- `RegistryProvenance` が実際の版を持つようになり、Gate の期待値と trace が同じ registry の版から来る
- worker のどの異常も Fallback / RulePolicy に落ち、Critical Safety と Reactive Guard の経路には触れない
- worker は hwmon に書く権限を持たないので、worker が乗っ取られても出せるのは authority の帯の中の
  requested までである

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| `coldaisle-fand` にスレッドが1本増える（0060 §2.7 は trace の保存のためのスレッドを却下した） | この経路は安全の経路の外にある。ループは lock を試すだけで待たず、スレッドの死は Learned を閉じるだけ（§2.2）。0060 §2.7 が嫌ったのは「安全側の経路に queue の溢れという失敗を足すこと」で、ここでは溢れたら捨てるだけで制御の結果は Fallback と同じ |
| 毎 tick の frame 送信で `coldaisle-fand` の仕事が増える | 送信は heartbeat の後で、非ブロッキング・失敗で捨てる。`duration_ms` の区間に入らない。frame の大きさと送信時間は段階 1 で測り、`max_message_bytes` で上限を掛ける |
| worker が frame を取りこぼすと window に欠けができ、Learned が使えない時間が増える | 安全側（Fallback）に倒れるだけ。欠けの頻度は段階 3 の shadow で数える |
| model の promotion を効かせるには `coldaisle-fand` の再起動（`STARTUP` の Max）が要る | 0071 §2.5 のとおり。rollback を急ぐときは worker の自主停止（§2.6）と authority の降格（0072）で先に Fallback にできる |
| registry が読めないとき、trace だけでは「読まなかった」と区別できない | 起動ログで区別する。trace で要るなら選択肢 B（§2.9） |
| 別プロセスの worker が、自分で整合させた偽の assessment を作れる | 0050 §3 / 0059 §2.1 が同一プロセスで受け入れた残余と同じ種類。worker は権限の低い別ユーザーで、効くのは authority の帯の中の requested だけ。Guard と Critical Safety は ML と独立に効く |
| 設定ファイルと unit が増える | 実行時の値はすべて設定に置き、テンプレートは 0069 と同じく仮の値だけ |

---

## 4. 却下した代替案

### 4.1 置き場所

| 案 | 利点 | 却下理由 |
|---|---|---|
| **A. 役割ごとの別 systemd unit（推奨）** | 権限を分けられる・片方の異常で他方を止めない | — |
| B. MPC と RL を1つの worker プロセスに同居 | unit が1つで済む・frame の受信が1回 | 片方のメモリ枯渇・hang で両方止まる。周期が6倍違い、スケジューリングが混ざる。**所有者が unit 数を嫌うなら採れる**（安全上の差は無い） |
| C. `coldaisle-fand` が子プロセスとして起動（`multiprocessing` など） | 配備が単純・寿命が揃う | hwmon に書ける権限を継承する。落とすには権限を下げる処理が要り、失敗の種類が増える。`coldaisle-fand` の再起動のたびに worker も作り直され、モデルの読み込みが `STARTUP` に重なる |
| D. `coldaisle-fand` の中のスレッド | IPC が要らない | 0028 §2.2（FINAL）の「制御ループの外の worker プロセス」に反する。数値計算が GIL を長く握ると tick の締め切りに響き、モデルの読み込み（deserialize）が制御プロセスに入る。変えるなら 0028 を Supersede する記録が要る |

### 4.2 経路

| 案 | 利点 | 却下理由 |
|---|---|---|
| **A. `SOCK_SEQPACKET` の Unix ソケット＋受付スレッド（推奨）** | 境界と相手の死を OS が教える・`SO_PEERCRED` で書き手を絞れる・0072 の部品を再利用できる | — |
| B. `RuntimeDirectory` の file を原子置換（worker が tmp に書いて rename、ループが毎 tick `stat` して読む） | スレッドが要らない・実装が小さい | 書き手をファイル権限でしか絞れない。**読み・検証が tick の中に入り**（Critical Safety の前）、大きな結果ほど tick が伸びる。worker の死を「更新が止まった」でしか知れない。逆向き（frame）にも同じ仕組みが要る。**スレッドを増やしたくないなら次善** |
| C. 共有メモリ（`mmap` のリングバッファ） | 最速 | 途中まで書かれた値を読む・版の管理・壊れたメモリの検証が難しい。1 Hz の制御に速さは要らない |
| D. SQLite の表（worker が書き、ループが読む） | 既存のストアを使える | ストアの書き手が増え、busy timeout の待ちがループに入る（0060 §2.7）。trace の保存とロックを取り合う |
| E. 読み取り API（HTTP）や `coldaisle-eventd` を使う | 既存の入口 | API は GET だけ（0009）。eventd は「記録と文脈であって指令ではない」（0064 §2.2）の入口で、書き手に Workspace が入る |

### 4.3 worker の入力

| 案 | 却下理由 |
|---|---|
| **A. 毎 tick の frame、window は worker が組み立てる（推奨）** | — |
| B. `coldaisle-fand` が `mpc.period_ms` ごとに window 全体を送る | `coldaisle-fand` がモデルの feature schema と window 長を知ることになり、モデルの変更が制御デーモンの変更になる。メッセージも大きい |
| C. worker が SQLite の `latest()` / 履歴を自分で読む | 別時刻の Telemetry で推論する（#86「MPC だけが別時刻の Raw Telemetry を再取得しない」、0060 §2.5 に反する）。Fallback・Safety の floor・effective demand が worker から見えない |

### 4.4 異常時・版・その他

| 案 | 却下理由 |
|---|---|
| worker が落ちても直前の提案を `mpc.valid_ms` まで使う | 死んだプロセスの直前の出力を健全とみなす根拠が無い（→ 所有者判断 4。**採っても安全側の裁定は変わらない**ので、所有者が稼働率を優先するなら採れる） |
| 受付スレッドの死で Max にする（0072 と揃える） | 人の安全側の入口を失う 0072 と違い、ここで失うのは任意の ML だけ。Max は騒音として安全系に ML の失敗を混ぜる |
| worker の異常を `EMERGENCY` の fault にする | Critical Safety を ML から独立させる（AGENTS.md ルール3）に反する。ML の失敗は Gate の Fallback で扱う（0028 §2.7） |
| MPC の期限を受信起点のまま（0028 §2.6 のみ）にする | 滞留した提案が受信時に新しく見える。暗黙の上限（snapshot の窓）は Supervisor の期限に引きずられて長い（→ 所有者判断 6） |
| MPC の期限を 0041 のように元 snapshot 起点へ**置き換える**（0028 §2.6 を Supersede） | 受信起点の条件（受信してからの時間の上限）が消える。両方を掛けるほうが保守側で、既存の記録も書き換えずに済む |
| `coldaisle-fand` が走行中に registry を `stat` して読み直す（PR #192 の journal と同じ形） | provenance と Gate の期待値が走行中に変わる。0071 §2.5（FINAL）の「promotion は再起動から trace に現れる」と食い違い、別の記録が要る（→ 所有者判断 8） |
| `--registry-root` が読めないときは起動しない（終了コード 5） | 任意の部品の破損で Fan の制御を手放す（→ 所有者判断 7） |
| worker に `expected_artifacts` を渡さず、各自で production を追わせる | promotion の直後に Gate と別の artifact で提案を作り続け、Fallback が続く。どの artifact で作ったかの説明も2か所に分かれる |
| worker が `origin`（`active_binding`）を名乗る | 別プロセスからの自己申告に制御権の用途を任せる。`for_active` が閉じている間は `shadow_binding` に固定する（0061 §2.4） |
| 認可で `coldaisle-fand` と同じ uid を認める | `coldaisle-fand` と同じ uid の別サービスが提案を差し込める（0072 §2.5 と同じ理由） |
| 経路の健全性を必ず trace に載せる（版上げを必須にする） | 推奨案では既存の欄で足りる。版上げは #192 と直列にしなければならず、段階 1 を待たせる（→ 所有者判断 9） |

---

## 5. 未決事項

### 所有者の判断を要する点（本記録の承認で決める）

| # | 論点 | 推奨 | 代わりの選択肢 |
|---|---|---|---|
| 1 | worker の置き場所（§2.1 / §4.1） | 役割ごとの別 unit | 1つの worker に同居 / `coldaisle-fand` の子 |
| 2 | 経路（§2.2 / §4.2） | `SOCK_SEQPACKET`＋受付スレッド | `RuntimeDirectory` の file の原子置換（スレッド無し） |
| 3 | worker の入力（§2.3 / §4.3） | 毎 tick の frame、window は worker | `coldaisle-fand` が window を送る |
| 4 | worker の切断時（§2.5） | その tick から Fallback | 直前の提案を期限まで使う |
| 5 | 受付スレッドの死（§2.2） | Learned だけ閉じる（Max にしない） | Max（0072 と同じ） |
| 6 | MPC の新しさ（§2.4 の4） | `mpc.max_source_age_ms` を新設して受信起点と両方掛ける（`fan-policy.yaml` の版上げ） | 受信起点のまま / 0028 §2.6 を Supersede して元 snapshot 起点へ置き換える |
| 7 | registry が読めない起動（§2.6） | Learned を無効にして起動を続ける | 終了コード 5 で制御を取らない |
| 8 | model の promotion の反映（§2.6） | `coldaisle-fand` の再起動から（0071 §2.5 のまま） | 走行中の読み直し（別の記録が要る） |
| 9 | 経路の健全性を trace に載せるか（§2.9） | 載せない（版を上げない） | `learned_channel` の塊を足す（#192 の v13 の後に直列で版上げ・`KNOWN_VERSIONS` 更新） |
| 10 | 再起動をまたいだ束縛（§2.4 の1） | 封筒の `run_id` と 0060 の照合の二重 | 0060 の照合だけ |

### 実測を待つ値（`status: provisional` で置き、実測後に決める）

| 値 | 待つもの |
|---|---|
| `mpc.period_ms` / `mpc.budget_ms` / `mpc.valid_ms`（0028 §2.6 の暫定値 10000 / 2000 / 20000）と新設の `mpc.max_source_age_ms` | 本番機（GPU 到着後）での推論・optimizer の所要時間の実測。#86 の候補（horizon 60〜120 秒・周期 2〜5 秒）は確定値ではない |
| `supervisor.valid_ms`（0041 §5 のまま） | RL 推論の所要時間の実測 |
| `max_message_bytes`・`worker_idle_timeout_ms`・接続数の上限 | 段階 1 での frame / 提案の大きさと送受信時間の測定 |
| worker の資源の上限（CPU の割り当て・`nice`・メモリ） | 本番機で worker と `coldaisle-fand` を同居させたときの tick の `duration_ms` の実測 |
| worker が GPU を使うか（Compute Mode との関係） | 本番機の構成。**制御は GPU AI Service に依存させない**（AGENTS.md）ので、既定は CPU で動くことを前提にする |

### 別の場所で決める点

| 内容 | 決める場所 |
|---|---|
| 実行ファイル・ユーザー・グループ・ソケットの path の名前（本記録の名前は仮称） | 段階 1 の実装 PR と 0060 未決 7 / #57 |
| worker の systemd unit の中身（`Restart`・資源の上限） | 段階 6（#57 / 0069 の後続） |
| `SupervisorPolicyBinding.for_active` を開く条件と、`active_binding` を経路で運ぶ形 | 0061 §5 / 0074 §5 のとおり別の決定記録 |
| `LearnedFailure` に経路の失敗の値を足すか、detail で表すか | 段階 1 の実装 PR（§2.5） |
| frame の列を再現のために保存するか（保存先・保持期間） | #86 の再現性の受入基準を満たす実装で。trace（0030）には入れない |
| 複数 kind（thermal model と confidence model など）を同時に入れ替える手順 | 0062 §5 のまま |
| 適用された Learned MPC の optimizer 実績を trace に残すか | 0059 §5 のまま |
