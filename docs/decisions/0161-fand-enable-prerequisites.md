# 決定記録 0161: fand を本番で `enable` する前に残っている判断（D 状態の hang・承認点 2 の値・unit の時間・HEAD での確認のやり直し・順序）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-21
- **Supersedes**: なし（[`0158`](0158-t5-11s-path-component-assurance.md) §5 の 2・[`0080`](0080-fand-systemd-unit.md) §5 の 1 / 2・
  [`0028`](0028-fan-control-contracts.md) §2.9 の承認点 2・[`0157`](0157-hint-runtime-launch-form.md) §5 の 4 の測定に答える案を出す。
  どの記録の決定も置き換えない。新しい安全の値は決めない）
- **関連**: [`0119`](0119-safety-values-first-host.md) §2.6〜§2.13 /
  [`0121`](0121-hardware-test-procedure.md) §2.1 / §2.3〜§2.10 / §5 /
  [`0123`](0123-cpu-power-floor-tests.md) §2.2 / §2.3 /
  [`0125`](0125-stall-rise-criterion-tach-update.md) / [`0126`](0126-startup-settle-front-spinup.md) /
  [`0136`](0136-t7-t8-t9-acceptance-exceptions.md) §2.2 / §2.3 /
  [`0148`](0148-fand-state-alert.md) §2.7 / §5 の 2 /
  [`0158`](0158-t5-11s-path-component-assurance.md) §2.4〜§2.6 / §5 /
  `docs/fan-header-mapping.md`「0121 の試験の結果」/ `docs/ubuntu-deploy.md` 4 / 4.1 / 6.2 / 6.5 / 6.9 /
  `docs/control-config.md`（Control Config v16 / v17）/ `deploy/systemd/coldaisle-fand.service` / PR #344 / PR #355
- **対象 Issue**: #74

---

## 1. Context

### 1.1 ここまでに済んだこと

- 0121 の実機の試験 T0〜T9 と T1（24 h、2026-10-15 16:36〜10-16 16:36）は合格した（`docs/fan-header-mapping.md` の
  「0121 の試験の結果」・PR #344）
- T5 の約 11 s の経路は実機で試さず、0158（FINAL）で部品とテスト・解析で担保した。0158 §2.5 の T1〜T3 のテストは
  PR #355（890949e）で入った（`tests/test_deploy_templates.py`・`tests/test_fand_exit_codes.py`）。0154 は Rejected
- T9 (b) の健全度と Guard の閾値は、#308（PR #314）で trace に載り、`tests/test_control_loop.py` の
  `test_cpu_power_staleness_alone_degrades_health_and_makes_the_guard_conservative` で確かめている（0136 §2.3）。
  T1 でも健全度は 86400 tick すべてで `NORMAL` だった

### 1.2 `enable` の前に残っていること

0121 §2.10 は「各試験の結果を残し、合格した値を所有者の承認で `confirmed` にし、すべてそろってから承認点 3 を完了とし、
`enable` は所有者が判断する」とする。そこまでに残っている論点は次の 5 つで、どれも決まっていない。

1. **D 状態の hang の受け入れ**（0158 §5 の 2）。「調べてから所有者が改めて決める」とされた
2. **承認点 2**（0028 §2.9 の 2、0121 §2.10）。`safety.yaml` の `provisional` の値（0119 §2.13 の表と §2.4）を、
   どの試験の結果で `confirmed` にするかの記録が無い
3. **unit の時間の値**（0080 §5 の 1: `TimeoutAbortSec` / `TimeoutStopSec` / `TimeoutStartSec` / `RestartSec`）と
   **`Nice=` / `CPUWeight=` / `OOMScoreAdjust=` の要否**（0080 §5 の 2）。0121 §5 の 3 は「§2.7 の結果の後」とし、
   0158 は `TimeoutAbortSec` を変えないことを解析の前提にした
4. **HEAD での確認のやり直し**。導入先の `/opt/coldaisle` は T1 の版 `d3c5404` のままで、main はその後 297 コミット進んだ。
   どこまでやり直すかの規定が無い。0157 §5 の 4 の `READY=1` までの時間の測定（`docs/ubuntu-deploy.md` 6.9 の手順 7）も残っている
