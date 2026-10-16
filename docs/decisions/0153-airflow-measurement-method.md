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
- **floor が掛かった段は捨てる。** 段の測定窓の中で、trace の `effective` が `manual` の値と違う tick（`bound_by` が
  `safety_floor` / Guard の floor / `forced_max` など）が 1 つでもあれば、その段は「floor に縛られた」として解析から外し、
  manifest（§2.7）に理由を残す。floor を下げて測り直すことはしない
- **中止の基準**は 0121 §2.1（0130 で置き換えた後）と同じ: `cpu.package` 88 °C・`gpu.0.core` 85 °C・`board.connector_12v2x6` 70 °C。
  どれかに届いたら、負荷を止めて `coldaisle-control max`。Critical Safety が `DEGRADED` / `EMERGENCY` になった・fault が出た段は
  使わず、負荷を止めて原因を見る。fand の挙動がおかしければ `systemctl stop coldaisle-fand`（BIOS へ返す）
- **負荷**は GPU と CPU を別々の固定の負荷にし、普段の最大を超えない（0121 §2.1 と同じ流儀）
  - GPU: 電力の上限を 300 W にして行列計算で上限に張りつかせる（600 W では Front / Rear の低い段で Guard の floor と
    中止の基準に近づき、段が floor に縛られるため。§5 の 5 で (a) を選べば、Rear の高い段で 450 W まで上げる）。終わったら上限を 600 W に戻す。負荷のスクリプトは SIGTERM で同期してから終わる（0121 T4 の記録）
  - CPU: Top の各段で、`cpu_cooling_floor`（60 °C → 0.35・80 °C → 1.0）と `cpu_power_cooling_floor`（100 W → 0.35・200 W → 1.0）が
    その段の Demand から 0.05 低い値を超えない範囲の負荷（`stress-ng` の worker の数）を段ごとに選ぶ（§5 の 5。高い段ほど重くできる）。
    段ごとの負荷を本番の前の予備の試行で決めて manifest に残す。floor が掛かった段は今までどおり捨てる
- **GPU の Fan は触らない。** `coldaisle-gpu-fan`（0128 / 0130）の曲線のまま動かし、`gpu.0.fan_speed` を記録して解析の共変量にする
  （§5 の 3）
- **所有者が在宅で**、各セッションの開始・負荷の切り替え・終了を行う。操作のスクリプトは事前に内容を示し、所有者が実行する（0121 §2.1）。
  スクリプトがするのは `coldaisle-control manual` / `auto` / `max` と負荷の開始・停止と manifest の追記だけ
- 測定の期間中は較正を変えない（`coldaisle-calibrate --apply` をしない。§2.7）
- 試験の側から `fanN_input` を高い頻度で読まない（読む頻度が更新を変える。0125）。回転数は fand の trace と `coldaisle-telemetry` の値を使う
- **LLM は関わらない。** 手順の実行・解析・承認のどこにも LLM を使わない。解析の結果を LLM のプロンプトへ入れない（ルール 1 / 8）

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

Top と Rear を、それぞれ 0.35・0.50・0.60・0.70・0.80・1.00 の **6 段**に変え、ほかの 2 zone を基準 R = 0.60 に保つ。0.60 の段は、
Rear の計算に使う `q_top(R)` を Top の各回で補間なしに直接得るため。0.35・0.60・0.80・1.00 の段は、零位法の基準の組（下）で使う
`q_rear` / `q_top` を補間なしに得るため（零位法が使う値は、すべてこの掃引で実測した段にする）。温度差はすべて `air.front_intake` を基準にする（室温の揺れを打ち消す）。

**負荷あり − 負荷なしの差で計算する。** 出口の空気には、アイドルの GPU・CPU・マザーボード・VRM・ポンプ・Fan の熱がいつも入っている。
負荷ありの値だけで割ると、分母の温度上昇にはこの基準の熱が入るのに分子は負荷の電力だけになり、EFU を系統的に小さく見積もる
（CPU の負荷を軽くしているぶん、Top で特に大きい。その誤差は Rear の Top の引き算にも入る）。そこで各段を、同じ Demand のまま
次の 3 つの区間で測る。

1. **B**（負荷なし）: Demand を指示し、§2.5 の落ち着きを待って最後の 2 分の平均
2. **L**（負荷あり）: 同じ Demand のまま固定の負荷を始め、落ち着きを待って最後の 2 分の平均
3. **B'**（負荷なし）: 負荷を止め、落ち着きを待って最後の 2 分の平均

