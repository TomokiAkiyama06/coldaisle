# 決定記録 0181: Thermal Dataset v2 の anchor の間隔と、学習の入口の上限（実データの 1 日で分かった桁の違い）

- **種別**: Decision Record
- **Status**: FINAL（2026-11-05、リポジトリ所有者の承認。§5 の 1〜3 はすべて推奨の案）
- **Date**: 2026-11-05
- **Supersedes**: なし（[`0087`](0087-dataset-v2-action-grid.md) の「anchor にできる tick はすべて anchor にする」を、v2 の spec の
  欄で間引けるようにする。0087 の格子・鮮度・除外の規則は変えない。[`0150`](0150-train-cli.md) の学習の入口の上限の値を変える。
  どちらも採用されたら旧記録に `Superseded by` を追記する）
- **関連**: [`0031`](0031-thermal-dataset-contract.md) §5 の 1 / [`0079`](0079-model-artifact-formats.md) /
  [`0109`](0109-dataset-declared-changes-cli.md) / [`0172`](0172-thermal-offline-evaluation.md) / #416（PR #417）
- **対象 Issue**: #83 / #84

## 1. Context

2026-11-05 に、本番の DB の写しで Dataset v2 の生成から学習までを通しで試した（#83 / #84 の実データの最初の試行。
写しは scratchpad の上だけで、Registry・設定・導入先には書いていない）。spec は試行用の仮の値（window 60 s・sample period 2.5 s・
horizon 30 / 60 / 120 s・action step 10 s × 12・feature 3（`air.room` / `air.front_intake` / `air.gpu_intake`）・
target 3（`air.*_exhaust`））。

| 段 | 結果 |
|---|---|
| Dataset v2 の生成（1 日・2026-10-28） | #417 の修正の後でピーク 10.8 GB・1 分 46 秒。example 約 86,000 件、`examples.jsonl` 1.1 GB（1 件 約 13 KB）。6 日分は修正の前に 82 GB を超えて止めた |
| 学習（`coldaisle-train`） | 入口で `Dataset artifact componentが安全なsize上限を超えている`（`MAX_DATASET_EXAMPLES_BYTES` = 256 MiB）で止まる |

原因は桁の違いで、どれか 1 つの不具合ではない。

1. **anchor が 1 s ごと。** 0087 は anchor にできる ControlTick をすべて anchor にする。fand の tick は 1 s なので 1 日 86,400 件になり、
   隣り合う example は window の 59 / 60 s を共有する。件数のわりに情報が増えない
2. **学習の入口の上限が、件数と特徴量の数に対して小さい。** 上限はコードの定数（`src/coldaisle/control/model/training.py`）

| 上限 | 値 | 試行の spec（特徴量 339 列）で入る train の件数 | 窓 30 s・5 s（123 列）で入る件数 |
|---|---|---|---|
| `MAX_DATASET_EXAMPLES_BYTES` | 256 MiB | 約 20,000 件（1 件 13 KB） | 同じ程度 |
| `MAX_TRAINING_CELLS`（train の件数 × 列） | 1,000,000 | 2,949 | 8,130 |
| `MAX_DATASET_FEATURE_CELLS`（全件 × 列） | 1,000,000 | 2,949（全 split の合計） | 8,130 |
| `MAX_RIDGE_WORK_UNITS`（出力 × (件数 × 列² + 列³)） | 50,000,000 | **0 件でも超える**（列³ だけで 3.5 億） | 244 |

ridge は純粋な Python（`math.fsum` と掃き出し）で解く。この機械で測った速さは **1 出力あたり約 3.5 × 10⁷ 単位/秒**
（列 123・2,000 件で 1.0 s）。今の上限 5 × 10⁷ は、出力 9 つを合わせて約 1.5 s の計算に当たる。

一方、offline 評価（0172）の既定の例（`config/thermal-evaluation.example.yaml`、仮の値）は test に 1,000 件以上・1 時間の block を
24 以上求める。test だけで 24 時間を超える期間が要るので、学習の dataset は数日分になる。いまの上限のままでは、
**どの anchor の間隔でも評価の最小の件数と学習の上限を同時に満たせない**。

## 2. Decision

（§5 の推奨の案。所有者が 2026-11-05 に承認した）

### 2.1 Dataset v2 の spec に `anchor_stride_ms` を足す（必須・既定値なし）

- `DatasetSpecV2` に `anchor_stride_ms`（正の整数・ms）を足す。**既定値は持たない**（0087 §2.2 / AGENTS.md ルール 9。値は spec で明示する）。
  CLI は `--anchor-stride-ms` を v2 で必須にする
- anchor の選び方: run の開始 `source_run.start_ms` から `anchor_stride_ms` ごとの格子の時刻 `g` のそれぞれについて、
  `g` 以上で最初の ControlTick を候補にする（同じ tick が 2 つの格子に当たれば 1 回だけ）。候補に今の 0087 の判定（window・target の
  完全さ・鮮度・除外の理由）をそのまま当てる。候補にならなかった tick は数えない（`excluded` の理由に足さない）
- `prior_action`・action の格子・as-of は、今のとおり**全 tick** から引く（間引くのは anchor だけ）
- manifest の `spec` に `anchor_stride_ms` が載るので、間隔の違う dataset は別の spec として区別される。`schema_version` は 2 のまま
  欄を足すか、版を上げるかは実装の PR で決める（v2 の既存の artifact を読めることを保つ）

