# 決定記録 0060: Control Loop の実行時契約（周期の置き場所・入力の供給・モードの入口・実行の記録）

- **種別**: Decision Record
- **Status**: Proposed（**所有者の承認が必要**。`safety.yaml` の schema を変えるため、0028 §2.9 の承認点 2 に当たる）
- **Date**: 2026-09-20
- **Supersedes**: なし
- **関連**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md)（§2.2 / §2.5 / §2.6 / §2.7 / §2.8、未決 3 と 8） /
  [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md) /
  [`0004-storage-read-contract.md`](0004-storage-read-contract.md) /
  [`0029-telemetry-loss-classes.md`](0029-telemetry-loss-classes.md) /
  [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md) /
  [`0041-supervisor-proposal-freshness.md`](0041-supervisor-proposal-freshness.md) /
  [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md) /
  `AGENTS.md`「絶対に守るルール」1〜4・6・9
- **対象 Issue**: #74（`coldaisle-fand`）

---

## 1. Context

決定記録 0028 は層間の契約を決めたが、**実行時の配線**として3つを未決のまま残した。

| 0028 の未決 | 内容 | 決める場所 |
|---|---|---|
| 3 | 運転モードを変える入口（ソケットのプロトコル・認可） | #74 / #60 |
| 8 | Telemetry を `coldaisle-fand` へ渡す経路と、制御に足る供給周期 | #65 / #74 |
| （§2.6 の表） | `tick_ms`（control loop の周期）を**どの設定ファイルが持つか** | 本記録 |

`tick_ms` は 0028 §2.6 の暫定値の表にあるのに、#103 が実装した `safety.yaml` / `fan-policy.yaml`
のどちらにも無い。周期が設定に無いと、**#74 の常駐ループが動かせない**か、
コードに定数を置くことになる（AGENTS.md ルール9 に反する）。

あわせて、0028 §2.6 が「overrun として記録する」と言う**記録先**も決まっていない。
`ControlTick`（#76 / #82）には tick の所要時間も締め切り超過も設定の版も無く、
いま残せるのは構造化ログだけである。ログと decision trace が別なら、
あとから「この判断は締め切りに間に合っていたのか」を trace だけでは言えない。

---

## 2. Decision

### 2.1 control tick の周期は `safety.yaml` が持つ（`tick_ms`）

`SafetyConfig` に `tick_ms` を足し、schema version を 3 に上げる（`ControlConfig` は 10）。
値は 0028 §2.8 と同じく `status` / `basis` 付きで、実測まで `provisional` として扱う。

#103 が読み込み時に次を確かめる。

| 不変条件 | 破れたときに起きること |
|---|---|
| `tick_deadline_ms <= tick_ms` | 超過を検出したときには次の tick が始まっている |
| `watchdog_timeout_ms > tick_ms` | 1 周期ぶんの遅れで deadman が落ち、健全な運転で再起動を繰り返す |

**`fan-policy.yaml` には置かない。** 締め切り（`tick_deadline_ms`）と overrun の上限は
0028 §2.8 で `safety.yaml` にあり、周期はその2つと同じ不変条件で縛られる。
別のファイルへ分けると、**片方だけを変えた組み合わせが検証されないまま採用される。**

> **承認点**: `safety.yaml` の schema を変えるので、0028 §2.9 の承認点 2 として
> 所有者の承認を得てからマージする。

### 2.2 Telemetry は読み取り専用のストア経由で供給する（0028 未決 8）

`coldaisle-fand` は **SQLite の `latest()`（決定記録 0004）だけ**を読む。
`air.*` は取り込みデーモンが、内部 Telemetry は Telemetry Collector（#65）が
同じ timeline へ書いている。**新しい IPC を作らない。**

- シリアルは開かない（AGENTS.md ルール6）、NVML も呼ばない（0001 D-07 / 0028 §2.2）
- 供給周期は入力側（#65 と #22）が決める。制御側は「待たない」ことだけを守る。
  読めなければその tick は欠測として扱い、Critical Safety が 0028 §2.7 の安全側へ倒す
