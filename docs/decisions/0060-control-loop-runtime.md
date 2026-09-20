# 決定記録 0060: Control Loop の実行時契約（周期の置き場所・入力の供給・モードの入口・実行の記録）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-20、リポジトリ所有者が承認。`safety.yaml` の schema 変更を含むため 0028 §2.9 の承認点 2 に当たる）
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
| `watchdog_timeout_ms >= (tick_ms + tick_deadline_ms) * 2` | heartbeat は tick ごとにしか出ない。次の tick が始まるまでと、その tick の処理の合計が heartbeat の間隔になるので、時間切れがそれを下回ると健全な運転でも deadman が鳴る（systemd も `WatchdogSec` の半分の間隔で通知することを前提にしている。§2.7） |

**`fan-policy.yaml` には置かない。** 締め切り（`tick_deadline_ms`）と overrun の上限は
0028 §2.8 で `safety.yaml` にあり、周期はその2つと同じ不変条件で縛られる。
別のファイルへ分けると、**片方だけを変えた組み合わせが検証されないまま採用される。**

> **承認点**: `safety.yaml` の schema を変えるので、0028 §2.9 の承認点 2 に当たる。
> 2026-09-20 に所有者が承認した。

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
  永久に期限切れにならない。**worker が一時的に読めない tick でも識別子と受信時刻を捨てない。**
  捨てると、同じ提案が後から出てきたときに新しい受信時刻を押してしまう
- RL Supervisor の出力は、**元 snapshot を loop 自身の単調時計で特定できたときだけ**使う
  （0041 の「元 snapshot の単調時刻から数える」を、worker の時計を信じずに満たす）。
  照合は `tick_id` だけで行わない。**番号は再起動で 0 に戻る**ので、前の process の出力が
  新しい process の無関係な snapshot に結び付く。`(tick_id, ts_ms, snapshot schema)` が
  そろった記録だけを「この loop が出した snapshot」とみなす

### 2.7 deadman（watchdog）は必ず配線し、heartbeat は書き込みの直後に出す

0028 §2.6 は deadman を**制御プロセスの外の systemd** に置くと決めた。その heartbeat を
出す側の契約をここで固定する。

- **heartbeat は「書き込みと検証が終わった」直後に出す。** そのあとに置く処理
  （降格の永続化・decision trace の保存）はどれも I/O を伴い、保存先のロックで待たされると、
  制御は終わっているのに deadman に殺される。`duration_ms` の区間（2.4）と同じ境目にする
- **完了しなかった tick では heartbeat を出さない。** Critical Safety と合成の例外は
  捕まえない（2.6）ので、その tick の heartbeat も出ない。これが deadman の効き目である
- **heartbeat の失敗で冷却を止めない。** 送れなかったことは記録に残して運転を続ける。
  本当に途切れていれば外の systemd が時間切れで終わらせ、引き継ぎで Max になる
- 本番の合成（`coldaisle-fand`）は deadman を**必ず**配線する。`NOTIFY_SOCKET` があれば
  `WATCHDOG=1` を送り、無ければ**無音の no-op にはしない**。起動時に「deadman が無い」ことを
  error として残し、heartbeat の間隔が `watchdog_timeout_ms` を超えたらそのつど記録する。
  service の unit は `Type=notify` と `WatchdogSec` を設定し、`--require-watchdog` を付けて
  「通知できないなら起動しない」にする
- 送れるのは `READY=1` と `WATCHDOG=1` の**固定 datagram だけ**で、値を引数や設定から
  組み立てない（Max しか書けない引き継ぎ実行部と同じ考え方。0028 §2.7）

#### deadman が「ある」と言える条件

**`NOTIFY_SOCKET` の存在を deadman の証拠にしない。** 通知先は `Type=notify` なら
`WatchdogSec` が無くても渡るので、それだけで通知を必須にした起動を通すと、
`WATCHDOG=1` は届くが誰も見ていない状態を「deadman あり」として運転してしまう。

判定は sd_watchdog_enabled(3) と同じにする。

| 条件 | 扱い |
|---|---|
| `WATCHDOG_USEC` が無い・0 以下・整数でない | deadman は無い（`--require-watchdog` なら起動しない） |
| `WATCHDOG_PID` があり、この process の PID と違う | deadman はこの process を見ていない（同上） |
| `WATCHDOG_USEC` が **heartbeat の間隔 × 2 未満** | **起動しない。** 健全な運転のまま殺され、再起動を繰り返す |
| `WATCHDOG_USEC` と `safety.yaml` の `watchdog_timeout_ms` が違う | 実際に効くのは環境側。warning を残して環境側で検証する |

heartbeat の間隔は `tick_ms + tick_deadline_ms`（次の tick が始まるまでと、その tick の処理）
として数える。`safety.yaml` 側も §2.1 で同じ式を検証する。

#### 記録の待ち時間は heartbeat の間隔を食いつぶせない

heartbeat を書き込みの直後へ出しても、**decision trace の保存がそのまま次の tick の前に
居座る**なら、2つの heartbeat のあいだに保存の待ち時間が丸ごと入る。ストアの既定の
busy timeout（5 秒）は暫定の `watchdog_timeout_ms`（5 秒）と同じなので、これだけで
時間切れに届く。

**制御ループの接続の busy timeout を `tick_deadline_ms` に絞る。**