5. **`enable` までの順序**。設定の移行・承認点 2・承認点 3・`enable` と同時に行う 0148 §5 の 2 の
   `fand_trace_silent.enabled` を true にする手順（`docs/ubuntu-deploy.md` 6.5 の最後）が、1 本の手順にまとまっていない

### 1.3 T1 の後に main で変わったもの（fand に関係するもの）

`git diff d3c5404 HEAD` で確かめた（2026-10-21、HEAD は 5ff6f96）。

| 変わったもの | 中身 | `enable` への影響 |
|---|---|---|
| unit の `ExecStart=` | console script から root の所有の venv の interpreter の `-I -m` へ（31f7a26、0157 §2.1）。`--hint-lockfile /opt/coldaisle/uv.lock` を足した（9ca575f） | 導入先の unit を置き直す（6.9）。T1 はこの形で動かしていない |
| 起動時の証明書の照合 | Workload Hint の `stage` が B1 / B2 のときだけ、`READY=1` の前に証明書を照合し実行環境の指紋を計算する（50408f6、0152 段 3・0157 §2.2） | `stage: off` / `observe` では指紋を計算しない（`control_daemon.py` の `_open_workload_hint`） |
| Workload Hint の読み手 | `stage` が `off` でなければ、loop の外のスレッドが `events` を読み、自分の表へ書く（0133 §2.8） | T1 には無かったスレッドと DB の書き込み |
| `control/loop.py` | Workload Hint の tracker と trace の欄（+110 行） | 毎 tick の処理が増えた。T1 の `duration_ms` はこの版のものではない |
| decision trace | `ControlTick` v15 → v16（`workload_hint` の塊。`stage: off` でも毎 tick 載る） | 0148 の通知は版を読み分けない |
| DB の migration | 0014（`events` の boot clock）・0015（Workload Hint の状態）・0016（hint の shadow の記録） | 書き手を止めて当てる（`docs/ubuntu-deploy.md` 4「コードを更新するとき」） |
| Control Config | v15 → v17。`fan-policy.yaml` v11 → v13（`supervisor.workload_hint` が**必須**）、`safety.yaml` v4 → v5（`workload_hint` の塊は省ける） | 移行しないと `config_invalid` の全 zone Max で止まる（`docs/control-config.md` の v16 / v17 の移行手順）。片方だけ上げても拒否される |
| `config/rules.yaml` | 0148 の fand の状態の通知（`fand_trace_silent` は出荷で `enabled: false`）ほか | `/opt` を更新すると出荷の値に戻る |

Critical Safety・Reactive Guard・Fallback・hardware backend・引き継ぎ実行部（`safety_handoff.py`）は、T1 の後に変わっていない。

## 2. Decision（提案）

各節に推奨の案を書く。代替案は §4、所有者が選ぶものは §5 に番号つきで並べる。

### 2.1 D 状態の hang（0158 §5 の 2 の X4）

**調べた結果**（2026-10-21）:

| 調べたもの | 範囲 | 結果 |
|---|---|---|
| `journalctl -k` の `blocked for more than` / `hung_task` | 2026-10-13 以降（boot -1 の 10-13 以降と、boot 0 = 10-14 02:18 以降） | 0 件 |
| T1 の fand の `duration_ms` | 2026-10-15 16:36〜10-16 16:36（86400 tick） | 最大 11 ms・p99.9 6 ms。`deadline_exceeded` 0 |
| T1 の `write_failure` / `readback_mismatch` / `enable_reverted` | 同上 | 0 回 |
| 予備確認（10-13）〜T1 の前の decision trace | 2026-10-13〜10-15 | **未確認**（DB が root でしか読めない） |

参考に、T8 の CPU の全スレッドの負荷を含む区間（2026-10-14 13:51〜、1684 tick）の journal でも `deadline_exceeded` は 0、
`duration_ms` の最大は 7 ms だった（試験の記録の journal から数えた）。

