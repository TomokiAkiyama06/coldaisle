# 決定記録 0028: Fan 制御の層間契約（入出力・優先順位・状態遷移・周期・故障時の扱い・設定・承認点）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-13
- **Supersedes**: なし
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md) /
  [`0026-three-zone-fan-control.md`](0026-three-zone-fan-control.md) /
  [`0001-initial-project-decisions.md`](0001-initial-project-decisions.md)（D-07） /
  [`0004-storage-read-contract.md`](0004-storage-read-contract.md) /
  [`0009-read-api.md`](0009-read-api.md) /
  [`0022-firmware-v1.md`](0022-firmware-v1.md) /
  [`docs/airflow-model.md`](../airflow-model.md) /
  `docs/requirements.md` S-13 / D-03 / Q-10 /
  `AGENTS.md`「絶対に守るルール」1〜10・「制御アーキテクチャの固定前提」
- **対象 Issue**: #61（本記録の Issue 番号は**すべて GitHub の番号**。`ISSUES.md` の論理番号ではない）

---

## 1. Context

決定記録 0027 は、Fan 制御の**経路・役割・制御権の段階的な解放**を決めた。
しかし #61 が「ADR で決めること」に挙げた次の項目は、0027 に書かれていない。

- 層ごとの入出力 schema
- override の優先順位
- authority / fallback の状態遷移
- control tick / deadline
- failure semantics
- config の境界
- safety の承認点

#74（Control Engine）と #76〜#82 は、いずれも #61 を前提にしている。
一方、閾値や floor の**値**を決める実測（#50 / #75）は進行中で、まだ無い。

そこで本記録は、**値を持たない構造と意味**を先に決める。値はすべて設定に置き、実測が揃うまで暫定として扱う。

あわせて、Issue の記述どうしの食い違いを解く。
`issues/30-fan-control-design.md` と `issues/43-fan-control-daemon.md` は「設定不正なら Safe / Max」とするが、
対象の PWM header を特定する設定そのものが壊れている場合に Max を書くと、**対象外の header へ書き込む**ことになる（同じ Issue の受入基準「対象外PWMを変更しない」に反する）。2.7 で扱いを分ける。

---

## 2. Decision

### 2.1 用語

| 用語 | 意味 |
|---|---|
| zone | `front` / `rear` / `top`。AIO Pump は含まない（0026） |
| demand | `0.0..1.0` の実数。**NaN / 無限大は拒否する** |
| requested | 制御器（Fallback / Learned MPC）または人の操作が求める demand |
| effective | Reactive Guard と Critical Safety を通した後の demand。**Hardware Backend が受け取るのはこれだけ** |
| tick | 制御ループの1回分 |
| 壁時計 / 単調時計 | 記録の時刻に使う時計 / 経過時間と期限に使う時計（2.6） |
| 暫定値 | 実測（#50 / #75）の前に置く値。設定上で `status: provisional` と明示する（2.8） |

### 2.2 プロセスと実行の分離

- Fan 制御は専用のデーモン（以下 `coldaisle-fand`）で行う。取り込みデーモン・API・GPU AI Service・Telemetry Collector とは別の責務として扱う
- **hwmon の PWM へ書き込むのは `coldaisle-fand` の Hardware Backend だけ**とする。
  唯一の例外は、`coldaisle-fand` が止まった**後**にだけ動く引き継ぎ実行部（2.7）で、これは **Critical Safety の一部**として **Max しか書けない**
- **安全の経路は ML を待たない。** 1 tick の中で「Telemetry 読み取り → Fallback → Reactive Guard → Critical Safety → 合成 → 書き込み → 検証 → 記録」を完結させる
- Learned MPC / RL Supervisor（M9）は**制御ループの外の worker プロセス**で動かす。
  worker は提案を置き、ループは最新の提案を読むだけにする。worker が落ちても、止まっても、ループは止まらない。
  Fallback Controller と RulePolicy は決定論的で軽いので、ループの中で動かす
- **モード変更の入り口は `coldaisle-fand` のローカル Unix ソケットだけ**とし、ファイル権限で利用者を絞る。
  読み取り API（決定記録 0009 の GET のみの保証）へ書き込みを足さない。LLM のツールと `server.py` からは到達できない（AGENTS.md ルール1）

Telemetry の入り口は次の3つに分ける。

