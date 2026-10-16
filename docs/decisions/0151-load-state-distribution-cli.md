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
  `settle_ms + power_window_ms + max(trace_max_age_ms, fan_max_age_ms)` で、設定から計算し別の値として持たない。
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

分布は次の鍵の組ごとに出す。

1. **`cooling`**（常に付ける）: 物理の Fan を誰が動かしていたか。値は `fand` / `bios` / `fand_override` /
   `undetermined` の 4 つで、判定は §2.6a。`fand_override` と `undetermined` は**既定では分布から除いて件数だけ
   数える**（`--include-override` で `fand_override` を含める。`undetermined` を含める選択肢は持たない）。
   Fan の制御が違う期間を混ぜると、同じ負荷でも温度差の分布がずれるため
2. **状態**（§2.2 の電力の帯）
3. **`period`**（任意）: `--periods <yaml>` に名前つきの区間（例: `ac_on` / `ac_off`、試験の区間）と除外する区間を
   書くと、区間の名前を鍵に足す。区間の外の評価時刻は使わない。エアコンの ON / OFF は DB に記録が無いので、
   人が区間で渡す（推測しない）
4. **`--group-by trace-regime`**（任意）: 状態の代わりに trace の `state.workload_regime` を鍵にする。trace は
   既存の `ControlTick` の検証を通ったものだけを使い、通らない行・Regime の無い tick は件数だけ数える。
   出力には「Regime の設定は `provisional`（0119 §2.14）」を必ず書く

### 2.6a `cooling` の判定: trace の有無ではなく、物理の Fan の観測と突き合わせる

**trace があることは「fand が物理の Fan を動かしていた」ことを意味しない。** fand は既定で `--backend simulated`
（`control_daemon.py` の `backend_factory_for`。0118 §2.5）で動き、そのまま既定の DB に向ければ物理の Fan は BIOS の
ままなのに trace が残る。逆に、trace が無い期間も BIOS とは限らない。fand が異常終了すると、引き継ぎ実行部
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
- `coldaisle-telemetry` が nct6799 の `pwmN` / `fanN_input` から読む `fan.{front,rear,top}.pwm`（`pwmN / 255 × 100` %。
  `internal_telemetry/hwmon.py`）と `fan.{front,rear,top}.rpm`（`config/internal-telemetry.yaml`。対応は
  `docs/fan-header-mapping.md`）

判定（評価時刻 `t` ごと）:

| 区分 | 条件（**すべて**を満たすときだけ） |
|---|---|
| `fand` | `[t - trace_max_age_ms, t]` に trace があり `operating_mode` が `auto`。その trace の 3 つの zone すべてで `hardware` があり **`write_ok` と `readback_ok` がともに true**（下の「書き込みを確かめられた tick だけを一致とする」）。3 つの zone すべてで、`[t - fan_max_age_ms, t]` に ok の `fan.<zone>.pwm` があり、trace の `pwm_raw / 255 × 100` との差が `pwm_match_tolerance_pct` 以内。さらに、その trace を含む**連続した trace の区間**（間隔が `trace_max_age_ms` を超えたら切る。`run_id` は v16 からなので使わない）の**始まりから終わりまで全体**（`--start` / `--end` の外も含む）で、突き合わせられた時刻のうち一致しなかった割合が `pwm_mismatch_max_ratio` 以下。区間の始まりを確かめられないとき（下の「区間の全体を読む」）は `undetermined` |
| `fand_override` | `fand` の条件のうち `operating_mode` だけが `auto` でない（`manual` / `max` / `calibration`。試験の操作） |
| `bios` | `[t - trace_max_age_ms, t]` に trace が無い。3 つの zone すべてで ok の `fan.<zone>.rpm` が `fan_max_age_ms` 以内にあり、**すべての zone が `full_speed_rpm_min.<zone>` 未満**。さらに、`t` より前に fand が動いていなかったか、直前の fand の停止の後が BIOS だったと確かめられる（下の「`bios` の前提」） |
| `undetermined` | 上のどれにも当たらない。例: trace はあるが物理の PWM と合わない（simulated の backend など）・区間の始まりを確かめられない・`fan.*` が欠けている・trace が無く 1 つでも全速の zone がある（引き継ぎの Max が一部の zone だけ成功した状態か、BIOS が高負荷で全速にしたかを区別できない）・trace が途絶えた後で、その停止が正常だったと確かめられない |

**書き込みを確かめられた tick だけを一致とする**（`fand` の判定）:

