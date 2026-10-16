# 決定記録 0151: 負荷の状態ごとの差温度（`d.*`）の分布を出す読み取り専用の CLI

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし
- **関連**: [0002](0002-metric-naming.md) §2.2（派生値は保存しない） / [0017](0017-daily-report.md) §2.4（生成物を `docs/` に置かない） /
  [0045](0045-local-socket-write-entry.md) §2.6（申告を根拠にしない） / [0063](0063-compute-mode-advisory.md) §5 #3 /
  [0070](0070-soak-acceptance-interpretation.md)（DB を読み取り専用で開く） /
  [0093](0093-air-balance-shadow-summary-and-baseline-arm.md)（合否を出さない集計の CLI） /
  [0096](0096-calibration-binding-digest.md) / [0099](0099-calibration-change-log.md)（較正の変更の記録） /
  [0119](0119-safety-values-first-host.md) §2.14（`workload_regime` は試験の値のまま `provisional`） /
  [0121](0121-hardware-test-procedure.md) §2.3（T1） / [0036](0036-transient-cpu-gpu-regime.md) /
  `config/metrics.yaml`、`config/rules.yaml`、`config/compute-mode-advisory.yaml`、`src/coldaisle/soak.py`、
  `src/coldaisle/control/supervisor/regime.py`
- **対象 Issue**: #50 / #68

## 1. Context

#50 の「監視アラート閾値（`config/rules.yaml`）の確定」は、無負荷・軽負荷（推論）・GPU フルロード・CPU フルロード・
CPU + GPU 同時の各状態で `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` / `d.case_delta` の分布を出し、
p99 + マージンを候補に暫定値を見直す、としている。#68 の Compute Mode アドバイザリ（0063）も、助言の上限と
フルロードの電力しきい値を #50 の実測で決める（0063 §5 #3）。どちらも「負荷の状態ごとの分布」を要る。

いま、それを出す道具が無い。

- `d.*` は**保存しない**（0002 §2.2。`readings` に `CHECK (metric NOT LIKE 'd.%')`）。参照のたびに
  `compute_derived()` で最新値から計算するだけで、時系列としての `d.*` を集計する経路は無い
- ロールアップ（`readings_1m` / `readings_1h`）にも `d.*` は無い。被減数と減数の分平均の差は、同じ時刻の差の
  分布と一致しない（分位点は差に対して線形ではない）
- `coldaisle-soak-report`（#47 / 0070）は受入基準の判定で、負荷の区別も `d.*` も持たない
- trace の `state.workload_regime` は fand が動いている期間にしか無く、導入先の `workload_regime` の値は
  試験の値のまま `provisional`（0119 §2.14）。fand は普段 disabled で、履歴の大半は BIOS の制御の期間である

2026-10-15 16:36 に始めた T1（24 h の連続運転。0121 §2.3）は、所有者の普段の使い方のまま fand が制御する
24 時間のデータで、#50 の材料としても使える。仕様に無い道具を足すので、実装より先に形を決める。

## 2. Decision

### 2.1 位置づけ: 合成の起点の新しい CLI。DB を読むだけで書かない

- モジュールは `src/coldaisle/load_distribution.py`、console script は `coldaisle-load-distribution`
  （`soak.py` / `air_balance_shadow.py` と同じ「合成の起点」）
- DB は `soak.py` の `SoakDatabase` と同じく **`mode=ro` の URI で開き、マイグレーションを当てない**。
  スキーマの版がコードと合わなければ開かずに終了する（0070）。同じ型を共有できるなら共有し、2 か所に書かない
- 書くのは `--out` で指定した出力ファイルだけ。DB・設定・`memory/`・`config/rules.yaml` は書かない
- **合否も閾値の提案も出さない**（0093 と同じ）。p99 + マージンの計算や `rules.yaml` の値の決定は人がする。
  確定値は別の決定記録に残す（#50 のとおり）
- API・AI ツール・日次レポートには足さない（§2.9）

### 2.2 負荷の状態: CPU と GPU の電力の帯の組み合わせ（fand が止まっていても使える）

状態の主軸は**観測された電力**から、この CLI の設定（§2.10）の帯で決める。

- 軸は CPU（既定 `power.cpu.package`）と GPU（既定 `power.gpu.0`）。metric 名は設定に書き、`metrics.yaml` に
  無い名前・単位が `W` でない名前は起動時に拒否する
- 軸ごとに、名前つきの帯を下限の昇順で並べる（例: GPU `idle` / `light` / `high` / `full`）。帯は `[下限, 次の帯の下限)`
  で隙間も重なりも作らない。最初の帯の下限は 0
- 各評価時刻 `t`（§2.4）の電力は、`(t - power_window_ms, t]` の `quality = ok` の行の**平均**。窓の中の ok の行が
  `power_window_ms / 期待周期 × power_min_ok_ratio` に満たない軸は `unknown`
- 状態のラベルは `cpu:<帯>/gpu:<帯>`。どちらかが `unknown` なら状態は `unknown` で、分布に入れず件数だけ数える
- **GPU Mode の申告（`events` / `sys.gpu_mode`）や Workload Hint は使わない**（0045 §2.6・0063 の I-3 と同じ。
  申告は観測ではない）

Workload Regime（`control/supervisor/regime.py`）を主軸にしない理由は §4。ただし trace に `workload_regime` が
あれば、**副の切り口**として `--group-by trace-regime` で同じ統計を Regime ごとにも出す（§2.6）。

### 2.3 移り変わり直後と欠けの扱い

- 状態が変わった時刻から `settle_ms` の間の評価時刻は **`transition`** とし、主の分布に入れない。
  `transition` は行き先の状態ごとに件数と分布を別に出す（Reactive Guard の材料になりうるため捨てない）
- 電力の軸が `unknown` になった・評価時刻の間隔が `max_gap_ms` を超えた後は、状態が同じでも落ち着きの計時を
  やり直す（欠けの前後を同じ定常として繋がない）
- 定常の連続した区間（segment）ごとに数え、状態ごとに **segment の数と最長の長さ**を出す。1 回の長い負荷だけで
  決まった p99 を、複数回の負荷の p99 と見分けるため
- **区間の始め（`--start`）の境目**: `--start` の直前に負荷が変わっていても、区間の中だけを読むと前の状態が
  見えず、最初の評価時刻を定常として数えてしまう。これを避けるため、状態の判定に使う行（電力・§2.6 の `cooling` の
  判定に使う trace と `fan.*`）は **`--start - lookback_ms` から読む**。`lookback_ms` は
  `settle_ms + power_window_ms + fan_max_age_ms + max(trace_max_age_ms, pwm_apply_delay_ms)` で、設定から計算し別の値として持たない。
  さかのぼった区間の評価時刻は状態と落ち着きの計時を決めるためだけに使い、**分布にも件数にも入れない**