| 入力 | 読み方 | 理由 |
|---|---|---|
| 内部 Telemetry（GPU、CPU 温度・Power、T_SENSOR） | **Telemetry Collector（#65）のタイムスタンプ付きの出力を読む。`coldaisle-fand` は NVML を呼ばない** | NVML を呼ぶのは Telemetry Collector だけ（決定記録 0001 D-07 / 要件 Q-10）。GPU の時系列を2本にしない（#65「同一 timeline」） |
| 外付けセンサーの `air.*` | ストアの `latest()` を読み取り専用で読む（SQLite WAL。決定記録 0004） | シリアルを開くのは取り込みデーモンだけ（AGENTS.md ルール6） |
| 制御対象ファンの tach と PWM の読み戻し | Hardware Backend が自分で読む | 書き込みの検証と stall の判定に使う**アクチュエーターの帰還**で、Telemetry の収集ではない。値は decision trace（#82）に残す |

Telemetry Collector や取り込みデーモンが止まれば、入力が stale になり、2.7 の扱い（安全側）になる。
Telemetry Collector の出力を受け渡す経路と供給周期は未決（5. の 8）。

### 2.3 層の入出力

各層の出力は **`tick_id` と `ts_ms`（壁時計）を共有**し、1 tick の判断の記録（decision trace）として #82 が保存する。型の定義は #76 が実装する。
期限はすべて `coldaisle-fand` 自身の単調時計で数える（2.6）。

| 層 | 入力 | 出力 | 有効期限 |
|---|---|---|---|
| Telemetry snapshot | Telemetry Collector の出力 / `latest()` / Hardware Backend の帰還 | metric ごとに `value` / `ts_ms` / `age_ms` / `status`（`fresh` / `stale` / `missing`） | その tick だけ |
| State Estimator | snapshot | 派生量（`dT/dt`、Power slope、case ΔT など）、`regime` と `regime_confidence` | その tick だけ |
| Supervisor | state | `policy` / `regime` / `weights` / `strategy` / `version` / `computed_at_ms`。**demand は出さない** | 受け取ってから `supervisor.valid_ms` |
| 制御器（Fallback / Learned MPC） | state、Supervisor の出力（任意） | zone ごとの `requested_demand` と `reason`。全体で `controller` / `model_version` / `confidence` / `ood` / `optimizer_status` / `latency_ms` / `seq` / `computed_at_ms` | 受け取ってから `mpc.valid_ms`（Fallback はその tick だけ） |
| Confidence / OOD Gate | 制御器の提案、authority stage | `active_controller` / `authority_stage` / zone ごとの `requested` / `fallback_reason` | その tick だけ |
| Reactive Guard | state、`requested` | zone ごとの `floor` / `ceiling` / `hold_until_mono_ms` / `reason` | `hold_until_mono_ms` まで |
| Critical Safety | snapshot、Hardware の状態、運転モード、`requested`、Guard の出力 | zone ごとの `floor` / `forced_max` / `faults` / `reason`、全体の `safety_state` | その tick だけ |
| 合成（2.4） | 上の3つ | zone ごとの `requested` / `effective` / `bound_by` / `reasons` | その tick だけ |
| Hardware Backend | `effective` | zone ごとの `pwm_raw` / `write_ok` / `readback_ok` / `rpm` / `fault` | その tick だけ |

**型で経路を縛る。**

- PWM の生値は Hardware Backend のモジュールの中でしか作れない型にする
- Hardware Backend が受け取るのは、合成（2.4）が返す effective demand の型だけにする
- Supervisor・制御器・Gate が作れるのは requested まで。「Critical Safety を通っていない値が Hardware Backend に届く経路が無い」ことを試験で確かめる（#74 の受入基準）

Air Balance Model（#81）と Acoustic Cost（#94）は**制御器が requested を作るための材料**であり、合成の上下限には入らない。

### 2.4 合成の順序と優先順位

zone ごと・tick ごとに、次の順で effective を決める。

```text
r  = requested                              （2.5 のモードと active controller が決める）
x1 = min(r,  guard_ceiling)                 Guard の ceiling。AUTO のときだけ掛ける（無ければ 1.0）
x2 = max(x1, guard_floor)                   Guard の floor（無ければ 0.0）
x3 = max(x2, safety_floor)                  Critical Safety の最低安全 demand
x4 = max(x3, prev_effective - ramp_down × Δt)   下げる速さだけを制限する
effective = 1.0 if forced_max else x4

forced_max = STARTUP ∨ EMERGENCY ∨ zone の fault で Max ∨ 運転モードが MAX
```

