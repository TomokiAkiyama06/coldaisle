# 決定記録 0069: Ubuntu 常駐化のテンプレート（固定デバイス名の優先・systemd / udev の置き方）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-29、リポジトリ所有者が承認。**実機での再起動・差し替えの確認は別**。§5-1）
- **Date**: 2026-09-29
- **Supersedes**: なし（[`0023`](0023-serial-source.md) §2.6 を補う。名前の順という規則は残る）
- **関連**: `docs/requirements.md` S-09 / FR-102 / NFR-01 /
  [`0008-rollup-and-retention.md`](0008-rollup-and-retention.md) §2.7・未決1 /
  [`0017-daily-report.md`](0017-daily-report.md) /
  [`0021-public-repo-hygiene.md`](0021-public-repo-hygiene.md) /
  [`0023-serial-source.md`](0023-serial-source.md) §2.6 /
  [`0060-control-loop-runtime.md`](0060-control-loop-runtime.md) 未決7 /
  `docs/ubuntu-deploy.md`
- **対象 Issue**: #57

---

## 1. Context

GPU サーバーが届き、Mac の手動起動から Ubuntu の常駐運用へ移る（S-09）。
受入基準は「OS 再起動後に監視が自動復帰する」「USB ポートを差し替えてもデバイス名が
安定する」の2つ。

FR-102 は自動検出の候補に `/dev/server-sensors` を挙げているが、実装（0023）の候補は
`/dev/cu.usbmodem*` と `/dev/ttyACM*` だけだった。固定名を足すだけでは、0023 §2.6 の
「名前の順」と組み合わさったときの振る舞いが決まらない。

- 固定名は udev が作る `/dev/ttyACM*` へのリンクなので、**同じ機器が2つの名前で
  見え、毎回「候補が複数」と警告される**
- 名前の順で固定名が先頭に来るかは、文字の並びの偶然に依存する

## 2. Decision

### 2.1 固定名 `/dev/server-sensors` は、見つかれば名前の順より先に選ぶ

- 候補の先頭に `/dev/server-sensors` を置く（`FIXED_PORTS`）
- 見つかれば、他の候補があっても**それを選び、「候補が複数」の警告を出さない。**
  固定名は VID / PID / シリアルで「どの機器か」を指しているので、選び間違いの
  心配（0023 §2.6 が警告する理由）がない
- 見つからなければ、従来どおり名前の順で選び、複数なら警告する（0023 §2.6 のまま）

### 2.2 テンプレートは `deploy/` に置き、個体識別子は仮の値だけにする

- `deploy/systemd/`: `coldaisle-daemon.service` / `coldaisle-api.service` /
  `coldaisle-rollup.service` + `.timer` / `coldaisle-report.service` + `.timer`
- `deploy/udev/99-coldaisle-sensors.rules`: VID / PID は `0000`、シリアルは
  `REPLACE-WITH-DEVICE-SERIAL`。**実機の値は導入先の `/etc/udev/rules.d/` にだけ書く**
- ユーザー / グループは仮の値 `coldaisle`。ホスト名・MAC・個体シリアルは書かない
- 静的な試験（`tests/test_deploy_templates.py`）で、unit の構文、必須の項目、
  仮の値のままであることを確かめる

### 2.3 unit の中身

| 項目 | 決定 | 理由 |
|---|---|---|
| 常駐サービス | `Restart=always`、`RestartSec=5`、`StartLimitIntervalSec=0` | NFR-01（10秒以内に復帰）。既定の StartLimit に当たると再起動を諦め、監視が黙って止まる |
| ログ | 標準出力 / 標準エラーを journald（`StandardOutput=journal`） | 既存のログは JSON Lines を標準出力へ出している。ファイルへの二重出力はしない |
| データ | `/var/lib/coldaisle`（`StateDirectory=coldaisle`）。`--db` / `COLDAISLE_DB` で明示する | #57 の配置先。FHS の標準の場所で、環境を特定しない（0021） |
| 作業ディレクトリ | `/opt/coldaisle`（`config/` を相対パスで読むため） | 各 CLI の既定の設定パスが作業ディレクトリ基準 |
| シリアルの権限 | `SupplementaryGroups=dialout`。udev は `GROUP="dialout"`, `MODE="0660"` | root で動かさない |
| API | `127.0.0.1:8000` で待ち受け | 外部公開は別の判断 |
| タイマー | ロールアップ 03:00、レポート 08:05、`Persistent=true` | `config/report.yaml` にある運用例に揃えた（0008 未決1 をここで閉じる） |
| report と rollup の順序 | `coldaisle-report.service` に `Wants=` + `After=coldaisle-rollup.service` | 0017 §2.1（集計元は1分ロールアップ）。`After=` は順序だけで起動しないため、両タイマーが停止後に同時に追いついたとき report が先に走りうる。`Wants=` で report の起動時に rollup を必ず先に queue する |

### 2.4 `coldaisle-fand` の unit はこの決定に含めない

`Type=notify` / `WatchdogSec` / `Restart` / `ExecStopPost` は Critical Safety の
deadman と Max への引き継ぎ（0028 §2.6 / §2.7）そのもので、0060 の未決7として
**安全系の人の判断**が要る。#57 の範囲から外し、別に決める。

## 3. Consequences

### 良くなること

- USB の差し込み口が変わっても、同じ機器を同じ名前で読む
- 再起動・クラッシュ後に取り込みと API が自動で戻る
- ロールアップとレポートが人手なしで毎日走る

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 固定名があると、他のシリアル機器が繋がっていても警告しない | 固定名は機器の同一性で割り当てている。別の機器を読んでいる疑いがあれば `--port` で明示する |
| テンプレートのパス（`/opt/coldaisle`）と導入先がずれうる | 手順書に置き場所を書き、ずれたら unit を書き換える |
| 日次 CSV の自動書き出しは未配線 | 手で `--export-day` を実行できる。未決2 |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 固定名を足すだけで名前の順に任せる | 同じ機器の重複で毎回警告が出る。先頭に来るかが文字の並びに依存する |
| リンク先を `realpath` で解決して重複を除く | 固定名を使う理由（同一性で選ぶ）が、表示とログから消える |
| 実機の VID / PID をテンプレートに書く | 個体の特定に近づく。導入先で調べれば足りる（0021） |
| fand の unit も同じ PR で置く | 安全系の未決（0060 未決7）を実装側で決めてしまう |

## 5. 未決事項

| # | 内容 | どこで |
|---|---|---|
| 1 | **実機での再起動・USB 差し替えの確認**（受入基準） | 人が実機で（`docs/ubuntu-deploy.md` §4） |
| 2 | 日次 CSV（`--export-day`）の自動実行の方法 | 必要になったら |
| 3 | `coldaisle-fand` / `coldaisle-telemetry` / `coldaisle-eventd` の unit | 0060 未決7 / 0045 未決4 |
