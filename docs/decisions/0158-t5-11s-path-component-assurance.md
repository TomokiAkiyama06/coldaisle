# 決定記録 0158: T5 の約 11 s の経路を、実機の試験の代わりに、実測済みの部品とテスト・解析で担保する

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: [`0121`](0121-hardware-test-procedure.md) §2.7 T5 のうち、約 11 s の経路（SIGABRT で終わらず `TimeoutAbortSec` を
  待つ経路）を実機で測ることを、本記録の部品ごとの根拠・テスト・解析で補う。0121 §2.7 の測り方・合格の条件・約 5 s の経路の
  合格は**変えない**。0121 は書き換えない
- **関連**: [`0154`](0154-t5-11s-path-test-exception.md)（Rejected。実機で再現する案）/
  [`0127`](0127-t5-gpu-steady-450w.md) §2.5 / §5 の 2（Rejected。(c)「試験をせず別の根拠で見積もる」に当たる）/
  [`0119`](0119-safety-values-first-host.md) §2.12 / §2.13 /
  [`0080`](0080-fand-systemd-unit.md) §2.3 / §2.5 / §5 の 1・3・4 /
  [`0028`](0028-fan-control-contracts.md) §2.7 / §2.9（承認点 2・3）/ 未決 6 /
  [`0118`](0118-hwmon-backend-label-less-headers.md) §2.6 /
  `docs/fan-header-mapping.md`（段階 5 の一部・承認点 3 の確認項目・「0121 の試験の結果」の T2・T5・T1）/
  `deploy/systemd/coldaisle-fand.service` / `src/coldaisle/safety_handoff.py` / `src/coldaisle/control_daemon.py`
- **対象 Issue**: #74

---

## 1. Context

### 1.1 約 11 s の経路

fand が hang したときに Max になるまでの順序は、0080 §2.5 のとおり
「watchdog の時間切れ → `WatchdogSignal`（SIGABRT）→ `TimeoutAbortSec` で SIGKILL → cgroup の停止 →
`ExecStopPost`（`safety_handoff.py` が root で3 zone に Max を書く）」である。

- **約 5 s の経路**: SIGABRT で fand が終わり、`TimeoutAbortSec` を待たずに引き継ぎが走る。T5 で実機で測り、合格した
- **約 11 s の経路**: SIGABRT では終わらないが SIGKILL では終わる hang（**kill できる hang**）。`TimeoutAbortSec` の 5 s も待つので、
  `WatchdogSec` 5 s + `TimeoutAbortSec` 5 s + SIGKILL から Max（実測 44〜51 ms）≒ 約 10.05 s（systemd の処理の遅れを除く）、
  丸めて約 11 s（0119 §2.12）。その間、Fan は止まる直前に書いた低い Demand のまま回る
- **D 状態の hang**（driver の中で戻らない中断できない I/O）: SIGKILL でも終わらず、Max までの時間に上限が無い（0080 §2.5 の最後・
  0028 未決 6・0154 §1.1a）。本記録の範囲の外で、残るリスクとして §5 の 2 に置く

0119 §2.13 は `watchdog_timeout_ms` を `confirmed` にする条件として、hang から Max までの実際の最悪時間（約 11 s）の間の温度の
上昇が上限までの余裕に収まることを求める。0121 §2.7 はこれを T5 で実機で測ることにした。

### 1.2 これまでの判断

- 2026-10-14 の T5 は 12 試行すべてが約 5.2 s の経路で終わり、約 11 s の経路は残りになった（0127 §2.5・`docs/fan-header-mapping.md` の T5）
- 所有者は 0127 §5 の 2 で (a)「試験専用の設定で再現する」を選び、その中身を 0154 で提案した
- 2026-10-16、所有者は 0154 を取り下げた。レビューで条件を厳しくした結果、実機では実質的に試せない形になったため
  （0154 の「取り下げの理由」: 更新の間隔の文書の上限が無いセンサー・CPU の中止の閾値 70 °C・`R` と熱の遅れに新しい値が要る）。
  **T5 の約 11 s の経路は実機では試さない**
- 残る道は 0127 §5 の 2 の (b)（最悪時間を短くする）か (c)（試験をせず別の根拠で担保する）である。本記録は (c) の形で、
  経路を部品に分け、部品ごとに実測とテストの根拠を示し、測っていない部分を残るリスクとして所有者に示す。(b) は §5 の 1 の選択肢に残す

## 2. Decision

### 2.1 範囲