**推奨**: D 状態の hang の残るリスクを受け入れる。ただし `enable` の前に、所有者が予備確認〜T1 の前の trace を読み取りだけで数える
（§2.5 の手順 2）。数えるのは、`faults` に `write_failure` / `readback_mismatch` が出た tick（**T2 の注入の区間 2026-10-14 02:26〜02:28 と
T6 の試験の設定の区間は除く。ほかの試験の操作の区間は結果の節と照らす**）と、journal の `control tick` の `duration_ms` の外れ値（125 ms 以上。0121 §2.3 の合格の条件の値で、新しい値ではない）。
どちらかが見つかったら受け入れを保留し、所有者に示し直す。

受け入れる理由:

- この機械では、約 8 日の運転と試験（故障の注入を含む）の間に、カーネルが hung task を一度も報告していない
- fand の tick は最大 11 ms で、hwmon の読み書きが driver の中で長く止まった兆しが無い
- D 状態で fand が止まると trace が途絶える。0148 の `fand_trace_silent` が 60 s で warning を出す（§2.5 で `enable` と同時に有効にする）。
  **ただしこれは知らせるだけで Max にはしない。夜間（22〜7 時）は warning が届かない**（0148 §2.7）
- チップの保護（CPU 95 °C・GPU の T.Limit のクロックの制限）と、fand と別のプロセスで動く GPU の Fan（0128）は、fand の hang の影響を受けない（0119 §2.12・0158 §2.3）

受け入れても残ること（`basis` と結果に書く）: D 状態の hang では Max までの時間に上限が無い。引き継ぎ実行部も同じ driver で止まりうる
（0080 §2.5 の最後・0028 未決 6）。この機械で D 状態を起こして振る舞いを確かめたことは無い（0080 §5 の 3 のうち D 状態の部分は確かめていない）。

### 2.2 承認点 2: `confirmed` にする値

0119 §2.13 の表の 10 行（`telemetry.*_ms` は 4 項目に分かれる）と、§2.4 の `cpu_power_cooling_floor` の 15 項目を、次の表のとおり `confirmed` にする。**値は変えない。**
値は 0119 §2.4 / §2.6〜§2.10 と 0126 §2.1 の記録から写した。`basis` の文言は案で、所有者が直してよい。

