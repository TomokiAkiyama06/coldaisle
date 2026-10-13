# 決定記録 0123: CPU Power の cooling floor と `telemetry.cpu_power_ms` を確定する試験を 0121 に足す

- **種別**: Decision Record
- **Status**: Proposed（所有者の承認待ち）
- **Date**: 2026-10-14
- **Supersedes**: [`0121`](0121-hardware-test-procedure.md) の一部（§2.3 の `*_telemetry_stale` の行のうち「`cpu_power_ms` は入力が無いので
  確定しない」と、§3「悪くなること」の同じ趣旨の項。0122 §5 の 1）
- **関連**: [`0119`](0119-safety-values-first-host.md) §2.4 / §2.8 / §2.13 /
  [`0121`](0121-hardware-test-procedure.md) /
  [`0122`](0122-cpu-power-publisher.md)
- **対象 Issue**: #278 / #74

---

## 1. Context

0122 で、導入先は `power.cpu.package` を 1 s 平均・1 W 単位で集めるようになった（2026-10-14 に導入先だけで有効化）。
2026-10-14 の確認で、アイドルは約 40〜50 W、CPU の全スレッドの負荷は約 200 W だった。

これで `safety.yaml` の2つの値を試験できる。どちらも 0119 で `provisional` に置いたまま。

- `cpu_power_cooling_floor`（100 W → 0.35、200 W → 1.0。Top にだけ効く）
- `telemetry.cpu_power_ms`（5000）

コードを確かめて分かったこと:

- CPU Power の入力が古い・欠けているとき、fault は上がらない。Top の power の項を外し、reason の detail に `cpu_power_unavailable` を残す。
  入力の重要度は `DEGRADED` なので、telemetry の健全度が `DEGRADED` になり、Reactive Guard は degraded の閾値を使う
- 入力があれば健全度は `NORMAL` になりうる（ほかの非 advisory の入力がそろっているとき）
- Top の floor の reason は、温度と電力のどちらの曲線が決めても同じ `cpu_cooling_floor` で、どちらかは trace に出ない（大きい方を取る）。
  trace は tick が使った入力の値も持たず、DB の時刻からその tick が観測した sample を 1 tick の精度で決めることもできない。
  そこで T8 は、温度の曲線では説明できない `safety_floor` が現れたことで power の曲線が効いたと判定する（§2.1）
- CPU Power の収集の側にも古さの判定がある（`internal-telemetry.yaml` の `cpu_power.stale_after_ms` 5000）。専用サービスだけを
  止めると、収集は 2.5 s ごとに行を書き続け、ファイルが古くなると adapter が `stale` で書き、fand はそれを見てすぐ power の項を外す。
  この場合は fand の側の `telemetry.cpu_power_ms` の計時を通らない。fand の計時を測るには、行そのものを止める（`coldaisle-telemetry` を止める）

## 2. Decision

0121 の共通の決まり（§2.1。経路を迂回しない・中止の基準・記録・所有者が現地）はそのまま使う。

### 2.1 T8: CPU Power の cooling floor

アイドル（`cpu.package` 50 °C 未満）で fand の auto が落ち着いた状態から、CPU の全スレッドの負荷を 5 分かけて止める。3 回行う。
GPU 600 W だけの負荷でも 1 回行う。

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| 負荷の開始から、Top の `safety_floor`（trace）が、その tick の前後 2.5 s の `cpu.package`（DB）の最大値から計算した温度の曲線の値より 0.05 以上高くなる最初の tick まで（温度の曲線では説明できず、power の曲線が効いたと分かる） | `cpu_power_cooling_floor` | 各試行で負荷の開始から 5 s 以内に現れる（専用サービスの 1 s 平均・収集の 2.5 s の周期・1 tick を最悪の位相で足した約 4.5 s に余裕を足す） |
| 全スレッドの定常（約 200 W）での Top の `effective` | 同上 | 1.0。温度と排気の温度が中止の基準に近づかない |
| GPU 600 W だけの負荷での `power.cpu.package` と Top の power の項 | 同上 | CPU Power が 100 W を超えず、power の floor が Top を上げない（GPU の負荷で誤って Top を全開にしない） |
| 0121 の T1（soak）で power の floor が Top を決めた時間の割合と、そのときの CPU の使われ方 | 同上 | 値として記録し、所有者が音とのつり合いを判断する（合否にしない） |