- 約 11 s の経路を、§2.2 の部品に分けて担保する。各部品の根拠は既存の記録と試験の結果から引き、新しい実機の試験は行わない
- **新しい安全の値は決めない。** `watchdog_timeout_ms` 5000・`WatchdogSec` 5s・`TimeoutAbortSec` 5s・`hardware_write_fail_exit_ms` 5000 は
  今のまま（`provisional` のものは `provisional` のまま）
- 本記録だけで `watchdog_timeout_ms` を `confirmed` にはしない。`confirmed` にできるのは、所有者が §2.4 の残るリスクを受け入れ（§5 の 1）、
  §2.5 の足すテストが入った後に、0028 §2.9 の承認点 2 を得たときだけである（§2.6）

### 2.2 経路の部品と根拠

| # | 部品 | 実測の根拠 | テストの根拠（既存） | 約 11 s の経路で実機で通っていないこと |
|---|---|---|---|---|
| P1 | **watchdog の検知**（`WatchdogSec` 5 s。heartbeat は tick の完了の後にだけ送る） | 段階 5 の一部: `kill -STOP` から 5.05 s で SIGABRT（simulated）、Top の確認で最後の heartbeat から 5.0 s（hwmon）。T5 の 12 試行: 位相 0.8〜8.0 ms で `SIGSTOP` から Max まで 5143〜5324 ms（検知と SIGABRT と引き継ぎを含む） | `tests/test_control_loop.py` の不変条件 19・21〜24・26（完了しない tick は heartbeat を送らない・heartbeat は保存より前・`WATCHDOG_USEC` の検査）、`tests/test_fand_exit_codes.py`（`WATCHDOG_USEC` が `watchdog_timeout_ms` より長ければ終了コード 4 で起動しない）、`tests/test_deploy_templates.py`（`Type=notify`・`NotifyAccess=main`・`--require-watchdog`） | なし（検知は経路によらず同じ） |
| P2 | **SIGABRT**（`WatchdogSignal=SIGABRT`・`faulthandler` が全スレッドの traceback を残して終わる・`LimitCORE=0`） | T5 の 12 試行すべてで、systemd が止まったプロセスにも SIGABRT を届け fand が終わった。SIGABRT から引き継ぎの完了まで 0.1 s（Top の確認）〜0.86 s（simulated、apport が core を受けた）。実行部の開始の遅れ 0.06〜0.83 s（0080 §5 の 3） | `tests/test_fand_exit_codes.py` の `test_main_enables_faulthandler_for_every_thread`、`tests/test_deploy_templates.py`（`WatchdogSignal=SIGABRT`・`LimitCORE=0`） | 約 11 s の経路は、この部品で**終わらない**場合である（§2.3） |
| P3 | **`TimeoutAbortSec` の SIGKILL**（5 s） | 実機で通したことは無い（外から作る手段が無い。0154 §1.2）。期限で SIGKILL を送ることは systemd の文書の振る舞い（`systemd.service(5)` の `TimeoutAbortSec=`）。SIGKILL の後は P4 と同じ経路で、`kill -KILL` で 44 ms（simulated）・51 ms（hwmon、3 zone に Max） | `tests/test_deploy_templates.py` の `test_fand_states_every_timeout`（明示してあることだけ。値の上限は見ない。§2.5 の T1） | **この部品の時間（5 s の待ち）と、その間の温度** |
| P4 | **cgroup の停止と引き継ぎ**（`KillMode=control-group`・`ExecStopPost=+/usr/bin/python3 -I -S …/safety_handoff.py`。root・venv に依存しない・記録があれば Max、無ければ何もしない） | T2: 書き込みの失敗の 2 回の終了コード 7 で、引き継ぎの実行部が3 zone に Max を書いた（`applied`）。T5 の 12 試行で `safety_handoff_completed`。段階 5: 再起動の連続で毎回 `ExecStopPost` が走った。`kill -KILL` の後にも走った（P3） | `tests/test_safety_handoff.py`（記録の検査・3 zone・一部の zone の失敗で他を止めない・読み戻し・標準ライブラリだけ・`-I -S` で環境に左右されない、など）、`tests/test_deploy_templates.py`（`KillMode`・`ExecStopPost` の形と権限） | なし（SIGABRT でも SIGKILL でも同じ実行部） |
| P5 | **引き継ぎの Max**（3 zone の `pwmN_enable` を全速に、manual なら `pwmN` を最大に。読み戻しで確かめる） | T5 は 3 zone の `pwmN_enable=0` を 20 ms ごとに読んで Max を確かめた（この driver は manual の 255 を `0` と読み返す。0118 §2.6）。Top の確認で `pwm2=89` から全速。T2 で3 zone `applied` | `tests/test_safety_handoff.py` の `test_handoff_writes_only_full_speed_enable_and_max_pwm`・`test_manual_readback_must_show_max_pwm` ほか | なし |
| P6 | **Max になるまでの時間** | 約 5 s の経路: 5143〜5324 ms（T5、実測）。約 11 s の経路: P1 の 5 s + P3 の 5 s + P4・P5 の 44〜51 ms ≒ 約 10.05 s（**計算。実測ではない**）。D 状態: 上限なし | — | 合計の時間の実測 |

