---
title: "ADR: デバイス出力JSONスキーマ v1 の確定"
labels: design, firmware, priority:must
milestone: "M0 基盤"
---

## 背景
元仕様 §10 のJSONは値のみで、スキーマバージョン・シーケンス番号・稼働時間・エラー理由が無い。
詳細は `docs/spec-review.md` W-04。

## やること
- [ ] `docs/adr/0002-device-json-schema.md`
- [ ] `schemas/device_v1.schema.json`（JSON Schema）を作成
- [ ] フィールド確定: `v` `type` `seq` `up` 各メトリクス `err`
- [ ] 起動バナー `type:"hello"`（`fw`, `dev`, `interval_ms`, センサーごとの `kind`/`gpio`/`rom`/`res`）を定義
- [ ] `tests/fixtures/` に正常系・欠測・ブートログ混入・不正JSON のサンプル行を配置

## 受入基準
- JSON Schema でフィクスチャが検証できる
- ファームウェアとホストの両方がこのスキーマを唯一の契約として参照する

## 依存
#1
