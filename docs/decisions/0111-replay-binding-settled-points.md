# 決定記録 0111: 0100 段 2（再生の照合と `dataset_source_run` の束縛。PR #256）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-08、リポジトリ所有者が推奨案で承認）
- **Date**: 2026-10-08
- **Supersedes**: なし（0100 に細部の無い点と、0100 に書かれていない点への追加。0100 / 0010 / 0031 / 0096 / 0108 の
  決定は置き換えない）
- **関連**: [0100](0100-replay-export-binding.md) §2.3 / §2.4 / §2.5 / §2.8 / §2.10 / §5 #7 /
  [0108](0108-export-manifest-settled-points.md)（段 1 の決着点。同じ形の記録）/
  [0010](0010-csv-replay.md) §2.7 / [0031](0031-thermal-dataset-contract.md) §2.3 / [0096](0096-calibration-binding-digest.md) §2.3 /
  `src/coldaisle/ingest/replay.py` / `src/coldaisle/csv_export_manifest.py` / `src/coldaisle/daemon.py` /
  `src/coldaisle/store/db.py` / `src/coldaisle/store/migrations/0013_dataset_source_run_export_binding.sql`
- **対象 Issue**: #237（実装 PR #256）

## 1. Context

0100 の段 2（再生の照合・DST の拒否・照合していない run・fingerprint の版・`dataset_source_run` の timezone と
`export_binding_sha256` の migration と bind 時の記録。0100 §2.10）を PR #256 で実装した。その実装で、0100 に細部の
無い点と書かれていない点が8点あり、PR #256 の「人間のレビューが必要な点」で推奨案とともに示した。2026-10-08、
所有者が8点すべてを推奨案で承認し、PR #256 はマージされた。決定記録は追記のみなので、0100 の本文には手を入れず、
ここに残す。記録だけを読んだ人が、実装と違う形を前提にしないためである。

## 2. Decision

### 2.1 `export_binding_sha256` の bytes の形

**決着（2026-10-08 所有者の決定、推奨案）。** 0100 §2.8 の「`export_id` の順に並べた `(export_id, export_record_sha256)`
の列の canonical JSON の SHA-256」は、`export_id` の順に並べた `[export_id, export_record_sha256]` の組（JSON の
2要素の配列）の JSON の配列を、0096 §2.3 と同じ規約（区切り `(",", ":")`・`ensure_ascii=False`・末尾改行）で直列化した
bytes の SHA-256 とする。`export_id` の重複は拒否する。golden vector を試験で固定した
（`csv_export_manifest.export_binding_sha256`）。

### 2.2 fingerprint の v2 の形

**決着（2026-10-08 所有者の決定、推奨案）。** 0100 §2.8 / §5 #7 の「fingerprint の規則に版を付ける」は、すべての CSV に
manifest がある入力で、hash の先頭に版の印 `coldaisle.replay_fingerprint\0v2\0` を置き、CSV ごとに v1 と同じ CSV の
部分（basename の長さ・basename・大きさ・内容）の後に、manifest を同じ形で足す形とする。v1 は basename の長さ
（8 bytes）から始まるので、v1 と v2 の入力の bytes 列は重ならない。manifest の無い入力の値は v1 のまま変えない。
一部の CSV にだけ manifest がある入力の fingerprint は計算せず拒否する。

### 2.3 通常の再生では `export_id` の重複を確かめない

**決着（2026-10-08 所有者の決定、推奨案）。** 0100 §2.3 が dataset 用でない再生に求めるのは「1〜6」なので、7（入力の
中の `export_id` の重複）は dataset 用の再生だけで確かめる。

### 2.4 manifest の無い CSV の警告は、dataset 用の再生でも出す

**決着（2026-10-08 所有者の決定、推奨案）。** 0100 §2.3 は dataset 用でない再生について「timezone を照合していない」
警告を1回出すとしている。dataset 用の再生（照合していない run を bind する場合）でも、manifest の無い CSV があれば
同じ警告を1回出す。v2 に使えない run であることを、再生の時点で人に知らせるためである。

### 2.5 manifest のある入力は、通常の再生でも symlink の CSV を拒否する

**決着（2026-10-08 所有者の決定、推奨案）。** 0100 §2.3 の「manifest のある入力では CSV と manifest の snapshot を取る」は、
dataset 用の再生と同じ `O_NOFOLLOW` の snapshot で行う。したがって manifest のある入力（有無が混ざる入力では manifest の
無い CSV も）に symlink の CSV があれば、通常の再生でも拒否する（従来の通常の再生は symlink を辿っていた）。manifest の
無い入力だけの通常の再生は従来どおり。manifest と照合した CSV では、オフセット付きの時刻の行も拒否する（照合で外れる）。

### 2.6 manifest の大きさの上限は 64 KiB

**決着（2026-10-08 所有者の決定、推奨案）。** manifest は数百 bytes なので、読み出しの上限 `MAX_MANIFEST_BYTES = 64 KiB`
をコードに置く（形の上限であり、運用の値ではない。AGENTS.md ルール 9 の対象外とする）。超えれば manifest の形の
食い違いとして拒否する。

### 2.7 manifest を探すのは日次 CSV の名前の CSV だけ

**決着（2026-10-08 所有者の決定、推奨案）。** manifest を対にするのは、export が書く名前 `sensors_YYYY-MM-DD.csv` の CSV
だけとする（`csv_name_for` に戻せる名前）。それ以外の名前の CSV（試作時の記録、試験の `replay.csv` など）は manifest の
無い CSV として扱う。

### 2.8 `coldaisle-daemon --timezone` の既定値は「指定しない」

**決着（2026-10-08 所有者の決定、推奨案）。** `--timezone` の既定値を `None`（指定しない）にした。manifest のある入力では
manifest の timezone を使い、明示した値が違えば拒否する（0100 §2.3 の 4）。manifest の無い CSV には、従来の既定
`Asia/Tokyo` を `DEFAULT_REPLAY_TIMEZONE` として当てる（0100 §2.3 の「省略時はその既定値」。0010 §2.7 のまま）。

## 3. Consequences

### 良くなること

- 記録だけを読んだ人が、`export_binding_sha256` や fingerprint の bytes を実装と違う形で前提にしない
- 段 3（`ReplayBindingV2` と学習の入口の照合）が、段 2 で固定した形の上に作れる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| manifest のある入力では、通常の再生でも symlink の CSV を使えなくなる（§2.5） | 実体を置く。照合した bytes と取り込む bytes を同じにするための制約 |
| 日次 CSV の名前を変えると manifest と対にならない（§2.7） | 照合していない入力として従来どおり読める（v2 には使えない）。拒否の理由に名前が出る |
| 通常の再生では `export_id` の重複を見ない（§2.3） | dataset 用の再生と Dataset v2 の生成で見る |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `export_binding_sha256` を `{"export_id":…, "export_record_sha256":…}` の object の列で直列化する | 意味は同じで、0100 §2.8 の「組の列」に配列のほうが素直に対応する |
| fingerprint の v2 を版の印なしで作る | v1 と v2 の入力の bytes 列が重なりうる |
| 通常の再生でも `export_id` の重複を拒否する | 0100 §2.3 が通常の再生に求める検査の外 |
| manifest のある入力の通常の再生で symlink を辿る | 照合した bytes と取り込む bytes が同じであることを、dataset 用の再生と同じ形で保てない |

## 5. 未決事項

なし（`ReplayBindingV2`・builder の照合・`csv_exports` との照合・学習の入口の検査・`docs/thermal-dataset.md` の更新は
0100 段 3。#237 に残る学習の入口の Codex P1 は段 3 の前に別に決める）。
