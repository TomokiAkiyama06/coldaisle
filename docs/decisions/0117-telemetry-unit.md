# 決定記録 0117: `coldaisle-telemetry` の systemd unit

- **種別**: Decision Record
- **Status**: FINAL（2026-10-10 所有者が承認）
- **Date**: 2026-10-10
- **Supersedes**: なし
- **関連**: [0069](0069-ubuntu-deploy-templates.md)（§5 未決 3） /
  [0080](0080-fand-systemd-unit.md)（§2.1 / §2.9 / §5 未決 12） /
  [0060](0060-control-loop-runtime.md)（§2.2） / [0049](0049-internal-telemetry-source-kind.md)
- **対象 Issue**: #57（#65）

## 1. Context

`coldaisle-telemetry`（NVML / hwmon / `/proc/stat` の周期収集。#65）は CLI として動くが、
常駐させる unit が無い（0069 §5 未決 3、0080 §5 未決 12）。

- 導入先の GPU サーバーでは、取り込み・API・ロールアップ・レポートの常駐を始めた。
  センサー基板は未接続だが、CPU・GPU・Fan の Telemetry は基板なしで集められる。
  実データ待ちの Issue（#50 / #83 / #84 など）のために、早く貯め始めたい
- `coldaisle-fand` の unit は、すでに `Wants=` / `After=` で `coldaisle-telemetry.service` を
  名指ししている（0080 §2.9）。名前はこれに合わせる必要がある
- fand は Critical Safety の入力（CPU・GPU の温度と電力）をストア経由で受ける（0060 §2.2）。
  Telemetry が止まると、fand の入力は stale になる

## 2. Decision

### 2.1 名前と形

- `deploy/systemd/coldaisle-telemetry.service` を置く。`Type=simple` の常駐とする
- `ExecStart=/opt/coldaisle/.venv/bin/coldaisle-telemetry --db /var/lib/coldaisle/coldaisle.db`。
  設定（`config/internal-telemetry.yaml` ほか）は `WorkingDirectory=/opt/coldaisle` からの既定の path で読む
- ユーザーとグループは、取り込みと同じ `coldaisle`（仮の値。0069 §2.2）。
  同じ DB へ書き、NVML と hwmon は一般ユーザーで読めるため、専用のユーザーを作る利点が無い

### 2.2 再起動と DB の共有

- 取り込みと同じく `Restart=always`・`RestartSec=5`・`StartLimitIntervalSec=0`（NFR-01）。
  止まったままだと fand の入力が stale のまま戻らない
- `StateDirectory=coldaisle`・`StateDirectoryMode=2770`・`UMask=0007` を、DB を共有する他の unit と揃える
  （0080 §2.1。どれかの起動で `/var/lib/coldaisle` の mode が戻されるため）

### 2.3 権限と sandbox

- `SupplementaryGroups=dialout` は付けない。シリアルを開かない（AGENTS.md ルール6）
- `PrivateDevices=` は付けない。NVML は `/dev/nvidia*` を開く
- `NoNewPrivileges=yes`・`PrivateTmp=yes` は取り込みの unit と同じにする。
  `ProtectSystem=` などの追加の締め付けは、他の監視系の unit と一緒に見直す（§5）

### 2.4 起動の順序

- `After=systemd-modules-load.service` だけを持つ。hwmon のドライバを `modules-load.d` で読み込む導入先で、
  最初の周期から hwmon を読めるようにする。hwmon は周期ごとに探し直すので、順序が崩れても欠測は最初の周期だけになる
- 取り込み（`coldaisle-daemon`）との順序は付けない。互いの入力に依存しないため

### 2.5 有効化

- 監視系の unit と同じく `systemctl enable --now` で有効にする（`docs/ubuntu-deploy.md` §4）。
  読むだけで hwmon へ書かないので、fand のような段階を踏まない

## 3. Consequences

- 基板を挿す前から、導入先で CPU・GPU・Fan の実データが貯まる
- fand の unit の `Wants=coldaisle-telemetry.service` が意味を持つ。fand の起動で Telemetry も起動する
- DB に書くプロセスが1つ増える。コードの更新で止めるものの一覧（`docs/ubuntu-deploy.md`
  「コードを更新するとき」）には、すでに `coldaisle-telemetry` が入っている
- dataset 専用の DB を `--db` に渡すと、起動時に終了コード 1 で止まり、`Restart=always` で再試行が続く。
  unit は本番の DB を固定で指すので、この形にはならない

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| 専用のユーザー（`coldaisle-telemetry`）で動かす | 同じ DB へ書くので、グループと mode の約束が増えるだけになる。読む先も一般ユーザーで読める |
| 取り込みの unit に `ExecStartPost=` などで同居させる | 片方の再起動でもう片方も止まる。基板が無い間に取り込みが再試行を続けても、Telemetry は止めたくない |
| `Restart=on-failure` | 監視系の常駐（取り込み・API）と揃わなくなる。終了コードで再起動を分ける理由も無い（再起動しても直らない終了は、本番の DB を固定で指す unit では起きない） |
| `PrivateDevices=yes` と `DeviceAllow=/dev/nvidia*` | NVML が開く device の一覧を固定することになり、ドライバの更新で黙って欠測になりうる |

## 5. 未決事項

| # | 内容 | どこで |
|---|---|---|
| 1 | 監視系の unit（取り込み・API・Telemetry）の sandbox の追加（`ProtectSystem=strict` など） | 必要になったら |
| 2 | `coldaisle-eventd` の unit | 0045 未決 4（変えない） |
| 3 | 0069 §5 未決 3 と 0080 §5 未決 12 のうち、Telemetry の部分はこの記録で閉じる | — |