| 項目 | 値 | 根拠の試験（`docs/fan-header-mapping.md`） | `basis` の文言（案） |
|---|---:|---|---|
| `tick_ms` | 1000 | T1: tick の間隔の最大 1023.96 ms・p99.9 1019.6 ms、`skipped_slots` 0、watchdog の再起動 0 | `0119 §2.10 / 0121 §2.3 T1 合格（docs/fan-header-mapping.md T1）/ 0161 §2.2。Learned を配備しない運用に限る（0121 §2.1）` |
| `tick_deadline_ms` | 250 | T1: `duration_ms` 最大 11 ms（< 125 ms） | 同上 |
| `overrun_consecutive_limit` | 3 | T1: `deadline_exceeded` 0 回（連続の最長 0） | 同上（0060 §5 未決 3 もこれで閉じる） |
| `telemetry.cpu_ms` | 5000 | T3: `cpu_telemetry_stale` まで 6000 ms（5000 ms + 1 tick 以内）・解消まで 30001 ms。T1: 0 回 | `0119 §2.8 / 0121 §2.5 T3・§2.3 T1 合格 / 0124 / 0161 §2.2` |
| `telemetry.gpu_ms` | 5000 | T3: `gpu_telemetry_stale` まで 6000 ms。T1: 0 回 | 同上 |
| `telemetry.air_ms` | 5000 | T3: `air_telemetry_stale` まで 6000 / 6001 ms・Top は Max にならない。T1: 0 回 | 同上 |
| `telemetry.cpu_power_ms` | 5000 | T9 (a): CI の `test_invariant_13_cpu_power_goes_stale_after_its_own_limit_without_a_fault`。T9 (b): 7311 ms（5000〜8500 ms）。T1: `cpu_power_unavailable` 0 回 | `0123 §2.2 T9 (a)(b)・§2.3 / 0136 §2.3 / 0161 §2.2` |
| `stall_window_ms` | 5000 | T6: 検出は Top 5000・Front 6000・Rear 5001 ms。立ち上がりは Top・Front とも 20 回すべて 2 tick（最長 2001 + 1500 + 1000 = 4501 ms < 5000）、Rear は 0.0 の間も 439 RPM 以上。T1: `tach_stall` 0 回 | `0119 §2.6 / 0121 §2.8 T6 / 0125 / 0161 §2.2` |
| `watchdog_timeout_ms` | 5000 | T5: 約 5 s の経路で `SIGSTOP` から Max まで 5143〜5324 ms（12 試行）。約 11 s の経路は 0158 の部品と解析・PR #355 のテスト | `0121 §2.7（約 5 s の経路の実測）/ 0158（約 11 s の経路は部品と解析。kill できる hang に限る。約 10.05 s は上限ではなく典型の見積もり）/ docs/fan-header-mapping.md T5 / 0161 §2.1・§2.2。D 状態の hang は範囲の外` |
| `write_fail_emergency_after` | 3 | T2: Front の注入で 3 回目の評価で `EMERGENCY`、一時的な失敗は 1 tick で戻り `EMERGENCY` にならない。T1: 書き込みの失敗 0 回 | `0119 §2.7 / 0121 §2.4 T2・§2.3 T1 / 0161 §2.2` |
| `hardware_write_fail_exit_ms` | 5000 | T2: Front・Top とも `failing_ms` 5001 で終了コード 7、引き継ぎが 3 zone に Max（`applied`）、再起動の takeover は成功し終了コード 7 を繰り返さない | `0119 §2.10・§2.12 / 0080 §2.6 / 0121 §2.4 T2 / 0161 §2.2` |
| `ramp_down_per_s` | 0.05 | T4: 下降率の最大 0.0500 / 0.0501 /s、アイドル・GPU 300 W とも温度の上がりは 3 °C 未満 | `0119 §2.9 / 0121 §2.6 T4 / 0126 §2.3 / 0161 §2.2` |
| `startup_settle_ms` | 15000 | T4: 全開の ±5% まで Front 最長 12841 ms（+ 1500 = 14341 ms < 15000）。T1: 起動の直後を含め `tach_stall` 0 回。T1 の起動（BIOS の制御の回転数から）で、3 zone とも遅くとも 10000 ms（tick 10）までに全開の ±5% に入り、Demand が下がり始めた tick 17（17001 ms）までそこに留まった（下の注） | `0126 §2.1・§2.3 / 0121 §2.6 T4・§2.3 T1 / 0161 §2.2` |
| `fault_clear_hold_ms` | 30000 | T3: 解消まで 30001 / 31000 / 30001 ms で途中に出入りしない、温度と tach は 30 s より短く落ち着く。T2 の一時的な失敗: 30 s の保持の後 `normal` | `0119 §2.9 / 0121 §2.5 T3・§2.4 T2 / 0124 / 0161 §2.2` |
| `cpu_power_cooling_floor` | 100 W → 0.35、200 W → 1.0 | T8: 全スレッドの 3 回で 2.1〜3.8 s で効き、範囲の外の tick 0、195 W 以上を含む。途中の補間は CI の `test_cpu_power_jump_raises_the_top_floor_even_when_the_cpu_is_cool` | `0119 §2.4 / 0123 §2.1 / 0136 §2.2（両端の 2 点。途中は CI のテスト）/ 0161 §2.2` |

注（`startup_settle_ms`）: 0126 §2.3 は T1 の起動の記録で「Demand が下がり始めるまでに 3 zone が全開の ±5% に入っていること」を確かめるとしたが、
`docs/fan-header-mapping.md` の T1 には `tach_stall` 0 回だけが書かれていた。T1 の試験の記録（DB から書き出した trace）の最初の 25 tick を
読み、tick 16（`startup` の最後）の回転数（Front 1776・Rear 1528・Top 2288 RPM）を全開とすると、±5% に入ったのは Rear・Top が tick 4、
Front が tick 10 で、どれも tick 17 で `ramp_down` が始まるまで外れなかった。本記録の PR で `docs/fan-header-mapping.md` の T1 にも足す。

