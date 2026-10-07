# 決定記録 0116: `coldaisle-dataset` の v2 は本番の DB の trace を `seq` ごと専用 DB へ写してから作る（0112 §5 の未決の決着）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-08、リポジトリ所有者が §2.1 を推奨案で承認。§2.2 の細部は確認待ち）
- **Date**: 2026-10-08
- **Supersedes**: なし（[0112](0112-training-replay-path-required.md) §5 の1つ目の未決「`coldaisle-dataset` の v2 で、専用 DB の
  ControlTick を本番の DB の trace から写す（または一致を確かめる）配線」を決着させる。0112 の決定は置き換えない。0112 は
  FINAL でマージ済みなので本文には手を入れず、ここに残す（README「追記のみ」）
- **関連**: [0112](0112-training-replay-path-required.md) §2.3 / §5 / [0100](0100-replay-export-binding.md) §2.6 /
  [0109](0109-dataset-declared-changes-cli.md) / [0087](0087-dataset-v2-action-grid.md) §2.1 / `src/coldaisle/dataset.py` /
  `src/coldaisle/training_entry.py` / `src/coldaisle/store/export_binding.py` / `docs/thermal-dataset.md`
- **対象 Issue**: #83（関連: #237）

## 1. Context

0112 §2.3 で、学習の入口は元の入力を一時の専用 DB へ再生し、本番の DB の trace を `seq` ごと写して dataset を作り直し、
公開物の bytes と比べるようになった（PR #264）。`control_trace_sha256` は `seq` を含むので、学習の入口を通る dataset は、
元の専用 DB の trace も本番の trace を `seq` ごと写したものでなければならない。ところが `coldaisle-dataset` の v2 は、
専用 DB に既にある trace をそのまま使っていて、本番の trace を写す経路が無かった（0112 §5 の未決）。

## 2. Decision

### 2.1 `coldaisle-dataset` の v2 は本番の DB の trace を写してから作る

**決着（2026-10-08 所有者の決定、推奨案）。** `coldaisle-dataset --dataset-version 2` は、`--calibration-history-db`
（本番の DB）を読み取り専用で開き、較正の記録・`csv_exports` の行と**同じ read transaction** で、source run の期間
`[--start-ms, --end-ms)` の ControlTick の trace を記録した順（`seq`）で読む（0112 §2.3 の学習の入口と同じ読み出し）。
専用 DB（`--db`）へ `seq` を保って写してから builder を走らせる。これで `coldaisle-dataset` で作った dataset は、
そのまま学習の入口（`training_entry.verify_training_dataset_v2`）を通る。

学習の入口と同じく、本番の DB の trace が保持期間で消えた期間（削除の境界が run の開始より後）と、移行前の行を
含む期間からは作らない（作っても学習に使えないため）。

### 2.2 細部（推奨案で実装。所有者の確認待ち）

| # | 論点 | 推奨案（実装済み） |
|---|---|---|
| 1 | 専用 DB に既に trace があるとき | 期間の trace が本番の trace と**完全に一致**（`seq`・時刻・`tick_id`・版・`trace_json`、順序も）すれば写さずにそのまま作る（同じ DB での作り直しを許す）。1件でも違えば作らない（上書き・混ぜることはしない） |
| 2 | 写す時点 | 宣言・入力の manifest・本番の DB の読み出しがすべて通った後、builder の直前。写した後で builder が拒否しても trace は残る（本番と一致するので、作り直しでは §2.2 の 1 で通る） |
| 3 | 学習の入口との共有 | 写すか一致を確かめる処理と、保持期間・移行前の判定は、`coldaisle-dataset` と学習の入口で同じ関数を使う |
| 4 | 本番の DB に期間の trace が1件も無いとき | 写すものが無いまま builder へ進み、builder が「完全な window / target を持つ ControlTick が無い」で拒否する（0087 のまま） |

## 3. Consequences

### 良くなること

- `coldaisle-dataset` の v2 で作った dataset が、そのまま学習の入口を通る
- ControlTick の出どころが、dataset の生成と学習の入口で同じ（本番の DB の trace）になる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| v2 の生成が専用 DB へ書く（trace を写す） | 専用 DB は1 run 専用の作業用の DB。readings は封印のまま（trigger）。trace は封印の対象外（0031） |
| 専用 DB に本番と違う trace があると作れない | 新しい専用 DB で再生し直す。拒否の理由をログに出す |
| 本番の trace が消えた期間からは作れない | 学習に使う期間は trace の保持期間（0030）の中で作る |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 一致だけを確かめ、写すのは別の手順にする | 手順が1つ増え、学習の入口を通らない dataset を作れてしまう |
| 専用 DB の trace を本番の trace で上書きする | 専用 DB の中身を黙って入れ替える。食い違いは人の誤りとして知らせる |
| `seq` を振り直して写す | `control_trace_sha256` が学習の入口と一致しない（0112 §2.3） |

## 5. 未決事項

- §2.2 の細部 1〜4（推奨案で実装済み。所有者の確認待ち）
