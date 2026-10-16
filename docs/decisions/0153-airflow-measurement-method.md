# 決定記録 0153: 3 系統 Fan の風量の測り方（回転数の掃引・熱収支による実効風量・測定中の安全・結果の置き場所）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし
- **関連**: [`0026`](0026-three-zone-fan-control.md) /
  [`0028`](0028-fan-control-contracts.md) §2.4 / §2.5 (a) /
  [`0033`](0033-air-balance-config-boundary.md) /
  [`0072`](0072-control-admin-entry.md) /
  [`0073`](0073-air-balance-control-config-integration.md) §2.3 / §5 /
  [`0078`](0078-air-balance-fallback-coordination.md) /
  [`0109`](0109-dataset-declared-changes-cli.md) /
  [`0119`](0119-safety-values-first-host.md) §2.2 / §2.14 /
  [`0120`](0120-fan-hardware-first-host.md) §2.2 / §5 /
  [`0121`](0121-hardware-test-procedure.md) §2.1 / §2.6 / §2.9 /
  [`0123`](0123-cpu-power-floor-tests.md) /
  [`0125`](0125-stall-rise-criterion-tach-update.md) /
  [`0126`](0126-startup-settle-front-spinup.md) /
  [`0128`](0128-gpu-fan-curve-service.md) / [`0130`](0130-gpu-fan-quieter-curve.md) /
  `docs/airflow-model.md` / `docs/airflow-sources.md` / `docs/air-balance-model.md` /
  `docs/fan-header-mapping.md` / `docs/spec-review.md` I-01・C-05 / AGENTS.md ルール 1〜3・8・9・10
- **対象 Issue**: #75（関連 #81 / #77 / #83）

---

## 1. Context

#75 は、Front / Rear / Top の風量を共通の尺度（Effective Flow。`q_front` / `q_rear` / `q_top`）で比べられるようにし、
#81 の Air Balance Model と #83 の Thermal Dataset へ渡すことを求める。風量の**測り方**はまだ決まっていない。
本記録は測り方だけを決める。実装と実機の測定は行わない（測定は後で所有者が実機で行う）。

いまの状態:

- `fan-hardware.yaml` の Fan profile は 0120 で承認済み。`pwm_to_rpm` の 0.40 以上は BIOS の運転記録の中央値
  （BIOS が PWM を動かしている途中の点を含む）、0.25 は 9月の概数の 1 点。`airflow_index` は「RPM ÷ demand 1.0 の RPM」
  （風量は回転数におおむね比例するという近似）。0120 §5 は `airflow_index` の実測を Air Balance を有効にする前の宿題にした
- `air-balance.yaml` は `tests/fixtures/` の `uncalibrated` の例だけで、導入先では Air Balance は無効（0073 §2.3。
  `source.status: calibrated` にすると有効になり、毎 tick の推定が trace に載る。協調の `mode` は `fan-policy.yaml` で別に `off`）。
  曲線は zone ごとに `demand → airflow_index → effective_flow` の点の列で、demand 0.0 / 1.0 と airflow_index 0.0 / 1.0 を必須にし、
  airflow_index 0.0 の effective_flow は 0.0 にする
- 原則（`docs/airflow-model.md`・#75・#81）: ファン径 × RPM で絶対 CFM を計算しない。公称 CFM を装着時の風量とみなさない。
  `airflow_index` は zone の中だけの比で、zone をまたいで比べない。EFU は物理 CFM でなくてよい。圧力センサーや常設の風量計は
  使わず、一時的な風速の計測は検証の補助にとどめ、v1 の必須条件にしない
- 測れるもの: 空気の温度 `air.front_intake` / `air.gpu_intake` / `air.gpu_exhaust` / `air.top_exhaust` / `air.rear_exhaust` / `air.room`
  （DS18B20・11 bit・2.5 s 周期。位置は spec-review I-01 / C-05）、湿度 `air.room_humidity`、`cpu.package`・`gpu.0.core` / `hotspot`、
  `board.connector_12v2x6`（T_SENSOR）、`power.gpu.0`（NVML）、`power.cpu.package`（RAPL。0122）、`fan.<zone>.rpm` / `pwm`、
  GPU の Fan の目標値 `gpu.0.fan_speed`、fand の decision trace（tick ごとの requested / effective / RPM / `bound_by`）
- GPU（RTX PRO 6000 Workstation Edition）は flow-through で、排気は上の Top のラジエーターへ向かう（C-05）。Top の排気には
  CPU の熱と GPU の熱が混ざる
- 制御の経路の制約（0121 で実機で確かめた）: manual でも最低安全 Demand 0.35・Guard の floor・Critical Safety の floor・
  `ramp_down_per_s` 0.05/s は掛かる。Top には `cpu_cooling_floor`（60 °C → 0.35・80 °C → 1.0）と `cpu_power_cooling_floor`
  （100 W → 0.35・200 W → 1.0。0123）が掛かる。nct6799 の `fanN_input` は読んだときに更新され、約 1.5 s より短い間隔では
  変わらない（0125）。Front は 0.35 から全開まで最長約 12.8 s かかる（0126）

## 2. Decision

### 2.1 何で推定するか（3 段の分担）