**tick 系の 3 つ（`tick_ms` / `tick_deadline_ms` / `overrun_consecutive_limit`）の確定は、Learned を配備しない運用に限る**
（0121 §2.1・§5 の 1）。worker の unit（`coldaisle-learnd-*`）を起動するときは 0121 §2.3 の soak をやり直す。`basis` にそう書く。

**`confirmed` にする作業は所有者が `sudo` で行う。** 導入先の `/etc/coldaisle/control-config/safety.yaml` はリポジトリに置かず、
本記録の作業では読めない。§2.5 の手順 4 で、Control Config の移行と同じ編集で行う（推奨。§5 の 7）。書き換えた後、fand の起動ログの
`safety_config_is_provisional` が false になることを確かめる（true なら `provisional` の項目が残っている）。

### 2.3 unit の時間の値と `Nice=` などの扱い（0080 §5 の 1 / 2）

**時間の値**（`deploy/systemd/coldaisle-fand.service` の今の値。変えない）:

| 項目 | 今の値 | 推奨 | 根拠 |
|---|---:|---|---|
| `WatchdogSec` | 5s | 確定 | `watchdog_timeout_ms` と同じ（0080 §2.3・0119 §2.10）。§2.2 の `watchdog_timeout_ms` と一緒に確定する |
| `TimeoutAbortSec` | 5s | 確定 | 0158 の解析の前提（`WatchdogSec` + `TimeoutAbortSec` ≤ 10 s）。PR #355 の `tests/test_deploy_templates.py` がこの合計を超えないことを守る。短くする案（0127 §5 の 2 の (b)）は所有者が 0158 §5 の 1 で選ばなかった |
| `TimeoutStopSec` | 10s | 確定 | `tick_ms + tick_deadline_ms`（1250 ms）と正常停止の返却の合計より長い（0080 §2.3）。T1 の終わりの `systemctl stop`（hwmon・3 zone を BIOS へ戻す）は `Stopping` から `Stopped` まで約 0.58 s、段階 5 の simulated は 0.6 s。超えても SIGKILL の後に `ExecStopPost` が Max を書く（安全側） |
| `RestartSec` | 2 | 確定 | 異常終了の後は `ExecStopPost` が Max を書いてから待つので、待つ間は Max（0080 §2.4）。段階 5・T2 で再起動と takeover が通り、終了コード 7 の繰り返しにならなかった |
| `TimeoutStartSec` | 30s | **§2.4 の `READY=1` の測定の後に確定** | `READY=1` は takeover の前に送る（`control_daemon.py`）ので、時間切れでも制御は取っておらず BIOS の制御のまま（再起動を繰り返す）。T1 の起動（`d3c5404`）は `Starting` から `Started`（`READY=1`）まで約 0.51 s。HEAD の形（`-I -m`）では測っていない |

`TimeoutStartSec` の合格の目安は、§2.4 で測った最長が `TimeoutStartSec` の**半分（15 s）未満**とする（推奨。§5 の 3）。
これは unit の猶予の余裕の目安で、安全の値ではない。満たさなければ `enable` せず、0157 §5 の 4 のとおり別の記録で扱う。

**`Nice=` / `CPUWeight=` / `OOMScoreAdjust=`**: **置かない**（推奨）。

- CPU の負荷: T1（普段の使い方）で `deadline_exceeded` 0・`duration_ms` 最大 11 ms、T8 の CPU の全スレッドの負荷を含む区間でも
  `deadline_exceeded` 0・最大 7 ms（期限 250 ms）。overrun を抑える手当てが要る兆しが無い
- メモリ: T1 の fand のメモリの最大は 288.7 MB、OOM による終了は 0。GPU の学習のメモリの圧迫の下では試していない。
  OOM で殺されても SIGKILL と同じ経路で `ExecStopPost` が Max を書き再起動する（0080 §2.4 の「シグナル」の行。安全側）