- ただし §2.6a の `fand` の判定に使う「連続した trace の区間の不一致の割合」は、`lookback_ms` で切らず、
  **区間の本当の始まりまでさかのぼって読む**（§2.6a）。`lookback_ms` はそれ以外の行にだけ当てる
- さかのぼった区間に行が無い・欠けている（保持期間の外・取り込みが止まっていた）・`max_gap_ms` を超える間隔がある
  ときは、状態が始めから続いていたとは言えない。その場合は**区間の中の最初の評価可能な時刻から落ち着きの計時を
  始め**、そこから `settle_ms` は `transition` とする（保守的に、定常へ入れない側に倒す）。こうなったことは出力の
  来歴に `start_lookback_incomplete: true` として残す

### 2.4 評価の時刻と `d.*` の組み立て

- 各 metric は**自分の `quality = ok` の行の時刻**で評価する。値を補間しない・リサンプルしない
- `d.*` の定義（被減数・減数）は `config/metrics.yaml` の `derived` だけを読む（コードに複製しない）。
  被減数の ok の行の時刻 `t` に、減数の ok の行を次の規則で対にする:
  - 同じ `ts_ms` の行があればそれ（基板の `air.*` は1サンプルで同じ時刻。`gpu.0.core` / `gpu.0.hotspot` も同じ収集の周期）
  - 無ければ `[t - pair_max_age_ms, t]` の最も新しい ok の行（`d.connector_rise` のように収集元が違う組）
  - どちらも無ければ `unpaired` として数え、分布に入れない
  - 差は `compute_derived()` と同じく `round(被減数 - 減数, 3)`
- 状態のラベルは評価時刻 `t` で §2.2 を当てる
- 1 行は 1 件。周期はほぼ一定（2500 ms）なので件数がおおよそ時間に比例する。状態ごとの観測時間は
  「件数 × 期待周期」ではなく、segment の端の時刻から出す

### 2.5 集計する metric と統計量

**metric**（設定の `metrics` に並べる。既定の一覧）:

- `config/metrics.yaml` のすべての `d.*`（いまは `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` / `d.top_rise` /
  `d.rear_rise` / `d.gpu_internal_delta` / `d.case_delta` / `d.connector_rise`）
- 関連の温度: `air.room` / `air.front_intake` / `air.gpu_intake` / `air.gpu_exhaust` / `air.rear_exhaust` /
  `air.top_exhaust` / `gpu.0.core` / `cpu.package` / `board.connector_12v2x6`、湿度 `air.room_humidity`
  （#68 の助言の上限の材料）
- 状態を決めた電力そのもの: `power.gpu.0` / `power.cpu.package`（帯の見直しの材料。0063 の `min_value` 400 W も）
- Fan の状態: `fan.front.pwm` / `fan.rear.pwm` / `fan.top.pwm`（同じ負荷でも Fan で温度差が変わるため並べて読む）

一覧に無い metric・`metrics.yaml` に無い名前は起動時に拒否する。

**統計量**（状態ごと・metric ごと）: 件数 `n`、`min` / `max` / `mean` / 標準偏差、設定の分位点
（既定 0.01 / 0.05 / 0.25 / 0.5 / 0.75 / 0.95 / 0.99）、segment の数と最長。

- 分位点は順序統計量の線形補間（Hyndman–Fan の type 7。NumPy の既定と同じ）。標準ライブラリで書き、依存を足さない
- `n` が `min_samples` 未満の組は分位点・標準偏差を `null` にし、理由 `insufficient_samples` を添える
  （少ない件数の p99 を数字として出さない）
- 値は小数 3 桁に丸める（`compute_derived()` と同じ）

### 2.6 切り口（グループの鍵）

**時刻に trace を結びつける規則（as-of）**。本記録で、ある時刻 `x`（評価時刻 `t` か Fan のサンプルの時刻 `t_f`）に
trace を結びつける箇所は、すべてこの 1 つの規則を使う（実装も 1 つの関数にする）。

- `x` の **as-of の trace** は、`ts_ms ≤ x` の trace のうち **`seq` が最も大きい**もの。`ts_ms` が同じ trace が複数
  あっても `seq` で 1 つに決まる（`seq` は `UNIQUE`）。`ts_ms > x` の trace（未来の tick・時計が戻った後に書かれ
  `ts_ms` が `x` より大きい行）は選ばない
- `x - ts_ms` が `trace_max_age_ms` を超えるなら、`x` の as-of の trace は**無い**（fand が止まった後に最後の tick を
  引きずらない）
- **壁時計の重なりにある時刻は曖昧として結びつけない。** ホストの時計が後ろへ戻り、後で同じ時刻の範囲にもう一度
  入ると、「`ts_ms ≤ x` で `seq` が最大」は、実時間では観測より後に書かれた trace を選びうる（戻る直前の 10:00 の
  読み値が、時計がもう一度 10:00 に来たときの tick と組になる）。そこで trace を `seq` の順に並べ、`ts_ms` が前の行
  より小さくなる箇所ごとに、**重なりの範囲** `[戻った後の ts_ms, それまでの ts_ms の最大]` を作る（同じ壁時計の
  時刻が 2 回刻まれた範囲）。`x` からさかのぼって使う範囲（`[x - max(power_window_ms, fan_max_age_ms +
  max(trace_max_age_ms, pwm_apply_delay_ms)), x]`）が重なりの範囲に触れる評価時刻・Fan のサンプルは、`cooling` を
  `undetermined`（理由 `clock_overlap`）、`trace-regime` を `unknown_ambiguous` にし、落ち着きの計時もそこでやり直す
- **telemetry・取り込みの行（`readings`）も同じ壁時計の `ts_ms` で、書いた順の列（`seq` のような）を持たない**
  （主キーは `(metric, ts_ms)`）。そのため `readings` だけから時計の戻りを見つける手段は無い。上の重なりの範囲は
  trace から見つけたものを、同じホストの壁時計の戻りとして `readings` の側にも当てる（重なりの範囲に入る
  `readings` の行の評価時刻も上と同じく除く）。trace が無い期間の戻りは見つけられないので、出力の来歴に
  「時計の戻りは trace のある期間でしか検出していない」と書き、人が知っている時計の調整は `--periods` の除外の
  区間で渡す
- 使う場所: §2.6a の Fan のサンプルの as-of・`bios` の「as-of の trace が無い」・下の 4 の `trace-regime`。
  どこでもこの規則（重なりの扱いを含む）だけを使う

分布は次の鍵の組ごとに出す。

1. **`cooling`**（常に付ける）: 物理の Fan を誰が動かしていたか。値は `fand` / `bios` / `fand_override` /
   `undetermined` の 4 つで、判定は §2.6a。`fand_override` と `undetermined` は**既定では分布から除いて件数だけ
   数える**（`--include-override` で `fand_override` を含める。`undetermined` を含める選択肢は持たない）。
   Fan の制御が違う期間を混ぜると、同じ負荷でも温度差の分布がずれるため
