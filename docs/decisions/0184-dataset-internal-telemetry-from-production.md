# 決定記録 0184: Thermal Dataset v2 の特徴量に内部 Telemetry（GPU・CPU の電力と温度）を使えるようにする（本番の DB から同じ read transaction で読む）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-11-08
- **Supersedes**: なし（足すだけ）。0008 §2.8（日次 CSV は従来の 7 列だけ）・0100 / 0112（再生の束縛）・0116（trace の出どころ）は変えない
- **関連**: [`0008`](0008-rollup-and-retention.md) §2.8 / [`0031`](0031-thermal-dataset-contract.md) / [`0087`](0087-dataset-v2-action-grid.md) /
  [`0100`](0100-replay-export-binding.md) / [`0112`](0112-training-replay-path-required.md) §2.3 / [`0116`](0116-dataset-cli-copies-production-traces.md) /
  [`0107`](0107-mpc-worker-observed-window.md) / [`0181`](0181-dataset-anchor-stride-and-training-limits.md) / [`0182`](0182-dataset-in-step-action-mean.md)
- **対象 Issue**: #83 / #84

## 1. Context

2026-11-08 の試行（7 日分・負荷の段の試験を含む test）で、offline 評価（0172）は underprediction の段で止まった。室温 mild の層で、
`air.gpu_exhaust` の 60 / 120 s 先の予測が 0.99 分位で 3.0 / 4.5 ℃ 低く外れた（上限 2.0 ℃）。GPU の負荷が急に上がった場面とみられる。

特徴量が空気の温度 6 つだけで、**GPU・CPU の電力も温度も入っていない**。負荷が上がり始めても、model からは排気が上がりそうだと
分からない。ところが、今の仕組みではこれらを特徴量にできない。

- Dataset v2 は、日次 CSV（0008 §2.8 で**センサー基板の 7 列だけ**）を専用 DB へ再生して作る（0100 / 0112）。内部 Telemetry
  （`coldaisle-telemetry` が本番の DB へ書く `power.gpu.0`・`power.cpu.package`・`gpu.0.core`・`cpu.package` ほか）は CSV に無い
- 専用 DB は再生の後に readings を封印し（`dataset_readings_seal`）、後で readings が変わっていれば生成を拒否する。本番の読み取りを
  専用 DB へ写すと、この封印に当たる

一方、運転中の入力は揃っている。fand の入力の契約（`control/loop.py`）は、Workload Regime の入力（`fan-policy.yaml` の
`workload_regime.cpu_power` / `gpu_power`）と必須の CPU・GPU の温度を、いつも含む。Learned worker の window（0107）は frame の
snapshot の signal から作るので、これらの metric を特徴量にした model も運転で同じ値を受け取れる。

本番の DB では、これらは 2.5 s ごと・品質 `ok` で記録されている（11-08 の 19 時間で各 27,990 行）。

## 2. Decision

### 2.1 出どころの分け方

- Dataset v2 の feature / target の metric のうち、**日次 CSV の列にある metric**（`air.*`。0008 §2.8）は今のとおり専用 DB
  （再生）から読む
- **CSV の列に無い metric**（内部 Telemetry）は、本番の DB（`--calibration-history-db`）から読む。どちらから読むかは metric の名前で
  決まり、spec に新しい欄は足さない（CSV の列の表は `channels.py` が持つ）
- 本番の DB から読む metric は、**較正の記録・`csv_exports`・trace と同じ read transaction**（0100 §2.6 / 0116 §2.1）で、
  source run の期間 `[start_ms, end_ms)` の raw の読み取り（`readings`。値と品質）を読む。専用 DB へは**写さない**（封印を変えない）
- 読んだ系列は builder に渡し、`air.*` と同じ規則（as-of・鮮度・品質の mask。0031 / 0087）で window と target の cell を作る

### 2.2 manifest に残すもの

- manifest に `production_metrics`（本番の DB から読んだ metric の名前の昇順の列）を足す
- `telemetry_sha256` は今のとおり、feature / target の全 metric の系列（どちらの出どころでも）から作る。これで、学習の入口が
  本番の DB から読み直した系列が 1 点でも違えば、作り直した bytes が一致せず拒否される（0112 §2.3 の 3）