- 見直すのは、Learned worker を配備するとき（0121 §5 の 1 の soak のやり直しと一緒）か、`deadline_exceeded` や OOM の終了が実運用の
  trace・journal に出たとき

### 2.4 HEAD へ更新した後にやり直す確認

§1.3 のとおり、T1 の後に変わったのは起動の形・Workload Hint・loop の trace の欄・DB・設定の版で、Critical Safety・Guard・Fallback・
hardware backend・引き継ぎ実行部は変わっていない。そこで、変わった部分を通る経路だけを短く確かめる（推奨。§5 の 6）。

**前提**: `fan-policy.yaml` の `supervisor.workload_hint.stage` は **`off`** にする（推奨。§5 の 5）。`off` なら Workload Hint の読み手の
スレッドも証明書の照合も動かず（`_open_workload_hint`）、T1 から増えるのは loop の trace の欄だけになる。worker の unit は起動しない。

| # | 確認 | 手順 | 合格の条件 | 根拠 | 所要 |
|---|---|---|---|---|---|
| R1 | 0080 段階 5 の短い確認（`--backend hwmon`・アイドル・所有者が現地） | 正式の unit を `systemctl start`（`enable` しない）。`kill -STOP` → Max を確かめて `systemctl stop`。`start` → `kill -KILL` → Max → `RestartSec` の後の再起動。`systemctl stop` で BIOS へ戻る。各 1 回 | `kill -STOP` から Max まで約 5 s（T5 のアイドルの 5294 ms と同じ程度。0121 §2.7 の約 11 s 以内）、`kill -KILL` の後に引き継ぎが 3 zone に Max（`applied`）、再起動で takeover、`stop` で `pwmN_enable` が 3 zone とも BIOS の値、`/run/coldaisle` が残る。動いているプロセスの引数に `-I` と `-m` がある（6.9 の手順 5 と同じ見方） | 起動の形と `ExecStart=` が変わったので、unit の経路を 1 回通す。温度は見ない（経路の確認で、0121 §2.7 の熱の試験ではない） | 約 30 分 |
| R2 | `READY=1` までの時間（0157 §5 の 4。6.9 の手順 7） | R1 と R3 の起動のたびに `ExecMainStartTimestampMonotonic` と `ActiveEnterTimestampMonotonic` の差を読む。5 回以上、OS の再起動の直後を 1 回含める | 最長が `TimeoutStartSec` の半分（15 s）未満（§2.3） | `-I -m` の起動の時間を測っていない。`stage: off` では指紋を計算しないので、B1 を設定する版で測り直す（6.9 の手順 7 の注） | R1・R3 に含む（OS の再起動に約 10 分） |
| R3 | 短い soak | 正式の unit で 2 時間。普段の使い方に加えて、CPU の全スレッド 10 分と GPU 600 W 10 分を別々に 1 回ずつ（0121 §2.1 の中止の基準と負荷の決まりのまま） | 0121 §2.3 の表と同じ条件（`duration_ms` 最大 125 ms 未満・`deadline_exceeded` の連続 1 以下・tick の間隔の最大 1500 ms 未満と p99.9 1100 ms 未満・`skipped_slots` 0・watchdog の再起動 0・stale / 書き込みの失敗 / `tach_stall` 0）。trace の `schema_version` が 16 | loop の毎 tick の処理と trace が変わったので、tick の時間と周期を負荷の下で見直す。24 h の T1 の値の確定はそのまま使い、回帰が無いことだけを見る | 約 2.5 時間 |

合計で約 3.5 時間（設定の移行を除く）。どれかが条件を満たさなければ `enable` せず、結果を所有者に示す。結果は `docs/fan-header-mapping.md` の
「0121 の試験の結果」の後に「HEAD での確認」として残す（`/opt` の版・trace の版を書く。実機のホスト名・path は書かない）。