2. **状態**（§2.2 の電力の帯）
3. **`period`**（任意）: `--periods <yaml>` に名前つきの区間（例: `ac_on` / `ac_off`、試験の区間）と除外する区間を
   書くと、区間の名前を鍵に足す。区間の外の評価時刻は使わない。エアコンの ON / OFF は DB に記録が無いので、
   人が区間で渡す（推測しない）
4. **`--group-by trace-regime`**（任意）: 状態の代わりに、評価時刻 `t` の **as-of の trace**（上の規則。古さの上限は
   `trace_max_age_ms` を共有し、別の値を持たない）の `state.workload_regime` を鍵にする。as-of の trace が無い
   （fand が止まっている・最後の tick が古すぎる）評価時刻の鍵は `unknown_no_trace`、壁時計の重なりに触れる
   評価時刻の鍵は `unknown_ambiguous`、as-of の trace が既存の
   `ControlTick` の検証を通らない・`workload_regime` を持たない評価時刻の鍵は `unknown_unreadable` とし、いずれも
   分布に入れず件数だけ数える（Regime の語彙の `unknown` と混ぜない）。regime が変わる前後でも、`t` より後の tick の
   regime は使わない。
   出力には「Regime の設定は `provisional`（0119 §2.14）」を必ず書く

### 2.6a `cooling` の判定: trace の有無ではなく、物理の Fan の観測と突き合わせる

**trace があることは「fand が物理の Fan を動かしていた」ことを意味しない。** fand は既定で `--backend simulated`
（`control_daemon.py` の `backend_factory_for`。0118 §2.5）で動き、そのまま既定の DB に向ければ物理の Fan は BIOS の
ままなのに trace が残る。逆に、trace が無い期間も BIOS とは限らない。fand は `--no-trace` で trace を書かずに
動く（`control_daemon.py` の `ControlTraceLogger(store) if config.record_trace else None`）し、trace の書き込みに失敗
しても制御を続ける（`control/loop.py` の `_record()`。0060 §2.6）。hwmon の backend なら、trace の表が空のまま
fand が実際に Fan を制御していることがありうる。またfand が異常終了すると、引き継ぎ実行部
（`safety_handoff.py`）が `pwmN_enable=0` / `pwmN=255` の Max を書き、次に fand が起動するまでそのまま残る。

DB に残る記録で、これを直接に言える欄は無い（2026-10-16 の main で確かめた）:

- `ControlTick`（v1〜v16）に backend の欄は無い。欄は `state` / `zones` / `runtime` / `safety_provenance` /
  `registry` / `air_balance` / `mode_command` / `authority` / `telemetry_health` / `run_id` / `workload_hint` /
  `faults` などで、`runtime.config` は 4 ファイルの hash と版だけである（`ControlConfigDigest`）
- 引き継ぎの記録は `/run/coldaisle/fan-handoff.json`（`safety_handoff.HANDOFF_RECORD_PATH`）で、tmpfs にあり DB に
  入らない。正常停止で fand が消す（`control/hardware/hwmon.py`）ので、後から読んでも残っていない
- `control_admin_audit`（0072 §2.7）は管理ソケットの指令の監査で、backend も引き継ぎも記録しない

そこで、fand と別のプロセスが物理の hwmon から読んで DB に残す値と突き合わせる。使う欄は次のとおり。

- trace の `zones.<zone>.hardware.pwm_raw`（`HardwareReadback`。backend が読み戻した値）と `state.operating_mode`
- `coldaisle-telemetry` が nct6799 の `pwmN` から読む `fan.{front,rear,top}.pwm`（`pwmN / 255 × 100` %。
  `internal_telemetry/hwmon.py`。対応は `config/internal-telemetry.yaml` と `docs/fan-header-mapping.md`）

判定（評価時刻 `t` ごと）:

| 区分 | 条件（**すべて**を満たすときだけ） |
|---|---|
| `fand` | `t` 以前で最も新しい、`fan_max_age_ms` 以内の Fan のサンプル（3 つの zone の ok の `fan.<zone>.pwm`）について、下の「Fan のサンプルと trace の突き合わせ（as-of）」で**一致**し、一致した tick がすべて `operating_mode` `auto`。さらに、その as-of の trace を含む**連続した trace の区間**（間隔が `trace_max_age_ms` を超えたら切る。`run_id` は v16 からなので使わない）の**始まりから終わりまで全体**（`--start` / `--end` の外も含む）で、突き合わせた Fan のサンプルのうち一致しなかった割合が `pwm_mismatch_max_ratio` 以下。区間の始まりか終わりを確かめられないとき（下の「区間の全体を読む」）は `undetermined` |
| `fand_override` | `fand` の条件のうち、一致した tick がすべて `auto` でない（`manual` / `max` / `calibration`。試験の操作）。一致した tick のモードが食い違えば `undetermined` |
| `bios` | `t` が `--periods` の **`bios_confirmed`** の区間の中にあり、`t` の as-of の trace が無く（§2.6 の規則）、壁時計の重なりに触れない（下の「`bios` の前提」）。**trace が無いことだけでは `bios` にしない** |
| `undetermined` | 上のどれにも当たらない。例: trace はあるが物理の PWM と合わない（simulated の backend など）・区間の始まりか終わりを確かめられない・`fan.*` が欠けている・trace が無く `bios_confirmed` の区間の外（trace の無い DB を含む）・壁時計の重なりに触れる |

**Fan のサンプルと trace の突き合わせ（as-of）**（`fand` の判定）:

- 突き合わせの単位は trace の tick ではなく **Fan のサンプル**（`coldaisle-telemetry` の 1 回の収集。2500 ms ごと）。
  trace は 1000 ms ごとなので、1 つのサンプルの前に trace が複数ある。どれと比べるかを次のとおり決める
- サンプルの時刻 `t_f` に対する **as-of の trace** は §2.6 の as-of の規則で選ぶ（as-of の trace が無ければ
  `fand` にも `fand_override` にもならない）
- 指令が物理の PWM に反映されるまでには遅れがある（fand の書き込みと telemetry の読み取りが別の周期で、ランプの最中は
  tick ごとに指令が変わる）。そこで **候補の trace** を、as-of の trace と同じ区間で、`seq` が as-of の trace 以下で、
  `ts_ms` が `[t_f - pwm_apply_delay_ms, t_f]` に入るもの（as-of の trace は必ず含める）とする