- `hardware.pwm_raw` は、書き込みやその確かめに失敗した tick にも残る。hwmon の backend の `_failed()` は写像した
  要求の PWM でこの欄を埋め、`ENABLE_REVERTED` のときはレジスタの値が入って `readback_ok = false` になる
  （`control/hardware/hwmon.py`）。前の duty が残っていた・BIOS の duty がたまたま同じだった、という tick を
  `pwm_raw` の一致だけで `fand` にしない
- そこで tick ごとの一致は、**3 つの zone すべてで `hardware` があり `write_ok` と `readback_ok` がともに true** で、
  かつ PWM の差が許容の内のときだけとする。flag のどれかが false・`hardware` が無い zone がある tick の評価時刻は
  `undetermined`
- 区間の不一致の割合では、そうした tick を**不一致として分子に数える**（分母は、区間の中で `fan.*.pwm` と
  突き合わせられた tick のすべて）。書き込みの失敗が多い運転を、確かめられた一致の多さで `fand` に入れないため。
  書き込みの失敗が続く区間は、区間ごと `undetermined` に倒れる

**区間の全体を読む**（`fand` の判定）:

- **区間は `seq` の順でたどる。** trace の書かれた順は `seq`（0007 で足した `NOT NULL UNIQUE` の列。書き手は
  `MAX(MAX(seq), pruned_through_seq) + 1` を挿入と同じ文で振る。`store/db.py`）で、`ts_ms` は壁時計なので時計が
  戻ると書いた順を表さない（0071 §2.2a の「並びは `seq`、`ts_ms` は絞り込みだけ」と同じ理由）。そこで、連続した
  trace の区間は「`seq` の昇順で隣り合う 2 行の `ts_ms` の差が `trace_max_age_ms` を超えたら切る」と定義する。
  差が負（時計が戻った）なら切らない（区間を短く切ると、外れた前半を落とせてしまうため、繋ぐ側に倒す）。
  `ts_ms` は、この途切れの比較と、評価時刻 `t` に対する trace の古さ（`[t - trace_max_age_ms, t]`）にだけ使う
- 評価時刻 `t` の窓に、別々の区間（`seq` でたどって別の区間）に属する trace が入ったとき（時計が戻った前後）は、
  どの区間の判定にも寄せず `undetermined` にする
- 不一致の割合は、`t` の trace を含む区間について、`seq` を小さい向きへ区間の本当の始まりまで、大きい向きへ
  終わりまでたどって読み、その区間の全体で数える。こうして判定が `--start` / `--end` の置き方に依らないようにする
- 決まった長さの窓（例: 直近 30 分の割合）にしないのは、simulated の運転で前半が外れ、窓に入る後半だけがたまたま
  合う、という場合を窓では捨てられないため。区間の全体なら、その運転全体の性質で判定できる
- 読むのは trace の `seq`・`ts_ms`・`state.operating_mode`・3 つの zone の `zones.<zone>.hardware.pwm_raw` /
  `write_ok` / `readback_ok`（`hardware` が無いことも含む）だけ（SQL の `json_extract` で取り出し、trace の全体を
  検証の型に通さない）と、同じ範囲の `fan.*.pwm` の ok の行。区間の割合の数え方は、上の tick ごとの規則と
  **同じ判定の関数**を使う（flag が false・`hardware` が無い tick は、PWM がたまたま合っていても不一致に数える）。
  2 つの経路で判定を書き分けない。保持期間（`control_trace_days` / `raw_days` 30 日）が上限なので、1 区間で最大
  約 260 万 tick。手動で時々走らせる CLI として許容する
- **区間の始まりを確かめられるか**は、時刻ではなく `seq` の境目で決める。根拠は `control_trace_prune` の列:
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
- 区間の最も小さい `seq` を `s0` とする。区間の始まりを**確かめられる**のは次のどちらかのとき:
  1. `s0 - 1` の行が残っていて、`s0` の行との `ts_ms` の差が `trace_max_age_ms` を超える（残っている行で途切れを見た）
  2. `s0 = 1` で、`legacy_through_seq = 0`（移行の前の行は無かった）
- それ以外（`s0 - 1` の行が無い。理由が削除でも欠番でも見分けない。`s0 ≤ legacy_through_seq` なら移行の前の行）は、区間の前半が消えたかもしれないので、その区間のすべての時刻を `undetermined` にする。区間の
  始まりが生データ（`fan.*.pwm`）の保持期間の外にあるときも同じ。消えた前半の一致を推測しない