部品ごとに見ると、約 11 s の経路で実機で通っていないのは **P3 の 5 s の待ちと、その間（低い Demand のまま）の温度**だけである。
P3 の後の SIGKILL から Max までは P4・P5 と同じ部品で、`kill -KILL` で実測してある。

### 2.3 約 11 s の経路が起こる条件（解析）

約 11 s の経路は「SIGABRT では終わらないが SIGKILL では終わる」ときだけ起こる。fand は SIGABRT に Python の処理関数を置かず、
`faulthandler`（C の処理関数）が traceback を書いてから元の処理に戻して signal を出し直す。SIGTERM・SIGINT の処理関数は
Python で置く（`control_daemon.py`）が、SIGABRT には置いていない（Python の `faulthandler` の実装の振る舞い。§2.5 の T2・T3 で前提をテストに固定する）。したがって、ユーザー空間で固まっただけ（無限ループ・
Python のロックの待ち・C の呼び出しの中の待ち）なら SIGABRT で終わり、T5 と同じ約 5 s の経路になる。

SIGABRT で終わらず SIGKILL で終わりうるのは、次のような場合と考えられる（**どれも実機で確かめていない仮説**）:

- `faulthandler` の traceback の書き出し（stderr = journal へのパイプ）が、journald の停止などでパイプが詰まって戻らない
- カーネルの中の、SIGKILL でだけ起こされる待ち（`TASK_KILLABLE`）に入っている
- SIGABRT の core の pipe（導入先の `kernel.core_pattern` は apport）の処理が長引く（段階 5 で 0.86 s を観測）

D 状態（中断できない I/O）は SIGKILL でも終わらないので約 11 s の経路ではなく、§1.1 の上限の無い場合に入る。
`docs/fan-header-mapping.md` の予備確認から T1 の 24 h（fand の再起動 0 回）までの記録に、約 11 s の経路に当たる終わり方は
残っていない（journal を数え直したものではなく、観測されていないことは起こらないことの証明でもない。§5 の 2 の調べで journal も数える）。

温度について使える根拠は次のとおりで、**どれも約 11 s の間の実測ではない**:

- T5 の約 5 s の経路の最高（Max の後 30 s まで）: (i) CPU 全スレッド 40 → 72 °C、(iii) CPU 43 → 74 °C、(iii) GPU 600 W（0130 の曲線）は負荷の間の最高が CPU / GPU / 12V-2x6 で 77 / 78 / 49 °C。
  0121 §2.7 の上限（CPU・GPU 85 °C 未満、12V-2x6 75 °C 未満）の内側
- 0119 §2.12: CPU と GPU のチップの温度は負荷の立ち上がりで数秒のうちに大きく上がり、その速さは Fan の回転数ではほぼ変わらない。
  Fan が効くのは冷却水と筐体の分の単位の温度で、数秒の間のチップの保護は CPU（95 °C）と GPU（T.Limit）のクロックの制限が担う
- 0154 §2.5b の見積もり（約 11 s の固まりの後の最高）: (i) CPU 約 76 °C・(iii) CPU 約 79 °C・(iii) GPU 約 72 °C・T_SENSOR 約 50 °C。
  0119 §2.12 の BIOS の制御の下の 16 s・60 s の窓の上がりから置いた値で、Fan を低い Demand に固めたときの実測ではない
- GPU の Fan は fand と別のプロセス（`coldaisle-gpu-fan`、NVML。0128）が動かし、fand の hang の影響を受けない

### 2.4 実機で確かめていない残りの危険と、受け入れる理由（所有者の受け入れの案）

