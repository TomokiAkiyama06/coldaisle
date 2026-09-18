# 決定記録 0047: CPU 使用率のメトリクス名（`cpu.utilization`）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-18、リポジトリ所有者が承認）
- **Date**: 2026-09-18
- **Supersedes**: なし（決定記録 0032 / 0043 は変えない。0002 §2.1 の文法で名前を足す）
- **関連**: [`0002-metric-naming.md`](0002-metric-naming.md) §2.1 /
  [`0029-telemetry-loss-classes.md`](0029-telemetry-loss-classes.md) §2.2 /
  [`0032-internal-telemetry-metric-names.md`](0032-internal-telemetry-metric-names.md) /
  [`0038-internal-telemetry-outage-counting.md`](0038-internal-telemetry-outage-counting.md) /
  [`0043-cpu-die-and-gpu-throttle-metrics.md`](0043-cpu-die-and-gpu-throttle-metrics.md)
- **対象 Issue**: #65

## 1. Context

GPU には `gpu.0.utilization`（0032）があるが、CPU の負荷を表す値が無い。CPU の温度
（`cpu.package` / `cpu.tctl` など）が上がったとき、それが負荷によるものか冷却の不足に
よるものかを後から切り分けるには、同じ時刻の CPU 使用率が要る。

2026-09-18、リポジトリ所有者がセッション内で、CPU 使用率を `cpu.utilization`（単位 %、
Advisory、`/proc/stat` から全 CPU を合わせて計算）として収集することと、その名前を承認した
（セッション内で所有者が直接回答）。

## 2. Decision

### 2.1 名前と定義

| 対象 | メトリクス名 | 単位 | 取得元 |
|---|---|---|---|
| CPU 使用率（全 CPU 合計） | `cpu.utilization` | % | `/proc/stat` の `cpu` 集計行 |

- `gpu.<index>.utilization` と同じ語を使い、domain だけを `cpu` にする
- 値は前回 poll からの差分で計算する: `100 × (Δtotal − Δidle) / Δtotal`
  - total は `cpu` 行の先頭8列（user nice system idle iowait irq softirq steal）の和。
    guest / guest_nice は kernel が user / nice に含めているため足さない
  - idle は idle + iowait。iowait は CPU が命令を実行していない時間であり、発熱に寄与しない
- 全 CPU（論理 CPU すべて）を合わせた値で、コアごとの値は持たない。0..100 % の範囲になる
  （`top` の「1コア = 100 %」の表記ではない）

### 2.2 欠測の扱い

`/proc/stat` は起動からの累積値なので、1回の読み取りでは使用率にならない。次の場合は
**0 % ではなく `missing`** とする。

- collector 起動後の最初の poll（前回値が無い）。source の状態は `ok` のまま
  （`detail: no_previous_sample`）
- 前回から累積値が進んでいない、または巻き戻った。巻き戻った値は次回の基準にする
- 読み取り・解析の失敗（source は `unavailable`）。失敗をまたいだ差分は使わず、復帰後の
  最初の poll は再び前回値待ちになる
- Linux 以外（`/proc/stat` が無い）。source は `unavailable`（`detail: unsupported_platform`）

有効な間は周期メトリクスとして登録し、停止は 0038 のとおり欠測として数える。

### 2.3 欠測の区分（0029）

**Advisory（記録のみ）。** 0029 §2.2 の GPU utilization と同じ扱いとする。
Critical Safety / Reactive Guard / Fallback Controller の入力にはしない。使う場合は
別の決定記録で決め、人の承認を得る。

## 3. Consequences

- CPU 温度の変化を、同じ時刻の負荷と並べて読める
- collector の再起動ごとに最初の1サンプル（2.5 秒）は必ず欠測になる。0 % を記録して
  「負荷が無かった」と誤読させるよりよいと判断した
- 設定に `proc_stat`（`enabled` / `path`）が加わる。Internal Telemetry の設定を書く
  すべての箇所で必須になる

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 最初の poll を 0 % とする | 負荷が無かったという誤った記録になる |
| 起動時に短く2回読んで最初から値を出す | 周期と異なる窓の値が混ざり、poll に待ち時間が入る |
| `/proc/loadavg` の load average | 実行待ちの数であって使用率ではなく、I/O 待ちも含む。1分の指数平均で遅れる |
| コアごとの `cpu.<n>.utilization` | 必要な用途がまだ無い。1 poll あたりの行数が論理 CPU 数だけ増える |
| `psutil` を依存に加える | 1行の解析のために依存を増やさない |
| `sys.cpu_utilization` | CPU の観測値は `cpu` domain に置く（0002 §2.1） |

## 5. 未決事項

- FINAL への変更（所有者）
- コアごと・CCD ごとの使用率が必要になった場合の名前