- 待てなかった書き込みは**失敗として残す**（`trace_dropped` を数え、log に理由を出す）。
  記録の失敗は制御の失敗ではない（§2.6）ので、運転は続ける
- 保存のあとにもう1回 heartbeat を出す案は採らない。間隔は**前回の heartbeat から**
  数えるので、保存が長ければ最初の間隔がそのまま伸びる（数を増やしても解決しない）
- 保存を別スレッドの queue へ逃がす案も採らない。安全側の経路にスレッドと、
  その落とし方という失敗の種類を増やすわりに、**待ちの上限を決めれば足りる**

#### 起動時の失敗は、種類ごとに報告する

`sd_notify` の失敗（通知先が消えている・権限が無い）を素の `OSError` のまま外へ出すと、
呼び出し側の分類が「設定が不正」へ落ち、**0028 §2.7 が決めていない状況で全 zone Max を
書く**ことになる。起動時の失敗は種類ごとに別の例外にし、終了コードも分ける。

| 失敗 | 扱い | 終了コード |
|---|---|---|
| `fan-hardware.yaml` が不正 | 制御を取らない（BIOS のまま） | 2 |
| hardware mapping が `provisional` | 制御を取らない（0028 §2.9 の承認点 3） | 3 |
| deadman が使えない（通知先・時間切れ・送信のいずれか） | 制御を取らない | 4 |
| `safety.yaml` / `fan-policy.yaml` が不正 | **0028 §2.7 のとおり全 zone Max** | 1 |
| それ以外（Metric Catalog・品質規則・ストア・入力契約の組み立て） | 制御を取らない（BIOS のまま） | 5 |

最後の行は 0028 §2.7 の「設定不正なら Max」**には当たらない**。あの規則は制御設定そのものが
読めない場合のもので、ここに混ぜると「DB が一時的に開けない」だけで Max を書く。
§2.9 の規則そのものは変えていない（適用する範囲を、制御設定の不正に限ると明示しただけである）。

### 2.8 authority の既定は `SHADOW`

`AuthorityRuntime`（#92）を配線しない構成では、固定 stage の authority を使い、**既定を
`SHADOW`（`BASELINE_STAGE`）にする**。設定の `authority_stage` は v9 から**上限**なので、
配線を忘れた起動がそれをそのまま制御権にすると、`full` と書かれた設定だけで
Learned MPC が実 Fan を握る。trace に残す stage も Gate と同じ式
（`min(いまの stage, 設定の上限)`）で数える。

### 2.9 設定不正時は Max を1回書いて保持する

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
| deadman を配線せず、port だけを用意して既定を no-op にする | hang しても heartbeat の欠落が起きず、`watchdog_timeout_ms` が一度も効かない（2.7） |
| deadman が無い環境では黙って何もしない | 「deadman がある」と思ったまま運転することになる。起動時と遅れのたびに記録する（2.7） |
| `NOTIFY_SOCKET` が無ければ常に起動しない | 手元実行（`uv run coldaisle-fand`）ができなくなる。必須にするのは service 側の `--require-watchdog`（2.7） |
| heartbeat を decision trace の保存後に出す | 保存先のロックで待たされているあいだ heartbeat が出ず、制御が終わっているのに殺される（2.7） |
| heartbeat を保存の前後の両方で出して済ませる | 間隔は前回の heartbeat から数えるので、保存が長ければ最初の間隔がそのまま伸びる（2.7） |
| decision trace の保存を別スレッドの queue へ逃がす | 安全側の経路にスレッドと「queue が溢れたときどうするか」という失敗の種類が増える。待ちの上限を決めれば足りる（2.7） |
| `NOTIFY_SOCKET` があれば deadman があるとみなす | `Type=notify` なら `WatchdogSec` が無くても渡る。`WATCHDOG=1` が誰にも見られない状態を「deadman あり」と扱う（2.7） |
| 起動時の失敗をまとめて「設定不正」として全 zone Max にする | Metric Catalog が読めない・DB が一時的に開けないだけで、0028 §2.7 が決めていない状況で制御を取る（2.7） |
| worker が読めない tick で、前回の結果の識別子と受信時刻を捨てる | 同じ提案が後から出てきたときに新しい受信時刻を押し、止まった worker の提案が有効期限を取り戻す（2.6） |
| RL 出力を `tick_id` だけで元 snapshot に結び付ける | 番号は再起動で 0 に戻る。前の process の出力が無関係な snapshot に結び付く（2.6） |
| `runtime` を持たない v8 の記録を許す | 版を見ても中身が言えなくなる。版は自分の中身を表す（2.4） |
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
| 7 | `coldaisle-fand` の systemd unit（`Type=notify` / `WatchdogSec` / `Restart` / `ExecStopPost`）の中身 | #57 / #64 |
| 2 | 制御の許容遅延とストアの `quality.yaml` のしきい値を完全に分けるか | #50 の実測後 |
| 3 | `tick_ms` / `tick_deadline_ms` / `overrun_consecutive_limit` の確定値 | #50 / #75 の後（0028 §2.9 の承認点 2） |
| 4 | 運転モードのソケットのプロトコルと認可 | #60 |
| 5 | Learned MPC / RL Supervisor の worker プロセスと、提案を置く経路の実装 | #86 / #89 |
| 6 | `ControlTick` の版を #159（PR #160）の v7 とどう整合させるか（両方がマージされた後の reader） | 後からマージする側の PR |