| # | 残りの危険 | 受け入れる理由（案） |
|---|---|---|
| X1 | 約 11 s の経路の間の温度を実測していない。0121 §2.7 の「開始 + 上昇が上限より 5 °C 以上低い」を、この経路では測定で示せない | 起こる条件が狭い（§2.3。ユーザー空間の固まりは約 5 s の経路になる）。約 5 s の経路の最高は上限の内側で、増える約 5 s はチップの温度より冷却水と筐体の温度に効く（0119 §2.12）。チップは自身のクロックの制限で守られ、GPU の Fan は別のプロセスで動く。見積もり（0154 §2.5b）は上限 85 °C の内側 |
| X2 | `TimeoutAbortSec` の期限で systemd が SIGKILL を送ることを、この機械で通していない | systemd の文書の振る舞いで、SIGKILL の後の経路（P4・P5）は `kill -KILL` で実測してある。`TimeoutAbortSec` の値が変わらないことは §2.5 の T1 のテストで守る |
| X3 | §2.3 の仮説の終わり方（journal のパイプの詰まりなど）が実際に起こるか・起こったときの時間を確かめていない | 記録に残っていない。起こっても kill できる限り `TimeoutAbortSec` の 5 s で上限がある |
| X4 | D 状態の hang（上限なし） | **本記録では受け入れない**。0154 §5 の 8・9 の論点を §5 の 2 に引き継ぐ |

X1〜X3 を受け入れるかは所有者が決める（§5 の 1）。受け入れないなら §2.6 のとおり `confirmed` にしない。

### 2.5 テストで担保する範囲

**既存のテストで担保していること**（§2.2 の「テストの根拠」）: heartbeat を送る条件と `WATCHDOG_USEC` の検査（P1）、
`faulthandler` を有効にすること・`WatchdogSignal=SIGABRT`・`LimitCORE=0`（P2）、`KillMode`・`ExecStopPost` の形・引き継ぎの
実行部の記録の検査と Max の書き込みと読み戻し（P4・P5）、`TimeoutAbortSec` などを明示すること（P3 の一部）。

**足りないこと**（本記録の PR はドキュメントだけなので、次の実装の PR の範囲とする。§5 の 3）:

| # | 足すテスト | 守ること |
|---|---|---|
| T1 | `tests/test_deploy_templates.py`: fand の unit の `WatchdogSec` + `TimeoutAbortSec` が、本記録の解析の前提（`WatchdogSec` 5 s・`TimeoutAbortSec` 5 s、合計 10 s）を超えない | 解析（§2.2 の P6）の前提を、unit の変更で黙って壊さない。値を変えるなら本記録の解析をやり直す。テストの上限は本記録の前提の値で、新しい安全の値ではない |
| T2 | `tests/test_fand_exit_codes.py`（または相当）: fand の起動の後、`signal.getsignal(SIGABRT)` が既定（`SIG_DFL`）のままで、Python の処理関数を置いていない | §2.3 の「ユーザー空間の固まりは SIGABRT で終わる」の前提。Python の処理関数は main thread がバイトコードに戻るまで動かないので、置くと約 5 s の経路が約 11 s の経路に変わりうる |
| T3 | `tests/test_fand_exit_codes.py`（または相当）: `faulthandler` を有効にした後に、fand が SIGABRT を `signal.pthread_sigmask` などで塞がない（塞ぐ処理が無いこと） | 同上（塞がれた SIGABRT は届かず、SIGKILL の経路になる） |

systemd を使った経路全体のテスト（コンテナや VM の systemd で `SIGSTOP` と SIGKILL の経路を通す）は CI では行わない
（`@pytest.mark.hardware` 相当。実機・systemd の環境が要る）。行うかは §5 の 4。

### 2.6 0121 の T5 の扱いの変化

0121 は書き換えず、本記録から次のとおり補う（0121 には `Superseded by` の追記だけを行う）。

- T5 の結果は「**SIGABRT で終わる約 5 s の経路は実機で合格**（`docs/fan-header-mapping.md` の T5）、**約 11 s の経路は実機では試さず、
  本記録の部品ごとの根拠・テスト・解析で担保する**」とする
- `watchdog_timeout_ms` 5000 は、次がそろったときだけ、0121 §2.10 のとおり承認点 2 で `confirmed` にできる:
  1. 所有者が §2.4 の X1〜X3 を受け入れる（§5 の 1）
  2. §2.5 の T1〜T3 が入る（§5 の 3）
  3. `basis` に「0121 §2.7（約 5 s の経路の実測）・本記録（約 11 s の経路は部品と解析。kill できる hang に限る）・`docs/fan-header-mapping.md` の T5」と書き、
     D 状態の hang（X4）が範囲の外であることを明記する
- 受け入れないときは、`watchdog_timeout_ms` は `provisional` のままで承認点 3 と `enable` に進まない。最悪時間を短くする案（0127 §5 の 2 の (b)）を
  別の決定記録で出す（§5 の 1）