### 2.2 T9: `telemetry.cpu_power_ms` と収集の側の古さの判定

**(a) fand の計時（`telemetry.cpu_power_ms`）**: 0121 の T3 で `coldaisle-telemetry` を止めるときに、あわせて測る（行そのものが止まるので
fand の age の計時を通る）。

| 測るもの | 確定する値 | 合格の条件 |
|---|---|---|
| DB に残った最後の `power.cpu.package` の行の時刻から、Top の reason に `cpu_power_unavailable` が出る tick まで（fand がその行を初めて観測した tick は記録に残らず、行の時刻から最大 1 tick 遅れる） | `telemetry.cpu_power_ms` 5000 | 5000 ms 以上・5000 ms + 2 tick 以内（観測の遅れの 1 tick を含める。早すぎても不合格）。CPU Power について fault は上がらない（同時に上がる `cpu_telemetry_stale` / `gpu_telemetry_stale` は T3 のとおり） |

**(b) 収集の側の判定**: fand が auto で動いている間に、`coldaisle-cpu-power` を `systemctl kill -s SIGSTOP` で 30 s 一時停止し、
`SIGCONT` で戻す（`systemctl stop` は unit の `RuntimeDirectory` を消し、ファイルが無いと adapter は `stale_after_ms` を待たずに
`missing` にするため。一時停止ならファイルは最後の内容のまま残り、更新だけが止まる）。

| 測るもの | 確かめること | 合格の条件 |
|---|---|---|
| 専用サービスの最後の書き込み（ファイルの `ts_ms`）から、Top の reason に `cpu_power_unavailable` が出る tick まで | `cpu_power.stale_after_ms` 5000（収集の設定） | 5000 ms + 収集の周期 2500 ms + 1 tick 以内。fault は上がらず、Top は温度の floor と Fallback のまま |
| その間の telemetry の健全度と Guard の閾値 | 健全度の連動 | 健全度が `DEGRADED`、Guard が degraded の閾値（`threshold_profile` が `conservative`）になる |
| 戻した後 | 復帰 | 新しい sample を観測した tick から power の項が戻り、健全度が `NORMAL`（ほかの入力がそろっているとき）に戻る |

### 2.3 0121 の T1（soak）への追加

0121 §2.3 の `*_telemetry_stale` の行は、`cpu_power_ms` も他と同じく数える（`cpu_power_unavailable` が出た回数。収集が止まった時間を除き 0 回）。
あわせて、soak の間の telemetry の健全度が `NORMAL` だった時間の割合と、Guard の発火の回数を記録する（合否にしない）。

### 2.4 順序

T8 と T9 (b) は 0121 の T7 の後、T1 の前に行う。T9 (a) は T3 の中で行う。順序は T0 → T2 → T3 → T6 → T4 → T5 → T7 → T8 → T9 → T1 になる。

## 3. Consequences

### 良くなること

- 0119 の CPU Power の floor と `cpu_power_ms` に、確定のための試験と合格の条件がそろう
- GPU の負荷で Top を誤って全開にしないことを確かめる

### 悪くなること・その緩和

- 試験が半日ほど延びる
- power の floor で Top が全開になる時間（音）は合否にせず、T1 の記録から所有者が判断する

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 0121 を書き直す | 決定記録は書き換えない（追記のみ）。一部を置き換える新しい記録にする |
| T1 の後に別の soak を回す | T1 の前に入れれば 24 時間の soak を1回で済ませられる |

## 5. 未決事項

| # | 内容 | 時期 |
|---|---|---|
| 1 | Guard の `cpu_power_metric`（CPU 電力の上昇での発火）を使うか | T1 の記録の後、別の記録（0122 §5 の 2） |
