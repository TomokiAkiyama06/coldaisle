# 決定記録 0154: T5 の約 11 s の経路を、試験専用の `WatchdogSec` / `watchdog_timeout_ms` 11 s で再現する

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: [`0121`](0121-hardware-test-procedure.md) §2.1 の「試験専用の設定は §2.8 の stall の試験だけで使う」に、
  本記録の T5 の約 11 s の経路の試験（§2.3）を例外として1つ足す。0121 §2.1 のほかの決まり（制御の経路を迂回しない・
  所有者が現地・記録・中止の基準・負荷）と、§2.7 の測り方・温度の合格の条件は**変えない**
- **関連**: [`0119`](0119-safety-values-first-host.md) §2.12 / §2.13 /
  [`0121`](0121-hardware-test-procedure.md) §2.1 / §2.7 / §2.8 / §2.10 / §5 の 3 /
  [`0127`](0127-t5-gpu-steady-450w.md) §2.4 / §2.5 / §5 の 2（Rejected。§5 の 2 の (a) を所有者が選んだ）/
  [`0080`](0080-fand-systemd-unit.md) §2.3 / §2.4 / §2.5 / §5 の 1・3 /
  [`0128`](0128-gpu-fan-curve-service.md) / [`0130`](0130-gpu-fan-quieter-curve.md) §2.2 /
  [`0028`](0028-fan-control-contracts.md) §2.9（承認点 2・3）/
  `docs/fan-header-mapping.md`「0121 の試験の結果」T5 / `docs/ubuntu-deploy.md` 6.5 /
  `deploy/systemd/coldaisle-fand.service` / `src/coldaisle/safety_handoff.py`
- **対象 Issue**: #74

---

## 1. Context

### 1.1 約 11 s の経路とは何か

fand が hang したときに Max になるまでの順序は、0080 §2.5 のとおり
「watchdog の時間切れ → `WatchdogSignal`（SIGABRT）→ `TimeoutAbortSec` で SIGKILL → cgroup の停止 →
`ExecStopPost`（`safety_handoff.py` が root で3 zone に Max を書く）」である。最悪時間は
**`WatchdogSec` 5 s + `TimeoutAbortSec` 5 s + 引き継ぎ実行部の時間（1 s 未満）≒ 約 11 s**（0080 §2.3・0119 §2.12）。
この間、Fan は止まる直前に書いた低い Demand のまま回る。

2026-10-14 の T5（`docs/fan-header-mapping.md`）では、`SIGSTOP` したプロセスにも systemd が watchdog の signal の後に
`SIGCONT` を送るため、fand は SIGABRT（faulthandler）で終わり、`TimeoutAbortSec` を待たずに引き継ぎが走った。
Max までは 12 試行すべてで 5143〜5324 ms（**約 5.2 s の経路**）で合格した。

SIGABRT で終わらないが SIGKILL では終わる hang（**kill できる hang**。ユーザー空間の固まりで SIGABRT の処理まで届かないもの）では
`TimeoutAbortSec` の 5 s も待ち、その後の SIGKILL から Max までは実測 44〜51 ms（予備確認の `kill -KILL`）である。つまりこの故障の最悪は
**5000 + 5000 + 約 50 ms ≒ 10.05 s**（systemd の処理の遅れを除く）で、これが「約 11 s の経路」である。
5.2 s の試行は Max を確かめた直後に負荷を止めたので、その後の約 5 s の上がりは測っていない（0127 §2.5）。
0119 §2.13 は `watchdog_timeout_ms` を `confirmed` にする条件として、この最悪時間の間の温度の上昇が上限までの余裕に
収まることを求める。

### 1.1a 故障のモデル: 本記録が扱うのは kill できる hang だけ

- **kill できる hang**（ユーザー空間で固まり、SIGABRT で終わるか、終わらなくても SIGKILL で終わる）: Max までの時間に
  上限がある（約 5.2 s か約 10.05 s）。本記録の試験が確かめるのはこの経路の**時間の長さと、その間の温度**である
- **D 状態の hang**（hwmon の書き込み・読み戻しが driver の中で戻らない。中断できないカーネルの I/O。0080 §2.5 の最後・
  0127 §2.5）: `TimeoutAbortSec` が約束するのは期限で SIGKILL を**送る**ことだけで、D 状態のプロセスは SIGKILL では
  終わらない。プロセスが実際に終わって `ExecStopPost` が動くまでの時間に**上限が無く**、systemd の deadman ではこの
  故障を守れない。測った 44〜51 ms も kill できるプロセスにだけ当てはまる。さらに `ExecStopPost` が動いても、同じ driver への
  書き込みで止まりうる（0028 未決 6）。**本記録の試験はこの故障を確かめず、確かめられない**（`SIGSTOP` は D 状態を作らない）