- **1 つの候補の tick が 3 つの zone のすべてと一致すること**を求める。候補の tick `c` が一致するとは、3 つの
  zone のすべてで物理の `fan.<zone>.pwm` と `c` の `pwm_raw / 255 × 100` の差が `pwm_match_tolerance_pct` 以内の
  とき。zone ごとに別々の tick と一致する（Front は ある tick、Rear は別の tick）ことは、どの tick も指令していない
  物理のベクトルなので一致にしない
- ただし候補の trace のどれか 1 つでも、3 つの zone のどれかに `hardware` が無いか `write_ok` / `readback_ok` が
  false なら、そのサンプルは一致にしない（下の「書き込みを確かめられた tick だけを一致とする」）
- **分類は、一致した tick の `operating_mode` で決める**（as-of の trace のモードではない）。一致した tick がすべて
  `auto` なら `fand`、すべて `auto` 以外（`manual` / `max` / `calibration`）なら `fand_override`。一致した tick どうしで
  モードが食い違う（例: 遅れたサンプルが古い `manual` の tick と、新しい `auto` の tick の両方と一致する）なら、
  そのサンプルは `undetermined`。どの tick とも一致しなければ不一致
- 3 つの zone の `fan.*.pwm` は同じ収集で同じ `ts_ms` を持つ。3 つが同じ `ts_ms` で揃わないサンプルは、1 つの
  ベクトルとして比べられないので `undetermined`（区間の割合では不一致に数える）
- 評価時刻 `t` の `cooling` は、`t` 以前で最も新しい `fan_max_age_ms` 以内のサンプルの結果を使う（サンプルが無ければ
  `undetermined`）

**書き込みを確かめられた tick だけを一致とする**（`fand` の判定）:

- `hardware.pwm_raw` は、書き込みやその確かめに失敗した tick にも残る。hwmon の backend の `_failed()` は写像した
  要求の PWM でこの欄を埋め、`ENABLE_REVERTED` のときはレジスタの値が入って `readback_ok = false` になる
  （`control/hardware/hwmon.py`）。前の duty が残っていた・BIOS の duty がたまたま同じだった、という tick を
  `pwm_raw` の一致だけで `fand` にしない
- そこで一致に使えるのは、**3 つの zone すべてで `hardware` があり `write_ok` と `readback_ok` がともに true** の
  trace だけとする。flag のどれかが false・`hardware` が無い zone がある trace が候補に入ったサンプルは、PWM が
  合っていても一致にせず、それを使う評価時刻は `undetermined`
- 区間の不一致の割合では、そうしたサンプルを**不一致として分子に数える**（分母は、as-of の trace がその区間に
  属する Fan のサンプルのすべて）。書き込みの失敗が多い運転を、確かめられた一致の多さで `fand` に入れないため。
  書き込みの失敗が続く区間は、区間ごと `undetermined` に倒れる

**区間の全体を読む**（`fand` の判定）:

- **区間は `seq` の順でたどる。** trace の書かれた順は `seq`（0007 で足した `NOT NULL UNIQUE` の列。書き手は
  `MAX(MAX(seq), pruned_through_seq) + 1` を挿入と同じ文で振る。`store/db.py`）で、`ts_ms` は壁時計なので時計が
  戻ると書いた順を表さない（0071 §2.2a の「並びは `seq`、`ts_ms` は絞り込みだけ」と同じ理由）。そこで、連続した
  trace の区間は「`seq` の昇順で隣り合う 2 行の `ts_ms` の差が `trace_max_age_ms` を超えたら切る」と定義する。
  差が負（時計が戻った）なら切らない（区間を短く切ると、外れた前半を落とせてしまうため、繋ぐ側に倒す）。
  `ts_ms` は、この途切れの比較と、Fan のサンプルに対する trace の古さ（as-of の選択と `pwm_apply_delay_ms`）にだけ使う
- サンプルの候補の窓に、別々の区間（`seq` でたどって別の区間）に属する trace が入ったとき（時計が戻った前後）は、
  どの区間の判定にも寄せず、そのサンプルを `undetermined` にする
- 不一致の割合は、as-of の trace を含む区間について、`seq` を小さい向きへ区間の本当の始まりまで、大きい向きへ
  終わりまでたどって読み、その区間の全体で数える。こうして判定が `--start` / `--end` の置き方に依らないようにする
- 決まった長さの窓（例: 直近 30 分の割合）にしないのは、simulated の運転で前半が外れ、窓に入る後半だけがたまたま
  合う、という場合を窓では捨てられないため。区間の全体なら、その運転全体の性質で判定できる
- 読むのは trace の `seq`・`ts_ms`・`state.operating_mode`・3 つの zone の `zones.<zone>.hardware.pwm_raw` /
  `write_ok` / `readback_ok`（`hardware` が無いことも含む）だけ（SQL の `json_extract` で取り出し、trace の全体を
  検証の型に通さない）と、同じ範囲の `fan.*.pwm` の ok の行。区間の割合の数え方は、上のサンプルごとの規則と
  **同じ判定の関数**を使う（flag が false・`hardware` が無い trace が候補に入ったサンプルは、PWM がたまたま合っていても
  不一致に数える）。
  2 つの経路で判定を書き分けない。保持期間（`control_trace_days` / `raw_days` 30 日）が上限なので、1 区間で最大
  約 260 万 tick。手動で時々走らせる CLI として許容する
- **区間の始まりと終わりを確かめられるか**は、時刻ではなく `seq` の境目で決める。根拠は `control_trace_prune` の列:
  - `legacy_through_seq`（0010）: 0007 が移行の前の行に `(ts_ms, tick_id)` の順で振った `seq` の上限。移行の前の
    行が無ければ 0。0010 の注釈のとおり、`legacy_until_ms` は空の DB でも作った時刻（`:now_ms`）で書かれるので、
    時刻では移行の前の行と今の行を見分けられない。`seq ≤ legacy_through_seq` の行だけが移行の前の行で、その前に
    移行の前の削除で消えた行があったかは分からない
  - `pruned_through_seq`（0007）: 保持期間の削除（`ts_ms < cutoff` の行）で消した行の `seq` の**上限**。消していなければ 0。
    削除は `delete_control_traces_before()` が `ts_ms` で行うので、時計が戻ると `seq` の先頭からの連続にならず、
    途中の `seq` だけが消え、それより小さい `seq` の行が残ることがある（`tests/test_control_trace_api.py` の seq 1 と 3 を
    残して 2 を消す例）。そこで本記録は `pruned_through_seq` を「消したことがあるか（0 か）」と「この値以下の
    `seq` は消えたかもしれない」の判断にだけ使い、「この値以下がすべて消えた・この値より上はすべて残る」とは読まない