- **新しさはストアの `age_ms` を使わない。** loop は metric ごとに
  「`ts_ms` が変わったことを観測した**自分の単調時計**の時刻」を持ち、
  `age_ms = いまの単調時計 - 最後に変化を観測した時刻` で数える（0028 §2.6）。
  供給側が同じ `ts_ms` を返し続けても fresh にならない
- ストアは `quality.yaml` のしきい値で古い行を `stale` に落とすが、
  制御はそのうえで `safety.yaml` の許容遅延を重ねる。
  **2つの判定は重なるほど保守側にしか動かない**ので、制御が甘い側へ倒れることはない

任意入力（Guard の trigger など）の許容遅延は、metric の domain から
`safety.yaml` の源へ対応づける（`air.*`→`air_ms`、`power.cpu.*`→`cpu_power_ms`、
`power.*`→`gpu_ms`、`cpu.*`→`cpu_ms`、`gpu.*`→`gpu_ms`）。
**対応づけられない metric は起動時に拒否する。** 既定値を置くと、未知の入力が
黙って甘い期限で通る。

入力の契約（`ControlInputContract`）は**手書きの表にせず**、検証済み設定が指している
metric（Guard の trigger・Fallback の入力・Workload Regime・MPC の cost metric）から
数え直して組み立てる。表にすると、設定へ metric を足したときに契約だけが古いまま残り、
その入力が「欠測」として静かに無視される。

### 2.3 運転モードの入口は読み取り専用の port にし、ソケットは #60 が配線する（0028 未決 3）

`coldaisle-fand` は `OperatingModeSource`（`current() -> ModeCommand` だけ）を通じて
モードを読む。**ローカル Unix ソケットのプロトコルと認可は #60 で決める。**
本記録では次だけを固定する。

- 既定は `AUTO`。loop はモードを永続化しないので、**再起動で必ず `AUTO` に戻る**（0028 §2.5 (a)）
- `MANUAL` / `CALIBRATION` の requested は `ModeCommand` が運ぶ。
  `MAX` は requested では表さない（Critical Safety の `forced_max` が所有する）
- **入口が読めない tick は直前のモードを保つ。** 読めないことを理由に `AUTO` へ戻すと、
  人が `MAX` にした運転が入口の故障で勝手に下がる（安全でない側への自動遷移になる）
- 読み取り API（0009）にも LLM のツール（0018）にもこの port を生やさない（AGENTS.md ルール1）

### 2.4 decision trace に「実行そのもの」を残す（`ControlTick` v8 の `runtime`）

判断（誰が何を要求し、何が効いたか）とは別に、**その判断を出した実行の条件**を同じ行に残す。

| 欄 | 意味 |
|---|---|
| `tick_period_ms` / `deadline_ms` | その tick に効いていた検証済み設定の周期と締め切り |
| `duration_ms` | **書き込みと検証まで**の所要時間（decision trace の保存時間を含まない） |
| `deadline_exceeded` | 締め切り超過。**`duration_ms` と `deadline_ms` から決まる**（型が一致を強制する） |
| `snapshot_schema_version` | その tick が読んだ `ControlStateSnapshot` の形（#102） |
| `config` | `fan-hardware.yaml` / `safety.yaml` / `fan-policy.yaml` の内容ハッシュ |

- `duration_ms` の区間は 0028 §2.6 の watchdog が数える区間と同じにする。
  保存時間を含めると、**保存先が遅いだけで安全側の縮退が起きる**
- `deadline_exceeded` を独立した欄にしない。同じ行の所要時間と食い違わせられる欄を作ると、
  あとから「遅れていない tick」に見せられる
- 設定は**内容ハッシュだけ**を残す。絶対 path も個体識別子も残さない（AGENTS.md ルール10）
- 番号は **v8** を使う。v7 は #159（PR #160）が先に確保しているため、取り合わない

### 2.5 1 tick の順序：Critical Safety は Gate より先に評価する

Critical Safety の裁定は requested を見ない（入力は snapshot・モード・actuator の帰還・overrun）。
したがって**先に評価してよく、先に評価しなければならない**。0028 §2.5 (c) は
「ML を使ってよいのは `safety_state` が `NORMAL` のときだけ」としており、
Gate が見る `safety_state` は**その tick のもの**でなければならないからである。