### 1.2 本番の設定では再現できない

- SIGABRT で終わらない hang を外から作る手段が無い。`SIGSTOP` は systemd の `SIGCONT` で解け、SIGABRT で終わる
- `WatchdogSignal` を無視される signal に変えると、`SIGCONT` で fand が動き出し、Demand を書き続ける。
  「低い Demand のまま固まる」状態にならない
- `TimeoutAbortSec` の経路そのもの（SIGKILL → 引き継ぎ）は予備確認で 44〜51 ms と測ってある。足りないのは
  **固まった時間が約 10 s 続くときの温度**だけである

### 1.3 これまでの判断

- 所有者は 0127 §5 の 2 で (a)「試験専用の設定で再現する」を選んだ（`safety.yaml` の `watchdog_timeout_ms` と unit の
  `WatchdogSec` を試験の間だけどちらも 11 s にし、(i) CPU 全スレッド・(iii) CPU・(iii) GPU を少なくとも行う）
- 0121 §2.1 は試験専用の設定を §2.8 の T6 にしか認めないので、実施の前にこの記録で例外と中身を決める
  （`docs/fan-header-mapping.md` の T5「残り」）
- 2026-10-16、所有者は「`enable` の前にこの試験を行う」を選んだ。あわせて、unit のタイムアウト
  （`TimeoutAbortSec`・`TimeoutStopSec`・`TimeoutStartSec`・`RestartSec`）は `provisional` のまま進む、アイドルの
  Guard の発火（T1 で 431 回）はこのまま `enable` してよく静音化は別の Issue、`/opt` は d3c5404 のまま `enable` する、
  と決めた。これらは本記録の範囲外である

## 2. Decision

### 2.1 試験の目的

`WatchdogSec` 5 s・`TimeoutAbortSec` 5 s の本番の deadman で起こりうる最悪の時間（約 10.05 s）以上、Fan を止める直前の
低い Demand に固めたまま負荷をかけ続け、その間と Max の後 30 s の温度の上昇が、0121 §2.7 の余裕に収まることを確かめる。
対象は §1.1a の kill できる hang だけで、D 状態の hang の時間と温度は本記録の試験では確かめない（§5 の 8・9）。
**本番の値（`watchdog_timeout_ms` 5000・`WatchdogSec` 5s・`TimeoutAbortSec` 5s）は変えない。** 11 s は固まる時間を
作るための試験の中だけの値で、本番へは持ち込まない。

### 2.2 合格の条件

| 測るもの | 合格の条件 | 根拠 |
|---|---|---|
| `SIGSTOP` から Max（3 zone の `pwmN_enable=0`、20 ms ごとに読む。CLOCK_MONOTONIC）まで | **10100 ms 以上・12000 ms 以下** | 下限: 本番の最悪 5000 + 5000 + SIGKILL から Max の実測の最長 51 ms を 100 ms に切り上げた値。これより短い試行は本番の最悪を覆わないので判定に使わない。上限: 11000 + SIGABRT から Max の実測の最長（T5 の 324 ms、apport が core を受けた予備確認の 0.86 s）に余裕を足した値。超えたら SIGABRT の経路が想定どおりでない |
| 止めている間と Max の後 30 s の温度 | 0121 §2.7 のまま: 開始 + 上昇が上限（`absolute_temp_ceiling_c` 90 °C、T_SENSOR 80 °C）より 5 °C 以上低い（`cpu.package`・`gpu.0.core` は 85 °C 未満、`board.connector_12v2x6` は 75 °C 未満）。**判定は §2.5a の速い読み取りの最大値に、読み取りの間の見落としの分（§2.5a の式の `r × L`）を足した値で行う**。DB の 2500 ms ごとの値は参考にだけ使う | 0119 §2.13・0121 §2.7。本記録で緩めない。2500 ms ごとの値では、CPU が約 5 s で 31〜32 °C 上がる間の最大を見落とし、誤って合格にしうる |
| 止める直前の `effective` | (i) は記録したアイドルの値のまま、(iii) はアイドルの値 + 0.05 以内（0121 §2.7。外れた試行は使わない） | 「低い Demand のまま固まる」最悪の条件にするため |
| 位相（`SIGSTOP` の時刻 − systemd が最後の `WATCHDOG=1` を受けた時刻） | 100 ms 以内の試行を各条件で少なくとも 1 つ（0121 §2.7） | 固まる時間を最も長くするため |