| 段 | 量 | 測り方 | 役割 |
|---|---|---|---|
| (a) 事前情報 | 公称の最大回転数・CFM・静圧（`docs/airflow-sources.md`） | 測らない | 記録だけ。どの値の計算にも使わない |
| (b) 回転数の表 | `pwm_to_rpm`（demand → RPM） | §2.3 の回転数の掃引 | 0120 の運転記録の中央値を、制御された掃引の値で置き換える。**風量ではない** |
| (b') zone の中の比 | `airflow_index(d)` = その zone の EFU を demand 1.0 の値で割った値 `q_z(d) / q_z(1.0)`（§2.4a で風速計を使ったときは、その出口の風速から求めた流量の比と突き合わせる） | Top / Rear は §2.4 の熱の掃引、Front は §2.4 の零位法の点 | 相対の風量を測った値にする。RPM の比は取り付けた状態のインピーダンスで風量に比例しなくなるので使わない（`docs/airflow-model.md`）。zone をまたいで比べない |
| (c) zone をまたぐ尺度 | `effective_flow(d)`（EFU） | §2.4 の熱収支（固定の熱負荷での温度差と電力） | `q_front` / `q_rear` / `q_top` の値 |

**EFU の定義**: 1 EFU = 1 W/K の「熱を運び出す実効のコンダクタンス」（空気の流れが温度差 1 K あたりに運ぶ熱。ρ·c_p·V に当たる量）。
物理の単位を持つが、空気の温度は 1 点のセンサーで測り（流れの平均ではない）、漏れの経路を含まないので、**CFM・m³/s へ換算して
表示しない**。換算の係数を設定・UI・報告に置かない。センサーを動かしたら測り直す（§2.8）。

**熱で測った EFU だけでは zone をまたいで比べられない。** 1 点のプローブの温度と、その出口の流量で重みづけした平均の温度との
関係は出口ごとに別々にずれる。`air.top_exhaust` は GPU の排気とラジエーターの排気が混ざった空気を測り、`air.rear_exhaust` は
別の出口の別の流れを測る（spec-review I-01・C-05）。どちらも再現性が高くても、`q_top` と `q_rear` の比はこのずれの比の分だけ
狂いうる。`q_front` も零位法の和（§2.4）を通してそのずれを受け継ぐ。したがって次のとおりとする。

- **`air-balance.yaml` を `calibrated` にする条件は §2.6 の 4 の一覧だけ**とし、その中に §2.4a の zone をまたぐ独立の確認（出口ごとの
  プローブの補正と、独立の `q_top / q_rear` の比較の**両方**）を含める。補助の測定は任意ではない。確かめ方と計測器の要否は §5 の 2 で所有者が決める
- それまでの EFU は **provisional** とし、使ってよいのは次だけにする
  - 報告（`docs/airflow-characterization-*.md`）に「provisional・zone をまたいで比べない」と明記して載せること
  - 同じ zone の中の形の比較（正規化した EFU と RPM の比（代用）の突き合わせ）
  - Mock / Replay / オフラインの試算で、`allow_uncalibrated_for_testing` を明示して使うこと（0033）
- provisional の EFU を使わないもの: `calibrated` への切り替え、#81 の `balance` / `thermal_limits` の値の根拠、zone をまたぐ
  優先順位の結論（0026 の Front + Rear 優先の見直しなど）、Thermal Dataset・Learned の入力や評価の根拠
- 応答の行列（§2.5）は各温度の直接の変化で、EFU の換算を通らないので、このずれの影響を受けない。#75 の「どの zone を上げると
  どの温度がどれだけ下がるか」の説明には応答の行列を使う
- `fan-hardware.yaml` の `pwm_to_rpm`（§2.3）は回転数の表で風量を含まないので、この条件に関わらず承認できる
- `airflow_index` は zone の中で正規化した EFU なので、出口ごとの掛け算のずれは割り算で消えるが、漏れや混ざり方による足し算のずれは残る。
  そのため **§2.4a の 1 と 2 の両方を済ませるまでは `fan-hardware.yaml` の `airflow_index` を今の代用（0120 の RPM の比）のまま置き**、`basis` に
  「RPM の比による代用。風量を測った値ではない」と書く。熱から求めた `airflow_index` は報告に provisional の候補として載せるだけにする。
  trace の `airflow_index` はこの値をそのまま運ぶので、代用の間は報告・画面で「測った風量」と書かない

RPM の読み戻しから推定する方式（0073 §5）には切り替えない。Air Balance は今までどおり `applied_demand` を曲線へ通す。
`air-balance.yaml` の点の `airflow_index` は (b') と同じ正規化した EFU にし、`effective_flow` はその EFU にする。参考として、
RPM の比と正規化した EFU の差（取り付けた状態で風量が回転数に比例しない度合い）を zone ごとに報告に残す。

### 2.2 共通の決まり（測定中の安全）

- **制御の経路を迂回しない。** Demand は `coldaisle-control manual`（管理ソケット。人の uid）で決め、Guard と Critical Safety を通す。
  sysfs・hwmon へ試験の側から書かない。本番の設定（`/etc/coldaisle/control-config/`）のまま測り、**試験専用の設定は使わない**
  （0121 §2.8 のような floor を外す設定を使わない）。0.35 未満は測らない（§2.9）
- fand は 0121 と同じく `systemctl start`（`enable` しない）で動かす。0121 T1 の soak と同時には行わない（統計を混ぜない）
- **lease**: 各段の `manual` は、その段の最長の時間 + 5 分の `--lease` で送る（`manual.max_lease_s` の 4 時間を使わない）。
  段ごとに送り直す。所有者が離れて送り直しが止まれば lease が切れて `AUTO` に戻る
- **CALIBRATION モードは使わない。** 0028 §2.5 の `CALIBRATION` は管理ソケットが受けず、MANUAL で足りる。新しい入口を足さない
- **floor が掛かった段は捨てる。** 段のどの測定窓（負荷なし・負荷ありのすべて）の中でも、**3 zone のどれか**で trace の `effective` が
  `manual` の値と違う tick（`bound_by` が `safety_floor` / Guard の floor / `forced_max` など）が 1 つでもあれば、その段は「floor に縛られた」として解析から外し、
  manifest（§2.7）に理由を残す。floor を下げて測り直すことはしない
- **中止の基準**は 0121 §2.1（0130 で置き換えた後）と同じ: `cpu.package` 88 °C・`gpu.0.core` 85 °C・`board.connector_12v2x6` 70 °C。
  どれかに届いたら、負荷を止めて `coldaisle-control max`。Critical Safety が `DEGRADED` / `EMERGENCY` になった・fault が出た段は
  使わず、負荷を止めて原因を見る。fand の挙動がおかしければ `systemctl stop coldaisle-fand`（BIOS へ返す）
- **負荷**は GPU と CPU を別々の固定の負荷にし、普段の最大を超えない（0121 §2.1 と同じ流儀）
  - GPU: 電力の上限を 300 W にして行列計算で上限に張りつかせる（600 W では Front / Rear の低い段で Guard の floor と
    中止の基準に近づき、段が floor に縛られるため。§5 の 5 で (a) を選べば、Rear の高い段の L_gpu で 450 W まで上げる）。終わったら上限を 600 W に戻す。負荷のスクリプトは SIGTERM で同期してから終わる（0121 T4 の記録）
  - CPU: CPU の負荷を使う窓（L_cpu。Top の段・Rear の段・零位法）で、`cpu_cooling_floor`（60 °C → 0.35・80 °C → 1.0）と
    `cpu_power_cooling_floor`（100 W → 0.35・200 W → 1.0）が、そのときの Top の Demand から 0.05 低い値を超えない範囲の負荷
    （`stress-ng` の worker の数）を選ぶ（§5 の 5。Top が高いほど重くできる）。
    段ごとの負荷を本番の前の予備の試行で決めて manifest に残す。floor が掛かった段は今までどおり捨てる
- **GPU の Fan**: EFU の差を取る窓どうしでは GPU の Fan の振る舞いを同じにすることを要件にする（§2.4 の同条件の検査）。GPU の Fan は
  fand ではなく GPU コアの温度で `coldaisle-gpu-fan`（0128 / 0130）が動かすので、GPU の負荷の窓（L_gpu）ではそのままでは満たせない。
  どうそろえるかは所有者の承認が要る判断で、§5 の 3 で決める。決まるまで、GPU の負荷を使う EFU（Rear と Front）は測らない。
  回転数の掃引と応答の行列は、今の曲線のまま行える。§5 の 3 で (a) を選んだときは、**EFU を出すすべての測定（Top と Rear の掃引・零位法の
  手順 1〜7・§2.4a の確認）で同じ固定の duty（主の duty。例 60 %）を使う**（3 zone の EFU を同じ GPU の Fan の状態でそろえる）。承認の前に曲線のまま測った
  Top の EFU は provisional の下見とし、承認の後に同じ duty で測り直す。主の duty と違う duty での影響は §2.4 の「GPU の Fan の duty の影響」で測る
  応答の行列（§2.5）は運転のままの系の応答を測るので、GPU の Fan は 0130 の曲線のまま動かし、`gpu.0.fan_speed` を記録する
- **所有者が在宅で**、各セッションの開始・負荷の切り替え・終了を行う。操作のスクリプトは事前に内容を示し、所有者が実行する（0121 §2.1）。
  スクリプトがするのは `coldaisle-control manual` / `auto` / `max` と負荷の開始・停止と manifest の追記だけ
- 測定の期間中は較正を変えない（`coldaisle-calibrate --apply` をしない。§2.7）
- 試験の側から `fanN_input` を高い頻度で読まない（読む頻度が更新を変える。0125）。回転数は fand の trace と `coldaisle-telemetry` の値を使う
- **LLM は関わらない。** 手順の実行・解析・承認のどこにも LLM を使わない。解析の結果を LLM のプロンプトへ入れない（ルール 1 / 8）
- **窓の覆いの検査**: 値を取るすべての窓（§2.3 の 60 s の窓・§2.4 の負荷なし / 負荷ありの 2 分の窓・零位法の観測の 30 s の窓・§2.5 の 2 分の窓）
  について、次の 2 つを検査し、満たさない窓は**値を計算する前に**捨てる（捨てた理由 `telemetry_coverage` / `trace_coverage`）。捨てた窓を
  含む段・回は、§2.4 の不確かさ・3 回のまとめ・§2.6 の 5 の承認の目安に入れない。残った数個の読み値から `s²/n` を出して
  `low_signal` を通ること、trace の欠けで一時的な floor が隠れることを防ぐ
  - **metric ごとの覆い**（その窓で使う metric のすべて。温度・電力・`gpu.0.fan_speed`・`fan.vrm.rpm`・`fan.aio_pump.rpm`・`air.room` など）:
    窓の中の行が**すべて** `quality = ok`（`stale` / `suspect` / 欠けの行が 1 つでもあれば捨てる。0029 の分類を値として使わない）・ok の行の数が
    `min_ok_samples` 以上・隣り合う ok の行の時刻の差と、窓の始まりから最初の行・最後の行から窓の終わりまでの差が `max_sample_gap_ms` 以下。
    値は各 metric の自分の行の時刻で使い、補間・リサンプルしない（0151（FINAL）§2 と同じ）。閾値は収集の周期が違うので
    source ごと（基板の `air.*`・`coldaisle-telemetry` の NVML / hwmon・`coldaisle-cpu-power`）に持つ
  - **trace の覆い**（fand の decision trace）: 窓に入る trace を `seq` の昇順で並べ（`seq` は並べるためだけに使う。`SequencedControlTrace` の
    約束のとおり `seq` は欠番を持ちうるので、その連続は求めない）、(1) すべての tick が同じ `run_id`（fand の再起動をまたがない。測定は
    `run_id` を持つ版の trace でだけ行う）、(2) その `run_id` の中で `tick_id` が窓の始まりから終わりまで 1 ずつ連続している（fand は tick ごとに
    `tick_id` を 1 ずつ振るので、欠けた tick が無いことをこれで確かめる）、(3) 隣り合う 2 行の `ts_ms` の差と、窓の始まりから最初の tick・最後の
    tick から窓の終わりまでの差が `trace_max_gap_ms` 以下、(4) tick の数が `min_trace_ticks` 以上、(5) すべての tick で `operating_mode` が
    `manual` で、その段の指令（`command_id`）のまま・Critical Safety が `NORMAL`・3 zone の `effective` が manual の値と同じ（§2.2 の floor の決まり）。
    (1)〜(4) は「floor が掛かっていないこと」を、欠けた tick の無い trace で確かめるためにある。
    0151（FINAL）は `run_id` の無い古い trace も読むので、`seq` の欠番がある区間を保守的に `undetermined` にする。本記録は `run_id` と
    `tick_id` で欠けた tick を直接確かめられる新しい trace だけを使うので、`seq` の欠番を問わない。目的と入力が違うので矛盾しない
  - 閾値 `min_ok_samples`・`max_sample_gap_ms`（source ごと）・`trace_max_gap_ms`・`min_trace_ticks` は §2.4 の「設定の値」と同じ
    `config/airflow-characterization.yaml` に `status: provisional` で置く。提案の初期値は、基板の 2.5 s の周期で 2 分の窓に対し `min_ok_samples` 44
    （48 の約 9 割）・`max_sample_gap_ms` 6000（2 周期と少し）、trace は `trace_max_gap_ms` 1500（0121 T1 の tick の間隔の上限）・`min_trace_ticks` は
    窓の長さ ÷ 1000 ms の 95 %。窓の長さが違う窓（60 s・30 s）には同じ割合で当てる

### 2.3 回転数の掃引（`pwm_to_rpm` と Fan profile）

- 条件: アイドル（`cpu.package` 50 °C 未満・`gpu.0.core` 45 °C 未満・`power.gpu.0` 50 W 未満）。対象でない 2 zone は manual 0.60
- 段: 0.35・0.40・0.45・0.50・0.60・0.70・0.80・0.90・1.00 を上りで、続けて下りで（ヒステリシスを見る）
- 落ち着き: 各段は、trace の `effective` が指示の値に届いた tick から 30 s 待つ（Front の全開まで最長約 12.8 s・tach の更新 1.5 s の
  2 回分に余裕。下りは `ramp_down_per_s` で律されるので、指示の時刻ではなく届いた tick から数える）。続く 60 s の trace の回転数の
  中央値をその段の値にする
- 回数: zone ごとに 3 回。うち少なくとも 1 回は別の日にする
- 応答の時間: 上りの段の、`effective` が変わった tick から trace の回転数が新しい段の値の ±5% に入って留まった tick まで（0126 と同じ測り方。
  tach の更新 1500 ms を足して記録）。下りは `ramp_down` の律速なので記録だけにする
- 出すもの: zone ごとの RPM(d)（3 回の中央値・範囲。`pwm_to_rpm` の候補）、観測の最大 RPM、上りと下りの差。`airflow_index` は出さない（§2.1）

### 2.4 熱収支による実効風量（EFU）

Top と Rear を、それぞれ 0.35・0.50・0.60・0.70・0.80・1.00 の **6 段**に変え、ほかの 2 zone を基準 R = 0.60 に保つ。曲線の点（§2.6）は
この 6 段で、`airflow_index` の分母の 1.00 も実測の段にする。温度差はすべて `air.front_intake` を基準にする（室温の揺れを打ち消す）。

**負荷あり − 負荷なしの差で計算する。** 出口の空気には、アイドルの GPU・CPU・マザーボード・VRM・ポンプ・Fan の熱がいつも入っている。
負荷ありの値だけで割ると、分母の温度上昇にはこの基準の熱が入るのに分子は負荷の電力だけになり、EFU を系統的に小さく見積もる。
そこで各段を、**3 zone の Demand を変えないまま**、負荷なしの窓（B）と負荷ありの窓（L）を交互に並べて測る。各窓は §2.5 の落ち着きを待った
最後の 2 分の平均。

- Top の段: B → L_cpu（CPU の固定の負荷・GPU はアイドル）→ B'
- Rear の段: B → L_cpu → B_mid → L_gpu（GPU の固定の負荷・CPU はアイドル）→ B'。L_cpu は、その Rear の段のときの Top のコンダクタンス
  `q_top(R | d_r)` を測るため（下の「zone の間の結合」）

各窓の**代表の時刻**（窓の中の sample の時刻の平均）を manifest に残す。L の基準は、L の直前と直後の負荷なしの窓（Top の段は B と B'、
Rear の段の L_cpu は B と B_mid、L_gpu は B_mid と B'）を **L の時刻へ線形に内挿した値**にする（落ち着きの待ちは窓ごとに違い、L は前後の
真ん中にあるとは限らないため）。前後の窓の時刻を `t_1 < t_L < t_2` とし、満たさない L は捨てる（内挿の分母を正にする）。

```text
x_base(t_L) = x_1 + (x_2 - x_1) * (t_L - t_1) / (t_2 - t_1)
Δx = x_L - x_base(t_L)
```

前後の負荷なしの窓で `air.*` の `air.front_intake` との差が 0.3 °C を超えて違う L は「基準のずれ」として捨てる（内挿は直線のずれだけを
打ち消し、大きなずれは曲がっている恐れがあるため）。`ΔT_z = Δ(T_z_exhaust − T_front_intake)`。電力の差 `ΔP_cpu` / `ΔP_gpu` も同じ内挿で求める。

**差を取る窓どうしで、測っている量（負荷）以外の条件をそろえる（同条件の検査）。** L と前後の負荷なしの窓のすべてで、次が成り立たない L は
捨てる（捨てた理由 `condition_mismatch`）。

| 条件 | 検査 |
|---|---|
| 3 zone の Demand | 3 zone のすべてで、trace の `effective` が manual の値と同じ（§2.2 の floor の決まり。対象でない zone も含む） |
| GPU の Fan | `gpu.0.fan_speed` の窓の中央値の差が `max_gpu_fan_delta_pct` 以内（§5 の 3。GPU の Fan は GPU コアの温度で動くので、GPU の負荷の L ではそのままでは満たせない） |
| VRM の Fan・AIO ポンプ | `fan.vrm.rpm`・`fan.aio_pump.rpm` の窓の中央値の差が `max_aux_fan_delta_rpm` 以内（どちらも fand の制御の外。VRM の Fan は BIOS が動かしうる） |
| 部屋 | 上の「基準のずれ」と、§2.5 の `air.room` の 1 °C の決まり |

**zone の間の結合**: Rear の Demand を変えると筐体の内の圧力が変わり、同じ Top の Demand でも Top の出口の風量が変わる。Rear の式で
Top の分を差し引くとき、Top の掃引（Rear を R に固定）の値を使うと、その違いがそのまま `q_rear` に入る。そこで Rear の各段で、同じ状態の
L_cpu から `q_top(R | d_r)` を測って使う（§5 の 6。推奨。Rear の段の窓が 3 から 5 に増える）。Top の掃引の `q_top(R)` との差は
「zone の間の結合の大きさ」として報告し、Air Balance Model の「各 zone の風量は自分の Demand だけで決まる」という近似の誤差として #81 へ渡す。

**GPU の Fan の duty の影響（普段の運転に当てはまるか）**: 本番の GPU の Fan は 0130 の曲線で 30〜100 % を動き、Top の経路と筐体の圧力を変える。
実行時のモデルは GPU の Fan の状態を読めない（fand は GPU の Fan を読まない。AGENTS.md・0128）ので、1 つの duty で測った曲線が普段の運転に
当てはまることを、測って確かめる。次の (A) を採る（推奨。§4 に (B) を却下した理由）。

- 主の duty（§5 の 3 (a) の固定の値）での全部の測定に加えて、`gpu_fan_duty.levels`（30・60・100 %。0130 の曲線の範囲の下端・中ほど・上端）の
  主でない duty で、次の部分の測定を行う（各 3 回。ほかの決まりは主の duty と同じ）
  - Top: R と 1.00 の段（GPU はアイドル）
  - Rear: R と 1.00 の段（L_gpu を含む）。ただし固定の duty が曲線の値を下回る窓は捨てる（§5 の 3 (a) の「曲線より冷やさない」）ので、
    GPU 300 W で曲線が 30 % を超える 30 % の duty では Rear を測れない。本番でも 30 % は GPU コアが 45 °C 未満のとき（GPU の熱が小さいとき）だけなので、
    30 % では Rear を測らず、次の零位法の `d_f*` のずれで圧力の釣り合いへの影響を確かめる
  - 零位法: 組（R, R）と（1.00, 1.00）の手順 1〜5（アイドルの `d_f*`）
- duty の影響の大きさ:

  ```text
  effect_top   = max_段 |q_top(duty) - q_top(主)| / q_top(主)            （30・100 %）
  effect_rear  = max_段 |q_rear(duty) - q_rear(主)| / q_rear(主)          （100 %）
  effect_null  = max_組 |q_front_curve(d_f*(duty)) - q_front_curve(d_f*(主))| / q_front_curve(d_f*(主))（30・100 %。`d_f*` のずれを
                 主の duty の Front の曲線で相対の風量に直す）
  ```

  3 つの**上側の境界**（§2.6a）がどれも `max_gpu_fan_duty_effect` 以下のときだけ `calibrated` にできる（§2.6 の 4 の 7）。超えたら、GPU の Fan の
  状態で風量が変わることを 1 次元の曲線では表せないので `calibrated` にしない。GPU の Fan の状態を入れたモデルは、fand が GPU の Fan を読まない
  決まり（0128）に関わるので、別の決定記録で扱う
- 時間: 30 % で Top 約 4.5 時間・零位法 約 1 時間、100 % で Top 約 4.5 時間・Rear 約 7.5 時間・零位法 約 1 時間。合わせて約 20 時間増える（§2.5）

**結合の上限（1 次元の曲線で表せるか）**: 本番の Air Balance Model は各 zone を自分の Demand だけで評価する。結合が大きいと、Rear が R から
離れたときの `q_rear + q_top` と協調が間違う。そこで次の 2 つの結合の比を求め、どちらかが `max_coupling_ratio` を超えたら `calibrated` に
しない（§2.6 の 4 の 5）。そのときは 1 次元の曲線では表せないので、相互作用を入れたモデルを別の決定記録で扱う。

```text
# (i) Rear を変えたときの Top: Rear の 6 段の q_top(R | d_r)（各段は有効な 3 回の平均）
coupling_top_by_rear = (max_d_r q_top(R | d_r) - min_d_r q_top(R | d_r)) / mean_d_r q_top(R | d_r)
# (ii) 零位法の 5 つの組の状態で測った q_rear / q_top と、1 次元の曲線のその Demand の値との差
coupling_null = max_組 max( |q_rear(組) - q_rear_curve(d_r)| / q_rear_curve(d_r),  |q_top(組) - q_top_curve(d_t)| / q_top_curve(d_t) )
```

(ii) は Top を変えたときの Rear（組（R, 0.80））と、Front を `d_f*` にしたときの両方（Top / Rear の掃引は Front を R に固定）を含む。
比は測った値だけでなく**上側の境界**（`比 + bound_coverage_factor × u(比)`）で `max_coupling_ratio` と比べる（§2.6a）。`u(比)` は、比に入る各段の
平均の不確かさ（§2.6a の `u(平均)`）から伝える。上側の境界が上限を下回らなければ `calibrated` にしない（§2.6 の 4 の 5）。

- **Top（`q_top`）**: 負荷で増えた CPU Package の熱は AIO の冷却水を経てほぼすべて Top のラジエーターの空気へ移ると近似する。

  ```text
  q_top(d_t) = ΔP_cpu / ΔT_top                                  [W/K]（Top の段の L_cpu）
  ΔP_cpu = Δ power.cpu.package（RAPL）
  ```

  残る偏り: 負荷で増える VRM の損失は RAPL に入らず、VRM の Fan で筐体の空気へ出る（一部は Rear へ）。その分は小さいが残るので、
  §2.4a の確認で見る

- **Rear（`q_rear`）**: 負荷で増えた熱（GPU と、負荷の間に動く CPU の分）は Rear と Top の流れが運び出すとし、Top の分を同じ Rear の段の
  `q_top(R | d_r)` で差し引く（GPU の排気は Top へ向かう。C-05）。

  ```text
  q_top(R | d_r) = ΔP_cpu[L_cpu] / ΔT_top[L_cpu]
  q_rear(d_r)    = (ΔP_gpu + ΔP_cpu - q_top(R | d_r) * ΔT_top) / ΔT_rear   [W/K]（Δ はすべて L_gpu）
  ΔP_gpu = Δ power.gpu.0（NVML のボードの電力）
  ```

  パネルの隙間などの漏れで出る熱は `q_rear` に含まれてしまう（「Rear に帰した値」）。6 段の点の形を優先し、絶対値の偏りは §2.4a で確かめる

- **Front（`q_front`）**: 吸気の側には熱が入らないので、熱収支だけでは Front の流量を Rear / Top と同じ尺度に直接載せられない。
  **吸気と排気が釣り合う点を見つける「零位法」**で Front を排気の尺度へ結びつける。手順は下の「零位法」。

**割る前の条件（信号 ÷ 不確かさ）**: すべての割り算について、分母（と、引き算で小さくなりうる Rear の分子）の不確かさを、
そこへ入る**すべての温度と電力の項**から伝えて見積もり、条件を満たさない点は出さない（捨てた理由 `low_signal`）。3 回が同じ値に
量子化されて範囲の検査を素通りすることや、Top の引き算で電力の増分がほとんど打ち消された雑音だけの分子が通ることを防ぐ。

**窓の平均の不確かさ（相関を見込む）と窓の中の傾き**: 2 分の窓の中の温度と電力はゆっくり変わるので、続いたサンプルは相関する。
`s² / n` は独立なサンプルを仮定し、独立の証拠が増えないのに不確かさを √n 近く小さくしうるので使わない。代わりに**ブロックに分けた推定**を使う。

- 各窓を時刻で `window_blocks` 個（k）の同じ長さのブロックに分け、ブロックごとに ok の行の平均を取る（ブロックの中の行が §2.2 の覆いを
  満たさない窓は既に捨てている）
- 窓の平均の不確かさは、ブロックの平均のばらつきと、窓を k 個の独立のサンプルとしか見なさない値の大きい方にする（保守的）:
  `u_mean(x̄) = max( sd(ブロックの平均) / sqrt(k),  sd(窓の行) / sqrt(k) )`
- 窓の中の線形の傾き `b` を最小二乗で求める。`|b| × 窓の長さ` が `max_window_trend`（温度と電力で別の値）を超える窓は、落ち着いていない
  として捨てる（`window_trend`）。超えない窓でも、傾きが窓の平均を代表の時刻の値として使う誤りを `u_trend(x̄) = |b| × 窓の長さ / sqrt(12)`
  として足す

```text
# 窓 1 つの平均 x̄ の不確かさ（相関と傾きを見込む）
u_win(x̄) = sqrt(u_mean(x̄)^2 + u_trend(x̄)^2)
# 内挿した差 Δx（x は温度の差 T_z - T_front_intake、または電力）。w = (t_L - t_1) / (t_2 - t_1)
u_noise(Δx) = sqrt(u_win(x̄_L)^2 + (1 - w)^2 * u_win(x̄_1)^2 + w^2 * u_win(x̄_2)^2)
u_drift(Δx) = drift_residual_fraction * |x_2 - x_1| # 直線の内挿で取り切れない曲がりの分
u_res(ΔT)   = 0.125 / sqrt(12) * sqrt(4)            # DS18B20 の分解能。2 本のプローブ × L と基準の 2 窓。平均しても量子化は減らないとみなす
u_res(ΔP)   = 1 / sqrt(12) * sqrt(2)                # 電力の 1 W の丸め。L と基準の 2 窓
u(ΔT_z) = sqrt(u_res(ΔT)^2 + u_noise^2 + u_drift^2)
u(ΔP)   = sqrt(u_res(ΔP)^2 + u_noise^2 + u_drift^2)

# Top（Top の段の q_top と、Rear の段の q_top(R | d_r) で同じ式）
q_top = ΔP_cpu / ΔT_top
u(q_top) = q_top * sqrt((u(ΔP_cpu)/ΔP_cpu)^2 + (u(ΔT_top)/ΔT_top)^2)
# Rear の分子 N（L_gpu の差）
N = ΔP_gpu + ΔP_cpu - q_top(R | d_r) * ΔT_top
u(N) = sqrt(u(ΔP_gpu)^2 + u(ΔP_cpu)^2 + (ΔT_top * u(q_top(R | d_r)))^2 + (q_top(R | d_r) * u(ΔT_top))^2)
q_rear = N / ΔT_rear
u(q_rear) = q_rear * sqrt((u(N)/N)^2 + (u(ΔT_rear)/ΔT_rear)^2)
# airflow_index（zone の中の正規化）
airflow_index(d) = q_z(d) / q_z(1.0)

条件（k = min_signal_to_uncertainty）:
  Top と L_cpu: ΔT_top > 0 かつ ΔT_top / u(ΔT_top) >= k かつ ΔT_top >= min_delta_t_c
                ΔP_cpu >= min_delta_power_w かつ ΔP_cpu / u(ΔP_cpu) >= k
  Rear（L_gpu）: ΔT_rear > 0 かつ ΔT_rear / u(ΔT_rear) >= k かつ ΔT_rear >= min_delta_t_c
                ΔP_gpu + ΔP_cpu >= min_delta_power_w
                N > 0 かつ N / u(N) >= k（Top の項の温度と電力の不確かさをすべて含めた u(N) で判定する）
                同じ段の L_cpu が上の Top の条件を満たすこと
  airflow_index: 分母の q_z(1.0) が上の条件を満たした段の値（有効な 3 回）であること。満たさなければ、その zone の airflow_index を出さない
  Front: 零位法の和は割り算を含まない。和の不確かさと d_f* の不確かさを下のとおり伝える
```

ばらつき（範囲 ÷ 平均）の分母の平均は、上の条件を満たした正の値だけから作る。

**零位法（Front）**:

0. **手順 1〜7 を通して、測っている量（Front の Demand と負荷）以外の条件を同じにする。** 圧力の零点は筐体の中のすべての Fan の
   流れで決まるので、どれかが変われば `q_front(d_f*) = q_rear + q_top` の等式が成り立たなくなる
   - **GPU の Fan**: §5 の 3 で (a) を選んだときは、手順 1 の前に GPU の Fan を EFU の窓と同じ固定の duty にし、手順 7 の終わりまで変えない
     （向きの探索・二分法・5 つの熱の窓・確かめ直しのすべてで同じ duty。曲線の値が固定の値を超えた窓は同条件の検査で捨てる）。
     ほかを選んだときは、手順 1〜7 のすべての窓で `gpu.0.fan_speed` が手順 2 の最初の窓の値から `max_gpu_fan_delta_pct` 以内であることを求める
   - **Rear と Top の Demand**: 手順 1 の組の値のまま、手順 7 まで変えない（trace の `effective` が manual の値と同じ。§2.2 の floor の決まり）
   - **VRM の Fan・AIO ポンプ**: 手順 1〜7 のすべての窓で、`fan.vrm.rpm`・`fan.aio_pump.rpm` が手順 2 の最初の窓の値から
     `max_aux_fan_delta_rpm` 以内（負荷の窓で VRM の Fan が上がれば、その回を捨てる）
   - **部屋**: 1 つの回の中で `air.room` が 1 °C を超えて動かない（§2.5）
   - manifest に、GPU の Fan の状態（固定の duty の値か曲線か）と各窓の値を残す。得た `q_front` の曲線は「その GPU の Fan の状態での値」で、
     運転中の GPU の Fan の違いによる零点のずれは、Air Balance Model の近似の誤差として報告に残す（§2.4a で見る）
   - **漏れの経路の一覧**: `q_front(d_f*) = q_rear + q_top` は「Front / Rear / Top 以外の経路を通る正味の流れが 0」と置くことである。漏れの
     経路が複数あり筐体の中に圧力の勾配があれば、1〜2 か所の向きが変わっても、ほかの経路の流れが残り正味は 0 にならない。そこで測定の前に、
     Front / Rear / Top の Fan 以外で空気が出入りしうる筐体の開口（パネルの合わせ目・拡張スロットの覆い・I/O の面・電源の吸排気・底面の
     通気口など）を**すべて**一覧にし、所有者が承認する（manifest と報告に一覧の版を残す）。**小さい開口も省かず、「無視できる経路」の区分は
     設けない**（§5 の 7 で (a) に決定）。一覧の各経路を次のどちらかに分ける
     - **観測する経路**: 圧力の釣り合いで向きが変わる経路。手順 2〜7 の向きの観測は**一覧の観測する経路のすべて**で行う。外から向きを
       見られない経路があれば、零位法の等式に測らない項が残るので、**Front は `calibrated` の条件（§2.6 の 4 の 2）を満たさない**
     - **駆動される経路**: Fan で駆動され、圧力の釣り合いで向きが変わらない経路（例: 筐体の中から吸う電源の Fan。電源の吸気の向きは
       実機で確かめて一覧に書く）。向きの合わせ方（手順 2）には入れない。§5 の 2 の風速計でその開口の流量を測り、同じ計測器で測った
       `q_rear + q_top` に当たる流量（§2.4a の 2 の出口の多点）に対する割合を求め、その上側の境界（§2.6a）が `null.max_driven_leak_ratio`
       以下であることを求める。超えた・測っていない・上限が未承認なら、**Front は `calibrated` の条件を満たさない**。割合は等式に足して
       補正せず、上限として扱う
1. Rear と Top を基準の組（Rear, Top）=（0.35, 0.35）・（R, R）・（0.80, R）・（R, 0.80）・（1.00, 1.00）の 5 つのどれかに保つ。
   手順 2〜5 はアイドルで行う（圧力差の向きに熱は要らず、floor に縛られない）
2. Front を §2.3 の段（0.35〜1.00）で上りに動かし、各段で `effective` が届いてから 30 s 待ち、続く 30 s の間の圧力差の向き（`+` 吸気が多い・
   `−` 排気が多い・`?` 決まらない）を、漏れの経路の一覧（手順 0）の**観測する経路ごとに**記録する（見方は §5 の 1）。その段の向きは、
   観測する経路の**すべて**が `+` なら `+`、すべてが `−` なら `−`、それ以外（経路の間で食い違う・どれかが `?`）は `?` とする。
   この合わせ方で、手順 3〜5 の区間は「すべての経路が内向き」の段から「すべての経路が外向き」の段までを覆う。Rear と Top を固定して
   Front を上げると筐体の中の圧力は上がり、各経路の外向きの流れは単調に増えるので、正味の漏れが 0 になる点はこの区間の中にある
   （経路ごとの向きが変わる点の違いは区間の幅に入る）。正味の漏れの残りは、`d_f*` の不確かさ `u(d_f*)`（手順 5。`max_null_halfwidth` で上限）と、
   それを Front の曲線の傾きで風量に直した `u_bracket(q_front)`（手順 8）で運ぶ。本番の `ZoneFlowCurve` は点の demand と EFU だけを持ち
   `u(d_f*)` を捨てるので、風量の誤差として上限を持たせる
3. 向きが `−` から `+` に変わる隣の 2 段を区間 [d_lo, d_hi] とする（`?` の段は区間の内側に含め、端にしない）。区間が無い（0.35 で既に `+`、
   または 1.00 でも `−`）組は、`d_f*` が測れる範囲の外として記録し、点を出さない
4. **二分法**で区間を狭める: 区間の中点を manual で指示し、2 と同じ待ちと観測をして、向きに合わせて区間の片方を置き換える。区間の幅が
   `null_resolution` 以下になるか、中点で `?` が出たら止める（`?` の点は区間の端にしない）
5. `d_f*` = 最後の区間の中点、`u(d_f*)` = 最後の区間の半幅。半幅が `max_null_halfwidth` を超える回は使わない（`null_unresolved`）
6. その状態（Front を `d_f*`、Rear と Top を組の値、GPU の Fan は手順 0 の状態）のまま、Rear の段と同じ 5 つの窓（B → L_cpu → B_mid → L_gpu → B'）で
   `q_top` と `q_rear` を**その状態で**測り（上の結合のため、6 段の掃引の値を流用しない）、`q_front(d_f*) = q_rear + q_top`、
   `u(q_front) = sqrt(u(q_rear)^2 + u(q_top)^2)` とする。同条件の検査と信号の条件は上と同じ。
   **負荷の間の零点の確認**: CPU / GPU の熱で筐体の中の空気が温まると浮力で圧力の零点がずれ、負荷の窓の間は等式が成り立たなくなりうる。
   そこで L_cpu と L_gpu のそれぞれで、窓の 2 分の平均を取り終えた後、**負荷を止めずに** Front を `d_lo` にして手順 2 と同じ待ちと観測、
   続けて `d_hi` で同じ観測をし、Front を `d_f*` に戻してから負荷を止める（L の窓の値はその前に取り終えているので、Front を動かしても
   変わらない。次の負荷なしの窓は `d_f*` で落ち着きを待ち直す）。`d_lo` が `−`、`d_hi` が `+` でなければ（`?` を含む）、負荷の間に
   零点が最後の区間の外へずれたとして、その回を捨てる（`null_shifted_under_load`）。Front を動かしている間も floor の決まりと
   中止の基準は同じ
   **観測の安全**: 向きの観測（§5 の 1 の糸・紙片）は、筐体を開けず、筐体の外の隙間・メッシュの面に外から当てて目で見るだけにする。
   手・道具を筐体の中へ入れない（回転中の Fan・12V-2x6 のコネクタに近づかない）。負荷の間の排気は温かいが、外から見るだけなので
   触れない。外から見られる観測点が無い組は、負荷の間の確認ができないので、その組の点を出さない。負荷の間の確認と手順 7 の確かめ直しの
   向きも、手順 2 と同じく観測する経路のすべてを合わせて判定する
7. **零点の確かめ直し**: 手順 6 の B' の後、負荷を止めたまま（手順 2〜5 と同じアイドルの条件）、最後の区間の両端 `d_lo` と `d_hi` で
   手順 2 と同じ観測をし直す。`d_lo` が `−`、`d_hi` が `+` のままでなければ（`?` を含む）、手順 6 の間に零点がずれたとして、その回を捨てる
   （`null_shifted`）。負荷の間のずれは手順 6 の「負荷の間の零点の確認」で捉える
8. 曲線: 組ごとの点（3 回の平均の `d_f*` と `q_front`）を線形につなぐ（RPM の比の形は使わない）。点の demand の不確かさは
   `u(d_f*)` の 3 回の最大と 3 回の範囲の半分の大きい方、EFU の不確かさは `u(q_front)` の最大と 3 回の範囲の半分の大きい方で、報告に残す。
   **区間の誤差を風量へ伝える**: 真の零点が区間の端にあると、点は横にずれ、傾きの大きい曲線ではそれが風量の誤差になる。点ごとに、その点の
   左右の区間の傾き（端の点は片側）の大きい方 `s_max` で `u_bracket(q_front) = s_max × u(d_f*)` とし、点の EFU の不確かさを
   `u_total(q_front) = sqrt(u(q_front)^2 + u_bracket(q_front)^2)` にする。`u_total` は EFU を値として使うすべての比べ方（`coupling_null`・
   `effect_null`・§2.4a・EFU のばらつき）で使う。demand の軸の `u(d_f*)` は範囲の覆い（手順 9）にだけ使い、同じ比べ方の中で 2 つの軸に
   重ねない。さらに、すべての点で `u_bracket` の上側の境界の割合 `bound_coverage_factor × u_bracket(q_front) / q_front` が
   `null.max_bracket_flow_error` 以下であることを `calibrated` の条件にする（§2.6 の 4 の 2。超えた点は区間を狭めて測り直す）
9. 範囲の判定: 最も低い点の `d_f* + bound_coverage_factor × u(d_f*)` が `0.35 + front_coverage_tolerance` 以下、かつ最も高い点の `d_f* − bound_coverage_factor × u(d_f*)` が
   `1.00 − front_coverage_tolerance` 以上のときだけ「範囲を覆う」とする（不確かさの分だけ厳しく判定する）。覆うときに限り、端の点から
   0.35 と 1.00 まで、隣の区間の傾きのまま伸ばした点を足し、報告に「端の延長」と示す。覆わなければ Front は `calibrated` の条件
   （§2.6 の 4）を満たさない。覆わないときに組を足すかは §5 の 1 と合わせて所有者が判断する。Front の `airflow_index` は demand 1.00 の
   点（端の延長を含む）で割って求め、覆わなければ代用のまま（§2.1）
10. 圧力差の向きを何で見るかは §5 の 1 で決める。決まるまで `q_front` は作らない

§2.5 の応答の行列の Front の段（F+ / F−）の熱の応答（`d.case_delta`・`air.gpu_intake`・`d.gpu_preheat`）を、零位法の結果と矛盾しないかの
目安にする（Front を上げても排気の温度差が下がらなくなるのは、吸気が足りている側）。

この節と §2.2・§2.5 の閾値との比較は、すべて §2.6a の形（上側・下側の境界）で行う。解析が使う値は §2.6b の設定に置く。

**繰り返しとまとめ方**:

- Top と Rear のそれぞれで 6 段の掃引を**独立に 3 回**行う。各回は別のセッション（負荷の開始・落ち着きの待ちからやり直す）で、3 回のうち少なくとも
  1 回は別の日にする（§2.3 と同じ）。時間によるずれを打ち消すため、回ごとに上り（0.35 → 1.00）と下り（1.00 → 0.35）を交互にする
- EFU は回ごとに段ごとに計算する（Rear の `q_top(R | d_r)` は同じ回の同じ段の L_cpu の値で、回をまたいで組み合わせない）
- 段の値は、有効な 3 回の EFU の**平均**とし、ばらつきは 3 回の**範囲 ÷ 平均**で表す（§2.6 の 5）
- 捨てた回（floor・未収束・`air.room` のずれ・同条件の不一致・信号の不足・中止）があり、その段の有効な回が 3 に届かなければ、届くまで追加の回を行う。
  **有効な回が 3 未満の段は値を出さない**（1 回だけの観測を、範囲 0 として目安に通さない）
- Front の零位法も、**5 つの基準の組のすべて**で、それぞれ独立に 3 回（別のセッション・少なくとも 1 回は別の日）行う。有効な回が 3 に
  届かない組は、届くまで追加の回を行い、届かなければその組の点を出さない

曲線の点: Top / Rear は測った 6 段の EFU、Front は零位法の点（と端の延長）に、形式上の端点 `{demand: 0.0, airflow_index: 0.0, effective_flow: 0.0}` を足す。0.0〜0.35 は測らず、
運転では使わない（applied demand は最低安全 Demand 0.35 と `minimum_stable_demand` の下に行かない）。Rear は PWM 0 でも回る
（約 440 RPM）ので、この端点は実際の風量より小さいが、届かない範囲の形の上の値として扱う。demand 1.0 の `airflow_index` は 1.0。

### 2.4a zone をまたぐ独立の確認（`calibrated` の前提）

§2.1 のとおり、1 点のプローブのずれは出口ごとに違う。`calibrated` にする前に、熱の EFU と独立な方法で、少なくとも次の 2 つを確かめる。

1. **出口ごとのプローブのずれ**: Top と Rear の出口で、固定のプローブの温度と出口の面の平均（できれば流速で重みづけした平均）の温度の
   関係を求め、出口ごとの補正の係数にする。補正の後の値で `q_top` / `q_rear` を計算し直す。
   **Top は熱源ごとに求める。** `air.top_exhaust` は GPU の排気とラジエーターの排気が混ざる 1 点なので、プローブと出口の平均の関係は
   どちらの熱が入るかで変わりうる（GPU の熱がプローブの近く・遠くに偏る）。そこで Top の補正の係数を、CPU の負荷（L_cpu。ラジエーターの熱）と
   GPU の負荷（L_gpu）の**両方**で同じ方法で求める。`q_top(R | d_r)` と Top の掃引の `q_top` は L_cpu の係数で、Rear の式の
   `q_top(R | d_r) × ΔT_top` の `ΔT_top`（L_gpu の差）は L_gpu の係数で補正する。2 つの係数の差は報告に残す。Top の L_gpu の補正が無い間は
   Rear の曲線は provisional のまま（§2.6 の 4 の 4）
2. **zone の間の比**: 熱の EFU（補正の後）の比 `q_top / q_rear` を、**両方を同じ状態で測った**点（3 zone とも R の状態＝Top と Rear の
   掃引の 0.60 の段、と零位法の 5 つの組の状態。結合があるので、別の状態で測った値どうしの比は使わない）で、同じ状態で独立に流量を
   測る方法（風速計の多点）で求めた比と、
   それぞれの不確かさとともに比べる。Front は吸気の面で同じ方法が使えるなら `q_front` も比べ、使えなければ零位法の和が補正の後の
   `q_rear + q_top` を使っていることで代える

**1 と 2 の両方**を済ませて初めて §2.4a を済ませたとする。プローブを動かす温度の分布（1）だけでは流量の比を独立に確かめたことにならず、
風速計の比（2）だけでは出口ごとの補正が無い。

- 確認は §2.4 と同じ固定の負荷・同じ基準の段で行い、制御の決まり（§2.2）も同じにする
- 確かめ方（計測器を買うか）は §5 の 2 で所有者が決める。1 と 2 の両方を済ませるまで、§2.4 の EFU は provisional のまま（§2.1）
- 一致の上限 `approval.max_independent_ratio_gap`（熱の比と独立の比の差が何 % 以内なら一致とするか）は、**§2.4a の測定を始める前に**
  承認する。値は、§5 の 2 で選ぶ計測器に求める精度から導く（下の表の案）。§5 の 2 の計測器を決める承認の記録で、この値を provisional として
  同時に決める。**結果を見た後に上限を広げることは、新しい決定記録でしかできない**（結果を受け入れる承認の記録の中で広げない）。
  観測した食い違いに合わせて上限を広げると、確認が循環するため
- 比べるのは差の上側の境界（`|差| + bound_coverage_factor × u(差)`。§2.6a）。**不確かさのモデルは次の 1 つにする**: 独立の比（風速計）の
  誤差は上限 `approval.max_independent_ratio_gap` の側に入れてあるので、`u(差)` には**熱の比の不確かさだけ**を入れる
  （`u(差) = u(q_top / q_rear)`。補正の後の `u(q_top)` と `u(q_rear)` から伝える）。独立の比の `u` を `u(差)` に足さない（計測器の誤差を
  上限と境界の両方で二重に数えない）。§2.6a の表の行もこの形にする

| §5 の 2 の選択肢 | 計測器の精度から見積もる、独立の比の誤差 | 上限の案 |
|---|---|---|
| (a) 羽根車式の多点 | 1 点の読み値 ±(3 % + 0.1〜0.2 m/s) に、格子の後ろの流れの不均一による 1 出口の流量 ±10〜20 %。2 つの出口の比で約 ±15〜25 % | 25 % |
| (c) (a) と (b) の組み合わせ | 比は (a) と同じ。温度の分布（(b)）は熱の側の補正に使い、独立の比の精度は変えない | 25 % |
| (e) 熱線式の多点（と (b)） | 1 点の読み値 ±(5 % + 0.05 m/s)。低い流速でも読めるが、不均一の誤差は (a) と同じ程度。比で約 ±15〜20 % | 20 % |
| (b) だけ・(d) | 独立の比が無い | —（`calibrated` にならない） |
  食い違いが説明できなければ `calibrated` にしない（条件の一覧は §2.6 の 4）

### 2.5 応答の行列（Thermal Effectiveness）と落ち着きの判定

- 負荷: (i) GPU 300 W だけ、(ii) CPU の軽い固定の負荷だけ、(iii) 両方。各負荷で、3 zone を R = 0.60 にした基準の段と、1 zone だけを
  0.90（+）・0.35（−）にした段、3 zone とも 0.90 の段を、**基準の段を間に挟んで**行う（R, F+, R, F−, R, Rr+, R, Rr−, R, T+, R, T−, R, All+, R）。
  基準の段の繰り返しから、時間の流れによるずれ（部屋・冷却水の蓄熱）を直線で補正する
- 見る指標: `air.gpu_intake`・`d.case_delta`・`gpu.0.core` / `hotspot`・`cpu.package`・`air.gpu_exhaust` / `rear_exhaust` / `top_exhaust`
  （それぞれ `air.front_intake` との差）。`air.room` / `air.room_humidity`・`board.connector_12v2x6`・`gpu.0.fan_speed`・`fan.vrm.rpm`・`fan.aio_pump.rpm`・電力も残す
- 比べる 2 つの段（基準の段と動かした段）で、動かした zone 以外の条件をそろえる: 動かしていない zone の `effective` が manual の値と同じ
  （floor の決まり）・負荷（電力の上限と worker の数）が同じ・`fan.aio_pump.rpm` の差が `max_aux_fan_delta_rpm` 以内。GPU の Fan と VRM の Fan は
  温度に応じて動くのが運転のままの応答の一部なので、そろえずに記録する（応答の行列は「運転のままの系で、その zone を動かすと何が起きるか」を測る）
- **熱の入力をそろえる**: Fan を変えると CPU / GPU の温度が変わり、boost・throttle・リークで、同じ電力の上限・同じ worker の数でも実際の電力が
  変わりうる。動かした段と、その前後の基準の段（時刻で内挿した値）の `power.cpu.package` と `power.gpu.0` の窓の平均の差が、上側の境界で
  `response_matrix.max_power_delta_w` 以下でない組は捨てる（`power_mismatch`。§2.6a）。**捨てる方を選ぶ。** 測った熱の入力の差で正規化する方法は、
  指標ごとに温度と電力の関係（熱抵抗）を別に測ってその線形性を確かめる必要があり、その模型の誤差が Fan の効果に混ざるため。捨てた組は、
  電力の差（と throttle の flag）を報告に残す（Fan を下げて throttle に入ること自体は意味のある結果なので、黙って消さない）
- 回数: 負荷ごとに 2 回（別の日）
- **落ち着き**: 0121 T4 の測り直しと同じ基準。直近 3 分の幅が、空気の温度 0.5 °C 以内・`cpu.package`（3 点の中央値）/ `gpu.0.core` /
  `board.connector_12v2x6` 1 °C 以内になったら落ち着いたとし、最後の 2 分の平均を値にする。20 分で落ち着かなければ「未収束」として
  解析から外す
- 1 つのセッションの中で `air.room` が 1 °C を超えて動いたら、そのセッションをやり直す（0121 §2.9 の吸気の決まりと同じ趣旨）
- 時間の目安（1 つの窓を落ち着きの待ちを含めて平均約 15 分とする）: 回転数の掃引 約 4 時間、Top の EFU 約 14 時間（6 段 × 3 窓 × 3 回）、
  Rear の EFU 約 23 時間（6 段 × 5 窓 × 3 回）、零位法 約 23 時間（向きの観測と二分法 約 4 時間 + その状態の 5 窓 × 5 組 × 3 回）、
  応答の行列 約 23 時間（15 段 × 3 負荷 × 2 回）、§2.4a の確認 約 8 時間、GPU の Fan の duty の影響 約 20 時間。**合計 約 115 時間**。所有者が在宅のセッションに分け、数週間かける

### 2.6 結果の置き場所と provisional の扱い

1. **生の記録**は今までどおり DB（telemetry と `control_traces`）にある。測定の区間と段は run の manifest（§2.7）に書き、
   `var/airflow-characterization/` に置く（`var/` は git の管理外）
2. **解析の入力は本番の DB そのものではなく、checkpoint された 1 ファイルの写し**にする。取り込み・`coldaisle-telemetry`・fand が書いている
   最中の DB を `immutable=1` で開くと、checkpoint されていない WAL の内容を黙って無視し、直前の測定を含まない古い断面を読みうる
   （`EvidenceDatabase` は空でない `-wal` / `-journal` があれば拒む。`src/coldaisle/evaluate.py`）。書き手は止めない
   - 写しは、既存の手順（`docs/ubuntu-deploy.md` の移行の手順 2）と同じ SQLite のオンラインバックアップ `sqlite3 <本番の DB> ".backup <写し>"`
     で作り、`PRAGMA integrity_check;` が `ok` であることを確かめる。`.backup` は書き手と読み手が動いたままでも整合した 1 ファイルを作り、
     `-wal` / `-shm` を伴わない
   - **権限**: 本番の DB のディレクトリは `2770`・グループ `coldaisle` で、運用者の uid からは読めない（0080 §2.1）。写しは所有者の許可を得て
     root で作り（`sudo`。0121 の試験の書き出しと同じ流儀）、運用者の作業ディレクトリ（`var/airflow-characterization/`）へ置いて運用者の
     所有・`0600` にする。運用者を `coldaisle` グループに入れて読ませる方法は取らない（書き込みの権限まで渡すため）
   - 写しは、回（セッション）の終了の時刻より後に作る。解析は、写しの中の telemetry と `control_traces` の最後の時刻が manifest の
     終了の時刻以上であること（区間を覆うこと）を確かめ、覆わなければ拒否する
   - manifest は写しに結びつける: 写しの SHA-256・作った時刻（ms）・元の DB に対する相対の名前（絶対パスは書かない）を manifest に追記する。
     解析は写しの SHA-256 を計算し直して照合し、違えば拒否する。写しを作り直したら manifest も更新する
   - 解析は読み取りだけの道具（実装は別の PR）が manifest と写し（`EvidenceDatabase` と同じ `immutable=1` と添えファイルの検査）を読み、
     zone ごとの RPM・`airflow_index`・EFU の点・応答の行列・再現性を JSON と Markdown の 1 ファイルに書く。設定・DB を書かない。
     control・AI 層・API から import しない
3. **報告**は #75 の成果物 `docs/airflow-characterization-YYYY-MM-DD.md` に値を残す（個体識別子・ホスト名・絶対パスを書かない。ルール 10）
4. **承認**は値ごとに新しい決定記録で所有者が行う（0120 と同じ流儀）。承認の前に設定を変えない
   - `fan-hardware.yaml`（導入先の `/etc/coldaisle/control-config/`。リポジトリに置かない）: 各 zone の `profile.pwm_to_rpm` と
     `profile.airflow_index` を置き換える。`pwm_to_rpm` は §2.3 の値。`airflow_index` は §2.4a の 1 と 2 の両方の後に、同じ demand の点での正規化した EFU
     （熱の 6 段の間の demand は隣の 2 段から線形に補間し、報告に補間した点を示す。単調に増えなければ値を出さず測り直す）。§2.4a の前は
     代用のまま（§2.1）。点の数をそろえる。0.35 未満の 0.25 の点は運転で届かないので 0120 の値（代用）を残し、`basis` にそう書く。`maximum_rpm` は
     観測の最大に約 5% を足す 0120 の規則のまま。ファイルの `approval` は `confirmed` のままで、`basis` を承認の記録に変える
   - `air-balance.yaml`: `zones.<zone>.points` を §2.4 の点にする。`source.status` はファイルに 1 つなので、**次の条件がすべてそろうまで
     `uncalibrated` のまま**にする。本記録で `calibrated` の条件を書くのはこの一覧だけで、ほかの節はここを指す
     すべての回が同じ Control Config（4 ファイルの SHA-256 が同じ。§2.7）で測られていること。ここで「有効な回」は、§2.2 の窓の覆い・floor の決まり、§2.4 の時刻の内挿・基準のずれ・同条件の検査・窓の傾き・信号 ÷ 不確かさ、
     零位法の `null_unresolved` / `null_shifted` / `null_shifted_under_load`、§2.6 の 2 の DB の写しの束縛、§2.7 の較正の束縛のすべてを通った回をいう
     1. Top と Rear の 6 段のすべてで有効な 3 回があり（Rear は同じ段の L_cpu も有効）、承認の目安（下の 5）を満たす
     2. Front の零位法の 5 つの組のすべてで有効な 3 回があり、点の範囲が `d_f*` の不確かさを含めて 0.35〜1.00 を覆う（§2.4 の零位法の 9）。
        漏れの経路の一覧（零位法の 0）が承認され、すべての回が承認した版の一覧のすべての観測する経路で向きを見ており、外から向きを見られない
        経路が一覧に無く、駆動される経路は風速計で測った割合の上側の境界が承認した `null.max_driven_leak_ratio` 以下であること。
        すべての点で区間の風量の誤差が `null.max_bracket_flow_error` 以下であること（零位法の 8）
     3. GPU の Fan のそろえ方（§5 の 3）が決まって承認され、(a) を選んだときは EFU を出すすべての測定（Top と Rear の掃引・零位法・§2.4a）が
        同じ主の duty で行われている（duty の影響を測る部分の測定（§2.4）を除く。曲線のまま測った下見の Top の EFU は使わない。§2.2）
     4. §2.4a の 1（出口ごとのプローブの補正。Top は L_cpu と L_gpu の両方の熱源で）と 2（同じ状態の点での独立の `q_top / q_rear` の比較。差の上側の境界を、測定の前に承認した上限と比べる。§2.4a・§2.6a）の**両方**を済ませ、結果を
        承認の記録で所有者が受け入れる
     5. zone の間の結合の比 `coupling_top_by_rear` と `coupling_null` の**上側の境界**がどちらも `max_coupling_ratio` を下回る（§2.4 の結合の上限・§2.6a）
     6. `balance`・`thermal_limits`・`thermal_inputs` の値が #81 の決定記録で承認されている（下の項）
     7. GPU の Fan の duty の影響 `effect_top`・`effect_rear`・`effect_null` の上側の境界が、どれも `max_gpu_fan_duty_effect` 以下（§2.4 の GPU の Fan の duty の影響）

     2 つの zone だけで `calibrated` にしない（比が作れない）。条件を満たす前の値は provisional で、§2.1 の範囲でだけ使う。
     値を書いても `uncalibrated` の間は Air Balance は無効で、Demand は変わらない（0073 §2.3）
   - `calibrated` にするには、曲線に加えて `balance`（目標の比と帯）・`thermal_limits`・`thermal_inputs` の値が要る。これらは本記録では
     決めず、#81 で応答の行列を材料に別の決定記録で決める。`calibrated` にすると Air Balance の推定が trace に載り、Learned MPC を
     配備したときは balance の項が効く（0073 §2.3）。協調の `mode` は `fan-policy.yaml` で別に `off` のまま（0078）
5. 承認の目安（承認の決定記録で所有者が最終に決める。比べ方は §2.6a の上側の境界）: 回転数は段ごとの 3 回（§2.3）の範囲が中央値の 3% 以内、EFU は段ごとの
   有効な 3 回の独立な掃引（§2.4）の範囲が平均の 15% 以内。有効な回が 3 未満の段は目安を当てずに値を出さない。
   外れた段は値を出さず、理由を報告に残す。EFU はこの目安を満たしても、上の 4 の条件がそろうまで provisional

### 2.6a 閾値との比べ方（上側・下側の境界）

本記録の閾値との比較は、**測った値だけで比べない**。上限の検査は `値 + bound_coverage_factor × u(値)`（上側の境界）が上限以下、下限の検査は
`値 − bound_coverage_factor × u(値)`（下側の境界）が下限以上のときだけ通す（`bound_coverage_factor` は §2.4 の設定。provisional）。
不確かさが上限・下限をまたぐ値（例: 結合 9 % ± 5 % で上限 10 %）は通さない。`u` が求められない値は通さない。

| 検査（節） | 量 | `u` の求め方 | 比べ方 |
|---|---|---|---|
| 落ち着き（§2.5） | 直近 `settle.span_s` のブロックの平均の最大 − 最小 | 分解能 `u_res` × √2 | 上側 ≤ `settle.max_range_air_c` / `settle.max_range_chip_c` |
| 窓の傾き（§2.4） | `|b| × 窓の長さ` | ブロックの平均から求めた傾きの標準誤差 × 窓の長さ | 上側 ≤ `max_window_trend` |
| 基準のずれ（§2.4） | 前後の負荷なしの窓の差 `|x_2 − x_1|` | `sqrt(u_win(x̄_1)^2 + u_win(x̄_2)^2)` | 上側 ≤ `max_baseline_shift_c` |
| 同条件: GPU の Fan（§2.4・零位法の 0） | 窓の中央値の差 | 目標値の 1 % の丸め（1/√12 × √2） | 上側 ≤ `max_gpu_fan_delta_pct` |
| 同条件: VRM の Fan・ポンプ（§2.4・§2.5・零位法の 0） | 窓の中央値の差 | 各窓の `u_win` の二乗和の平方根 | 上側 ≤ `max_aux_fan_delta_rpm` |
| 部屋（§2.5・零位法の 0） | セッションの中の `air.room` のブロックの平均の最大 − 最小 | `u_res` × √2 | 上側 ≤ `max_room_drift_c` |
| 温度上昇・電力の増分の最小（§2.4） | `ΔT_z` / `ΔP` | §2.4 の `u(ΔT_z)` / `u(ΔP)` | 下側 ≥ `min_delta_t_c` / `min_delta_power_w` |
| 信号 ÷ 不確かさ（§2.4） | `値 / u(値)` | — | ≥ `min_signal_to_uncertainty`（それ自体が不確かさの検査） |
| 回転数のばらつき（§2.6 の 5） | 3 回の範囲 ÷ 平均 | 各回の `u`（窓の中央値の `u_win` と tach の更新の量子化）から、範囲は √2 × 最大の `u`、平均は 最大の `u` / √3 | `(範囲 + k × u(範囲)) / (平均 − k × u(平均))` ≤ `approval.max_rpm_spread` |
| EFU のばらつき（§2.6 の 5） | 3 回の範囲 ÷ 平均 | 各回の §2.4 の `u(q)` から同じく | 同じ形で ≤ `approval.max_efu_spread` |
| GPU の Fan の duty の影響（§2.4） | `effect_top` / `effect_rear` / `effect_null` | 両方の duty の段の平均の `u(平均)` から伝える | 上側 ≤ `max_gpu_fan_duty_effect` |
| 結合の比（§2.4） | `coupling_top_by_rear` / `coupling_null` | 各段の平均の `u(平均) = max(伝えた u / √3, 3 回の範囲 / 2)` から伝える | 上側 < `max_coupling_ratio` |
| 零位法の区間の風量の誤差（零位法の 8） | `u_bracket(q_front) / q_front` | —（それ自体が不確かさ。`bound_coverage_factor` を掛けて比べる） | `k × u_bracket / q_front` ≤ `null.max_bracket_flow_error` |
| 零位法の駆動される経路（零位法の 0） | 駆動される経路の流量 ÷ `q_rear + q_top` に当たる流量（同じ風速計） | 風速計の読み値と面積の不確かさから伝える | 上側 ≤ `null.max_driven_leak_ratio`（零位法の測定の前に承認） |
| Front の範囲の覆い（零位法の 9） | 端の点の `d_f*` | `u(d_f*)`（零位法の 8） | 最低の点の上側 ≤ 0.35 + 許容・最高の点の下側 ≥ 1.00 − 許容 |
| 零位法の半幅（零位法の 5） | 区間の半幅 | —（指示した Demand の差で、測った値ではない） | ≤ `max_null_halfwidth` |
| 零点の確認（零位法の 6 の負荷の間・7 のアイドル） | 両端の向き | —（定性的） | `d_lo` が `−`・`d_hi` が `+`。`?` は不合格 |
| §2.4a の一致（§2.4a） | 熱と独立の方法の比の差 | 熱の比の `u` だけ（独立の比の誤差は上限の側に含めてあるので足さない。§2.4a） | 上側 ≤ `approval.max_independent_ratio_gap`（計測器の精度から導き、§2.4a の測定の前に承認。後から広げるのは新しい決定記録だけ） |
| 応答の行列の熱の入力（§2.5） | 動かした段と基準の段の電力の差（CPU・GPU） | 各窓の `u_win` と `u_res(ΔP)` | 上側 ≤ `response_matrix.max_power_delta_w` |
| まとめに入る回の Control Config（§2.7） | 4 ファイルの SHA-256 | —（照合） | すべての回で同じ |
| 窓の覆い・`tick_id` の連続・hash・較正の digest（§2.2・§2.6・§2.7） | 数・時刻の差・一致 | —（数えた値と照合で、不確かさを持たない） | そのまま |

### 2.6b 解析の設定（`config/airflow-characterization.yaml`）

解析が検査と計算に使う**すべての数値**（閾値だけでなく、解析が manifest と照らして検査する手順の値も）を、解析の道具の設定
`config/airflow-characterization.yaml`（Control Config ではない）に置く。各値は `status: provisional` と `basis` を持ち、コードに既定値を
置かない（AGENTS.md ルール 9）。本文の数値は提案の初期値で、承認の記録で見直す。設定が欠けた・不正なら、解析は何も出さずに失敗する。

| 区分 | 鍵 | 提案の初期値 | 使う節 |
|---|---|---|---|
| 境界の係数 | `bound_coverage_factor` | 2 | §2.6a |
| 回転数の掃引 | `rpm_sweep.levels` / `rpm_sweep.other_zones_demand` / `rpm_sweep.settle_s` / `rpm_sweep.record_s` / `rpm_sweep.repetitions` / `rpm_sweep.min_distinct_days` | 0.35・0.40・0.45・0.50・0.60・0.70・0.80・0.90・1.00 / 0.60 / 30 / 60 / 3 / 2 | §2.3 |
| 応答の時間 | `rpm_sweep.response_band` / `tach_update_ms` | ±5 % / 1500 | §2.3 |
| アイドルの条件 | `idle.cpu_package_c` / `idle.gpu_core_c` / `idle.gpu_power_w` | 50 / 45 / 50 | §2.3 |
| EFU の掃引 | `efu.levels` / `efu.reference_demand` / `efu.repetitions` / `efu.min_distinct_days` | 0.35・0.50・0.60・0.70・0.80・1.00 / 0.60 / 3 / 2 | §2.4 |
| 窓 | `window.length_s` / `window.max_settle_s` / `window_blocks` | 120 / 1200 / 4 | §2.4・§2.5 |
| 落ち着き | `settle.span_s` / `settle.max_range_air_c` / `settle.max_range_chip_c` | 180 / 0.5 / 1.0 | §2.5 |
| 窓の傾き | `max_window_trend.temperature_c` / `max_window_trend.power_w` | 0.2 / 5 | §2.4 |
| 基準のずれ | `max_baseline_shift_c` / `drift_residual_fraction` | 0.3 / 0.25 | §2.4 |
| 部屋 | `max_room_drift_c` | 1.0 | §2.5 |
| 同条件 | `max_gpu_fan_delta_pct` / `max_aux_fan_delta_rpm` | 2 / 50 | §2.4 |
| 信号 | `min_signal_to_uncertainty` / `min_delta_t_c` / `min_delta_power_w` | 10 / 1.0 / 20 | §2.4 |
| 分解能 | `resolution.air_c` / `resolution.power_w` / `resolution.gpu_fan_pct` | 0.125 / 1 / 1 | §2.4・§2.6a |
| CPU の負荷の余裕 | `cpu_floor_margin_demand` | 0.05 | §2.2 |
| 負荷の定義（manifest と NVML の電力の上限・worker の数と照らす） | `load.gpu_power_limit_w` / `load.gpu_power_limit_max_w` | 300 / 450 | §2.2・§5 の 5 |
| 零位法 | `null.combos` / `null.settle_s` / `null.observe_s` / `null_resolution` / `max_null_halfwidth` / `front_coverage_tolerance` / `null.repetitions` / `null.min_distinct_days` / `null.max_bracket_flow_error` | （0.35, 0.35）・（0.60, 0.60）・（0.80, 0.60）・（0.60, 0.80）・（1.00, 1.00） / 30 / 30 / 0.02 / 0.05 / 0.05 / 3 / 2 / 5 % | §2.4 |
| 応答の行列 | `response_matrix.reference_demand` / `response_matrix.plus_demand` / `response_matrix.minus_demand` / `response_matrix.repetitions` / `response_matrix.min_distinct_days` | 0.60 / 0.90 / 0.35 / 2 / 2（2 回のすべてが別の日） | §2.5 |
| 結合 | `max_coupling_ratio` | 10 % | §2.4 |
| GPU の Fan の duty | `gpu_fan_duty.levels` / `gpu_fan_duty.main_pct` / `max_gpu_fan_duty_effect` | 30・60・100 / 60 / 10 % | §2.4 |
| GPU の Fan の duty（主でない duty の部分の測定） | `gpu_fan_duty.efu_levels` / `gpu_fan_duty.null_combos` / `gpu_fan_duty.repetitions` / `gpu_fan_duty.min_distinct_days` / `gpu_fan_duty.rear_excluded_pct` | 0.60・1.00（Top と Rear）/ （0.60, 0.60）・（1.00, 1.00） / 3 / 2 / 30（Rear を測らない duty） | §2.4 |
| 応答の行列の熱の入力 | `response_matrix.max_power_delta_w` | 5 | §2.5 |
| 承認の目安 | `approval.max_rpm_spread` / `approval.max_efu_spread` | 3 % / 15 % | §2.6 の 5 |
| 窓の覆い | `coverage.<source>.min_ok_samples` / `coverage.<source>.max_sample_gap_ms` / `coverage.trace_max_gap_ms` / `coverage.min_trace_ticks_ratio` | 44（2 分の窓）/ 6000 / 1500 / 0.95 | §2.2 |

設定に置かないもの: 中止の基準（`cpu.package` 88 °C・`gpu.0.core` 85 °C・`board.connector_12v2x6` 70 °C）は 0121 / 0130 の値で、所有者が測定中に
見て負荷を止める判断に使う。解析の検査ではないので、解析の設定に写さない（写すと 2 か所で食い違いうる）。CPU の floor の点（60 / 80 °C・100 / 200 W）は `safety.yaml` の値で、解析は manifest の Control Config の hash を通して同じものを読み、写さない。GPU の Fan の固定の duty の値は、
§5 の 3 の承認の記録と `coldaisle-gpu-fan` の側に置き、解析は manifest に残った値と `gpu.0.fan_speed` を照らすだけにする。
§2.4a の一致の上限 `approval.max_independent_ratio_gap` は、§5 の 2 の計測器を決める承認の記録で、§2.4a の測定の前に値を決めて足す（§2.4a の表の案）。
零位法の漏れの経路の一覧と、駆動される経路の割合の上限 `null.max_driven_leak_ratio`（案 2〜5 %。計測器の精度を見て決める）は、零位法の測定の前に
承認の記録で決めて足す（§5 の 7。一覧は数値ではないので manifest に版を残し、解析は manifest の一覧の版が承認の記録の版と同じことを照らす）。

### 2.7 run の manifest

測定のセッションごとに 1 ファイル（YAML）。形は解析の道具の PR で固定する。最低限、次を持つ。

- `schema_version`・`run_id`（#83 の `source_run_id` にそのまま使える形）・`kind`（`rpm_sweep` / `thermal_efu` / `response_matrix` / `null_balance`）
- 使った Control Config の 4 ファイルの SHA-256（trace の `runtime.config` と突き合わせ、違えば解析が拒否する）。さらに、**1 つのまとめ
  （段の 3 回の平均・zone の曲線・3 zone をそろえた `calibrated` の候補・結合の比・§2.4a の比較）に入るすべての回で、4 ファイルの SHA-256 が
  同じ**であることを求める。違う回は別の測定として扱い、まとめない（報告では hash の組ごとに分けて示す。捨てた理由 `config_mismatch`）。
  特に `fan-hardware.yaml` が変わると、同じ Demand でも PWM / RPM が変わる。測定の期間中は 4 ファイルを変えない（§2.3 の結果で
  `fan-hardware.yaml` を更新するなら、EFU の測定の前に済ませる。provisional の値を導入先の `air-balance.yaml` へ書くのも期間の後にする）
- 較正の digest（区間の開始の時点で有効な `calibration_activations` の行の `offsets_sha256`。0096 / 0099）
- 解析に使う DB の写しの SHA-256・作った時刻（§2.6 の 2）
- 負荷の定義（GPU の電力の上限・CPU の worker の数）と予備の試行の結果
- 段ごとの指示の値（3 zone）・lease・開始と終了の時刻（ms）・捨てた理由（`floor_bound` / `unsettled` / `aborted` / `safety_fault` / `room_drift` / `baseline_drift` / `condition_mismatch` / `low_signal` / `window_trend` / `null_unresolved` / `null_shifted` / `null_shifted_under_load` / `config_mismatch` / `power_mismatch` / `calibration_changed` / `telemetry_coverage` / `trace_coverage`）と、各窓の代表の時刻
- 零位法では、漏れの経路の一覧の版、観測した Front の値ごとの**経路ごとの**向き（`+` / `−` / `?`）と合わせた向き、二分法の順序

**較正との結びつけ**: 保存された温度にはプローブごとの offset（`config/calibration.json`）が掛かっている。EFU は 2 つのプローブの差を
使うので、区間の途中で較正が変わると、その変化が EFU のばらつきに見えたり本当のばらつきを隠したりする。解析は DB の
`calibration_activations`（取り込みが起動時に追記する。0099 §2.2）を読み、次のとおりにする。

- 温度を使う回（`thermal_efu` / `response_matrix` / `null_balance`）の区間 [開始, 終了] の中に較正の有効化の行が 1 つでもあれば、**その回を拒否する**
  （正規化して使わない）。区間の開始の時点で有効な行が無い（記録が区間を覆わない）回も拒否する
- 有効な行の `offsets_sha256` が manifest の digest と違えば拒否する
- 回の間で digest が違うことは許す（EFU は B / L の差で計算するので、回の中で一定の offset は打ち消される）。ただし報告に回ごとの
  digest を出す。応答の行列（§2.5）は温度の水準そのものを比べるので、digest の違う回をまとめない
- 測定の期間中は `coldaisle-calibrate --apply` を行わない（§2.2 の運用の決まりに加える）

#83 への取り込み（Thermal Dataset v2 の run としての扱い・宣言された変更との関係）は #83 で決める。本記録は run_id と区間を
manifest に残すことだけを決める。

### 2.8 測り直しの条件

次のどれかが変わったら、その zone（または全部）を測り直す。Thermal Dataset v2 の宣言された変更（0109）にも書く。

- Fan・ラジエーター・フィルター・パネルの交換・取り付けの向き
- 空気のセンサーの位置（EFU は 1 点の温度に依るため）
- GPU・CPU・AIO の交換、筐体の置き場所（背面・上面の壁との距離）

### 2.9 測らないもの

- 0.35 未満の回転数・停止と再始動の点（0120 §5 の 1）。試験専用の設定が要り、最低安全 Demand を下げる前に別の記録で測る
- 騒音（0067 のとおり別の方法で扱う）
- AIO ポンプ・GPU の Fan の特性

### 2.10 実機が無くても確かめること（テストの方針）

解析の道具の PR に、`-m "not hardware"` で通る次のテストを入れる。

- **合成の筐体**: 3 zone の真の EFU の曲線を与えた単純な熱の模型（熱源・漏れ・1 点のセンサーの偏りを含む）から、§2.3〜§2.5 の手順どおりの
  時系列を作り、解析が曲線を許容の範囲で取り戻すこと。漏れの量を変えて `q_rear` の偏りの向きが仕様どおりであること
- floor に縛られた段（trace の `effective` ≠ manual の値）・未収束・`air.room` のずれ・前後の負荷なしの窓のずれ・時刻の内挿（L が前後の窓の真ん中にない場合）・
  信号 ÷ 不確かさの不足（量子化された同じ値の 3 回を通さない）・Control Config の hash の不一致・
  区間の中の較正の有効化と digest の不一致・DB の写しの SHA-256 の不一致・空でない `-wal` の添えファイル・写しが区間を覆わないことを、
  manifest と trace の fixture で正しく外す・拒否すること
- 出した点が `FanProfile`（単調・`maximum_rpm`・点の数）と `ZoneFlowCurve`（端点・単調）の検証をそのまま通り、`calibrated` に
  書き換えた写しで `ConfiguredAirBalanceModel` が作れること（テストの中だけ）
- 分母が正で信号 ÷ 不確かさの条件を満たさない点を出さないこと（3 回とも同じ値に量子化された合成の例を含む）。L の時刻が前後の負荷なしの窓の
  真ん中にない合成の例で、時刻の内挿がずれを取り除くこと
- GPU の Fan・VRM の Fan・ポンプ・他の zone の `effective` が B と L で違う合成の例を、同条件の検査で捨てること
- 窓の平均の不確かさ: 強く相関した（ゆっくり漂う滑らかな）合成の窓で、ブロックに分けた推定が `s² / n` より大きい不確かさを出し、
  信号の条件を素通りさせないこと。傾きが `max_window_trend` を超える窓を `window_trend` で捨てること
- 応答の行列: 動かした段で電力が変わった（throttle・boost の）合成の例を `power_mismatch` で捨て、電力の差を報告に残すこと。trace の
  `seq` に正当な欠番があっても `tick_id` が連続する窓は捨てず、`tick_id` が欠けた窓は捨てること
- GPU の Fan の duty の影響: duty で Top の風量や `d_f*` が上限を超えて変わる合成の筐体で `calibrated` を提案しないこと
- 結合の上限: Rear を変えると Top の風量が `max_coupling_ratio` を超えて変わる合成の筐体、と、測った比は上限より小さいが上側の境界が
  上限をまたぐ合成の例（例: 9 % ± 5 % で上限 10 %）で、`calibrated` を提案しないこと。§2.6a の表のほかの検査も、境界をまたぐ例で通さないこと
- 窓の覆い: 収集が途切れて数個の同じ読み値だけが残った窓・`stale` / `suspect` の行を含む窓・`tick_id` が欠けた窓・tick の間隔が
  開いた窓・`run_id` が途中で変わる窓・欠けた tick の間に floor が掛かっていた例を、値を計算する前に捨て、3 回のまとめに入れないこと
- Rear を変えると Top の風量も変わる（結合のある）合成の筐体で、同じ段の L_cpu の `q_top(R | d_r)` を使った `q_rear` が真の値を取り戻し、
  Top の掃引の `q_top(R)` を流用した場合との差が結合の大きさとして報告されること
- 零位法: 手順 0〜7 のどこかで GPU の Fan・VRM の Fan・ポンプ・Rear / Top の `effective` が変わった回と、確かめ直しで端の向きが変わった回を捨てること。同じ向きの観測の列から、二分法の区間・`d_f*`・`u(d_f*)` が一意に決まること（`?` を端にしない・区間が無い組は点を出さない）。
  範囲の判定が `u(d_f*)` を含めて行われ、ぎりぎり届かない例で「覆わない」になること
- 零位法の漏れ: 向きの変わる Demand が違う 2 つ以上の漏れの経路を持つ合成の筐体で、経路の間で食い違う段を `?` とし、区間がすべての経路の
  変わる点を覆うこと（正味の漏れが 0 の点が区間の中にあること）。外から向きを見られない経路が一覧にある例、駆動される経路の割合の上限が未承認・上側の境界が上限を超える例、manifest の
  一覧の版が承認の版と違う例、観測する経路の一部の向きが記録に無い例で、Front の `calibrated` を提案しないこと
- 零位法の区間の誤差: 傾きの大きい Front の曲線で、半幅は `max_null_halfwidth` 以内でも `u_bracket` の割合が `null.max_bracket_flow_error` を
  超える合成の例で `calibrated` を提案せず、`u_total(q_front)` が `coupling_null` などの比べ方に入ること
- Top の熱源ごとの補正: GPU の熱がプローブの近くに偏る合成の筐体で、L_cpu の係数だけで補正した `q_rear` が偏り、L_gpu の係数を使うと真の値を
  取り戻すこと。L_gpu の係数が無い出力で Rear に provisional の印が付くこと
- Top の引き算で電力の増分がほとんど打ち消され、`q_top` の 3 回がそろっている合成の例で、温度と電力の不確かさを伝えた `u(N)` によって
  雑音だけの正の分子を出さないこと
- 段・組の有効な回が 3 未満なら値を出さないこと（1 回の観測を範囲 0 として通さない）。§2.6 の 4 の条件の 1〜5 のどれかが欠けた出力では、
  EFU に provisional の印が付き、`calibrated` を提案しないこと（§2.4a の 1 だけ・2 だけの例を含む）
- 解析の道具が DB へ書かない・設定へ書かない・control と AI 層から import されないこと（既存の import の検査に足す）
- 報告の Markdown に個体識別子が出ないこと（`tests/test_repo_hygiene.py` の流儀）

## 3. Consequences

### 良くなること

- 3 zone を W/K という 1 つの尺度で比べる根拠ができ、#81 の `balance_ratio` に意味が付く
- 制御の経路の中で測るので、測定の間も Critical Safety と Guard が効いたまま。lease で放置に強い
- `pwm_to_rpm` が運転記録の途中の点ではなく、落ち着いた段の値になる。`airflow_index` が RPM の比の代用から、測った相対の風量になる
- 応答の行列で「どの zone を上げるとどの温度がどれだけ下がるか」を説明できる（#75 の受入基準）

### 悪くなること・その緩和

- 1 点の温度センサーのずれが出口ごとに違い、漏れの経路もあるため、熱の EFU だけでは zone をまたいで比べられない。§2.4a の独立の確認を
  `calibrated` の前提にし、それまでは provisional として使い道を限る（§2.1）。確認に計測器の購入と数時間の手間が要りうる（§5 の 2）
- `q_front` は零位法の検出の方法（§5 の 1）が決まり、点の範囲が 0.35〜1.00 を覆うまで作れず、それまで Air Balance は無効のまま
- floor に縛られた段は捨てるので、高い負荷での低い Demand の点は取れない。運転で届かない領域なので、曲線の外挿にしない（端点は形式上の値）
- 測定に数日かかる。セッションに分け、各セッションの manifest で区切る
- GPU の Fan は GPU コアの温度で動くので、GPU の負荷の窓をそろえるには所有者の承認が要る（§5 の 3）。決まるまで Rear と Front の EFU は出ない
- 零位法の等式は漏れの正味が 0 のときだけ成り立つ。小さい開口も省かず漏れの経路を一覧にして承認し、すべての観測する経路の向きで区間を決め、
  駆動される経路は風速計で割合を測って上限以内になるまで、外から見られない経路があれば常に、Front を `calibrated` にしない（§2.4 の零位法の 0・§5 の 7）。観測の点が増え、測り方が決まるまで Front は出ない
- 同条件の検査・zone の間の結合・GPU の Fan の duty の影響の測定で、測定の時間が合計 約 115 時間に増える（§2.5）。数週間のセッションに分ける

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 公称 CFM × RPM の比で `q_*` を作る | 装着時の抵抗を含まず、#75 / #81 / `docs/airflow-model.md` の原則に反する。事前情報の記録にとどめる |
| 風速計の測定を主な方法にする | 吸気の側（前面パネルの内側）に当てにくく、格子の後ろの流れは一様でない。`docs/airflow-model.md` は v1 の必須条件にしないとした。補助にとどめる（§5 の 2） |
| `airflow_index` を制御された掃引の RPM の比にする | 回転数の表は良くなるが相対の風量は測っていない。取り付けた状態のインピーダンスで風量は回転数に比例しない（`docs/airflow-model.md`）。trace が代用を測った風量のように見せる |
| Rear の Top の引き算に、Top の掃引（Rear を R に固定）の `q_top(R)` を流用する | Rear を変えると筐体の圧力が変わり Top の風量も変わる。その差がそのまま `q_rear` に入る |
| GPU の Fan の速さを共変量として記録するだけにする | B と L で風量のコンダクタンスが違えば、差を取っても 1 つの `q_rear` にならない。式が直らない |
| 零位法で向きの変わる隣の 2 段のどちらか（または中点）をそのまま使う | 段の幅の分だけ `d_f*` があいまいで、実装ごとに違う曲線になる。二分法と半幅の不確かさで決める |
| GPU の Fan の主の duty で測った状態だけで `calibrated` とし、それ以外では `uncalibrated` として扱う（(B)） | 実行時に GPU の Fan の状態を読めないので、どの tick がその状態か分からず、実質的に `calibrated` にならない。duty の影響を測って小さいと示せる (A) を採る |
| B と B' の算術平均を基準にする | 落ち着きの待ちが区間ごとに違い、L が真ん中にないとずれが ΔT に漏れる。時刻で内挿する |
| 0120 の運転記録の中央値のまま `pwm_to_rpm` を使う | BIOS が動かしている途中の点を含み、0.40 未満が無い。制御された段の値に置き換える |
| 試験専用の設定で floor を外して低い Demand・高い負荷を測る | Critical Safety を緩める設定を測定のために使うことになる。運転で届かない領域なので測る必要がない |
| `CALIBRATION` モードを管理ソケットに足して使う | 新しい入口と承認が要る。MANUAL と lease で同じことができ、どちらも Guard と Safety を通る |
| fand を止めて sysfs に直接書いて掃引する | Guard と Safety を迂回する（ルール 2。0121 §2.1） |
| 3 zone を同時にランダムに動かして回帰で一度に求める | 落ち着くまで 10 分以上かかる系では段の数が足りず、floor の介入の判定も難しい。1 つずつ動かし、基準の段を挟む |
| GPU 600 W を固定の負荷にする | 低い Demand の段で Guard の floor と中止の基準に近づき、段の多くが捨てられる |

## 5. 未決事項

所有者の判断が要る点。いずれも測定の前に決める。

| # | 論点 | 選択肢 | 推奨 |
|---|---|---|---|
| 1 | `q_front` の零位法で、筐体の内外の圧力差の**向き**を何で見るか（あわせて、5 つの組で `d_f*` の範囲が 0.35〜1.00 を覆わないときに組を足すか） | (a) 細い糸・薄い紙片を漏れの経路の一覧（§2.4 の零位法の 0）の観測する経路のそれぞれに外から当て、なびく向きが変わる点を目で見る（買い足しなし。定性的） (b) 差圧センサー（数 Pa を測れる品）を基板に足す（ファームウェアと新しいメトリクスの決定記録が要る） (c) 線香の煙の向きを見る (d) 零位法をやめ、`q_front` を Rear / Top の熱の傾きと公称の比から仮に置く（`calibrated` にしない） | **(a) から始める。** 向きの変わる点が段の幅（0.05〜0.10）で決まらないときに (b) を別の決定記録で検討する。(c) は煙の粒子を筐体に入れるので避ける。範囲が覆わなければ、足す組（その組で使う `q_rear` / `q_top` も 6 段の掃引に段を足して実測する）を結果を見て決める |
| 2 | §2.4a の zone をまたぐ独立の確認を何で行うか（**`calibrated` の前提。「行わない」は選べない**。計測器を買うかは所有者が決める） | (a) **羽根車式の風速計で各出口を多点で測る**（Top のラジエーターの排気面・Rear の格子をそれぞれ 9 点程度の格子で。流速 × 面積で比を出す）。費用 約 3,000〜15,000 円。手間 半日（段ごとに数分の読み取り）。格子の後ろの流れが一様でなく ±10〜20% の誤差 (b) **同じプローブを出口の複数の点へ動かして温度の分布を測る**（予備の DS18B20 を 1 本足すか、既存の 1 本を一時的に動かす。動かす間はその metric が欠け、宣言された変更（0109）に書く）。費用 0〜数百円。手間 大（点ごとに落ち着きの待ち。2 出口 × 9 点で数時間）。流速の重みが無いと面の単純平均までしか出ない (c) **(a) と (b) を同じ点で組み合わせる**（流速で重みづけした出口の平均温度が出て、出口ごとの補正の係数と独立な流量の比の両方が得られる）。費用は (a) と同じ。手間 1 日 (d) 既存の `air.*` のプローブを出口の間で入れ替える。費用 0・手間 小。ただしプローブの個体の差（`coldaisle-calibrate` で既に扱える）しか打ち消せず、位置と混ざり方のずれは残るので**単独では確認にならない** (e) 熱線式の風速計で多点を測る（低い流速に強い）。費用 約 20,000〜50,000 円。手間は (a) と同じ | **(c)。** 一致の上限の案は §2.4a の表（(a)・(c) 25 %、(e) 20 %）で、計測器を決める承認の記録で同時に決める。出口ごとの補正と独立の比の両方が得られ、§2.4a の 1 と 2 を一度に満たす（(e) と (b) の組み合わせでも満たす）。**(a) だけ・(b) だけ・(d) では `calibrated` にならない**: (b) は重みのない出口の温度の補正だけで、zone ごとの流量の比を独立に確かめない。(a) は比は得られるが出口ごとの補正が無い。風速計を買わない判断なら (b) で補正だけを行い、`air-balance.yaml` は `uncalibrated` のまま（EFU は provisional。§2.1 の範囲で使う）。(d) は (b) / (c) の前のプローブの個体差の確認として併用してよい |
| 3 | EFU の差を取る窓（B と L_gpu）で GPU の Fan の振る舞いをどうそろえるか（**所有者の承認が要る**。中止の基準 `gpu.0.core` 85 °C は変えない） | (a) **EFU を出す測定の間だけ GPU の Fan を固定の duty にする**（Top・Rear の掃引・零位法の手順 1〜7・§2.4a の確認のすべてで同じ値）: `coldaisle-gpu-fan` に試験専用の一定の値（主の duty。例 60 %。GPU の Fan の duty の影響を測るときは 30・100 % も）を与え、実際に書く値は「固定の値と 0130 の曲線の値の大きい方」にする（曲線より冷やさない）。書き手は今の root の専用サービスのままで、新しい書き手を作らない（AGENTS.md ルール 2 の例外の中）。ただし 0128 / 0130 の曲線を試験の間だけ変えることになるので、別の決定記録と承認が要る。止まった・落ちたときに GPU 自身の既定の制御へ戻る 0128 の決まりはそのまま。GPU 600 W で 70 % は 78 °C で落ち着いた（0130）ので、300〜450 W では余裕がある。曲線の値が固定の値を超えた窓は同条件の検査で捨てる (b) 曲線のまま、GPU の Fan の差が `max_gpu_fan_delta_pct` に収まる L だけを使う: GPU の負荷でコアが 45 °C 未満から約 60 °C へ上がり、曲線は 30 % → 40 % 程度に動くので、Rear と Front の L はほぼすべて捨てられる。Top（GPU はアイドル）には効く (c) 補正の式を作る: 同じ負荷で GPU の Fan だけを変えた窓を足して、Fan の速さによる ΔT の変化を測って差し引く。GPU の Fan を変える手段に (a) と同じ承認が要り、補正の検証の測定も増える | **(a)。** 差の式の前提（B と L で同じ流れ）をそのまま満たす。曲線より冷やさないので GPU の安全は今より弱まらない。承認されるまで Rear と Front の EFU は測らず、Top の EFU と回転数の掃引・応答の行列から進める |
| 4 | §2.6 の 5 の承認の目安（回転数 3%・EFU 15%）と §2.4 の信号 ÷ 不確かさの初期値 | (a) この値で進め、承認の記録で見直す (b) 先に予備の測定で決める | **(a)** |
| 5 | 排気の温度上昇が小さい（Top の 1.00 で CPU の負荷 +50 W なら約 1 °C の見込み）点をどう扱うか。中止の基準は 0121 / 0130 のまま | (a) **段ごとに負荷を変える**: 各段の Demand から 0.05 低い値を floor が超えない範囲で、高い段ほど負荷を重くする（Top は CPU の power と温度の floor、Rear は GPU を 300 W から 450 W まで。Guard の floor が掛かれば捨てる）。EFU は電力の差 ÷ 温度の差なので、負荷が段で違っても比べられる（熱の運び方が負荷に線形という近似） (b) 軽い固定の負荷のまま、条件を満たさない高い段を出さない（曲線の上端が欠ける） (c) Top の熱源に GPU の負荷を使う（GPU の排気は Top へ向かう。C-05）。熱の分かれ方が分からないので `q_top` を決められない (d) 窓を長くする（量子化は減らない） | **(a)。** floor は Demand より下でしか効かないので、高い段では安全の範囲を変えずに負荷を上げられる。それでも条件を満たさない段は (b) と同じく出さない |
| 6 | zone の間の結合（Rear を変えると Top の風量も変わる）を Rear の計算にどう入れるか | (a) **Rear の各段で L_cpu を足し、その状態の `q_top(R | d_r)` を測って使う**（§2.4 の既定）。Rear の段の窓が 3 → 5 になり、Rear の EFU が約 14 → 23 時間（+約 9 時間） (b) Rear の 0.35 と 1.00 でだけ `q_top(R | d_r)` を測り、Top の掃引の `q_top(R)` との差の大きい方を結合の上限として `u(q_top)` に足す（+約 3 時間）。不確かさが大きくなり、信号の条件で点が捨てられやすい | **(a)。** 結合を推定ではなく測った値で除き、結合の大きさそのものも #81 の近似の誤差として残せる。時間の増分は合計 約 115 時間のうち約 1 割 |
| 7 | 零位法の漏れの経路（§2.4 の零位法の 0）: 漏れの扱いの方針、一覧、駆動される経路の割合の上限 | (a) **すべての開口を観測する経路にする**（無視できる区分を使わない）。駆動される経路（電源の Fan が筐体の中から吸う場合など）があれば、§5 の 2 の風速計でその開口の流量を測り、`q_rear + q_top` に対する割合の上限と比べる (b) 開口の面積の上限（例: 合わせ目の幅 × 長さの合計）を決め、それ以下の開口を無視できる経路とする (c) 駆動される経路があれば Front は `uncalibrated` のままにし、相互作用・漏れを入れたモデルを別の決定記録で扱う | **方針は (a) に決定（所有者、2026-10-16）。** 次の 3 つは未決で、零位法の測定の前に実機で決める: 漏れの経路の一覧、割合の上限 `null.max_driven_leak_ratio` の値（案 2〜5 %。計測器の精度（§2.4a の表）を見て決める）、電源の吸気の向き。決まるまで Front は `calibrated` にならない（§2.6 の 4 の 2） |

本記録が決めず別に決めること: `balance` / `thermal_limits` / `thermal_inputs` の値（#81）、Thermal Dataset への run の取り込み（#83）、
0.35 未満の測り直し（0120 §5 の 1）、解析の道具と manifest の形（実装の PR）。