- **floor は常に ceiling に勝つ。** Reactive Guard の ceiling が Critical Safety の floor を下回っても、floor が残る
- **Max はすべてに勝つ。** 運転モードの `MAX` は requested ではなく、**Critical Safety の `forced_max`（Manual safety override。0027 §2.3）として表す**。Guard の ceiling で下がらない
- **Guard の ceiling は `AUTO` の制御器の出力にだけ掛ける。** ceiling はハンチングや行き過ぎを抑えるためのもので、安全のためのものではない。人が指定した `MANUAL` / `CALIBRATION` の値を下げない（floor はどのモードでも掛かる）
- **上げる速さは制限しない。** 冷却を遅らせないため。下げる速さの制限（`ramp_down`）は Critical Safety の段に置き、Manual・Fallback・ML のどれにも等しく効かせる。
  回り始めに必要な起動 PWM（kick）は Hardware Backend の責務で（#77）、demand を下げる向きには働かない
- **Top の CPU 冷却は Critical Safety が持つ。**
  `top の safety_floor = max(top の最低安全 demand, cpu_cooling_floor(CPU 温度, CPU Power))` とし、`cpu_cooling_floor` は設定の曲線から決定論的に求める。
  制御器が Top に出す requested は `case_aux_exhaust_demand` に当たる。
  これで 0026 の `top_demand = max(cpu_cooling_demand, case_aux_exhaust_demand, safety_floor)` を満たし、**CPU 冷却の下限を ML が下げる経路は無くなる**（ML は上げることだけできる）
- `bound_by` には effective を決めた項を記録する。複数の項が同じ値なら、安全側を記録する（`forced_max` > `safety_floor` > `ramp_down` > `guard_floor` > `guard_ceiling` > `requested`）

**Reactive Guard と Critical Safety の違い。**

| | Reactive Guard | Critical Safety |
|---|---|---|
| 目的 | 危険になる**前**に、数秒で先回りする | 絶対の条件と fault 時の最終裁定 |
| 出すもの | 一時的な floor / ceiling と hold | 最低安全 demand、CPU cooling floor、下げる速さの制限、forced Max（`MAX` モードを含む） |
| 解除 | hysteresis と hold で遅く解除 | fault が一定時間ずっと解消していること（2.5） |
| 落ちたとき | EMERGENCY（2.7） | プロセスごと終了し、引き継ぎで Max（2.7） |

### 2.5 状態遷移

状態は4つの軸に分ける。軸ごとに**誰が変えられるか**が違う。

#### (a) 運転モード：人だけが変える

| モード | 扱い |
|---|---|
| `AUTO`（既定） | requested は Gate が選んだ制御器の値 |
| `MANUAL` | requested は人が zone ごとに指定した値 |
| `MAX` | **Critical Safety の `forced_max`**（2.4）。requested では表さない |
| `CALIBRATION` | requested は人が開始した測定計画（#75）の値 |

- **どのモードでも Critical Safety を通り、Reactive Guard の floor が掛かる。** `CALIBRATION` でも外さない。Guard が介入した時点は記録し、その測定区間に印を付ける
- 変更はローカル Unix ソケットからだけ（2.2）
- **再起動すると `AUTO` に戻る。** `MANUAL` の低い値や測定途中の状態を持ち越さない

#### (b) authority stage：設定の上限。昇格は人だけ

`SHADOW`（既定）→ `LIMITED` → `EXPANDED` → `FULL`（#92）

| stage | ML の提案の扱い |
|---|---|
| `SHADOW` | 記録するだけ（counterfactual）。requested は Fallback の値 |
| `LIMITED` / `EXPANDED` | Fallback の値を中心とした帯（stage ごとの `limit_up` / `limit_down`）に収め、stage ごとに許可した zone だけに使う |
| `FULL` | そのまま requested にする |

- **昇格は自動で行わない**（2.9）
- 降格は自動で行ってよい。`demote_window_ms` の間に ML から Fallback への切り替えが `demote_after` 回を超えたら、stage を `SHADOW` へ下げて設定上も残し、理由を記録する

#### (c) active controller：tick ごとに決まる

ML を使ってよいのは、次を**すべて**満たすときだけ。

- モードが `AUTO`
- stage が `LIMITED` 以上
- 提案が期限内（受け取ってから `mpc.valid_ms` 以内。2.6）で、`optimizer_status` が正常
- `confidence` が stage ごとの閾値以上で、OOD ではない
- モデルの version が期待と一致する
- `safety_state` が `NORMAL`
- 復帰の hold を満たしている