試験の値で固まる時間は約 11.1〜11.9 s になり、本番の最悪（約 10.05 s）より約 1 s 長い。温度の面で保守的な側である。

参考として、Max から3 zone の `fanN_input` が全開の 90% に届くまでの時間も記録する（判定には使わない）。

### 2.3 試験専用の設定（例外の中身）

本番の設定を書き換えず、0121 §2.1 の T6 と同じく**別の場所の設定と unit の drop-in**で差し替える。

1. **設定**: `/etc/coldaisle/control-config-test-t5-11s/` に、本番の `/etc/coldaisle/control-config/` の4ファイルを
   所有者・権限ごと写し、`safety.yaml` の `watchdog_timeout_ms.value` だけを `5000` → `11000` にする（`status` は
   `provisional` のまま。`basis` は足さない）。ほかの値はすべて本番と同じ。**`hardware_write_fail_exit_ms` は 5000 のまま**
   （0080 §2.6 の `hardware_write_fail_exit_ms <= watchdog_timeout_ms` を満たし、T2 で測った書き込みの失敗の経路は変えない）。
   置いた後に `diff -r` で、違いがこの1行だけであることを確かめる
2. **drop-in**: `/etc/systemd/system/coldaisle-fand.service.d/t5-11s.conf` に次の2つだけを書く
   - `ExecStart=` の空行と、本番の `ExecStart`（置いてある unit の値をそのまま。導入先で足した引数も含む）の
     `--config-dir` だけを上の場所に替えた行
   - `WatchdogSec=11s`

   `TimeoutAbortSec`・`TimeoutStopSec`・`TimeoutStartSec`・`RestartSec`・`WatchdogSignal`・`ExecStopPost`・
   `KillMode`・hardening は**変えない**。0080 §2.3 の `WATCHDOG_USEC <= watchdog_timeout_ms` は 11000 どうしで等しく、
   食い違えば fand は終了コード 4 で制御を取らない（安全側）
3. **期間**: 置くのは試験のスクリプトの実行の間だけ。スクリプトの先頭で置いて `systemctl daemon-reload` する。
   終了時（正常・中止・エラー・Ctrl-C のどれでも。`trap` を `EXIT`・`INT`・`TERM`・`HUP` に置き、後片付けの間は
   `INT` を無視して二重に走らせない）の後片付けは、**次の順序を必ず守る**。設定を先に消すと、すでに動いている fand が
   読み込み済みの 11 s の deadman のまま Fan を持ち続け、しかも reload 後の unit の定義は本番に戻ったように見えるためである
   1. **負荷を止める**: CPU / GPU の負荷のプロセスを止めて終わるのを待ち、GPU の電力の上限を既定に戻す
   2. **fand を止める**: fand が `SIGSTOP` で止まっている（`/proc/<main PID>/stat` の状態が `T`）なら、**先に `SIGKILL` を送る**
      （`SIGCONT` で動かさない。動き出した fand は止める前の古い状態で数 tick 書きうるので、止まったまま殺して
      `ExecStopPost` に Max を書かせる。実測 44〜51 ms）。そのあと `systemctl stop coldaisle-fand`（`Restart=always` の再起動を
      止める。再起動していれば正常停止で BIOS へ返す）。`systemctl stop` が `TimeoutStopSec`（10 s）+ 5 s で終わらなければ
      `systemctl kill -s SIGKILL coldaisle-fand` を送ってから、もう一度 `systemctl stop` する
   3. **止まったことを確かめる**: `systemctl is-active coldaisle-fand` が `inactive`（`failed` なら `reset-failed` の後に
      `inactive`）、main PID が 0、3 zone の `pwmN_enable` がすべて `5`（正常停止で BIOS へ返した）か、すべて `0`
      （`SIGKILL` の後の `ExecStopPost` の Max。引き継ぎ記録が残る）であること
   4. 3 が確かめられたときだけ、**drop-in と試験の設定のディレクトリを消して `daemon-reload`** する
   5. **本番の設定に戻ったことを確かめる**（下の 4）

   3 を確かめられないとき（`active` / `deactivating` のまま、`pwmN_enable` に `1` が残る、読めない）は、**drop-in と試験の設定を
   消さずに残し**、端末に大きく警告を出して（「fand が 11 s の試験の設定のまま動いている可能性。drop-in を残した」）、
   0 以外で終わる。所有者はその場で次を行う: `systemctl status coldaisle-fand` と `ps` で fand のプロセスが残っていないか、
   3 zone の `pwmN_enable` と `pwmN` を見る → 残っていれば `systemctl kill -s SIGKILL coldaisle-fand` と `systemctl stop` →
   それでも `pwmN_enable` が `1` なら 0121 §2.1 のとおり `5` を書く → `inactive` を確かめてから、手で drop-in と設定を消して
   `daemon-reload` し、下の 4 を確かめる

   `pwmN_enable` が `0`（Max）で終わったときは、試験の設定を消した後に、本番の設定で fand を `systemctl start` →
   STARTUP の後に `systemctl stop` で BIOS へ返す（0080 §2.8 の正常停止）か、Max のまま置くかを所有者が決める（どちらも安全側）
