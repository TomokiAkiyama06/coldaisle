---
title: "GitHub Actions CI（実機なしで完走すること）"
labels: infra, priority:must
milestone: "M0 基盤"
---

## 背景
実機が無くてもすべてのソフトウェアレイヤが検証できることが本プロジェクトの前提（要件 D-02）。
CIがそれを強制する。

## やること
- [ ] `.github/workflows/ci.yml`
- [ ] Python 3.12 / uv セットアップ
- [ ] `ruff check` → `ruff format --check` → `mypy src` → `pytest -k "not hardware"`
- [ ] カバレッジ計測（`pytest-cov`）と70%閾値
- [ ] PRに対してのみ実行、`main` へのpushでも実行

## 受入基準
- `hardware` マークのテストがCIで実行されない
- カバレッジが70%を下回るとCIが落ちる

## 依存
#1