- **区間の中で `seq` が連続していることを求める。** 区間の最も小さい `seq` から最も大きい `seq` までに欠番（穴）が
  1 つでもあれば、その区間のすべての時刻を `undetermined` にする。欠けた tick に不一致や書き込みの失敗があったかも
  しれないので、残った行だけで割合を数えない。欠番は削除のほかにも起こりうる: 書き手の `INSERT OR IGNORE` は
  重複で捨てた挿入の番号を使わないと注釈している（`store/db.py` の `record_control_trace` の「`seq` は欠番を持ちうる」）。
  理由を見分けずに同じく保守的に扱い、出力に穴のあった区間の数と欠番の数（`seq_gaps`）を出して、人が気づけるようにする
- 区間の最も小さい `seq` を `s0`、最も大きい `seq` を `s1` とする。始まりと終わりは隣の `seq` の行が残って
  いるかで確かめる。DB の端は、始まりの側（`seq` は 1 から振る）だけ示せる。終わりの側の端（`seq` が最大）は、まだ
  書き足されうるので示せない（下の「`seq` が最大の区間」）:
  - 始まりを**確かめられる**のは次のどちらかのとき:
    1. `s0 - 1` の行が残っている（区間の定義から、その行とは `trace_max_age_ms` を超えて途切れている。別の区間に属してよい）
    2. `s0 = 1` で、`legacy_through_seq = 0`（移行の前の行は無かった。`seq` は 1 から振る）
  - 終わりを**確かめられる**のは、`s1 + 1` の行が残っているときだけ（同じく別の区間に属してよい）
- **`seq` が最大の区間（DB の末尾の区間）は常に開いているとみなす。** その区間のすべての時刻を `undetermined`
  （理由 `segment_open`）にし、件数だけを出す
  - fand が書き足している最中なら、区間はまだ終わっていない。途中までの区間を受け入れると、後で足される tick で
    不一致の割合が上限を超えたとき、走らせ直すと報告済みの観測が `fand` から `undetermined` に変わる
  - 閉じたことを DB の中で確かめる手段が無い。fand の正常停止・run の終わりの印は DB に残らない（§2.6a の `bios` の
    前提）。`readings` には挿入の順の列が無く（§2.6）、圧縮した mock / replay のように `readings` の時刻が制御の
    書き手より先に進んだ DB もあるので、「最後の trace より後の時刻の `readings` がある」から途切れを推し量らない。
    CLI を実行した時刻（壁時計の「いま」）も使わない（0153 と同じく checkpoint した写しでは、写した時点で開いていた
    区間が「いま」との差で閉じたように見える）
  - 将来、fand の run の終わりの印を DB に残す決定がされたら（§5 #7）、その印で末尾の区間を閉じられる
  - `--end` が末尾の区間より前（`--end` を含む区間が `s1 + 1` の行で終わりを確かめられる）なら影響は無い。影響を
    受けるのは末尾の区間に属する評価時刻だけ。ただし T1 のように fand を止めた後の DB でも、その後に fand が
    trace を書いていなければ最後の運転の区間は数えられない（`segment_open` の件数だけが出る。§2.12）
  - 集計は 1 つの読み取りの transaction の中で行い（WAL の読み取りの snapshot）、trace と `readings` を同じ時点の
    内容で読む。読んでいる途中に足された行は見ない
- どちらかを確かめられない区間（`s0 - 1` か `s1 + 1` の行が無い。理由が削除でも欠番でも見分けない。`s0 ≤ legacy_through_seq`
  なら移行の前の行）は、区間の前後が消えたかもしれないので、その区間のすべての時刻を `undetermined` にする。例:
  `(seq, ts_ms) = (1, 5000)` と `(3, 8000)` が残り `(2, 1000)` を消した DB では、全部の履歴なら時計が戻った `seq 2` は
  `seq 1` と同じ区間だが、残った行では `seq 1` だけの区間に見える。始まりは `s0 = 1` で通るが、終わりは `seq 2` が
  無いので通らず `undetermined`（`seq 3` の区間も始まりで通らない）。区間の始まりか終わりが生データ（`fan.*.pwm`）の
  保持期間の外にあるときも同じ。消えた部分の一致を推測しない
- `legacy_until_ms` に近い時刻に書かれたというだけでは、今の行を拒まない（新しい DB では 0010 が
  `legacy_through_seq = 0` を記録し、最初の tick は `seq = 1` から始まるので 2 で確かめられる。ただし区間の中の
  `seq` の連続は別に求める）

**`bios` の前提**（人が外で確かめた区間だけ）:

- trace が無いことは、fand が Fan を制御しなかった証拠にならない。理由は 3 つ: (1) `--no-trace` や trace の書き込みの
  失敗では、fand が hwmon を制御していても trace が残らない（上）。(2) 異常終了の後の引き継ぎは zone ごとに成否が
  分かれ（`safety_handoff.py` の `HandoffZoneResult` の `status`）、成功した zone は全速、失敗した zone は fand が最後に
  書いた PWM のまま残りうるので、回転数が全速未満でも BIOS とは限らない。(3) 正常停止かどうかを DB で確かめられる
  記録が無い（fand は正常停止で引き継ぎの記録を消すだけで DB に書かない。引き継ぎの結果 `safety_handoff_completed`
  は journal のログにだけ出る。`control_admin_audit` は管理ソケットの指令だけで `systemctl stop` を記録しない）
- そのため **`bios` は `--periods` の `bios_confirmed` の区間の中だけ**にする。trace の無い DB・trace の途絶の後を
  含め、どんな DB でも `bios_confirmed` の外では、trace が無ければ `undetermined`。DB の記録（trace の有無・
  `control_trace_prune` の境目）から「fand が書いたことが無い」と推すことはしない
- `bios_confirmed` は、人が journal（その期間に fand の unit が動いていない・`safety_handoff_completed` が無い、または
  `record_found: false`）や `systemctl` の記録で、fand が止まっていて BIOS が制御していたことを確かめて書く。区間ごとに
  根拠の文（`basis`）を必須にし、出力の来歴にそのまま写す
- `bios_confirmed` の中でも、as-of の trace がある時刻（人の記録と DB が食い違う）と壁時計の重なりに触れる時刻は
  `bios` にせず `undetermined` にし、食い違いの件数を出力に出す
- 引き継ぎの結果・fand の正常停止・backend を DB に残すことは、別の記録の候補とする（§5 #7）。それが入れば、
  `bios_confirmed` を人が書かずに済む

- 区間の単位で `pwm_mismatch_max_ratio` を見るのは、simulated の backend でも BIOS の PWM がたまたま指令と
  近い時刻があり、時刻ごとの一致だけでは `fand` に入ってしまうため
- trace に backend が載るようになったら（§5 #7）、その欄を優先する別の記録で置き換える

### 2.7 品質と較正

- 使うのは **`quality = ok` かつ値が `NULL` でない行だけ**。`missing` / `suspect` / `stale` は metric ごと・グループ
  ごとに品質別の件数として出し、`d.*` では `unpaired` も別に数える。黙って捨てない