切り替えの規則は次のとおり。

- ML → Fallback：**条件が1つでも崩れたら、その tick で即座に切り替える**（下げる向きに hysteresis を置かない）
- Fallback → ML：条件が `ml_recovery_hold_ms` の間**ずっと**満たされてから戻す（#79）

M8 には ML が無いため、active controller は常に Fallback になる。

#### (d) safety state：Critical Safety が決める

| 状態 | 動作 |
|---|---|
| `STARTUP` | 制御を取った直後。すべての zone を Max |
| `NORMAL` | 通常の運転 |
| `DEGRADED` | zone 単位の fault がある。該当 zone を 2.7 の demand へ上げ、ほかの zone は運転を続ける |
| `EMERGENCY` | すべての zone を Max |

| 遷移元 | 遷移先 | 条件 |
|---|---|---|
| （制御を取る） | `STARTUP` | takeover（2.7） |
| `STARTUP` | `NORMAL` | `startup_settle_ms` が過ぎ、CPU 温度が fresh で、全 zone の tach の応答を確認できた |
| `STARTUP` | `DEGRADED` | 上の条件を満たしたが、zone 単位の fault が残っている |
| `NORMAL` | `DEGRADED` | zone 単位の fault（2.7）。**即座に** |
| `STARTUP` / `NORMAL` / `DEGRADED` | `EMERGENCY` | 全体の fault（2.7）。**即座に** |
| `DEGRADED` | `NORMAL` | すべての fault が `fault_clear_hold_ms` の間ずっと解消 |
| `EMERGENCY` | `DEGRADED` / `NORMAL` | 全体の fault が `fault_clear_hold_ms` の間ずっと解消（zone の fault が残れば `DEGRADED`）。**ただし `config_invalid` は再起動まで解除しない**（2.7） |

- **安全側への遷移は即座に行う。** 安全でない側への遷移は、fault が `fault_clear_hold_ms` の間**ずっと**解消してから行う
- `DEGRADED` / `EMERGENCY` から `NORMAL` へ戻ったときの active controller は**必ず Fallback**。ML へは (c) の復帰 hold を経て戻る

### 2.6 周期と締め切り

#### 時計

時計は2つに分けて注入する。

| 時計 | 使い道 | 本番 | 試験 |
|---|---|---|---|
| 壁時計 `Clock.now_ms()`（`src/coldaisle/clock.py`、#73） | **記録の時刻（`ts_ms`）だけ** | `WallClock` | `SimulatedClock` |
| 単調時計（新設。`monotonic_ms()`） | **周期・締め切り・hold・期限・stall・overrun・Telemetry の経過時間のすべて** | OS の単調時計 | 試験用に進める単調時計 |

- 壁時計は時刻合わせで前後に飛ぶ。期限の判定に使うと、時計が戻ったときに古い提案が有効なまま残り、stall や stale の timer が満了しなくなる。tick は回り続けるので watchdog も介入しない
- **期限は `coldaisle-fand` 自身の単調時計だけで数え、ほかのプロセスの時計の値と比べない。**
  worker の提案と Supervisor の出力は、`coldaisle-fand` が受け取った時刻（単調時計）から `mpc.valid_ms` / `supervisor.valid_ms` で期限切れにする
- **Telemetry の経過時間に `now_ms - ts_ms` を使わない。** `ts_ms` はホスト受信時刻の壁時計（決定 D-05）だからである。
  `coldaisle-fand` は metric ごとに「`ts_ms` が**変わったことを観測した**単調時計の時刻」を持ち、`age_ms = 単調時計の現在 - 最後に変化を観測した時刻` とする。
  起動してからまだ変化を観測していない metric は stale とみなす（`STARTUP` の Max の間に揃う）

#### 周期

値はすべて設定に置き、下の数値は**初期の暫定値**とする。

| 項目 | 暫定値 | 意味 |
|---|---|---|
| `tick_ms` | 1000 | 制御ループの周期。Reactive Guard と Critical Safety は毎 tick 評価する |
| `tick_deadline_ms` | 500 | 1 tick の処理の締め切り。超えたら overrun として記録する |
| `overrun_emergency_after` | 3 | overrun が連続したら `EMERGENCY` |
| `mpc.period_ms` / `mpc.budget_ms` / `mpc.valid_ms` | 10000 / 2000 / 20000 | ML の提案周期・worker 側の計算の予算（超えたら worker が `optimizer_status = timeout` として出す）・受け取ってからの有効期限 |
| `supervisor.period_ms` / `supervisor.valid_ms` | 60000 / 180000 | 期限が切れたら RulePolicy（#88） |
| Telemetry の許容遅延 | 源ごとに設定 | これを超えたら `stale`。`air.*` はセンサー周期 2.5 秒（決定記録 0022）より長くする |