4. **元に戻ったことの確認**（スクリプトの最後と、所有者が目で）:
   - `systemctl show -p WatchdogUSec --value coldaisle-fand` が `5s`
   - `systemctl show -p ExecStart --value coldaisle-fand` に `--config-dir /etc/coldaisle/control-config ` がある
   - `/etc/systemd/system/coldaisle-fand.service.d/` に `t5-11s.conf` が無く、`/etc/coldaisle/control-config-test-t5-11s/` も無い
   - `systemctl is-enabled coldaisle-fand` が試験の前と同じ（`disabled`）。**drop-in がある間に `enable` しない**
   - 3 zone の `pwmN_enable` が `5`（`systemctl stop` で BIOS へ返した）か `0`（上の 3 の Max）で、`1` が無い

一時的な unit（`systemd-run` など）は使わない。本番の unit と違う形で動かすと、`ExecStopPost`・`KillMode`・権限が
本番と同じであることを別に確かめる必要が出るためである。

### 2.4 試験の条件と回数

`coldaisle-gpu-fan` を 0130 の曲線で動かした状態で、0121 §2.7 のとおり行う（負荷は GPU 600 W と CPU 全スレッドを別々に）。

| 順 | 条件 | 回数 | 目的 |
|---|---|---|---|
| 1 | (ii) アイドル | 有効な試行 1 回 | 同じ日のアイドルの `effective` を記録する（(i)・(iii) の基準）。アイドルの 11 s の上がりも記録する |
| 2 | (i) CPU 全スレッド | 有効な試行 1 回 | 5.2 s の経路で CPU の上がりが最も大きかった（40 → 72 °C） |
| 3 | (iii) CPU 10 分 → CPU 全スレッド | 有効な試行 1 回 | 冷却水が温かい状態からの CPU（43 → 74 °C） |
| 4 | (iii) GPU 600 W 10 分 → GPU 600 W | 有効な試行 1 回 | 温まった状態からの GPU |

(i) GPU 600 W を足すかは §5 の 1。(ii) の負荷の定常は、止める前に Fallback と floor が Demand を上げているので
本記録では行わない（5.2 s の経路の結果で開始の温度の高い側を押さえている）。

有効な試行は、§2.2 の位相・`effective`・時間の下限を満たし、中止の基準で止めなかったもの。満たさない試行は記録して
やり直す。1 条件のやり直しは 2 回まで、それでも得られなければ止めて所有者が扱いを決める。

### 2.5 安全の扱い

- **制御の経路を迂回しない**（0121 §2.1）。Demand は fand の auto で決まり、Guard と Critical Safety を通る。
  Max を書くのは引き継ぎ実行部（Critical Safety の一部）だけで、人も試験のスクリプトも PWM を書かない。LLM は関わらない
- **中止の基準**は 0121 §2.1（`gpu.0.core` は 0130 §2.2 の 85 °C）と同じ: `cpu.package` 88 °C・`gpu.0.core` 85 °C・
  `board.connector_12v2x6` 70 °C。ただし見張りは T5 のスクリプトの DB（API の最新値。2500 ms ごと）ではなく、
  §2.5a の速い読み取りで行い、閾値は §2.5a の式で基準より下げる。**`SIGSTOP` の間は fand 自身の Critical Safety も
  止まっている**ので、この見張りと systemd の deadman だけが頼りになる
- **止めている間に見張りに届いたら**、負荷を止め、すぐに fand の main PID へ `SIGKILL` を送る（管理ソケットは止まった
  fand に届かない）。SIGKILL の後は本番の `TimeoutAbortSec` の経路と同じく `ExecStopPost` が Max を書く（実測 44〜51 ms）。
  その試行は合格にしない
- **`SIGSTOP` から 12000 ms で Max が見えなければ**、同じく `SIGKILL` を送る。試行は無効とし、原因（journal の
  `Watchdog timeout` と `safety_handoff_completed`）を確かめるまで次の条件へ進まない。固まる時間は最長でも約 12 s に限る