各窓の**代表の時刻**（窓の中の sample の時刻の平均）`t_B` / `t_L` / `t_B'` を manifest に残す。`t_B < t_L < t_B'` でない段は捨てる（内挿の分母を正にする）。落ち着きの待ちは区間ごとに違う（最長 20 分）ので、
L は B と B' の真ん中にあるとは限らない。基準は B と B' の算術平均ではなく、**L の時刻へ線形に内挿した値**にする。

```text
x_base(t_L) = x_B + (x_B' - x_B) * (t_L - t_B) / (t_B' - t_B)
Δx = x_L - x_base(t_L)
```

B と B' の `air.*` の `air.front_intake` との差が 0.3 °C を超えて違う段は「基準のずれ」として捨てる（内挿は直線のずれだけを打ち消し、
大きなずれは曲がっている恐れがあるため）。B' は次の段の B を兼ねない（Demand が違う）。`ΔT_z = Δ(T_z_exhaust − T_front_intake)`。
電力の差 `ΔP_cpu` / `ΔP_gpu` も同じ内挿で求める。

**割る前の条件（信号 ÷ 不確かさ）**: すべての割り算について、分母（と、引き算で小さくなりうる Rear の分子）の不確かさを、
そこへ入る**すべての温度と電力の項**から伝えて見積もり、条件を満たさない点は出さない（捨てた理由 `low_signal`）。3 回が同じ値に
量子化されて範囲の検査を素通りすることや、Top の引き算で電力の増分がほとんど打ち消された雑音だけの分子が通ることを防ぐ。

```text
# 内挿した差 Δx（x は温度の差 T_z - T_front_intake、または電力）の不確かさ
u_noise(Δx) = sqrt(s_L^2 / n_L + s_B^2 / n_B)       # 窓の中の標準偏差と sample の数（B と B' は大きい方）
u_drift(Δx) = drift_residual_fraction * |x_B' - x_B| # 直線の内挿で取り切れない曲がりの分
u_res(ΔT)   = 0.125 / sqrt(12) * sqrt(4)            # DS18B20 の分解能。2 本のプローブ × L と基準の 2 窓。平均しても量子化は減らないとみなす
u_res(ΔP)   = 1 / sqrt(12) * sqrt(2)                # 電力の 1 W の丸め。L と基準の 2 窓
u(ΔT_z) = sqrt(u_res(ΔT)^2 + u_noise^2 + u_drift^2)
u(ΔP)   = sqrt(u_res(ΔP)^2 + u_noise^2 + u_drift^2)

# Top
q_top = ΔP_cpu / ΔT_top
u_rel(q_top) = sqrt((u(ΔP_cpu)/ΔP_cpu)^2 + (u(ΔT_top)/ΔT_top)^2)
# Rear の分子 N と、そこへ伝える q_top(R) の不確かさ（Top の 3 回の平均を使う）
u(q_top(R)) = max(平均の q_top(R) * 3 回の u_rel(q_top(R)) の二乗平均の平方根 / sqrt(3),  Top の 3 回の範囲 / 2)
N = ΔP_gpu + ΔP_cpu - q_top(R) * ΔT_top
u(N) = sqrt(u(ΔP_gpu)^2 + u(ΔP_cpu)^2 + (ΔT_top * u(q_top(R)))^2 + (q_top(R) * u(ΔT_top))^2)
q_rear = N / ΔT_rear
u_rel(q_rear) = sqrt((u(N)/N)^2 + (u(ΔT_rear)/ΔT_rear)^2)
# airflow_index（zone の中の正規化）
airflow_index(d) = q_z(d) / q_z(1.0)

条件（k = min_signal_to_uncertainty）:
  Top : ΔT_top > 0 かつ ΔT_top / u(ΔT_top) >= k かつ ΔT_top >= min_delta_t_c
        ΔP_cpu >= min_delta_power_w かつ ΔP_cpu / u(ΔP_cpu) >= k
  Rear: ΔT_rear > 0 かつ ΔT_rear / u(ΔT_rear) >= k かつ ΔT_rear >= min_delta_t_c
        ΔP_gpu + ΔP_cpu >= min_delta_power_w
        N > 0 かつ N / u(N) >= k（Top の項の温度と電力の不確かさをすべて含めた u(N) で判定する）
  airflow_index: 分母の q_z(1.0) が上の条件を満たした段の値（有効な 3 回）であること。満たさなければ、その zone の airflow_index を出さない
  Front: 零位法の点は割り算を含まない（q_rear + q_top の和）。和の不確かさは両方の u を二乗和で伝えて報告する。
        airflow_index の分母 q_front(1.0) は、1.00 が零位法の点の範囲にあるときだけ（下）
```

