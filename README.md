# coldaisle

GPUサーバーの温湿度監視 + ローカルLLM（Qwen3.8-27B）による運用アシスタント。

> リポジトリ名は変更可能です。`grep -rl coldaisle . | xargs sed -i '' 's/coldaisle/<新名称>/g'（macOS）で一括置換できます。

> **v0.2**: Personal AI Workspace 構想メモとの統合を反映しました。
> 本システムは独立アプリではなく **Workspace の Core Service** です。
> 境界の定義は [`docs/api-contract.md`](docs/api-contract.md) を参照してください。

## これは何か

XIAO ESP32-S3 に接続した DS18B20 ×5 と AM2320 で
GPUサーバー周辺の**空気の温度**を測り、以下を行います。

- 単一のデーモンがUSBシリアルを占有し、SQLiteへ時系列を蓄積
- 決定論的なルールエンジンが異常を検知し、Slack / LINE へ通知
- ローカルLLMが履歴の自然言語問い合わせ・異常説明・日次レポートを担当

マザーボード内蔵センサーでは分からない「排気の再循環」「室温上昇」といった
**設置環境起因の問題**を切り分けることが目的です。

## 設計上の3つの柱

**1. AIを安全系に入れない**

| 層 | 責務 | AI |
|---|---|---|
| Safety-0 | BIOS Q-Fan / GPUサーマルスロットリング | なし |
| Safety-1 | ルールエンジン（閾値・継続時間・ヒステリシス） | **なし** |
| Advisory-2 | LLMによる説明・診断・要約 | あり（**読み取り専用・提案のみ**） |

**2. Core Service は GPU に依存しない**

Kaggle・研究がGPUを使う「Compute Mode」では、ローカルAIは完全停止してVRAMを全解放します。
しかし **Compute Mode こそGPUが最も熱くなる時間帯** です。

| | Core Service（取り込み・アラート・API・UI） | GPU AI Service（説明・チャット） |
|---|---|---|
| AI Mode | 稼働 | 稼働 |
| **Compute Mode** | **稼働** | **完全停止** |

**3. ハードウェア非依存で開発する**

データソースを `serial` / `mock` / `replay` で抽象化しているため、
**GPUサーバーもESP32も無い状態で、ダッシュボード・アラート・AI機能のすべてを開発・テストできます。**

```bash
uv run coldaisle-daemon --source mock --scenario ramp
```

## ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/requirements.md`](docs/requirements.md) | 要件定義書。スコープ、ユースケース、アーキテクチャ、機能/非機能要件、フェーズ計画 |
| [`docs/api-contract.md`](docs/api-contract.md) | **Personal AI Workspace との境界。**この契約だけが2リポジトリの接点 |
| [`docs/spec-review.md`](docs/spec-review.md) | ハードウェア仕様のレビューと改訂提案（重大な指摘6件を含む） |
| [`docs/decisions/`](docs/decisions/) | 決定記録。追記のみ。変更は新しい記録を作り `Supersedes` で参照する |
| [`ISSUES.md`](ISSUES.md) | Issue一覧と着手順 |
| [`issues/`](issues/) | GitHub登録用の個別Issue（フロントマター付き） |
| [`prompts/claude-code.md`](prompts/claude-code.md) | **Claude Code 向けプロンプト集。**キックオフ、Issue実装テンプレート、レビュー用 |
| [`AGENTS.md`](AGENTS.md) | AIコーディングエージェント向け指示の正本（Claude Code / Codex 共通） |

## Issueの一括登録

```bash
gh auth login
DRY_RUN=1 ./scripts/create_issues.sh   # 確認
./scripts/create_issues.sh             # 実行
```

ラベルとマイルストーンも自動作成されます。
登録後、本文中の `#番号` を実際のIssue番号に合わせて修正してください。

## 最初にやること

1. [`docs/requirements.md`](docs/requirements.md) を読み、設計の前提を把握する
2. [`docs/decisions/`](docs/decisions/) で確定済みの方針を確認する
3. **#41**（秘匿情報の混入防止）— 最初のコミット前に
4. **#1〜#4、#31** で基盤・スキーマ・モデル役割を固める
5. **#6（MockSource）を最優先で完了させる** — ここが全体のクリティカルパス

## スコープ外

本リポジトリは**ソフトウェアとファームウェアのみ**を扱います。
物理的な組み立て、部品調達、設置環境に関する事項は管理対象外です。
それらがソフトウェア要件に影響する場合のみ、
`docs/spec-review.md` に技術的な背景として記述しています。

## ライセンス

Apache License 2.0

Copyright 2026 TomokiAkiyama06