- tick は**単調時計の締め切りで刻む**（ドリフトしない。決定記録 0022 と同じ考え方）。遅れた tick を後から取り戻して連続実行しない
- 制御用の許容遅延は `quality.yaml` の `stale_after_ms` とは**別に持つ**。保存とアラートのための判定と、アクチュエーションのための判定は目的が違う
- **deadman は、制御プロセスの外にある systemd の watchdog が担う。**
  ループは「書き込みと検証まで終えた tick」ごとに watchdog へ通知する。止まれば systemd がプロセスを終わらせ、2.7 の引き継ぎで Max になる。
  試験では watchdog を抽象化して扱う
- 暫定値は #75（RPM の応答時間）と #50 の実測で見直す

### 2.7 故障時の扱い

#### Telemetry

| 事象 | 対応 | safety state |
|---|---|---|
| CPU 温度が stale / missing（Telemetry Collector の停止を含む） | Top を Max | `DEGRADED` |
| GPU Telemetry が stale / missing（同上） | Front / Rear を `fault_demand` | `DEGRADED` |
| T_SENSOR が stale / missing（同上） | Front / Rear を `fault_demand` | `DEGRADED` |
| 必須の `air.*` が stale（取り込みデーモンの停止を含む） | Front / Rear を `fault_demand` | `DEGRADED` |
| 任意の Telemetry（hotspot など）が無い | 動作は変えない。記録する（ML の confidence が下がりうる） | 変えない |

#### Fan / Hardware

| 事象 | 対応 | safety state |
|---|---|---|
| Front / Rear の tach stall（指令が `stall_check_min_demand` 以上なのに、`stall_ms` の間 RPM が `stall_rpm` 未満） | その zone を Max（再始動を試みる）。ほかの zone を `fault_demand`。通知する | `DEGRADED` |
| **Top の tach stall** | 全 zone を Max。通知する | **`EMERGENCY`** |
| Front / Rear の書き込み失敗・読み戻し不一致・`pwmN_enable` が外から戻された | 再試行し、ほかの zone を `fault_demand`。`write_fail_emergency_after` 回続いたら `EMERGENCY` | `DEGRADED` |
| **Top の書き込み失敗・読み戻し不一致** | 全 zone を Max | **`EMERGENCY`** |

#### 制御器・ループ

| 事象 | 対応 | safety state |
|---|---|---|
| ML の失敗（読み込み失敗・例外・timeout・低 confidence・OOD・version 不一致・提案の期限切れ） | active controller を Fallback へ | 変えない |
| Supervisor の失敗・期限切れ | RulePolicy へ。RulePolicy も失敗したら、Supervisor の文脈なしで Fallback | 変えない |
| **Fallback Controller / Reactive Guard の例外** | 全 zone を Max。決定論的な層の失敗は不具合として扱う | **`EMERGENCY`** |
| **Critical Safety / 合成の例外** | **捕まえない。** プロセスを終わらせ、引き継ぎで Max にする（AGENTS.md「例外は握りつぶさない」） | — |
| tick の overrun が `overrun_emergency_after` 回連続 | 全 zone を Max | `EMERGENCY` |
| ループが止まる（hang） | systemd の watchdog が終了させ、引き継ぎで Max | — |

`fault_demand` の暫定値は **1.0**。これより下げてよいかは #50 の実測で決める（5. 未決事項）。

#### 設定

| 事象 | 対応 |
|---|---|
| **起動時、ハードウェアの特定（`fan-hardware.yaml`）が不正、または header を一意に特定できない** | **制御を取らない。** BIOS の制御（Safety-0）のまま、0 以外で終了し、通知する。特定できない header へ Max を書くこと自体が危険なため |
| 起動時、ハードウェアの特定は正しいが、`safety.yaml` / `fan-policy.yaml` が不正 | 制御を取り、**全 zone を Max**（`EMERGENCY`、理由 `config_invalid`）。正しい設定で再起動するまで解除しない（#78「設定不正時の Emergency Max」） |
| 動作中の設定変更 | **v1 では読み直さない。** 反映は再起動で行う（再起動時は `STARTUP` の Max を通る） |