- それでも Max にならなければ 0121 §2.1 のとおり `systemctl stop coldaisle-fand`、それも効かなければ
  `pwmN_enable` に `5` を書く
- 所有者が現地にいて、開始と終了を行う（0121 §2.1）

### 2.5a 試験の見張りと判定に使う速い温度の読み取り

試験のスクリプトが、telemetry とは独立に、次を **250 ms ごとに直接読む**（読むだけで書かない。fand・telemetry・DB を通さない）。

| 対象 | 読む場所 | 使い方 |
|---|---|---|
| `cpu.package` | telemetry と同じ hwmon の属性（`config/internal-telemetry.yaml` の driver・label で特定した `tempN_input`） | 判定と見張り |
| CPU の内蔵センサー（k10temp の `Tctl`。`cpu.tctl`） | hwmon の `tempN_input` | 見張りだけ（`cpu.package` と高いほうで見張る。判定には使わない） |
| `gpu.0.core` | NVML の GPU コアの温度（`nvidia-smi` の起動は遅いので、試験のスクリプトから NVML を直接読む） | 判定と見張り |
| `board.connector_12v2x6` | telemetry と同じ hwmon の属性（T_Sensor） | 判定と見張り |

**見落としの分の式**: 読み取りから反応までの遅れ `L` = センサー自身の更新の間隔 `U` + 読み取りの間隔 0.25 s + 反応
（負荷を止めて SIGKILL を送り、`ExecStopPost` が Max を書くまで。実測 44〜51 ms を 0.1 s に丸める）。最大の上昇の速さを `r` とすると、
読み取りの間に見落としうる上がりは `r × L` である。

- **中止の閾値** = 中止の基準 − `r × L` − 余裕 1 °C（切り捨て）
- **合格の判定** = 速い読み取りの最大値 + `r × L` が、§2.2 の上限（85 °C / 75 °C）未満

`r` は、記録にある最も速い上がりより保守的に置く。2500 ms ごとの記録の平均の速さは、(i) CPU の 5.2 s で
40 → 72 °C（約 6 °C/s）、0119 §2.12 の 16 s で GPU +23 °C（約 1.4 °C/s）・T_SENSOR +3 °C（約 0.2 °C/s）である。
立ち上がりの最初の 1 s は平均より速いので、平均の約 2.5〜5 倍を置く。`U` は試験の前に実測する（§2.6 の 1）。
実測するまでの見込みは、k10temp と NVML が 0.25 s 以下、`asusec`（EC）の値は 1 s 以下である。

| 対象 | `r`（°C/s） | `U`（見込み） | `L` | `r × L` | 中止の基準 | 中止の閾値 | 合格の判定（速い読み取りの最大値） |
|---|---:|---:|---:|---:|---:|---:|---|
| CPU（`Tctl` と `cpu.package` の高いほう） | 15 | 0.25 s（`Tctl`） | 0.6 s | 9 °C | 88 | **78 °C** | `cpu.package` の最大 + 15 × (`U` + 0.35) < 85（`U` = 1 s なら 64.75 °C 未満、`U` = 0.25 s なら 76 °C 未満） |
| `gpu.0.core` | 5 | 0.25 s | 0.6 s | 3 °C | 85 | **81 °C** | 最大 + 3 < 85（82 °C 未満） |
| `board.connector_12v2x6` | 1 | 1 s | 1.35 s | 1.35 °C | 70 | **67 °C** | 最大 + 1.35 < 75（73.65 °C 未満） |

- `U` を実測したら、その値で上の表を計算し直し、試験の前に所有者に示す。各試行の後に、速い読み取りから求めた
  実際の最大の上昇の速さ（250 ms の差分）が `r` を超えていたら、その `r` で閾値を下げてから次の条件へ進む
- `cpu.package` の EC の更新が遅い（`U` が大きい）と、合格の判定の幅が狭くなる。CPU の見張りは更新の速い `Tctl` も
  使うので中止は遅れないが、判定は `cpu.package` の値で行う（0121 §2.7 の上限が `cpu.package` に対するものであるため）。
  `cpu.package` の `U` が 1 s を超えたら §5 の 5

### 2.5b 止めている間の温度の見積もり

固まる時間は約 11.1〜11.9 s（§2.2）。記録から見積もる最高は次のとおり。