時刻の内挿の分母 `t_B' − t_B` は `t_B < t_L < t_B'` を求めることで正にする。ばらつき（範囲 ÷ 平均）の分母の平均は、上の条件を
満たした正の値だけから作る。

`drift_residual_fraction`・`min_signal_to_uncertainty`・`min_delta_t_c`・`min_delta_power_w` は解析の道具の設定
（`config/airflow-characterization.yaml`。Control Config ではない）に `status: provisional` と `basis` 付きで置き、コードに既定値を置かない（ルール 9）。
提案の初期値は 0.25・10・1.0 °C・20 W で、承認の記録で見直す。

**繰り返しとまとめ方**:

- Top と Rear のそれぞれで 6 段の掃引を**独立に 3 回**行う。各回は別のセッション（負荷の開始・落ち着きの待ちからやり直す）で、3 回のうち少なくとも
  1 回は別の日にする（§2.3 と同じ）。時間によるずれを打ち消すため、回ごとに上り（0.35 → 1.00）と下り（1.00 → 0.35）を交互にする
- EFU は回ごとに段ごとに計算する。Rear の式の `q_top(R)` は、Top の各回の 0.60 の段の値（補間しない）の 3 回の平均を使う。Rear と Top は
  別のセッションなので回を対にしない。不確かさは、Rear の各点を Top の 3 回の `q_top(R)` のそれぞれで計算し直した最小と最大を
  「Top から伝わる幅」として、Rear 自身の 3 回の範囲と分けて報告に残す
- 段の値は、有効な 3 回の EFU の**平均**とし、ばらつきは 3 回の**範囲 ÷ 平均**で表す（§2.6 の 5）
- 捨てた回（floor・未収束・`air.room` のずれ・中止）があり、その段の有効な回が 3 に届かなければ、届くまで追加の回を行う。
  **有効な回が 3 未満の段は値を出さない**（1 回だけの観測を、範囲 0 として目安に通さない）
- Front の零位法の `d_f*` も、**下に挙げた 5 つの基準の組のすべて**で、それぞれ独立に 3 回（別のセッション・少なくとも 1 回は別の日）
  求め、平均と範囲を出す。有効な回が 3 に届かない組は、届くまで追加の回を行い、届かなければその組の点を出さない

- **Top（`q_top`）**: L の負荷は CPU の軽い固定の負荷・GPU はアイドル。負荷で増えた CPU Package の熱は AIO の冷却水を経てほぼすべて
  Top のラジエーターの空気へ移ると近似する。

  ```text
  q_top(d_t) = ΔP_cpu / ΔT_top                                  [W/K]
  ΔP_cpu = Δ power.cpu.package（RAPL）
  ```

  残る偏り: 負荷で増える VRM の損失は RAPL に入らず、VRM の Fan で筐体の空気へ出る（一部は Rear へ）。その分は小さいが残るので、
  §2.4a の確認で見る

- **Rear（`q_rear`）**: L の負荷は GPU 300 W の固定の負荷・CPU はアイドル・Top は R。同じ問題（アイドルの GPU 約数十 W・CPU・ボードの
  基準の熱が分母に入る）があるので、同じく差で計算する。負荷で増えた熱（GPU と、負荷の間に動く CPU の分）は Rear と Top の流れが
  運び出すとし、Top の分を `q_top(R)` で差し引く（GPU の排気は Top へ向かう。C-05）。

  ```text
  q_rear(d_r) = (ΔP_gpu + ΔP_cpu - q_top(R) * ΔT_top) / ΔT_rear  [W/K]
  ΔP_gpu = Δ power.gpu.0（NVML のボードの電力）
  ```

  GPU の Fan の回転（`gpu.0.fan_speed`）は B と L で変わり、筐体の中の流れを動かす。段ごとに B と L の値を残し、共変量にする（§5 の 3）

  パネルの隙間などの漏れで出る熱は `q_rear` に含まれてしまう（「Rear に帰した値」）。Rear を変えたときの傾きは漏れに左右されにくいので、
  6 段の点の形を優先し、絶対値の偏りは §2.4a の zone をまたぐ確認で確かめる