- DB の値は取り込みで較正を当てた後の値である（0096）。期間の中に較正の変更（0099 の記録）があれば、その時刻と
  digest を出力の先頭に一覧で出し、`calibration_changed: true` を立てる。期間を自動では分けない（§5 で扱う余地は
  残さない。分けたければ人が `--periods` で分ける）
- 生データ（`readings`）だけを読む。ロールアップは使わない（§1）。そのため**生データの保持期間（`raw_days`）の
  うちに**走らせる。期間が `max_period_days` を超える指定は拒否する

### 2.8 出力の形

- `--format json`（既定）と `--format markdown`。`--out <path>` に書き、省けば標準出力
- JSON は版つき（`version: 1`）で、先頭に入力の来歴を置く: 期間（UTC の ms とタイムゾーンつきの表示）、
  設定ファイルと `metrics.yaml` の SHA-256、DB のスキーマの版、`--periods` の SHA-256、除いた件数の内訳、
  較正の変更の一覧。DB のパスは出さない（実行環境の絶対パスを残さない。AGENTS.md ルール 10）
- Markdown は metric ごとの表（行 = グループ、列 = `n`・p50・p95・p99・max・segment の数）と、除いた件数の表
- 既定の出力先の例は `var/load-distribution/`。**`docs/` には書かない**（室温の推移は生活パターン。0017 §2.4）。
  #50 の `docs/baseline-YYYY-MM-DD.md` は、人が中身を確かめてから必要な数字だけを写す

### 2.9 LLM へ渡さない

- AI 層（`ai/`）・API（`api/`）・`server.py` からこのモジュールを import しない。AI ツールにも日次レポートにも足さない
- 出力は集計値だけだが、それでも LLM のプロンプトへの経路を作らない（ルール 8 の「生の時系列を渡さない」に加え、
  閾値の判断を LLM に寄せないため）。import の向きは AST で import を走査するテスト（§2.11）で CI が見張る

### 2.10 設定は `config/load-distribution.yaml`

次の値をすべてここに置き、コードに既定値を持たない（ルール 9）。鍵の欠け・未知の鍵・型の違いは起動時に拒否する。

| 鍵 | 内容 | 初期値（**すべて `provisional`**） |
|---|---|---|
| `version` | 設定の版 | 1 |
| `timezone` | `--start` / `--end` の解釈と表示 | `Asia/Tokyo` |
| `max_period_days` | 1 回で集計できる期間の上限 | 30（`raw_days` と同じ） |
| `expected_interval_ms` | 窓の中の ok の件数の基準 | 2500 |
| `axes.cpu` / `axes.gpu` | metric 名と帯（名前と下限 W） | §5 #2 |
| `power_window_ms` | 電力の平均の窓 | 30000 |
| `power_min_ok_ratio` | 窓の中の ok の割合の下限 | 0.5 |
| `settle_ms` | 移り変わり直後とみなす長さ | 300000（§5 #3） |
| `max_gap_ms` | これを超える間隔で落ち着きの計時をやり直す | 10000（`stale_after_ms` と同じ） |
| `pair_max_age_ms` | `d.*` の減数を対にする古さの上限 | 5000（2 周期） |
| `trace_max_age_ms` | as-of の trace の古さの上限（§2.6 の規則。`cooling`・`trace-regime` で共有）・連続した trace の区間を切る間隔 | 2000（tick 1000 ms の 2 倍） |
| `fan_max_age_ms` | `fan.*.pwm` を評価時刻に使う古さの上限 | 5000（2 周期） |
| `pwm_match_tolerance_pct` | trace の `pwm_raw` と物理の `fan.*.pwm` の差の許容 | 2.0（約 5 / 255） |
| `pwm_mismatch_max_ratio` | 連続した trace の区間で一致しなかった Fan のサンプルの割合の上限 | 0.05 |
| `pwm_apply_delay_ms` | 指令が物理の PWM に反映されるまで許す遅れ（候補の trace の窓） | 3500（telemetry の 1 周期 2500 ms + fand の 1 tick 1000 ms） |

`--periods` の YAML は、名前つきの区間・除外する区間に加えて、`bios_confirmed` の区間（`start` / `end` / 必須の `basis`）を持てる（§2.6a）。
| `quantiles` | 出す分位点 | 0.01 / 0.05 / 0.25 / 0.5 / 0.75 / 0.95 / 0.99 |
| `min_samples` | 分位点を出す件数の下限 | 120（約 5 分） |
| `metrics` | 集計する metric の一覧 | §2.5 |

### 2.11 テストの方針（実機不要）

すべて CI（`-m "not hardware"`）で走る。DB は一時ディレクトリに既存のマイグレーションで作り、合成の行を入れる。

- 読み取り専用: 集計の前後で DB のバイト列とスキーマの版が変わらない。版の合わない DB は開かずに終了する
- 帯: 境界の値（下限ちょうど）が上の帯に入る。窓の中の ok が足りない軸は `unknown`
- 落ち着き: 状態の変化から `settle_ms` 未満は `transition`、以上は主の分布。`max_gap_ms` を超える欠けで計時をやり直す
- `d.*` の対: 同じ時刻の対・`pair_max_age_ms` 以内の対・超えた `unpaired`・減数が `suspect` の行を使わない
- 品質: `missing` / `suspect` / `stale` / `NULL` が分布に入らず件数に出る
- 分位点: 手で計算した type 7 の値と一致する。`min_samples` 未満で `null` と理由
- `cooling`（§2.6a）: trace があり物理の PWM が合えば `fand`。trace はあるが PWM が合わない（simulated の
  backend を模した合成データ）・区間の不一致の割合が上限を超える・`fan.*` が欠けると `undetermined`。
  `bios_confirmed` の中で as-of の trace が無ければ `bios`。**trace の表が空の DB**（`--no-trace` や書き込みの失敗を
  模した合成データ）でも、`bios_confirmed` の外なら `undetermined`。`bios_confirmed` の中に trace があれば
  `undetermined` で食い違いの件数が出る。`manual` の tick が `fand_override` で
  既定では除かれ、`undetermined` は件数だけに出る
- 書き込みの確かめ（§2.6a）: PWM が合っていても、候補の trace に `write_ok` か `readback_ok` が false の zone がある
  （`ENABLE_REVERTED` で `readback_ok = false` を含む）・`hardware` の無いサンプルは `undetermined`。区間の中で
  そうしたサンプルが `pwm_mismatch_max_ratio` を超えると、合っているサンプルも含めて区間ごと `undetermined`
- `seq` の境目（§2.6a）: 新しい DB（`legacy_through_seq = 0`）で最初の tick が `legacy_until_ms` から
  `trace_max_age_ms` 以内に書かれても、`seq = 1` から続く区間は確かめられて `fand` になりうる。`seq ≤ legacy_through_seq`
  の行から始まる区間・`s0 - 1` の行が無い区間は `undetermined`