- `legacy_until_ms` に近い時刻に書かれたというだけでは、今の行を拒まない（新しい DB では 0010 が
  `legacy_through_seq = 0` を記録し、最初の tick は `seq = 1` から始まるので 2 で確かめられる。ただし区間の中の
  `seq` の連続は別に求める）

**`bios` の前提**（引き継ぎの Max と BIOS を取り違えない）:

- 異常終了の後の引き継ぎは zone ごとに成否が分かれる（`safety_handoff.py` の `HandoffZoneResult` の `status`）。
  成功した zone は全速のまま、失敗した zone は fand が最後に書いた PWM のまま残りうる。そこで「1 つでも全速未満」では
  なく **すべての zone が全速未満**を求める。ただしそれだけでは、すべての zone の引き継ぎが失敗した状態を除けない
- 正常停止かどうかを DB で確かめられる記録は**無い**（2026-10-16 の main で確かめた）。fand は正常停止で引き継ぎの
  記録を消すだけで DB に書かない。引き継ぎの結果（`safety_handoff_completed` と zone ごとの `status`）は journal の
  ログにだけ出る。`control_admin_audit` は管理ソケットの指令だけで、`systemctl stop` を記録しない
- そのため `bios` は次のどちらかのときだけにする:
  1. fand がこの DB に**一度も書いていない**と `seq` の境目で示せる DB。示せるとは、`control_traces` に**全期間で
     1 行も無く**、`control_trace_prune` の `pruned_through_seq` が 0（消した行が無い）で、`legacy_through_seq` が 0
     （0010 の時点で移行の前の行が無かった）のときだけ。さらに `t` が `legacy_until_ms` より後であること
     （0007 より前の削除は記録が無いので、0007 を当てた時刻より前の期間には使わない。新しい DB では
     `legacy_until_ms` は DB を作った時刻で、それより前の readings は無い）。
     **trace の無い区間は、fand が動かなかった証拠にしない。** 保持期間の削除で古い trace が消えると、消した範囲と
     残る最初の trace の間には trace が無いが、その前の運転が異常終了し 3 つの zone の引き継ぎがすべて失敗していれば、
     全速未満の回転数のまま fand の後の状態が続きうる。trace が 1 行でもある DB・削除の記録がある DB では 1 を使わず、
     `bios_confirmed` が無ければ `undetermined`。fand がほかの DB へ書いていた期間は判定できない（fand は取り込みと
     同じ DB に書く前提。0080）
  2. `t` が `--periods` の **`bios_confirmed`** の区間の中にある。人が journal（`safety_handoff_completed` が無い、
     または `record_found: false`）や `systemctl` の記録で、直前の fand の停止が正常だったこと・再起動したことを
     確かめて書く。区間ごとに根拠の文（`basis`）を必須にし、出力の来歴にそのまま写す
- 上のどちらでもない trace の途絶の後は、物理の回転数が BIOS らしく見えても `undetermined`
- 引き継ぎの結果と fand の正常停止を DB に残すこと（例: 取り込みが読める追記の表）は、§5 #7 の trace の backend の欄と
  同じく別の記録の候補とする。それが入れば、`bios_confirmed` を人が書かずに済む

- 区間の単位で `pwm_mismatch_max_ratio` を見るのは、simulated の backend でも BIOS の PWM がたまたま指令と
  近い時刻があり、時刻ごとの一致だけでは `fand` に入ってしまうため
- `bios` の判定に PWM ではなく回転数を使うのは、`pwmN_enable=0`（全速）のときに `pwmN` を読んで何が返るかを
  導入先で確かめていないため（§5 #7）。回転数は物理の結果なので、enable の値に依らない
- 高負荷で BIOS が 1 つでも zone を全速にした時刻は `undetermined` に入り、`bios` の高負荷の分布から抜ける。件数は出すので、
  多ければ人が気づける。誤って引き継ぎの Max を BIOS に混ぜるより、抜けるほうを選ぶ
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
| `trace_max_age_ms` | trace があるとみなす古さの上限・連続した trace の区間を切る間隔 | 2000（tick 1000 ms の 2 倍） |
| `fan_max_age_ms` | `fan.*.pwm` / `fan.*.rpm` を評価時刻に使う古さの上限 | 5000（2 周期） |
| `pwm_match_tolerance_pct` | trace の `pwm_raw` と物理の `fan.*.pwm` の差の許容 | 2.0（約 5 / 255） |
| `pwm_mismatch_max_ratio` | 連続した trace の区間で一致しなかった割合の上限 | 0.05 |
| `full_speed_rpm_min.front` / `.rear` / `.top` | これ以上を全速とみなす回転数 | §5 #8 |

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
  backend を模した合成データ）・区間の不一致の割合が上限を超える・`fan.*` が欠けると `undetermined`。trace が無く
  すべての zone が全速未満で、それより前に trace が無く prune も無いか `bios_confirmed` の中なら `bios`。
  一部の zone だけ全速（引き継ぎの一部の成功を模した合成データ）・trace の途絶の後で `bios_confirmed` が無い・
  prune の記録がある DB では `undetermined`。`manual` の tick が `fand_override` で
  既定では除かれ、`undetermined` は件数だけに出る