- **Front（`q_front`）**: 吸気の側には熱が入らないので、熱収支だけでは Front の流量を Rear / Top と同じ尺度に直接載せられない。
  **吸気と排気が釣り合う点を見つける「零位法」**で Front を排気の尺度へ結びつける。Rear と Top を基準の組（Rear, Top）=
  （0.35, 0.35）・（R, R）・（0.80, R）・（R, 0.80）・（1.00, 1.00）の 5 つに保ち（R = 0.60。使う `q_rear` / `q_top` はすべて上の 6 段で実測した値）、
  アイドルで（圧力差の向きに熱は要らず、floor にも縛られない）、Front を 0.35 から 1.00 まで §2.3 の段で動かして、筐体の内外の圧力差の
  向きが変わる Front の値 `d_f*` を探す。その点で `q_front(d_f*) = q_rear + q_top` とし、組ごとの点を線形につないで `q_front(d)` の曲線を作る
  （RPM の比の形は使わない）。`q_rear` / `q_top` は、ほかの zone を R にした掃引の値で、組の中のもう一方の zone の違いによる相互の影響は
  無視する近似とし、§2.4a の確認で見る。
  点の範囲（最も小さい `d_f*` から最も大きい `d_f*`）の外は測っていないので曲線を作らない。範囲が 0.35〜1.00 を覆わなければ Front は
  `calibrated` の条件（§2.6 の 4）を満たさない。覆わないときに組を足すかは §5 の 1 と合わせて所有者が判断する。Front の `airflow_index` も
  同じ点を demand 1.0 の値で割って求め、1.00 が範囲の外ならそれまで代用のまま（§2.1）。
  圧力差の向きを何で見るかは §5 の 1 で決める。決まるまで `q_front` は作らない（§2.6 のとおり `air-balance.yaml` は `uncalibrated` のまま）
- §2.5 の応答の行列の Front の段（F+ / F−）の熱の応答（`d.case_delta`・`air.gpu_intake`・`d.gpu_preheat`）を、零位法の結果と矛盾しないかの
  目安にする（Front を上げても排気の温度差が下がらなくなるのは、吸気が足りている側）

曲線の点: Top / Rear は測った 6 段の EFU、Front は零位法の点に、形式上の端点 `{demand: 0.0, airflow_index: 0.0, effective_flow: 0.0}` を足す。0.0〜0.35 は測らず、
運転では使わない（applied demand は最低安全 Demand 0.35 と `minimum_stable_demand` の下に行かない）。Rear は PWM 0 でも回る
（約 440 RPM）ので、この端点は実際の風量より小さいが、届かない範囲の形の上の値として扱う。demand 1.0 の `airflow_index` は 1.0。

### 2.4a zone をまたぐ独立の確認（`calibrated` の前提）

§2.1 のとおり、1 点のプローブのずれは出口ごとに違う。`calibrated` にする前に、熱の EFU と独立な方法で、少なくとも次の 2 つを確かめる。

1. **出口ごとのプローブのずれ**: Top と Rear の出口で、固定のプローブの温度と出口の面の平均（できれば流速で重みづけした平均）の温度の
   関係を求め、出口ごとの補正の係数にする。補正の後の値で `q_top` / `q_rear` を計算し直す
2. **zone の間の比**: 熱の EFU（補正の後）の比 `q_top / q_rear`（R と 1.00 の段）と、独立に流量を測る方法（風速計の多点）で求めた比を、
   それぞれの不確かさとともに比べる。Front は吸気の面で同じ方法が使えるなら `q_front` も比べ、使えなければ零位法の和が補正の後の
   `q_rear + q_top` を使っていることで代える

**1 と 2 の両方**を済ませて初めて §2.4a を済ませたとする。プローブを動かす温度の分布（1）だけでは流量の比を独立に確かめたことにならず、
風速計の比（2）だけでは出口ごとの補正が無い。