#### 制御の引き継ぎ（takeover / handoff）

hwmon の ABI では、`pwmN_enable` は `0`＝制御なし（全速）、`1`＝手動、`2` 以上＝自動、`pwmN` は `0..255`（`255` が最大）である。

**制御を取るとき**

- Hardware Backend が header を特定・検証した後、`pwmN_enable` を手動へ切り替える**前に**、引き継ぎ記録を `/run` の下へ書く。
  リポジトリには置かず、`coldaisle-fand` の実行ユーザーだけが書ける権限にする。上位層・ML・LLM は作れない
- 引き継ぎ記録の中身は、header ごとの対象ファイルの場所、特定に使った情報（hwmon の `name` と label）、元の `pwmN_enable` と `pwmN`
- その後に `STARTUP` の Max を書く

**正常停止**（SIGTERM で終了し、systemd の `SERVICE_RESULT` が `success`）

- 元の `pwmN_enable` が `0` または `2` 以上：その値へ戻して制御を返す。戻す前に PWM を下げない
- 元の `pwmN_enable` が `1`（手動）：**返す先の自動制御が無い。** 元の `pwmN` の固定値へ戻すと負荷に追従しない値が残るため、**Max のまま終了し、通知する**

**異常終了**（例外・SIGKILL・watchdog）

systemd の `ExecStopPost` で**引き継ぎ実行部**を動かし、引き継ぎ記録の header を Max にする。

引き継ぎ実行部は **Critical Safety の一部**であり、「異常終了なら Max」という Critical Safety の規則を、`coldaisle-fand` が止まった後に執行する。
effective demand の型を通らないため、次の制約で**冷却を弱める経路にならない**ことを保証する。

| 制約 | 理由 |
|---|---|
| 書けるのは `pwmN = 255` と `pwmN_enable = 1` だけ。**値を引数・設定・記録から受け取らない**。一時的にも下がらない順で書く | 下げる向きの値を表現できない。Reactive Guard / Critical Safety のどの出力よりも安全側になる |
| 書き込み先は引き継ぎ記録の header だけ。書く前に hwmon の `name` と label が記録と一致するかを確かめ、一致しなければ書かない | 対象外の header へ書かない |
| 引き継ぎ記録が無ければ何もしない | 制御を取っていない |
| 動くのは `coldaisle-fand` が止まった後だけ | 書き手が同時に2つにならない |
| 標準ライブラリだけで書き、coldaisle のパッケージ・設定・モデルに依存しない | 壊れた環境でも動く |

試験は Critical Safety と一緒に置き、偽の sysfs で上の制約を確かめる。

**再起動と残るリスク**

- **再起動**：`Restart=always`。起動するたびに `STARTUP`（Max）を通る
- **残るリスク**：OS ごと止まるなど `ExecStopPost` が動かない場合、PWM は最後の値のまま残る。
  制御を取った後は BIOS Q-Fan も自動では戻らないため、このときの最終防衛線は **CPU / GPU 自身のサーマルスロットリング**になる（要件 D-03 の Safety-0 のうち、制御を取った後も残るもの）。
  チップがどう振る舞うかは #74 で実機確認する

### 2.8 設定の境界

AGENTS.md の「ファイル構成」にある3つのファイルへ分ける。

| ファイル | 中身 | 読む層 | 変更に要るもの |
|---|---|---|---|
| `config/fan-hardware.yaml` | zone と hwmon の対応（driver 名・label・属性名。**`hwmonN` の番号は書かない**）、Fan profile（起動 PWM・最低安定 PWM・最大 RPM・PWM→RPM 表・Airflow Index）。#75 の結果 | Hardware Backend | 特定の変更は実機での再確認。profile は #75 の測定記録 |
| `config/safety.yaml` | 絶対温度上限、zone の最低安全 demand、`cpu_cooling_floor` の曲線、`fault_demand`、stall の判定、Telemetry の許容遅延、`ramp_down`、`startup_settle_ms`、`fault_clear_hold_ms`、tick の締め切りと overrun、watchdog | Critical Safety（ほかの層は制約として読むだけ） | **決定記録と所有者の承認**（2.9） |
| `config/fan-policy.yaml` | Fallback の曲線、Reactive Guard の閾値と hold、ML / Supervisor の周期と予算、Gate の閾値、authority stage と stage ごとの制限、復帰 hold、降格の条件 | Fallback / Reactive Guard / Gate / ループ | authority stage の昇格と Reactive Guard の閾値の確定は所有者の承認。それ以外は PR レビュー |