### 2.3 学習の入口

- 学習の入口（0112 §2.3）も、同じ関数で、同じ read transaction の中で本番の DB から系列を読み、builder に渡す
- **本番の DB の raw が保持期間で消えた期間は作らない・学習に使えない**。本番の DB の raw の読み取りの保持期間（`raw_days`、
  既定 30 日）を過ぎた run は、作り直しで系列が欠けて bytes が一致しない。trace の保持期間（0112 §2.3 の 4）と同じ扱いで、
  欠けた系列で黙って作らないよう、期間の先頭より前の raw が本番の DB に 1 行も無い metric は拒否する（§5 の 2）

### 2.4 運転との対応

- model の feature の metric が fand の入力の契約に無ければ、運転ではその cell が毎回欠測になる。**起動時の registry の束縛
  （0098）で、artifact の feature の metric が入力の契約にすべて含まれることを確かめ、含まれなければその artifact を
  Learned に使わない**（Fallback のまま運転する）。黙って欠測の入力で推論しない

### 2.5 試験（すべて `-m "not hardware"`）

- `air.*` は専用 DB から、内部 Telemetry は本番の DB から読み、どちらも同じ規則で cell になる
- 本番の DB の系列が 1 点でも違えば、学習の入口で作り直した bytes が一致せず拒否される
- 期間の先頭より前の raw が無い metric は拒否する
- 入力の契約に無い feature の metric を持つ artifact は、起動時の束縛で Learned に使わない
- 内部 Telemetry を使わない spec では、今と同じ bytes（`production_metrics` が空の列）

## 3. Consequences

良くなること:

- 負荷の変化（電力）と部品の温度を特徴量にでき、負荷が上がり始めた場面の予測の外れを減らせる見込み
- CSV の形（0008）・再生の束縛（0100）・trace の出どころ（0116）を変えずに済む

悪くなること（と緩和策）:

| 悪くなること | 緩和策 |
|---|---|
| 内部 Telemetry の系列は再生の fingerprint（`source_sha256`）の外にある | 学習の入口が本番の DB から読み直し、`telemetry_sha256` を含む bytes の一致を求める（§2.2）。trace と同じ守り方 |
| 本番の DB の raw の保持期間（30 日）を過ぎた dataset は学習に使えない | trace（30 日）と同じ制約。学習は採ってから 30 日以内に行う |
| 本番の DB の読み出しの間、系列をメモリに持つ | 2.5 s ごとの 1 metric で 1 日約 35,000 点。数 metric × 数日で数百 MB に収まる |
| manifest の形が変わる | 内部 Telemetry を使わない dataset でも欄（空の列）が足される。実データの dataset はまだ試行だけ |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| 日次 CSV に内部 Telemetry の列を足す | 0008 §2.8 の「従来の列だけ」を変え、export の照合（0100 / 0108）・再生にも影響が広がる |
| 本番の読み取りを専用 DB へ写す（trace と同じ） | 専用 DB の readings の封印（再生の後の変更を拒む）に当たる。封印の例外を作るより、写さずに渡すほうが守りが単純 |
| 内部 Telemetry の第 2 の CSV を作って再生する | export と再生の経路が 2 つになり、束縛（0100）も 2 つ要る |
| spec に「本番から読む metric」の欄を足して人に選ばせる | 出どころは metric の名前で一意に決まる。選ばせると誤りの入口が増える |

## 5. 未決事項

| # | 論点 | 選択肢 |
|---|---|---|
| 1 | 最初の試行で足す metric | **(推奨) `power.gpu.0`・`power.cpu.package`** / 電力と温度（`gpu.0.core`・`cpu.package`）の 4 つ / 利用率（`gpu.0.utilization`）も |
| 2 | raw が保持期間で消えたことの見分け方 | **(推奨) 期間の先頭より前の raw が本番の DB に 1 行も無い metric を拒否** / 保持期間の設定値と run の開始で判定 / 判定せず bytes の不一致に任せる |
| 3 | 起動時の束縛で入力の契約を確かめる（§2.4）のを、本記録の実装に含めるか | **(推奨) 含める**（Learned を shadow にする前に要る） / 別の PR にする |