- 確認は §2.4 と同じ固定の負荷・同じ基準の段で行い、制御の決まり（§2.2）も同じにする
- 確かめ方（計測器を買うか）は §5 の 2 で所有者が決める。1 と 2 の両方を済ませるまで、§2.4 の EFU は provisional のまま（§2.1）
- 一致の目安（比の差が何 % 以内なら `calibrated` にしてよいか）は、確認の結果を見て承認の決定記録で所有者が決める。
  食い違いが説明できなければ `calibrated` にしない（条件の一覧は §2.6 の 4）

### 2.5 応答の行列（Thermal Effectiveness）と落ち着きの判定

- 負荷: (i) GPU 300 W だけ、(ii) CPU の軽い固定の負荷だけ、(iii) 両方。各負荷で、3 zone を R = 0.60 にした基準の段と、1 zone だけを
  0.90（+）・0.35（−）にした段、3 zone とも 0.90 の段を、**基準の段を間に挟んで**行う（R, F+, R, F−, R, Rr+, R, Rr−, R, T+, R, T−, R, All+, R）。
  基準の段の繰り返しから、時間の流れによるずれ（部屋・冷却水の蓄熱）を直線で補正する
- 見る指標: `air.gpu_intake`・`d.case_delta`・`gpu.0.core` / `hotspot`・`cpu.package`・`air.gpu_exhaust` / `rear_exhaust` / `top_exhaust`
  （それぞれ `air.front_intake` との差）。`air.room` / `air.room_humidity`・`board.connector_12v2x6`・`gpu.0.fan_speed`・電力も残す
- 回数: 負荷ごとに 2 回（別の日）
- **落ち着き**: 0121 T4 の測り直しと同じ基準。直近 3 分の幅が、空気の温度 0.5 °C 以内・`cpu.package`（3 点の中央値）/ `gpu.0.core` /
  `board.connector_12v2x6` 1 °C 以内になったら落ち着いたとし、最後の 2 分の平均を値にする。20 分で落ち着かなければ「未収束」として
  解析から外す
- 1 つのセッションの中で `air.room` が 1 °C を超えて動いたら、そのセッションをやり直す（0121 §2.9 の吸気の決まりと同じ趣旨）
- 時間の目安: 回転数の掃引 約 4 時間、EFU の熱の段 約 25 時間（6 段 × B / L / B' × Top・Rear × 3 回）、零位法 約 8 時間（5 組 × 3 回）、応答の行列 約 15 時間、§2.4a の確認 半日〜1 日。数日のセッションに分ける

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
     1. Top と Rear の 6 段のすべてで、有効な 3 回の EFU が信号 ÷ 不確かさの条件（§2.4）と承認の目安（下の 5）を満たす
     2. Front の零位法の 5 つの組のすべてで有効な 3 回があり、点の範囲が 0.35〜1.00 を覆う
     3. §2.4a の 1（出口ごとのプローブの補正）と 2（独立の `q_top / q_rear` の比較）の**両方**を済ませ、その結果を承認の記録で所有者が受け入れる
     4. `balance`・`thermal_limits`・`thermal_inputs` の値が #81 の決定記録で承認されている（下の項）

     2 つの zone だけで `calibrated` にしない（比が作れない）。条件を満たす前の値は provisional で、§2.1 の範囲でだけ使う。
     値を書いても `uncalibrated` の間は Air Balance は無効で、Demand は変わらない（0073 §2.3）
   - `calibrated` にするには、曲線に加えて `balance`（目標の比と帯）・`thermal_limits`・`thermal_inputs` の値が要る。これらは本記録では
     決めず、#81 で応答の行列を材料に別の決定記録で決める。`calibrated` にすると Air Balance の推定が trace に載り、Learned MPC を
     配備したときは balance の項が効く（0073 §2.3）。協調の `mode` は `fan-policy.yaml` で別に `off` のまま（0078）
5. 承認の目安（承認の決定記録で所有者が最終に決める）: 回転数は段ごとの 3 回（§2.3）の範囲が中央値の 3% 以内、EFU は段ごとの
   有効な 3 回の独立な掃引（§2.4）の範囲が平均の 15% 以内。有効な回が 3 未満の段は目安を当てずに値を出さない。
   外れた段は値を出さず、理由を報告に残す。EFU はこの目安を満たしても、上の 4 の条件がそろうまで provisional

### 2.7 run の manifest

測定のセッションごとに 1 ファイル（YAML）。形は解析の道具の PR で固定する。最低限、次を持つ。