この表は今回の更新（`d3c5404` → HEAD）だけのもので、`enable` の後の `/opt` の更新で何をやり直すかの一般の規定ではない（§5 の 8）。

### 2.5 `enable` までの順序（所有者の手順）

どれも導入先で所有者が行う（`sudo` を含む）。本記録の作業では `/etc`・`/opt`・systemd に触れていない。

| # | 手順 | 参照 |
|---|---|---|
| 1 | 本記録を承認する（§5 の選択を含む。PR のマージで FINAL） | — |
| 2 | （§5 の 1 で推奨を選んだとき）予備確認〜T1 の前の trace と journal を、DB の写しで読み取りだけで数える（§2.1）。見つかれば止めて示し直す | §2.1 |
| 3 | DB に書くものをすべて止め、`/opt/coldaisle` をその時点の main へ更新し、root で venv を同期し、`coldaisle-rollup` で migration（0014〜0016）を当てる。`/etc/coldaisle/internal-telemetry.yaml` の写しの差分も見る | `docs/ubuntu-deploy.md` 2 / 4「コードを更新するとき」/ 4.1 |
| 4 | Control Config を移行する: `fan-policy.yaml` を v13 にして `supervisor.workload_hint: {stage: off}` を足し、`safety.yaml` を v5 にする（両方そろえる）。**同じ編集で** §2.2 の 15 項目の `status` を `confirmed`・`basis` を §2.2 の文言にする | `docs/control-config.md` の v16 / v17 の移行手順 / 6.2 / §2.2 |
| 5 | fand の unit を `-I -m` の形へ置き直し、導入先で書き換えていた行（`WatchdogSec=`・`--t-sensor-metric`）を戻し、`systemd-analyze verify` と `ExecStart=` を確かめる。引き継ぎ実行部の `exit=0` も見る | 6.9 の手順 1〜4 / 6.5 |
| 6 | §2.4 の R1〜R3 を行い、結果を残す。fand の起動ログの `safety_config_is_provisional` が false であることを見る | §2.4 / 6.9 の手順 7 |
| 7 | 承認点 3 の完了を所有者が判断する（0121 §2.10） | 0028 §2.9 |
| 8 | `sudo systemctl enable --now coldaisle-fand`。trace が届き始めたこと（`/api/v1/control/latest` か DB の最新の trace）を見る | 6.5 |
| 9 | `/opt/coldaisle/config/rules.yaml` の `fand_trace_silent.enabled` を true にし、`coldaisle-daemon` を再起動する（8 の後に行う。先に行うと fand が動く前に途絶の warning が届く） | 6.5 の最後 / 0148 §5 の 2 |
| 10 | 数日、0148 の通知と trace の `deadline_exceeded` を見る。`/opt` を更新したら `fand_trace_silent.enabled` が出荷の false に戻っていないかを確かめる | 6.5 の最後 |

## 3. Consequences

### 良くなること

- `enable` の前に残る判断が 1 か所にそろい、所有者が §5 を選べば手順どおりに進められる
- 承認点 2 の値ごとに根拠の試験と `basis` が対応づき、後で値を見直すときの比較の基準になる
- T1 の版と HEAD の差のうち、変わった経路だけを短く確かめるので、24 h の soak をやり直さずに済む

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| D 状態の hang は守れないまま残る | 調べた範囲で兆しが無い。0148 の途絶の warning（夜間は届かない）。チップと GPU の Fan の自身の保護。残ることを `basis` と結果に書く |
| HEAD の tick の時間は 2 h しか見ない | 変わったのは trace の欄だけで、T1 の最大 11 ms は期限の 1/20 以下。`enable` の後も 0148 の通知と trace で見続ける |
| `stage: off` に固定すると Workload Hint の B0 を始められない | B0 を始めるときは 0133 §2.6 の前提に加えて、読み手のスレッドを含めて R3 をやり直す（§5 の 5） |
| `Nice=` などを置かないので、GPU の学習のメモリの圧迫の下の振る舞いは未確認のまま | OOM の終了は安全側（Max と再起動）。Learned を配備するときに見直す |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| D 状態の hang を受け入れず、手当て（ハードウェアの watchdog・EC の fail-safe など）を決めるまで `enable` しない | 調べた範囲で兆しが無く、手当ての候補はどれも新しい部品か新しい安全の値を要する。§5 の 1 の選択肢には残す |
| HEAD で T1（24 h）をやり直す | 変わったのは Critical Safety の外の部分で、値の確定は T1 の結果をそのまま使える。§5 の 6 の選択肢には残す |
| `/opt` を `d3c5404` のまま `enable` する | 0157 の `-I -m` の切り替えは `enable` の前に行うと決まっている（6.9）。また古い版のまま常駐させると、後の更新で同じ確認が要る |
| unit の `TimeoutAbortSec` を短くする | 0158 §5 の 1 で所有者が選ばなかった。新しい安全の値を決めることになる |
| 承認点 2 の status を今の `safety.yaml` v4 のまま先に書き換える | 書き換えは手順 4 の移行でもう一度 `safety.yaml` を開くので、2 回に分けると食い違いの機会が増える。§5 の 7 の選択肢には残す |