- `seq` の穴（§2.6a）: `ts_ms` で消して seq 1 と 3 を残し 2 を消した合成データ（`tests/test_control_trace_api.py` と同じ形）で、
  `s0 = 1` でも区間が `undetermined` になる。重複で捨てた挿入による欠番も同じで、`seq_gaps` に数が出る
- 時計の戻り（§2.6a）: `seq` の順で `ts_ms` が戻る合成データで、区間が `seq` の順でたどられ（時刻の順に並べ替えない）、
  戻った所で区間が切れない。評価時刻の窓に 2 つの区間の trace が入ると `undetermined`
- 壁時計の重なり（§2.6）: `seq` の順で `ts_ms` が 10:00:05 → 09:59:50 と戻り再び 10:00:05 を越える合成データで、
  戻る前に記録した 10:00:00 の読み値の評価時刻が、戻った後の tick と組にならず `cooling: undetermined`
  （`clock_overlap`）・`trace-regime` は `unknown_ambiguous` になる。重なりの範囲の外では as-of の規則どおりに組になる。
  `bios_confirmed` の中でも重なりに触れれば `bios` にならない
- as-of の突き合わせ（§2.6a）: ランプの最中に、Fan のサンプルの時刻の直前の trace は新しい指令・物理の PWM は
  1〜3 tick 前の指令という合成データで、`pwm_apply_delay_ms` の窓の候補と一致して `fand` になる（最新の trace だけと
  比べて外れにしない）。遅れが `pwm_apply_delay_ms` を超えるデータは一致にしない。trace の `ts_ms` が Fan のサンプルより
  後（telemetry の時刻とずれた）の trace は as-of に選ばれない。同じ `ts_ms` の trace が 2 つあるときは大きい `seq` を選ぶ。
  候補の中に flag が false の trace があるサンプルは、PWM が合っていても一致にならない。Front は tick A、Rear は tick B
  とだけ合う（どの 1 つの tick とも 3 zone が揃わない）サンプルは一致にならない。遅れたサンプルが古い `manual` の tick
  とだけ合うなら、as-of の trace が `auto` でも `fand_override`、`manual` と `auto` の両方の tick と合うなら `undetermined`。
  3 zone の `fan.*.pwm` の `ts_ms` が揃わないサンプルは `undetermined`
- `trace-regime`（§2.6 の 4）: regime が変わる tick の前後の評価時刻で、`t` より後の tick の regime を使わない。同じ
  `ts_ms` の 2 つの trace では大きい `seq` の regime を使う。fand が止まり最後の tick が `trace_max_age_ms` より古い
  評価時刻は `unknown_no_trace`、検証を通らない trace は `unknown_unreadable`、壁時計の重なりは `unknown_ambiguous` で、
  いずれも件数だけに出る
- 区間の終わり（§2.6a）: `(1, 5000)` と `(3, 8000)` が残り `(2, 1000)` を消した合成データで、`seq 1` の区間が終わりの
  検査で、`seq 3` の区間が始まりの検査で `undetermined` になる。`seq` が最大の区間は、
  後の時刻の `readings` があっても（圧縮した replay を模す）、実行した時刻に依らず常に `segment_open` で `undetermined`
  になり、その後に別の区間の trace（`s1 + 1`）を足すと終わりを確かめられる
- 区間の割合の数え方（§2.6a）: flag が false の trace が候補に入り PWM がたまたま観測と合うサンプルの合成データで、
  そのサンプルが区間の割合の不一致に数えられ、サンプルごとの判定と区間の判定が同じ関数の結果になる
- 区間の全体（§2.6a）: `--start` より前から続く trace の区間で、前半の PWM が外れ `--start` 以後だけが合う合成データを
  `undetermined` にする（`--start` を区間の途中のどこに置いても判定が変わらない）。区間の最初の行の前の `seq` の行が
  無い DB では、その区間を `undetermined` にする。`--end` の後に外れが続く場合も同じ
- 区間の始めの境目（§2.3）: `--start` の `settle_ms` 前に負荷が変わったデータで、`--start` 直後の評価時刻が
  `transition` になる（さかのぼった区間の時刻は件数に入らない）。`--start` より前に行が無いデータでは、区間の
  最初から `settle_ms` が `transition` になり、`start_lookback_incomplete: true` が出る。`--start` の
  `settle_ms` より前から状態が変わっていないデータでは、`--start` 直後から定常に入る
- 切り口: `--periods` の区間の外を使わない。`--group-by trace-regime` で検証を通らない trace を件数だけ数える
- 設定: 未知の metric・`W` でない電力の metric・帯の下限が昇順でない・鍵の欠けを拒否する
- 出力: JSON の版と来歴の欄、DB のパスが出ないこと、Markdown の表の形
- import の向き: `ai/` / `api/` / `server.py` がこのモジュールを import しない

### 2.12 T1 のデータへの使い方

導入先の DB は作業者の uid から読めないので、root で SQLite の `.backup` で写しを作り、その写しに対して走らせる
（書いている最中の DB をファイルの複写で写さない）。期間は T1 の開始から 24 h を `--start` / `--end` で渡す。
T1 は hwmon の backend で fand が制御しているので、§2.6a の突き合わせが合えば `cooling: fand` に入る。合わない時刻が
多ければ `undetermined` が増えるので、その件数を先に確かめる。**ただし T1 が DB の最後の fand の運転のままだと、
`seq` が最大の区間なので `segment_open` で数えられない**（§2.6a）。T1 の後に fand が別の運転で trace を書いた
（`s1 + 1` の行がある）DB の写しで集計するか、§5 #7 の run の終わりの印が入るのを待つ。BIOS の期間との比較は、同じ DB の
T1 より前の期間を別に走らせて並べる。BIOS の期間（試験の前も、試験の間と後で fand が止まっていた期間も）は、
所有者が journal を確かめて `bios_confirmed` を書いたときだけ `bios` に入る。

## 3. Consequences

### 良くなること

- #50 の「状態ごとの `d.*` の分布」と、#68（0063 §5 #3）の助言の上限・フルロードの電力の材料が、同じ道具・同じ
  規則で出る。後で閾値を見直すときも同じ手順で作り直せる
- fand が止まっている期間（履歴の大半）にも使える。fand / BIOS / 試験の操作を分けて出すので、制御の違いが
  分布に混ざらない