- `schema_version`・`run_id`（#83 の `source_run_id` にそのまま使える形）・`kind`（`rpm_sweep` / `thermal_efu` / `response_matrix` / `null_balance`）
- 使った Control Config の 4 ファイルの SHA-256（trace の `runtime.config` と突き合わせ、違えば解析が拒否する）
- 較正の digest（区間の開始の時点で有効な `calibration_activations` の行の `offsets_sha256`。0096 / 0099）
- 解析に使う DB の写しの SHA-256・作った時刻（§2.6 の 2）
- 負荷の定義（GPU の電力の上限・CPU の worker の数）と予備の試行の結果
- 段ごとの指示の値（3 zone）・lease・開始と終了の時刻（ms）・捨てた理由（`floor_bound` / `unsettled` / `aborted` / `safety_fault` / `room_drift` / `baseline_drift` / `low_signal` / `calibration_changed`）と、各窓の代表の時刻

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
- floor に縛られた段（trace の `effective` ≠ manual の値）・未収束・`air.room` のずれ・B と B' のずれ・時刻の内挿（L が B と B' の真ん中にない場合）・
  信号 ÷ 不確かさの不足（量子化された同じ値の 3 回を通さない）・Control Config の hash の不一致・
  区間の中の較正の有効化と digest の不一致・DB の写しの SHA-256 の不一致・空でない `-wal` の添えファイル・写しが区間を覆わないことを、
  manifest と trace の fixture で正しく外す・拒否すること
- 出した点が `FanProfile`（単調・`maximum_rpm`・点の数）と `ZoneFlowCurve`（端点・単調）の検証をそのまま通り、`calibrated` に
  書き換えた写しで `ConfiguredAirBalanceModel` が作れること（テストの中だけ）
- 分母が正で信号 ÷ 不確かさの条件を満たさない点を出さないこと（3 回とも同じ値に量子化された合成の例を含む）。L の時刻が B と B' の
  真ん中にない合成の例で、時刻の内挿がずれを取り除くこと
- Top の引き算で電力の増分がほとんど打ち消され、`q_top` の 3 回がそろっている合成の例で、温度と電力の不確かさを伝えた `u(N)` によって
  雑音だけの正の分子を出さないこと
