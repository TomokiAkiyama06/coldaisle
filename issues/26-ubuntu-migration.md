---
title: "Ubuntu移行（systemd / udev / 固定デバイス名）"
labels: infra, priority:must, blocked-by-hardware
milestone: "M6 移行"
---

## 背景
元仕様 §16。**GPUサーバー到着後に実施。**

## やること
- [ ] udevルールで `/dev/server-sensors` の固定名を割り当て（VID/PID + シリアル）
- [ ] `coldaisle-daemon.service`（systemd, `Restart=always`）
- [ ] `coldaisle-api.service`
- [ ] ログを journald へ
- [ ] ユーザーを `dialout` グループに追加する手順
- [ ] DBとCSVの保存先を `/var/lib/coldaisle/` へ
- [ ] Macからのデータ移行手順

## 受入基準
- 再起動後に自動で監視が復帰する
- USBポートを差し替えてもデバイス名が変わらない

## 依存
#15
