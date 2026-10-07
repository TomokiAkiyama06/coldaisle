# 決定記録 0102: 0099（較正の変更の記録先。PR #240）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が推奨案で承認）
- **Date**: 2026-10-07
- **Supersedes**: なし（0099 が「実装 PR で決める」とした点と、0099 に書かれていない点への追加。0099 / 0096 /
  0087 / 0094 の決定は置き換えない）
- **関連**: [0099](0099-calibration-change-log.md) §2.2 / §2.6 / §5 #3 / §5 #10 / §5 #11 / §5 #12 /
  [0096](0096-calibration-binding-digest.md) §5 #4 / [0094](0094-dataset-v2-settled-points.md) §2.4 /
  [0087](0087-dataset-v2-action-grid.md) §2.6 /
  `src/coldaisle/calibration_log.py` / `src/coldaisle/daemon.py` / `src/coldaisle/store/calibration_history.py` /
  `src/coldaisle/store/migrations/0011_calibration_activations.sql` / `src/coldaisle/dataset.py` /
  `docs/calibration.md` / `docs/ubuntu-deploy.md`
- **対象 Issue**: #233（実装 PR #240）/ #83

## 1. Context

0099 の記録先（migration 0011 の `calibration_activations`、取り込みの書き込み、読む関数、Dataset v2 の引数、
学習の入口の検査）を PR #240 で実装した。その実装で、0099 が「実装 PR で決める」とした点（書き手の排他の仕組み）と、
0099 に書かれていない点が5点あり、PR #240 の「人間レビューが必要な点」で推奨案とともに示した。2026-10-07、
所有者が5点すべてを推奨案で承認した。決定記録は追記のみなので、0099 の本文には手を入れず、ここに残す。
記録だけを読んだ人が、実装と違う挙動を前提にしないためである。

## 2. Decision

### 2.1 書き手の排他は、DB の実体の path の隣の lock ファイルへの `flock`

**決着（2026-10-07 所有者の決定、推奨案）。** 0099 §2.2 / §5 #12 の「DB ごとに取り込みを1つに限る lock」は、
DB の実体の path（`Path.resolve()`）の隣の `<db>.ingest.lock` に `flock(LOCK_EX | LOCK_NB)` を取る形にする。
lock は取り込みが終わるまで持ち、ファイルは消さずに残す（`flock` はプロセスの終了で外れる）。

- DB ファイル自体には lock を掛けない。同じプロセスで DB の別の fd を閉じると SQLite の POSIX lock が外れるため
- symlink や相対 path の別名は実体の path に揃うので、同じ lock になる
- hard link の別名がある DB（リンク数が 1 でない）では、lock を共有できないので取り込みを起動しない
- **DB ファイルだけを bind mount した別名は検出できない。** 運用の前提として、DB をそのような形で配置しない
  （`docs/ubuntu-deploy.md` / `docs/calibration.md` に明記した）。inode を鍵にした lock を別の場所に置く案は採らない

### 2.2 運転中に捨てた sample の件数は、ログにだけ残す

**決着（2026-10-07 所有者の決定、推奨案）。** 0099 §2.2 / §5 #11 の「捨てた件数の記録」は、取り込みの
`Stats.before_calibration_record`（終了時の構造化ログ）と、捨てたときの警告ログ（先頭10件）とする。
DB（`sys.*` の metric など）には書かない。

### 2.3 学習の入口と v2 の CLI の配線は、`coldaisle-dataset` の v2 対応の PR で行う

**決着（2026-10-07 所有者の決定、推奨案）。** 学習の CLI と `coldaisle-dataset` の v2 対応（0094 §2.4）はまだ無い。
PR #240 は、検査の関数（`verify_training_calibration`）と `ThermalDatasetV2Builder.build(calibration_history=)`
の必須引数までを入れた。本番の DB の path（仮に `--calibration-history-db`）を必須の引数にする配線は、v2 の CLI の
PR で行う（0099 §5 #10 の「同じか直前の PR」の「直前」に当たる）。

### 2.4 運転中の時刻の下限は sample だけに掛ける

**決着（2026-10-07 所有者の決定、推奨案）。** 0099 §2.2 の「最後の記録の時刻より前の sample は保存しない」は、
sample（readings を書くメッセージ）だけに掛け、起動バナー（hello）には掛けない。hello は readings を書かず、
較正の値を含まないためである。

### 2.5 起動を拒否したときの終了コードは 1

**決着（2026-10-07 所有者の決定、推奨案）。** 0099 §5 #3 の「取り込みを起動しない」は、`coldaisle-daemon` が
構造化ログに理由を出して終了コード 1 で終わる形とする。再起動は systemd（`Restart=`）に任せる。

## 3. Consequences

### 良くなること

- 記録だけを読んだ人が、実装と違う排他の仕組みや件数の残し方を前提にしない
- 取り込みの排他が SQLite の lock と干渉しない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| DB ファイルだけの bind mount の別名からは排他が効かない（§2.1） | 運用の前提として docs に明記した。hard link は検出して拒否する |
| 捨てた件数を API や DB から見られない（§2.2） | 終了時のログと警告ログで見る。必要になれば別の記録で metric を足す |
| v2 の CLI ができるまで、学習の入口の検査は関数だけで、呼び出しを強制する入口が無い（§2.3） | v2 の CLI の PR で必須の引数として配線する |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| DB ファイル自体に lock を掛ける | 同じプロセスで別の fd を閉じると SQLite の POSIX lock が外れる |
| inode を鍵にした lock を固定の場所（`/run/lock` など）に置く | 置き場所の設定と権限が増える。bind mount の配置をしなければ不要 |
| 捨てた件数を `sys.*` の metric として DB に書く | 0099 が求めていない。取り込みの readings の形を増やす |
| hello にも時刻の下限を掛ける | hello は readings を書かず、較正の値を含まない |

## 5. 未決事項

なし（v2 の CLI の配線は §2.3 のとおり後続の PR、再生の束縛と timezone は #237 / 0100）。