- **暫定値と確定値を設定の上で区別する。**
  `safety.yaml` の値と Reactive Guard の閾値には、`status: provisional | confirmed` と `basis`（根拠の決定記録・測定記録）を付ける。
  `confirmed` には `basis` を必須にする。起動時に暫定値の一覧をログへ出す（`config/rules.yaml` の「閾値はすべて暫定値」と同じ扱い）
- 読み込みは既存の設定と同じく pydantic の `frozen=True, extra="forbid"` にする。
  読み込み時に不変条件を確かめる（例：floor は 0〜1、ceiling ≥ floor、`fault_demand` ≥ 最低安全 demand、`air.*` の許容遅延 > センサー周期）
- **ML の層はどの設定も書き換えられない。** 学習済みモデルのファイル（M9）は設定に含めず、`model_version` で参照する
- 引き継ぎ実行部（2.7）は設定を読まない
- 設定に個体識別子を書かない（AGENTS.md ルール10）。driver 名と label は書いてよいが、シリアル番号・ホスト名・絶対パスは書かない

### 2.9 安全に関わる承認点

次は**所有者の承認なしに進めない。**

| # | 対象 | 時期 | 残し方 |
|---|---|---|---|
| 1 | 本記録（0028） | #74・#76〜#82 の実装を始める前 | PR のマージで FINAL |
| 2 | `safety.yaml` の値を `confirmed` にする、または**緩める**（floor を下げる・許容遅延を延ばすなど） | #50 / #75 の後 | 決定記録と承認 |
| 3 | **実機のファンの制御を初めて取る**（Hardware Backend の書き込みを有効にする） | #74 の実機確認 | 所有者が現地で行う。kill・watchdog・正常停止時の引き継ぎ・引き継ぎ実行部を確認項目にする |
| 4 | `CALIBRATION` の実行 | #75 | 所有者が開始する。自動では入らない |
| 5 | authority stage の昇格 | #92 | 承認と、ゲートの根拠（#90 / #91） |
| 6 | 合成の順序・優先順位・状態遷移の変更 | — | 本記録を Supersede する決定記録 |

**自動で行ってよいのは、安全側への変更（降格・Fallback への退避・Max）だけ**とする。

---

## 3. Consequences

### 良くなること