```text
snapshot → Supervisor → Reactive Guard → Fallback → Critical Safety
  → Confidence / OOD Gate → requested → 合成（0028 §2.4）→ Hardware Backend → 記録
```

- 1 tick が使う `ControlStateSnapshot` は**1つだけ**で、層をまたいでも作り直さない
- 合成は最後に1回だけ。requested・Guard・Safety の3つを 0028 §2.4 の優先順で束ねる
- **tick を重ねない。** tick の中から次の tick を始めようとしたら拒否する
- actuator の帰還（tach・読み戻し・書き込み失敗）は**次の tick の Safety** へ渡す。
  書き込みはその tick の Safety 裁定より後に起きるので、同じ tick では裁定できない

### 2.6 例外の翻訳（0028 §2.7 の実装）

| 起きたこと | loop の扱い |
|---|---|
| Telemetry の読み取り失敗 | その tick は欠測として扱う（0 で埋めない）。Gate へは `snapshot_unavailable` |
| snapshot の組み立て失敗 | 空の frame で作り直し、Gate へは `snapshot_invalid` |
| Supervisor / Workload Regime の失敗 | 戦略なしで続ける。`safety_state` は変えない |
| Reactive Guard の例外 | `guard_exception` の fault（無条件に `EMERGENCY`） |
| Fallback Controller の例外 | `fallback_exception` の fault（無条件に `EMERGENCY`）。requested は Max |
| Confidence / OOD Gate の例外 | `fallback_exception` の fault。その tick は Fallback の値で続ける |
| **Critical Safety・合成の例外** | **捕まえない。** プロセスを終わらせ、引き継ぎで Max（0028 §2.7） |
| Hardware Backend の例外 | 全 zone の `write_failure` として**次の tick**の Safety へ渡す |
| Shadow 記録・decision trace の保存の失敗 | 記録の失敗は制御の失敗ではない。log に残して続ける |

- **締め切りを過ぎた tick では ML を通さない。** 遅れた tick で新しい提案を採ると、
  安全側の裁定が ML の遅延を待つ形になる
- worker 結果の受信時刻は**初めて見た結果にだけ**押す（識別子 `MpcProposal.result_digest()` で
  新旧を見分ける）。毎 tick 現在時刻を押すと、worker が止まって同じ結果を返し続けても
  永久に期限切れにならない
- RL Supervisor の出力は、**元 snapshot を loop 自身の単調時計で特定できたときだけ**使う
  （0041 の「元 snapshot の単調時刻から数える」を、worker の時計を信じずに満たす）

### 2.7 authority の既定は `SHADOW`

`AuthorityRuntime`（#92）を配線しない構成では、固定 stage の authority を使い、**既定を
`SHADOW`（`BASELINE_STAGE`）にする**。設定の `authority_stage` は v9 から**上限**なので、
配線を忘れた起動がそれをそのまま制御権にすると、`full` と書かれた設定だけで
Learned MPC が実 Fan を握る。trace に残す stage も Gate と同じ式
（`min(いまの stage, 設定の上限)`）で数える。

### 2.8 設定不正時は Max を1回書いて保持する

0028 §2.7 の「hardware は正しく `safety.yaml` / `fan-policy.yaml` が不正」の経路では、
確認済みの hardware mapping だけを使って `config_invalid` の `EMERGENCY`（全 zone Max）を
**1回書き**、正しい設定で再起動するまで解除しない。**周期的に書き直さない**（未決 1）。
不正な閾値を読まないので、この経路は Max 以外を表現できない。

---

## 3. Consequences

### 良くなること