## 5. 未決事項（所有者が選ぶ。推奨を先頭に）

| # | 内容 | 選択肢（推奨を先に） | 時期 |
|---|---|---|---|
| 1 | D 状態の hang（0158 §5 の 2）を受け入れるか | **(推奨) 受け入れる。`enable` の前に予備確認〜T1 の前の trace と journal を所有者が数え（§2.1・§2.5 の 2）、何も無ければそのまま** / 受け入れる。予備確認〜T1 の前は数えない（journal の hung_task 0 件と T1 で足りるとする）/ 受け入れず、手当てを決めるまで `enable` しない | 承認点 3 の前 |
| 2 | §2.2 の表の 15 項目を `confirmed` にするか | **(推奨) 表のとおり、値を変えずに `confirmed`。tick 系の 3 つは Learned を配備しない運用に限ると `basis` に書く** / 一部を `provisional` のまま残す（どれを残すかを示す。0121 §2.10 の「すべてそろってから承認点 3」に当たらなくなる） | 承認点 3 の前 |
| 3 | unit の時間の値 | **(推奨) `WatchdogSec` 5s・`TimeoutAbortSec` 5s・`TimeoutStopSec` 10s・`RestartSec` 2 を確定。`TimeoutStartSec` 30s は R2 の最長が 15 s 未満なら確定** / `TimeoutStartSec` も R2 の最長が 30 s 未満なら確定（余裕の目安を置かない）/ どれも暫定のまま `enable` する | `enable` の前（R2 の後） |
| 4 | `Nice=` / `CPUWeight=` / `OOMScoreAdjust=`（0080 §5 の 2） | **(推奨) 置かない。Learned の配備か、overrun・OOM の終了が出たときに見直す** / `OOMScoreAdjust=` だけ負の値で置く（値は別に決める）/ 3 つとも置く（値は別に決める） | `enable` の前 |
| 5 | `enable` のときの Workload Hint の `stage` | **(推奨) `off`。B0 を始めるときに R3 を読み手のスレッドを含めてやり直す** / `observe`（B0）で `enable` し、R3 を `observe` で行う（0133 §2.6 の前提も要る） | 手順 4 |
| 6 | HEAD での確認のやり直しの範囲 | **(推奨) §2.4 の R1〜R3（約 3.5 時間）** / T1（24 h）をやり直す / R1 と R2 だけ（soak を省く） | 手順 6 |
| 7 | 承認点 2 の status を書き換える時期 | **(推奨) 手順 4 の Control Config の移行と同じ編集** / 先に今の `safety.yaml` v4 で書き換えてから移行する | 手順 4 |
| 8 | `enable` の後に `/opt` を更新するとき、何をやり直すかの一般の規定 | **(推奨) 本記録では決めない。最初の更新の前に別の記録で決める（変わった部分で R1〜R3 のどれをやり直すかの決まり）** / 本記録の §2.4 を常にやり直す（毎回 3.5 時間） | `enable` の後の最初の更新の前 |