| 条件 | 根拠 | 見積もりの最高 |
|---|---|---|
| (i) CPU 全スレッド | 5.2 s の経路で 40 → 72 °C（Max の後 30 s まで）。0119 §2.12 では 16 s の窓で +31 °C、60 s で +33 °C と、最初の数秒の段の後は遅い（チップと冷却水の差が先に開き、冷却水は分の単位で上がる） | 40 + 33 + 余裕 3 ≒ **76 °C** |
| (iii) CPU | 5.2 s の経路で 43 → 74 °C。冷却水が温かいぶん開始が高い | 43 + 33 + 余裕 3 ≒ **79 °C**（中止の閾値 78 °C を超えうる） |
| (iii) GPU 600 W | 5.2 s の経路で 49 → 68 °C。0119 §2.12 の 16 s で +23 °C。GPU は自身の Fan（0130 の曲線）で冷え、筐体の Fan を固めても 600 W の定常は 78 °C（0130 §2.4 の 2） | 49 + 23 ≒ **72 °C**（定常の 78 °C を超えない） |
| T_SENSOR | 5.2 s の経路の最高 46 °C、16 s で +3 °C | **約 50 °C** |

GPU と T_SENSOR は、中止の閾値と合格の判定の両方に余裕がある。CPU は、(i) が閾値の内側、(iii) が閾値に届きうる見積もりで、
合格の判定（`cpu.package` の `U` しだいで 64.75〜76 °C 未満）にはどちらも届きうる。中止すれば、その試行は合格にしない（§2.5）。
CPU の条件が中止や判定の上限に届いたら、その条件は負荷を下げる（CPU の半分のスレッド）か外すかを所有者が決める（§5 の 6）。
届くと分かっている負荷で繰り返さない。

### 2.6 手順の骨子

所有者の作業用のディレクトリ（リポジトリの外）に、T5 のスクリプトを元にした試験のスクリプトを用意し、内容を事前に
示してから所有者が実行する（`! sudo bash <作業用のディレクトリ>/deploy/t5-11s.sh ii-idle i-cpu iii-cpu iii-gpu600`）。
スクリプトの中身はリポジトリに置かない（実機の識別子を含むため。AGENTS.md ルール 10）。

1. 前提の確認: fand が止まっていて `disabled`、本番の unit に drop-in が無い、`coldaisle-gpu-fan` が 0130 の曲線で
   動いている、ほかの CPU 負荷（Docker のコンテナなど）が無い、CPU 電力が 40 W 未満。あわせて、§2.5a の各センサーを
   50 ms ごとに 60 s 読み（アイドルで 30 s、CPU 全スレッドで 30 s。fand は BIOS のまま）、値が変わる間隔から `U` を、
   250 ms の差分から負荷の始まりの上昇の速さを実測して §2.5a の表を計算し直す
2. §2.3 の 1・2 を置き、`daemon-reload`。`WatchdogUSec` が `11s`、`ExecStart` の `--config-dir` が試験の場所であることを確かめる。
   `trap` で終了時の後片付け（§2.3 の 3・4）を登録する
3. 条件ごとに: `systemctl start`（`enable` しない）→ STARTUP の 20 s → 条件の準備（落ち着きの判定・(iii) の 10 分と
   アイドル + 0.05 への戻り）→ `WatchdogTimestampMonotonic` が変わった直後に `SIGSTOP` → (i)・(iii) はその直後に負荷を始める
   → 20 ms ごとに `pwmN_enable` を読み Max を待つ（§2.5 の 12000 ms と、§2.5a の 250 ms ごとの見張り）→ Max の後に負荷を止め 30 s 見る →
   `systemctl stop` で BIOS へ返す → 60 s 置く
4. 後片付け: §2.3 の 3 の順序（負荷 → fand の停止 → 停止の確認 → 設定を消す → 本番の確認）で行い、結果を表示する。
   DB・journal（`__MONOTONIC_TIMESTAMP` つき）を書き出す

### 2.7 記録の置き場所

- 生の記録（DB の書き出し・journal・印の時刻・§2.5a の速い読み取りの全点（CLOCK_MONOTONIC つき））は、これまでの T5 と同じく所有者の作業用のディレクトリに
  `t5-11s-<HHMM>-*` の名前で残す（リポジトリには置かない）
- 結果は `docs/fan-header-mapping.md` の「0121 の試験の結果」T5 に「約 11 s の経路（0154）」の表として足す
  （条件・位相・`SIGSTOP` から Max・止める直前の `effective`・開始 → 最高の温度・判定）。実機の識別子は書かない

### 2.8 合格した後の確定

- 4 条件すべてで §2.2 を満たしたら、0121 §2.10 のとおり、`watchdog_timeout_ms` 5000 の `status` を `confirmed`・`basis` を
  0121 §2.7・本記録・`docs/fan-header-mapping.md` の T5 にする変更を、所有者の承認（0028 §2.9 の**承認点 2**）を得て
  `/etc/coldaisle/control-config/safety.yaml` に当てる。確定するのは 5000 で、試験の 11000 ではない
