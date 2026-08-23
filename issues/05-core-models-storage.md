---
title: "コアデータモデルとSQLiteストレージ層"
labels: core, priority:must
milestone: "M1 データ基盤"
---

## 背景
実機に依存しない中核。ここが出来ればサーバー未着でも上位レイヤを積める。

## やること
- [ ] `Sample` / `Reading` / `Quality` の Pydantic モデル
- [ ] `SqliteStore`: `insert_sample()` `latest()` `series()` `stats()`
- [ ] WALモード有効化、`PRAGMA synchronous=NORMAL`
- [ ] `v_latest` ビュー
- [ ] マイグレーション機構（`schema_version` テーブル、単純な連番SQL適用で可）
- [ ] 品質フラグ判定: `-127.00` / **ちょうど85.00** / `null` / `stale`（spec-review C-02）

## 受入基準
- 1万サンプルの投入と範囲クエリのテストが通る
- `85.00` ちょうどが `suspect` として記録される単体テストがある

## 依存
#3