- #76〜#82 と #74 を、**実測値を待たずに実装・試験できる**（値は暫定として設定に置く）
- 合成（2.4）が1つの関数になり、「floor が ceiling に勝つ」「Max がすべてに勝つ」「下げる速さだけを制限する」を単独で試験できる
- ML が遅れても、落ちても、安全の経路が止まらない（2.2 / 2.6）
- 時刻合わせで時計が飛んでも、期限・stale・stall の判定が崩れない（2.6）
- プロセスが異常終了しても Max で止まる（2.7）
- 「設定不正なら Max」と「対象外の header に書かない」の食い違いが解ける（2.7）
- GPU の時系列が Telemetry Collector の1本のまま保たれる（2.2 / D-07）
- 判断の記録（decision trace）の形が決まるので、#82 と #90 が同じものを保存できる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 設定が3ファイルに増える | 読み込み時の不変条件で食い違いを起動前に落とす（2.8） |
| 引き継ぎ実行部という書き手が増える | Critical Safety の一部とし、Max しか書けない・照合しないと書かない・止まった後だけ動く、の制約を偽の sysfs で試験する（2.7）。実機確認は承認点 3 |
| 異常終了・再起動のたびに Max になり、うるさい | 安全側を優先する。再起動が続いたら通知する |
| 元の制御が手動だった header は、正常停止でも Max のまま残る | 通知し、人が戻す（2.7） |
| 暫定値が保守側なので、実測まで騒音が大きい | #50 / #75 の結果で確定させる（承認点 2） |
| `fault_demand` が 1.0 だと、センサー1本の欠測でも Front / Rear が Max になる | #50 で下げてよい値を確かめる（未決 4） |
| ML を別プロセスにすると構成が複雑になる | M8 には ML が無いので単一プロセスで始め、worker は M9 で入れる |
| 内部 Telemetry を Telemetry Collector に、`air.*` を取り込みデーモンに依存するため、どちらかの停止が制御に波及する | 波及先は安全側（Top は Max、Front / Rear は `fault_demand`）。供給周期と経路は未決 8 で決める |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 安全条件を MPC の目的関数の penalty にする | 検証できない。安全の最終裁定は決定論的な hard constraint にする（0027 §2.3） |
| ML を制御ループの中で同期に動かす | optimizer の timeout が安全の tick を止める（2.2） |
| `coldaisle-fand` が NVML を直接呼ぶ | NVML を呼ぶのは Telemetry Collector だけという決定（0001 D-07 / 要件 Q-10）に反し、GPU の時系列が2本になる。変えるなら D-07 を Supersede する決定記録が要る（2.2） |
| 上げる速さも制限して滑らかにする | 冷却を遅らせる。制限するのは下げる速さだけにする（2.4） |
| Reactive Guard の ceiling が Critical Safety の floor より優先される | floor は常に勝つ（2.4） |
| `MAX` モードを requested = 1.0 で表す | Guard の ceiling で下がり、「Max はすべてに勝つ」が崩れる。forced_max として表す（2.4） |
| `CALIBRATION` では Reactive Guard / Critical Safety を外す | 迂回の経路を作ることになる（#74 の受入基準）。介入を記録して区間に印を付ける（2.5） |
| 期限・hold・stale・stall を壁時計で数える | 時刻合わせで飛び、時計が戻ると timer が満了しなくなる（2.6） |
| worker の提案に付いた期限を、worker の時計の値のまま比べる | プロセス間で時計の基準をそろえる前提が要る。受け取った時刻から数える（2.6） |
| 起動時にハードウェアの特定が不正でも Max を書く | 特定できない header へ書くことになる。BIOS の制御のまま終了する（2.7） |
| 正常停止でも常に Max にする | 元が自動制御なら、人が明示的に止めたときは BIOS の制御（導入前の既知の状態）へ返す（2.7） |
| 正常停止で、元が手動なら保存した `pwmN` へ戻す | 負荷に追従しない固定値が残る。Max のまま終了して通知する（2.7） |
| 引き継ぎ実行部を置かず、異常終了時は PWM を最後の値のまま残す | 低い値のまま止まりうる（2.7） |
| 引き継ぎ実行部が設定やモデルを読んで書く値を決める | 壊れた環境で動かず、下げる向きの値を書ける経路になる（2.7） |
| 設定を動作中に読み直す | 途中で不正な設定を読むと状態が中途半端になる。再起動で反映すれば `STARTUP` の Max を通る（2.7） |
| モード変更を読み取り API（HTTP）の POST にする | GET のみの保証（0009）を崩し、LLM のツールから届く経路になる（2.2） |
| 制御の許容遅延に `quality.yaml` の `stale_after_ms` を使い回す | 保存・アラートとアクチュエーションで目的が違う（2.6） |
| deadman を制御プロセスの中のスレッドだけで持つ | プロセスが止まれば一緒に止まる。外の systemd の watchdog にする（2.6） |
| authority stage を条件を満たしたら自動で昇格する | #92 の原則に反する。昇格は人の承認（2.9） |
| Top の CPU 冷却の下限を ML に決めさせる | CPU の冷却は Top だけが担う。下限は決定論的な曲線にし、ML は上げることだけできる（2.4） |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | 2.6・2.7・2.8 の暫定値の確定（tick・締め切り・許容遅延・floor・stall の判定・`ramp_down`） | #50 / #75 の後、決定記録（承認点 2） |
| 2 | decision trace の保存先（同じ SQLite のテーブルか、別ファイルか）と保持期間 | #82 |
| 3 | GUI / Workspace からのモード変更の配線（ソケットのプロトコル・認可） | #74 / #60 |
| 4 | Front / Rear の `fault_demand` を 1.0 より下げてよいか | #50 |
| 5 | 学習済みモデルの置き場所と version の付け方 | #84 |
| 6 | `ExecStopPost` が動かないとき、チップが PWM をどう保つか | #74（実機確認） |
| 7 | コンテナ化（#64）で `coldaisle-fand` をホストで直接動かすか | #64 |
| 8 | Telemetry Collector の出力と `air.*` を `coldaisle-fand` へ渡す経路（ストア経由 / ローカルソケット）と、制御に足る供給周期 | #65 / #74 |
| 9 | `issues/30` / `issues/43` の定義ファイルと GitHub の #61 / #74 の記述の同期 | 別途（同期スクリプトの問題と合わせて） |