### 2.2 学習の入口の上限を上げる（コードの定数のまま）

| 上限 | 今 | 新しい値 | 目安 |
|---|---|---|---|
| `MAX_DATASET_EXAMPLES_BYTES` | 256 MiB | 1 GiB | 30 s 間隔で 30 日分（約 86,000 件・約 1.1 GB）に近い |
| `MAX_TRAINING_CELLS` / `MAX_DATASET_FEATURE_CELLS` / `MAX_DATASET_TARGET_CELLS` | 1,000,000 | 50,000,000 | 50,000 件 × 1,000 列。float の tuple で数 GB |
| `MAX_RIDGE_WORK_UNITS` | 5 × 10⁷ | 2 × 10¹⁰ | この機械で約 10 分（3.5 × 10⁷ 単位/秒の実測から） |

- 上限は**資源の安全弁**（巨大な入力で CPU とメモリを食い尽くさない）で、制御・安全の閾値ではないので、コードの定数のままにする
  （`config/` に出すと、設定の書き換えで弁を外せてしまう）
- 学習の CLI は人が手で実行し、timer にしない（0150）。上限に当たったときの終了とログは今のまま
- 学習は導入先の常駐サービスと同じ機械で走りうるので、`docs/thermal-model.md` に「`systemd-run --user --scope -p MemoryMax=…` で
  上限を付けて走らせる」手順を書く（Dataset の生成も同じ）

### 2.3 最初の実データの試行の値（仮の値。本番の値は 0031 §5 の 1 のまま、評価の後に決める）

- `anchor_stride_ms` 30,000（1 日 2,880 件・約 37 MB）。6 日分（train 4・validation 1・test 1 日）で約 17,000 件・約 220 MB
- window 30 s・sample period 5 s（特徴量 123 列。air.* の CSV は 2.5〜3 s 間隔なので、5 s の格子で欠けにくい）
- この組で ridge の演算量は約 9 ×（11,500 × 123² + 123³）≈ 1.6 × 10⁹（約 50 秒）で、§2.2 の上限に収まる

### 2.4 実装の順と試験

1. `anchor_stride_ms`（§2.1）と CLI の引数。試験: 間隔 1 tick で今の出力と bytes が一致する（既存の試験の spec に 1 tick の間隔を足して
   通す）、間隔 N で anchor が格子の直後の tick だけになる、`prior_action` と格子は全 tick から引く、既定値が無く省くと拒否する
2. §2.2 の上限の値と、上限の手前・超えたところの試験の付け替え
3. `docs/thermal-dataset.md` / `docs/thermal-model.md` の手順（`--anchor-stride-ms`・メモリの上限の付け方）

`dataset.py` と `control/model/` だけの変更で、fand の import の閉包の外（0167 の区分では `/opt` の常駐に影響しない。学習は導入先の
`/opt` の venv で人が実行するので、学習を導入先で行う前に `/opt` を更新する）。

## 3. Consequences

### 良くなること

- 数日分の実データで Dataset → 学習 → offline 評価（0172）を通せる。#84 の段 3（実データで評価して閾値を決める）へ進める
- 隣り合う anchor の重なりが減り、件数あたりの情報が増える。Dataset の生成のメモリと時間も間隔に比例して減る

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 間引いた tick の action の変化を anchor として学ばない | `prior_action` と action の格子は全 tick から引く。間隔は spec の値で、評価を見て詰められる |
| 学習の上限を上げると、1 回の学習が数分〜10 分かかり、メモリも数 GB 使う | 人が手で実行する。メモリの上限を付ける手順を書く。上限の値そのものは残す |
| v2 の spec の欄が増える | 既定値を持たないので、古い呼び出しは黙って 1 s 間隔にならず拒否される |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| anchor を間引かず、上限だけ上げる | 1 日 1.1 GB・30 日 33 GB。生成も学習も example を全部メモリに持つ形なので、数日分でも数十 GB になる |
| Dataset を作った後に example を間引く（学習の側で） | Dataset の bytes と manifest の `examples_sha256` は全件を指す。学習の入口は builder で作り直して bytes を比べる（[`0112`](0112-training-replay-path-required.md)）ので、後から間引くと束縛が崩れる |
| ridge を numpy で解く | 依存が増え、純粋な Python で決定的に解く今の性質（同じ入力で同じ bytes）を確かめ直す必要がある。§2.2 の上限で当面足りる |
| 上限を `config/` に出す | 資源の安全弁を設定の書き換えで外せてしまう |

## 5. 判断点（推奨を先頭に）

| # | 判断 | 選択肢 | いつまでに | 状態 |
|---|---|---|---|---|
| 1 | anchor の間引き | **(推奨) spec の必須の欄 `anchor_stride_ms` で間引く（§2.1）** / 間引かず上限だけ上げる / 学習の側で間引く | 実装の前 | 決定済み（所有者、2026-11-05）: 推奨の案 |
| 2 | 学習の入口の上限 | **(推奨) §2.2 の値に上げ、コードの定数のまま** / もっと控えめに（例: ridge 2 × 10⁹・約 1 分）/ `config/` へ出す | 実装の前 | 決定済み（所有者、2026-11-05）: 推奨の案 |
| 3 | 最初の試行の値 | **(推奨) §2.3（30 s 間隔・窓 30 s・5 s・6 日）。本番の値は評価の後** / 別の値を所有者が指定する | 試行の前 | 決定済み（所有者、2026-11-05）: 推奨の案 |
