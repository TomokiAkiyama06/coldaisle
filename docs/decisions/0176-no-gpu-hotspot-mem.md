# 決定記録 0176: 導入先の GPU が公開しないホットスポットとメモリ温度を、記録も表示もしない

- **種別**: Decision Record
- **Status**: FINAL（2026-10-29、リポジトリ所有者の承認）
- **Date**: 2026-10-29
- **Supersedes**: なし（0040 の `missing_tolerated` の仕組みは変えない。導入先の設定で、出ない値を最初から集めないようにする）
- **関連**: [`0040`](0040-server-health-api.md) / [`0119`](0119-safety-values-first-host.md) / [`0153`](0153-airflow-measurement-method.md) /
  [`0174`](0174-no-airflow-characterization.md)
- **対象 Issue**: #394

## 1. Context

導入先の GPU（RTX PRO 6000 Blackwell、driver 595.91.07）は、NVML で `gpu.0.hotspot` と `gpu.0.mem` を公開しない。
`nvidia-smi -q -d TEMPERATURE` でも `Memory Current Temp: N/A` と出て、ホットスポットの項目そのものが無い（0040 §1・0119 でも同じことを確認した）。
driver が読めない値なので、こちらの工夫では取れない。

collector（`coldaisle-telemetry`）は 2 つを毎回 `quality=missing` で保存している（それぞれ 1 日 1,440 行）。
状態の画面（`server-health.yaml` の `panels.gpu`）は値が無くても常に載せるので、画面と Slack ボットの答えに「—」「欠測」が出続ける。
2026-10-29、所有者が「取れないなら外してほしい」と判断した。

一方で、2 つの名前は機種によらない定義として制御の側にもある。

| 場所 | 役割 | 導入先での実際 |
|---|---|---|
| Reactive Guard の trigger `gpu_hotspot_high`（`fan-policy.yaml` の `gpu_hotspot_c`） | ホットスポットが高いと Front / Rear の floor を上げる | 値が来ないので一度も発火していない |
| fand の入力の契約（`control/loop.py`） | Guard が読む metric を **ADVISORY** として加える | ADVISORY は使えなくても健全度を下げない（`control/state.py` の `_degrades_health`） |
| Critical Safety の `ABSOLUTE_TEMPERATURE_METRICS` | 値が来たときに絶対温度上限を掛ける対象 | 値が来ないので判定に入らない。Critical の入力ではない（0119） |
| Metric Catalog の派生値 `d.gpu_internal_delta` | ホットスポット − コア | 常に欠ける |

## 2. Decision

### 2.1 収集: 記録しない metric を設定で指定する

- `config/internal-telemetry.yaml` の `nvml` に `excluded_metrics` を足す。既定は空で、導入先の設定では `[gpu.0.hotspot, gpu.0.mem]` にする
  - 機種の名前をコードに書かない（AGENTS.md ルール 9）。値を出す GPU に替えたら、一覧から消すだけで元に戻る
- **書けるのは `gpu.<index>.hotspot` と `gpu.<index>.mem` だけ**にする。それ以外（特に `gpu.<index>.core` は Critical Safety の入力、
  `power.gpu.<index>` は Workload Regime と Guard の入力）を書いた設定は、起動時に拒否する。誤記で制御の入力を黙って止めないため
- 一覧にある metric は、NVML から読まず、行も書かない（`missing` の行も残さない）
- **起動時に 1 回だけ読んでみる。** 値が取れたら構造化ログに警告（`excluded_metric_available`）を出す。行は書かない。
  driver の更新で値が出るようになったとき、黙って捨て続けないため

### 2.2 表示・評価から外す

- `config/server-health.yaml`: `panels.gpu` から 2 つを消す。`missing_tolerated` からも消す（もう行が来ないので要らない。`gpu.0.fan_policy` は残す）
- `config/evaluation.yaml`: `temperature_metrics` から `gpu.0.hotspot` を消す
- `config/airflow-characterization.yaml`: `telemetry.metrics` から `gpu.0.hotspot` を消す（0174 で測らないことにしたが、残すと `min_ok_ratio` で必ず不合格になる）
- `docs/requirements.md` の metric の表に、導入先では公開されず記録しない旨を書き添える

### 2.3 残すもの（機種によらない定義）

- Reactive Guard の trigger と `gpu_hotspot_c`、Critical Safety の `ABSOLUTE_TEMPERATURE_METRICS`、Metric Catalog の 2 つと `d.gpu_internal_delta`、NVML から読むコードは**変えない**
- 理由:
  - 導入先の制御の動きは変わらない。今も値は「使えない」で、ADVISORY なので健全度は下がらない。記録をやめても同じ（§2.4）
  - Guard の trigger を消すと `fan-policy.yaml` の形が変わり、導入先の制御の設定の書き換えと人のレビューが要る。得るものが無い
  - 値を出す GPU に替えたとき、設定を戻すだけで Guard と Critical Safety がそのまま働く

### 2.4 fand から見た変化

- fand は Store の最新の行を読む（`StoreTelemetrySource`）。記録をやめた後は、最後の `missing` の行が古くなって使えないまま、保持期間で消えた後は行そのものが無い。
  どちらも Guard の入力は「使えない」で、今と同じく発火しない。健全度・Fallback・Critical Safety の判定は変わらない
- trace の入力の状態の表記（`missing` か、古い・無いか）だけは変わりうる。Demand には効かない
- 実装の PR で、契約の ADVISORY の metric が「行が無い」ときに健全度を下げず、Guard が発火しないことを試験で固定する

### 2.5 既にある行と反映

- 既に保存した `missing` の行は消さない。保持期間（rollup）で自然に消える
- 導入先へは、次の `/opt` の一括の更新に含める（収集のコードと設定の変更。`coldaisle-telemetry` の再起動が要る。fand の設定は変えない）

## 3. Consequences

- 状態の画面・Slack ボットの答えに 2 つの「—」が出なくなる。DB に毎日 2,880 行の `missing` が増えなくなる
- 値を出す GPU に替えたら、`excluded_metrics` から消し、§2.2 の一覧へ戻す。Guard と Critical Safety は設定の変更なしで働く
- driver の更新で値が出るようになっても、起動時の警告が出るだけで記録はしない。その警告を見て設定を戻すかを人が決める

## 4. 承認

- 2026-10-29: 所有者が「取れないなら外してほしい」と判断し、同日に本記録の範囲（§2.1〜§2.5）を承認