- この確定は **kill できる hang の経路（§1.1a）に対する条件つき**であり、`basis` にもそう書く。D 状態の hang で Max までの
  時間に上限が無いことは確定の範囲の外で、その残るリスクを受け入れるかは §5 の 8 で所有者が決める。受け入れないなら
  `confirmed` にしても承認点 3 と `enable` には進まない
- この確定は「`TimeoutAbortSec` が 5 s 以下」を前提にする。unit のタイムアウトは `provisional` のまま進む（§1.3）が、
  後で `TimeoutAbortSec` を 5 s より長くするなら、本記録の試験をやり直す
- 0121 のほかの試験の値の確定とそろってから**承認点 3** を完了とし、`enable` は所有者が判断する（0121 §2.10）
- 満たさない条件があれば、値も設定も本記録では変えない。§5 の 3 のとおり別の決定記録で案を出す。それまで `enable` しない

## 3. Consequences

### 良くなること

- 0119 §2.13 が `watchdog_timeout_ms` に求める「最悪時間の間の温度」を、本番の最悪より約 1 s 長い時間で実測できる
- 本番の値・unit・`safety.yaml` を書き換えずに済む。違いは `diff -r` で1行・drop-in で2つの指令だけ
- 引き継ぎ（Max を書く経路）・`KillMode`・権限は本番のまま試すので、5.2 s の経路の結果と同じ部品で比べられる

### 悪くなること・その緩和

- 試験の間は deadman が 11 s に延び、hang から Max までが本番より遅い。緩和: 所有者が現地・`enable` しない・スクリプトの
  実行の間だけ・止めている間の見張りと 12000 ms での SIGKILL・`trap` での後片付けと確認
- SIGKILL で終わる本番の最悪の経路の「時間」は、この試験では通らない（SIGABRT で終わる）。緩和: SIGKILL から Max は
  予備確認の 44〜51 ms で足りており、本記録で要るのは固まる時間の長さだけである。時間の下限 10100 ms で本番の最悪を覆う
- **D 状態の hang は守れないまま残る**（§1.1a）。試験に合格しても、hwmon の driver の中で書き込みが戻らなければ、Fan は最後の
  低い Demand のまま上限の無い時間残りうる。緩和は本記録の中には無く、§5 の 8・9 で所有者が受け入れか手当てを決める
- drop-in を消し忘れると deadman が 11 s のまま残る。緩和: `trap`・§2.3 の 4 の確認・`enable` の前に
  `systemctl cat coldaisle-fand` で drop-in が無いことを所有者が確かめる

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| `WatchdogSignal` を無視される signal にして `TimeoutAbortSec` の経路を本物で通す | systemd の `SIGCONT` で fand が動き出し、Demand を書き続ける。「低い Demand のまま固まる」状態にならない |
| `SIGCONT` の後にもう一度 `SIGSTOP` する | 動き出した瞬間に fand が書くかどうかが競合で決まり、固めた Demand が保証できない |
| `WatchdogSec` / `watchdog_timeout_ms` を 10 s にする | 固まる時間が約 10.1〜10.9 s で、本番の最悪（約 10.05 s）との余裕が小さい。0127 §5 の 2 (a) の 11 s のままにする |
| 一時的な unit（`systemd-run`）で動かす | `ExecStopPost`・`KillMode`・権限・hardening が本番と同じであることを別に確かめる必要がある（§2.3） |
| 本番の `/etc/coldaisle/control-config/safety.yaml` と unit を一時的に書き換える | 戻し忘れが本番の値に残る。0121 §2.1 の T6 と同じく別の場所と drop-in にする |
| 5.2 s の結果から 10 s の上がりを見積もる（0127 §5 の 2 (c)） | 5 s の負荷で `cpu.package` が +31〜32 °C 上がり、その後の上がり方の根拠が無い（0127 §2.5）。所有者は (a) を選んだ |
| 最悪時間そのものを短くする（`TimeoutAbortSec` を短く、または `WatchdogSignal=SIGKILL`。0127 §5 の 2 (b)） | 0080 §5 の 1・3 の決定に当たり、所有者はタイムアウトを `provisional` のまま進むと決めた（§1.3）。試験が不合格なら §5 の 3 で改めて出す |

## 5. 未決事項