- 除いた件数と理由・segment の数を出すので、「1 回の負荷で決まった p99」や「欠けの多い期間の分布」を数字の
  見た目だけで信用しない

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 電力の帯は Workload Regime と別の分け方で、語彙が 2 つになる | 帯は集計の切り口で、制御には使わない。trace があれば `--group-by trace-regime` で Regime の語彙でも出せる |
| 帯の初期値が根拠の薄い `provisional` | 電力そのものの分布も出すので、1 回走らせてから帯を見直せる（§5 #2） |
| 生データだけを読むので、保持期間（30 日）より古い期間は集計できない | `d.*` は保存しないので、ロールアップからは正しい分布が出ない。必要な期間は保持期間のうちに走らせ、出力を残す |
| 24 h × 2.5 s で metric あたり約 3.5 万件をメモリに持つ | 期間の上限 `max_period_days` で抑える。30 日でも metric あたり約 100 万件の float で、1 回の手動の集計として許容できる |
| `bios` の分布を出すには、人が journal を確かめて `bios_confirmed` を書く手間が要る。書かなければ BIOS の期間はすべて `undetermined` | trace が無いことは BIOS の証拠にならない（`--no-trace`・書き込みの失敗・引き継ぎの一部の成功）。誤って fand や引き継ぎの Max を BIOS に混ぜるより安全側。引き継ぎと正常停止と backend が DB に載れば解消する（§5 #7） |
| `seq` が最大の区間（最後の fand の運転。T1 も、後に fand が動いていなければ）は数えられない | 件数（`segment_open`）は出す。後の運転の trace がある写しで集計するか、run の終わりの印（§5 #7）を待つ。途中までの区間で出した値が後で変わるより安全側 |
| 時計の戻りは trace のある期間でしか見つけられない | 出力の来歴に書き、人が知っている調整は `--periods` の除外で渡す |
| エアコンの ON / OFF を自動で区別できない | DB に記録が無いものを推測しない。人が `--periods` で区間を渡す |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| trace の `workload_regime` だけで分ける | fand が動いている期間にしか無い（履歴の大半は BIOS）。導入先の `workload_regime` の値は試験の値のまま `provisional`（0119 §2.14）。Regime は idle / active の 2 段で、#50 の「軽負荷（推論）」と「フルロード」を分けられない |
| Regime の推定器（`WorkloadRegimeEstimator.step`）を DB の上で再生して分ける | fand が無くても使え語彙も揃うが、`ControlStateSnapshot` を readings から組み立てる別の経路が要り、推定器の設定が `provisional` なのは同じ。§5 #1 で将来の拡張として残す |
| ロールアップ（`readings_1m`）から計算する | `d.*` が無く、被減数と減数の分平均の差は同じ時刻の差の分布と一致しない。`suspect` などの品質の内訳も残らない |
| API のエンドポイントにする | 生データを広い期間で読むので重く、AI ツールの経路に近づく。手動で時々走らせる用途に常駐の口は要らない |
| `coldaisle-soak-report` に足す | soak は受入基準の判定（0070）で、期間も目的も違う。判定の CLI に分布を混ぜると、判定の再現性の範囲が広がる |
| trace の有無だけで fand と BIOS を分ける | simulated の backend の fand でも trace は残り、異常終了の後の引き継ぎの Max の期間には trace が無い。どちらも物理の Fan の状態と食い違う（§2.6a） |
| GPU Mode の申告（`events` / `sys.gpu_mode`）で負荷を分ける | 申告は観測ではない（0045 §2.6・0063 の I-3）。申告と実際の負荷がずれた期間を誤った状態に入れる |
| p99 + マージンを CLI が計算して閾値の候補を出す | 閾値は安全・通知に関わる人の判断（AGENTS.md「迷ったら」）。マージンの決め方も未決で、数字が出ると候補が既定値のように扱われる |

## 5. 未決事項

人の判断が要る点。いずれも推奨案を先頭に置く。

| # | 論点 | 選択肢（先頭が推奨） | 決める場所 |
|---|---|---|---|
| 1 | 負荷の状態の主軸 | **(a) 電力の帯を主軸・trace の Regime は副の切り口（§2.2 / §2.6。推奨）**／(b) trace の Regime だけ／(c) Regime の推定器を DB の上で再生する（`workload_regime` の値が承認された後の拡張として残す）／(d) (a) と (c) の両方を最初から | 本記録の承認 |
| 2 | 帯の初期値 | **(a) 次の `provisional` で始め、T1 の電力の分布を見て見直す（推奨）**: GPU `idle` 0 / `light` 30 / `high` 250 / `full` 450 W（アイドル約 6 W・TGP 600 W・0063 の 400 W と 0127 の 450 W の定常を跨がない）、CPU `idle` 0 / `light` 50 / `full` 120 W（全スレッドで約 200 W・0123）／(b) 先に T1 の電力の分布だけを出して帯を決めてから実装する／(c) 0063 の 400 W に揃えて GPU を `low` / `full` の 2 段にする | 本記録の承認（値は設定。変えるときは設定の差分と出力の来歴で追える） |
| 3 | `settle_ms`（移り変わり直後を除く長さ） | **(a) 300 s（推奨。2026-10-13 の 5 分の負荷で排気の温度がほぼ上がりきったため）**／(b) 600 s（筐体の空気がより落ち着くが、短い負荷が定常に入らない）／(c) 120 s | 本記録の承認 |
| 4 | `config/rules.yaml` の現在の閾値を並べ、超えた割合を出すか | **(a) 出さない（推奨。人が表と `rules.yaml` を並べて読む。CLI をルールの形の変化から切り離す）**／(b) `d.*` を対象にする `threshold` 型のルールだけ、閾値と超過の割合を参考に出す | 本記録の承認 |
| 5 | `fand_override`（`manual` / `max` / `calibration` の tick）の既定の扱い | **(a) 既定で除き件数だけ出す・`--include-override` で含める（推奨。試験の操作は普段の運転ではない）**／(b) 既定で含め別のグループにする | 本記録の承認 |
| 6 | 実装の担当（AGENTS.md「実装の担当」） | **(a) ローカル LLM（推奨。仕様が本記録で明確・`soak.py` の読み取り専用の型を模倣でき・実機不要のテストで確かめられる）**／(b) Claude | 本記録の承認の後、Issue で |
| 7 | `pwmN_enable=0`（引き継ぎの全速）のときに nct6799 の `pwmN` を読むと何が返るか・trace に backend の欄を足すか・引き継ぎの結果と fand の正常停止（run の終わりの印）を DB に残すか | **(a) 本記録では `bios_confirmed` だけで `bios` を判定し（§2.6a）、`pwmN` の読みの確認・trace の backend の欄（`ControlTick` の版上げ）・引き継ぎと正常停止の記録は別の Issue と記録に分ける（推奨。CLI のために制御の trace と引き継ぎ実行部の形を変えない。run の終わりの印が DB に入れば、`seq` が最大の区間を `segment_open` にせず閉じられる（§2.6a））**／(b) 先に `ControlTick` へ `backend` を、DB へ引き継ぎの zone ごとの結果と正常停止を足し、それを `cooling` の主の根拠にする | 本記録の承認。(b) は別の記録 |