- 0028 の未決 3 と 8 が閉じ、#74 の常駐ループが**設定だけで**動かせる
- 周期・締め切り・watchdog の組み合わせが起動前に検証される（2.1）
- decision trace だけで「この判断は間に合っていたか」「どの設定で回っていたか」が言える（2.4）
- 入力の契約が設定から導出されるので、metric を足したときに契約が置き去りにならない（2.2）
- 新しい IPC を増やさずに済み、シリアル・NVML の単一所有（ルール6 / D-07）が保たれる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| `safety.yaml` の schema 変更に所有者の承認が要る | 値は `provisional` のままで、意味は 0028 §2.6 の表と同じ |
| SQLite 経由なので、供給周期が制御周期より遅いと入力が古いまま回る | 古さは `safety.yaml` の許容遅延で検出し、0028 §2.7 の安全側へ倒す |
| ストアの `quality.yaml` のしきい値が制御の判定に混ざる | 重なるほど保守側にしか動かない（2.2）。実測後に分離を検討（未決 2） |
| モードの入口が無いので、いまはモードを変えられない | 既定の `AUTO` で M8 の運転は成立する。入口は #60 |
| `ControlTick` の版がまた上がる | v1〜v7 はそのまま読める。`runtime` は省略可能な欄 |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `tick_ms` を `fan-policy.yaml` に置く | `tick_deadline_ms` と別ファイルになり、組み合わせの不変条件を検証できない（2.1） |
| `tick_ms` を CLI の必須引数にする | 設定ファイルに無い値が運転を決める。0028 §2.8 の「値はすべて設定に置く」に反する |
| `tick_ms` をコードの既定値にする | AGENTS.md ルール9 |
| Telemetry を専用のローカルソケットで渡す | 書き手が増え、供給側（#65 / 取り込み）の変更が制御へ直接届く。まず既存の読み取り契約で足りるか実測する |
| ストアの `age_ms` をそのまま制御の新しさに使う | 壁時計の差なので、時刻合わせで飛ぶと期限切れの値が fresh になる（0028 §2.6） |
| 読み取り API（HTTP）から Telemetry を取る | GET のみの保証（0009）に依存を足し、API の停止が制御に波及する |
| モード変更を HTTP の POST にする | 0009 を崩し、LLM のツールから届く経路になる（0028 §2.2） |
| モードの入口が読めない tick を `AUTO` に戻す | `MAX` を勝手に解除する。安全でない側への自動遷移になる（2.3） |
| Gate のあとに Critical Safety を評価する | Gate が前 tick の `safety_state` を見ることになり、0028 §2.5 (c) の条件が1 tick 遅れる |
| `duration_ms` に decision trace の保存時間を含める | 保存先が遅いだけで overrun になり、安全側の縮退が記録の都合で起きる（2.4） |
| `deadline_exceeded` を所要時間と独立した欄にする | 片方だけを書き換えて「遅れていない tick」に見せられる（2.4） |
| Backend の帰還 fault を同じ tick の Safety へ渡す | 書き込みは裁定より後に起きる。渡すには裁定を2回することになる（2.5） |
| Guard / Fallback / Gate の例外でプロセスを落とす | 決定論的な層の不具合で冷却が止まる。Max へ倒して運転を続ける（0028 §2.7） |
| Critical Safety・合成の例外を fault へ翻訳する | 安全の最終裁定を握りつぶすことになる。落として引き継ぎで Max にする（0028 §2.7） |
| worker 結果の受信時刻を毎 tick 更新する | 止まった worker の古い提案が永久に期限切れにならない（2.6） |
| authority の既定を設定の `authority_stage` にする | 配線の抜けが制御権を増やす形になる（2.7 / #79 と同じ理由） |
| 設定不正時に Max を周期的に書き直す | 書き直す周期を決める設定が（不正なので）読めない。未決 1 として残す |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | 設定不正時の Max を周期的に書き直すか（外から `pwmN_enable` を戻されたときの再主張） | #57（実機 backend）と合わせて |
| 2 | 制御の許容遅延とストアの `quality.yaml` のしきい値を完全に分けるか | #50 の実測後 |
| 3 | `tick_ms` / `tick_deadline_ms` / `overrun_consecutive_limit` の確定値 | #50 / #75 の後（0028 §2.9 の承認点 2） |
| 4 | 運転モードのソケットのプロトコルと認可 | #60 |
| 5 | Learned MPC / RL Supervisor の worker プロセスと、提案を置く経路の実装 | #86 / #89 |
| 6 | `ControlTick` の版を #159（PR #160）の v7 とどう整合させるか（両方がマージされた後の reader） | 後からマージする側の PR |