| # | 内容 | 選択肢（推奨を先に） | 時期 |
|---|---|---|---|
| 1 | (i) GPU 600 W（アイドルから先に止めて GPU 600 W を始める）も行うか | **(推奨) 行う**: 1 回で 10〜30 分（落ち着きの判定を含む）。5.2 s の経路では GPU 29 → 39 °C と余裕が大きく危険は小さいが、0121 §2.7 の (i) の2つがそろう / 行わない: 所有者の指定の3条件だけにする（GPU の上がりは (iii) で押さえる） | 承認の時 |
| 2 | 同じ日のアイドルの `effective` を、試験の設定の (ii) アイドルで取るか | **(推奨) 取る**（§2.4 の 1。11 s のアイドルは危険が小さく、(i)・(iii) の基準を同じ日の値にできる）/ T1 の記録のアイドルの値を使う（T1 の後に Guard の発火の扱いを変えていないが、日と室温が違う） | 承認の時 |
| 3 | どれかの条件が §2.2 の温度を満たさなかったときの次の案 | **(推奨) 別の決定記録で、最悪時間を短くする案（`TimeoutAbortSec` を短く、または `WatchdogSignal=SIGKILL`。0080 §5 の 1・3）を先に出し、それでも足りなければ floor を強める案を出す。それまで `enable` しない** / 余裕の基準（5 °C）を見直す案を出す（安全の基準を緩めるので推奨しない） | 結果の後 |
| 4 | 時間の下限 10100 ms・上限 12000 ms でよいか | **(推奨) このまま**（§2.2 の根拠）/ 下限を 11000 ms（0127 の丸めた値）にする: 下限が試験の設定そのものになり、位相と systemd の遅れで有効な試行が得にくい | 承認の時 |
| 5 | `cpu.package`（EC）の更新の間隔 `U` が 1 s を超えたときの CPU の判定 | **(推奨) 判定の幅（§2.5a の式）が狭くなっても `cpu.package` で判定し、足りなければ §5 の 6 の扱いにする** / `Tctl` で判定する（0121 §2.7 の上限は `cpu.package` に対するもので、別のセンサーへ移すと上限の意味が変わる） | `U` の実測の後 |
| 6 | 見積もり（§2.5b）や結果が中止の閾値・合格の判定に届いた CPU の条件の扱い | **(推奨) 半分のスレッドに下げてやり直し、それでも届けば条件から外し、外した理由と CPU 自身のクロックの制限への委ね方を結果に書く** / 中止の基準を上げる（安全の基準を緩めるので推奨しない） | 試験の中 |
| 7 | §2.5a の `r`（CPU 15・GPU 5・T_SENSOR 1 °C/s）の置き方 | **(推奨) このまま置き、§2.6 の 1 と各試行の実測で大きいほうへだけ直す** / §2.6 の 1 の実測の値そのものを使う（1 回の測りで最悪を覆う根拠が無い） | 承認の時 |
| 8 | **D 状態の hang（§1.1a）の残るリスクを所有者が受け入れるか** | **(推奨) 次の条件で受け入れる**: 9 の調べで D 状態の兆しが無いこと、§2.8 の確定を kill できる hang に限ると明記すること、D 状態では下の BIOS / EC・CPU・GPU の自身の保護に頼る範囲を所有者が承認すること / 受け入れず、9 の (b) 以降の手当てを決めるまで `enable` しない | 承認の時（§2.8 の確定の前） |
| 9 | D 状態への手当てと調べ方 | (a) **(推奨) 調べる**: 予備確認から T1 までの journal と `dmesg` で `hung_task`（`blocked for more than`）・fand の `duration_ms` の外れ値・`write_failure` / `readback_mismatch` を数え、T1 の 24 h の tick の最大の時間（hwmon の 1 回の書き込みと読み戻しの時間）を示す。運用では `kernel.hung_task_timeout_secs` の警告を通知に足すかを別に決める / (b) **別のプロセスから書く**（root の見張りが PWM を Max にする）: 同じ driver・同じ SuperIO のロックで止まりうるので D 状態には効かない見込みが高く、Fan へ書く経路も増える（AGENTS.md ルール 2）。推奨しない / (c) **BIOS / EC・チップの自身の保護に頼る範囲を明示する**: SuperIO（nct6799）の fail-safe（自動制御へ戻る仕組み）が有るかは未確認で、有ると見なさない。CPU は自身の温度の上限でクロックを下げ、GPU は自身の Fan（`coldaisle-gpu-fan` は fand と別のプロセス・別の driver（NVML））と温度の保護を持つ。冷却水と筐体の分の単位の温度は守られない / (d) **最悪を観測で区切る**: Critical Safety とは別に、取り込みのルールエンジンが fand の trace の途絶を通知する案（PR #337 で提案中）があれば、人が気づくまでの時間に置き換えられる | 8 と同時 |