- 書き込みの確かめ（§2.6a）: PWM が合っていても `write_ok` か `readback_ok` が false の zone がある tick
  （`ENABLE_REVERTED` で `readback_ok = false` を含む）・`hardware` の無い tick は `undetermined`。区間の中で
  そうした tick が `pwm_mismatch_max_ratio` を超えると、合っている tick も含めて区間ごと `undetermined`
- `seq` の境目（§2.6a）: 新しい DB（`legacy_through_seq = 0`）で最初の tick が `legacy_until_ms` から
  `trace_max_age_ms` 以内に書かれても、`seq = 1` から続く区間は確かめられて `fand` になりうる。`seq ≤ legacy_through_seq`
  の行から始まる区間・`s0 - 1` の行が無い区間は `undetermined`
- `seq` の穴（§2.6a）: `ts_ms` で消して seq 1 と 3 を残し 2 を消した合成データ（`tests/test_control_trace_api.py` と同じ形）で、
  `s0 = 1` でも区間が `undetermined` になる。重複で捨てた挿入による欠番も同じで、`seq_gaps` に数が出る
- 時計の戻り（§2.6a）: `seq` の順で `ts_ms` が戻る合成データで、区間が `seq` の順でたどられ（時刻の順に並べ替えない）、
  戻った所で区間が切れない。評価時刻の窓に 2 つの区間の trace が入ると `undetermined`
- `bios` の 1（§2.6a）: trace が 1 行でもある DB（`t` より後の trace だけでも）・`pruned_through_seq` が 0 でない DB・
  `legacy_through_seq` が 0 でない DB では、trace の無い時刻で全 zone が全速未満でも `bios_confirmed` が無ければ
  `undetermined`。保持期間で消した範囲と残る最初の trace の間の時刻も同じ
- 区間の割合の数え方（§2.6a）: flag が false の tick で PWM がたまたま観測と合う合成データで、その tick が区間の
  割合の不一致に数えられ、tick ごとの判定と区間の判定が同じ関数の結果になる
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
多ければ `undetermined` が増えるので、その件数を先に確かめる。BIOS の期間との比較は、同じ DB の
T1 より前の期間を別に走らせて並べる。導入先の DB には試験（0121 の T0〜T9）の trace が
あるので §2.6a の 1 は使えず、BIOS の期間（試験の前も、試験の間と後で fand が止まっていた期間も）は、所有者が journal を
確かめて `bios_confirmed` を書いたときだけ `bios` に入る。

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
| 高負荷で BIOS がどれかの zone を全速にした時刻と、fand を一度動かした後で `bios_confirmed` を書いていない期間が `undetermined` になり、`bios` の分布が薄くなる | 件数を出すので気づける。引き継ぎの Max を BIOS に混ぜるより安全側。trace に backend と引き継ぎが載れば解消する（§5 #7） |
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
| 7 | `pwmN_enable=0`（引き継ぎの全速）のときに nct6799 の `pwmN` を読むと何が返るか・trace に backend の欄を足すか・引き継ぎの結果と fand の正常停止を DB に残すか | **(a) 本記録では回転数と `bios_confirmed` で `bios` を判定し（§2.6a）、`pwmN` の読みの確認・trace の backend の欄（`ControlTick` の版上げ）・引き継ぎと正常停止の記録は別の Issue と記録に分ける（推奨。CLI のために制御の trace と引き継ぎ実行部の形を変えない）**／(b) 先に `ControlTick` へ `backend` を、DB へ引き継ぎの zone ごとの結果と正常停止を足し、それを `cooling` の主の根拠にする | 本記録の承認。(b) は別の記録 |
| 8 | `full_speed_rpm_min` の値 | **(a) T2・T5・T6 の記録の全速の回転数（Front / Rear / Top）の最小から 5 % 引いた値を所有者が確かめて置く（推奨）**／(b) `docs/fan-header-mapping.md` の定格から決める | 本記録の承認（値は設定） |