- 承認点 3 の完了と `enable` は、これまでどおり 0121 §2.10 のとおり所有者が判断する。D 状態の扱い（§5 の 2）が決まるまで `enable` しない
- 本記録が FINAL になったら、`docs/fan-header-mapping.md` の T5 の「残り」と「残り」の節の約 11 s の経路の行を、本記録への参照に直す

## 3. Consequences

### 良くなること

- 0154 で実機では実質的に試せなくなった約 11 s の経路について、何を測ってあり何を測っていないかが部品ごとに分かる
- 試験専用の 11 s の設定を本番の機械に置かずに済み、置き忘れや、Critical Safety が止まった状態で負荷をかける危険が無い
- 解析の前提（unit の時間・SIGABRT の扱い）をテストで固定し、後の変更で黙って崩れないようにする

### 悪くなること・その緩和

- **約 11 s の間の温度は測らないまま残る**（X1）。緩和: 起こる条件の解析（§2.3）・約 5 s の経路の実測・チップと GPU の自身の保護。
  受け入れは所有者の判断にし（§5 の 1）、受け入れないなら `confirmed` にしない
- 0119 §2.13 と 0121 §2.7 が求めた「実測」を、この経路については解析で置き換える。緩和: `basis` に条件つきであることを書く（§2.6）。
  後で最悪時間や unit の時間を変えるなら、本記録の解析をやり直す（§2.5 の T1 が気づかせる）
- D 状態の hang は守れないまま残る（X4）。緩和は本記録の中には無く、§5 の 2 で所有者が決める

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 0154 の試験専用の設定で実機で再現する | 所有者が取り下げた（0154 の取り下げの理由） |
| 約 5 s の経路の結果だけで約 11 s の経路も合格とする | 0127 §2.5 のとおり、約 5 s の結果から約 11 s の上がりを保守的に見積もる根拠が無い。本記録は合格とせず、測っていない部分を残るリスクとして所有者の受け入れに回す |
| 最悪時間を短くする（`TimeoutAbortSec` を短く、または `WatchdogSignal=SIGKILL`）を本記録で決める | 新しい安全の値を決めることになる。0080 §5 の 1・3 の決定に当たり、所有者は unit のタイムアウトを `provisional` のまま進むと決めた（0154 §1.3）。§5 の 1 の選択肢として残す |

## 5. 未決事項（所有者の判断が要る点。推奨を先頭に）

| # | 内容 | 選択肢（推奨を先に） | 時期 |
|---|---|---|---|
| 1 | §2.4 の X1〜X3（約 11 s の経路の温度を実測していない・`TimeoutAbortSec` の SIGKILL をこの機械で通していない・§2.3 の仮説の終わり方を確かめていない）を受け入れるか | **(推奨) 受け入れる**: §2.5 の T1〜T3 が入ってから、`basis` に条件つき（kill できる hang に限る・約 11 s の経路は部品と解析）と書いて承認点 2 で `confirmed` にする / 受け入れず、最悪時間を短くする案（`TimeoutAbortSec` を短く、または `WatchdogSignal=SIGKILL`。0080 §5 の 1・3）を別の決定記録で出す。値はその記録で決める / どちらもせず `provisional` のまま `enable` しない | 承認点 2 の前 |
| 2 | **D 状態の hang（X4）の残るリスク**（0154 §5 の 8・9 から引き継ぐ） | **(推奨) 調べてから決める**: 予備確認から T1 までの journal と `dmesg` で `hung_task`（`blocked for more than`）・fand の `duration_ms` の外れ値・`write_failure` / `readback_mismatch` を数え、T1 の 24 h の tick の最大の時間を示す。そのうえで、兆しが無ければ、BIOS / EC・CPU・GPU の自身の保護に頼る範囲を所有者が承認して受け入れる / 受け入れず、手当て（fand の trace の途絶の通知など。PR #337 の案）を決めるまで `enable` しない | 承認点 3 の前 |
| 3 | §2.5 の T1〜T3 のテストを足す実装の PR | **(推奨) 本記録の FINAL の後、承認点 2 の前に、別の Issue・ブランチで足す**（制御の挙動は変えない。テストの追加と、T2・T3 が落ちたときだけ実装を直す）/ 足さずに承認点 2 へ進む（解析の前提が守られない。推奨しない） | 承認点 2 の前 |
| 4 | systemd を使った経路全体のテスト（VM・コンテナの systemd で `SIGSTOP` と SIGKILL の経路を通し、`ExecStopPost` が走ることと時間を測る。simulated backend） | **(推奨) 行わない**: 段階 5 と T5 の実機の結果があり、systemd の振る舞いはこの機械の実測のほうが根拠として強い / 行う: `hardware` 印のテストとして足す（CI では走らない） | 3 と同時 |