- 段・組の有効な回が 3 未満なら値を出さないこと（1 回の観測を範囲 0 として通さない）。§2.6 の 4 の条件の 1〜3 のどれかが欠けた出力では、
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
- GPU の Fan の回転が段ごとに変わり、筐体の中の流れを動かす。記録して共変量にし、固定するかは §5 の 3

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 公称 CFM × RPM の比で `q_*` を作る | 装着時の抵抗を含まず、#75 / #81 / `docs/airflow-model.md` の原則に反する。事前情報の記録にとどめる |
| 風速計の測定を主な方法にする | 吸気の側（前面パネルの内側）に当てにくく、格子の後ろの流れは一様でない。`docs/airflow-model.md` は v1 の必須条件にしないとした。補助にとどめる（§5 の 2） |
| `airflow_index` を制御された掃引の RPM の比にする | 回転数の表は良くなるが相対の風量は測っていない。取り付けた状態のインピーダンスで風量は回転数に比例しない（`docs/airflow-model.md`）。trace が代用を測った風量のように見せる |
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
| 1 | `q_front` の零位法で、筐体の内外の圧力差の**向き**を何で見るか（あわせて、5 つの組で `d_f*` の範囲が 0.35〜1.00 を覆わないときに組を足すか） | (a) 細い糸・薄い紙片を前面と背面の隙間に当て、なびく向きが変わる点を目で見る（買い足しなし。定性的） (b) 差圧センサー（数 Pa を測れる品）を基板に足す（ファームウェアと新しいメトリクスの決定記録が要る） (c) 線香の煙の向きを見る (d) 零位法をやめ、`q_front` を Rear / Top の熱の傾きと公称の比から仮に置く（`calibrated` にしない） | **(a) から始める。** 向きの変わる点が段の幅（0.05〜0.10）で決まらないときに (b) を別の決定記録で検討する。(c) は煙の粒子を筐体に入れるので避ける。範囲が覆わなければ、足す組（その組で使う `q_rear` / `q_top` も 6 段の掃引に段を足して実測する）を結果を見て決める |
| 2 | §2.4a の zone をまたぐ独立の確認を何で行うか（**`calibrated` の前提。「行わない」は選べない**。計測器を買うかは所有者が決める） | (a) **羽根車式の風速計で各出口を多点で測る**（Top のラジエーターの排気面・Rear の格子をそれぞれ 9 点程度の格子で。流速 × 面積で比を出す）。費用 約 3,000〜15,000 円。手間 半日（段ごとに数分の読み取り）。格子の後ろの流れが一様でなく ±10〜20% の誤差 (b) **同じプローブを出口の複数の点へ動かして温度の分布を測る**（予備の DS18B20 を 1 本足すか、既存の 1 本を一時的に動かす。動かす間はその metric が欠け、宣言された変更（0109）に書く）。費用 0〜数百円。手間 大（点ごとに落ち着きの待ち。2 出口 × 9 点で数時間）。流速の重みが無いと面の単純平均までしか出ない (c) **(a) と (b) を同じ点で組み合わせる**（流速で重みづけした出口の平均温度が出て、出口ごとの補正の係数と独立な流量の比の両方が得られる）。費用は (a) と同じ。手間 1 日 (d) 既存の `air.*` のプローブを出口の間で入れ替える。費用 0・手間 小。ただしプローブの個体の差（`coldaisle-calibrate` で既に扱える）しか打ち消せず、位置と混ざり方のずれは残るので**単独では確認にならない** (e) 熱線式の風速計で多点を測る（低い流速に強い）。費用 約 20,000〜50,000 円。手間は (a) と同じ | **(c)。** 出口ごとの補正と独立の比の両方が得られ、§2.4a の 1 と 2 を一度に満たす（(e) と (b) の組み合わせでも満たす）。**(a) だけ・(b) だけ・(d) では `calibrated` にならない**: (b) は重みのない出口の温度の補正だけで、zone ごとの流量の比を独立に確かめない。(a) は比は得られるが出口ごとの補正が無い。風速計を買わない判断なら (b) で補正だけを行い、`air-balance.yaml` は `uncalibrated` のまま（EFU は provisional。§2.1 の範囲で使う）。(d) は (b) / (c) の前のプローブの個体差の確認として併用してよい |
| 3 | 測定中の GPU の Fan | (a) `coldaisle-gpu-fan` の曲線のまま、`gpu.0.fan_speed` を記録して共変量にする (b) 測定の間だけ固定の % にする（root のサービスの試験用の曲線。0128 の変更として別の決定記録と承認が要る） | **(a)。** GPU 300 W では回転の変化が小さい見込み。解析で `gpu.0.fan_speed` の幅が段の間で 10 % を超えたら (b) を検討する |
| 4 | §2.6 の 5 の承認の目安（回転数 3%・EFU 15%）と §2.4 の信号 ÷ 不確かさの初期値 | (a) この値で進め、承認の記録で見直す (b) 先に予備の測定で決める | **(a)** |
| 5 | 排気の温度上昇が小さい（Top の 1.00 で CPU の負荷 +50 W なら約 1 °C の見込み）点をどう扱うか。中止の基準は 0121 / 0130 のまま | (a) **段ごとに負荷を変える**: 各段の Demand から 0.05 低い値を floor が超えない範囲で、高い段ほど負荷を重くする（Top は CPU の power と温度の floor、Rear は GPU を 300 W から 450 W まで。Guard の floor が掛かれば捨てる）。EFU は電力の差 ÷ 温度の差なので、負荷が段で違っても比べられる（熱の運び方が負荷に線形という近似） (b) 軽い固定の負荷のまま、条件を満たさない高い段を出さない（曲線の上端が欠ける） (c) Top の熱源に GPU の負荷を使う（GPU の排気は Top へ向かう。C-05）。熱の分かれ方が分からないので `q_top` を決められない (d) 窓を長くする（量子化は減らない） | **(a)。** floor は Demand より下でしか効かないので、高い段では安全の範囲を変えずに負荷を上げられる。それでも条件を満たさない段は (b) と同じく出さない |

本記録が決めず別に決めること: `balance` / `thermal_limits` / `thermal_inputs` の値（#81）、Thermal Dataset への run の取り込み（#83）、
0.35 未満の測り直し（0120 §5 の 1）、解析の道具と manifest の形（実装の PR）。
