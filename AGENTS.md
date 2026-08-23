# AGENTS.md

このファイルが AI コーディングエージェント向け指示の**正本**です。
Claude Code は `CLAUDE.md` から本ファイルを参照します（内容を二重管理しないこと）。

## プロジェクト

GPUサーバーの温湿度監視 + ローカルLLM運用アシスタント。
仕様は `docs/requirements.md`、ハードウェア指摘は `docs/spec-review.md`。

## コマンド

```bash
uvx pre-commit install               # 秘匿情報チェックの導入（clone 後1回だけ）
uv sync                              # 依存解決
uv run pytest                        # テスト
uv run pytest -k "not hardware"      # 実機不要のテストのみ（CIと同じ）
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run coldaisle-daemon --source mock # 実機なしでデーモン起動
uv run uvicorn coldaisle.api:app --host 127.0.0.1 --port 8000
```

## 絶対に守るルール

1. **LLMに書き込み・実行権限を与えない。** AIレイヤのツールは読み取り専用のみ。
   `subprocess` / `eval` / 任意SQL を呼ぶツールを追加しない。
2. **ファン制御・シャットダウン等のアクチュエーションを実装しない。** v1のスコープ外。
   もし必要と判断したら、実装せず Issue を立てて人間に確認する。
3. **シリアルポートを開くのは ingest daemon だけ。** API層・UI層・AI層から
   `serial.Serial(...)` を呼ぶコードを書かない。
4. **実機がなくてもテストが通ること。** 実機必須のテストには `@pytest.mark.hardware` を付ける。
   CIは `-k "not hardware"` で走る。
5. **生の時系列をLLMのプロンプトに直接入れない。** 必ず集計してから渡す（FR-504）。
6. 閾値・ピン番号・保持期間などの定数をコードにハードコードしない。
   `config/*.yaml` または環境変数へ。

## コード規約

- Python 3.12+, 型注釈必須, `mypy --strict` を通す
- `src/coldaisle/` レイアウト。レイヤ間の依存は一方向（L0→L1→L2→L3→L4）
  - 下位レイヤが上位レイヤを import しない
- 例外は握りつぶさない。ただし**取り込みループだけは例外**で、
  1サンプルのパース失敗でデーモンを落とさない（ログして継続）
- 公開関数には docstring。コメントは「なぜ」を書く。「何を」はコードで示す
- ログは構造化（JSON Lines）。`print` を使わない

## Git 運用

- ブランチ: `feat/<issue番号>-<短い説明>` / `fix/...` / `docs/...`
- 1 Issue = 1 ブランチ = 1 PR。複数Issueをまとめない
- コミットは Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`)
- **`main` に直接コミットしない**

## エージェント併用（Claude Code / Codex CLI）

同一ワーキングツリーで2つのエージェントを同時に走らせない。必ず worktree で分離する。

```bash
git worktree add ../coldaisle-a -b feat/12-serial-source
git worktree add ../coldaisle-b -b feat/17-rule-engine

# ターミナル1
cd ../coldaisle-a && claude
# ターミナル2
cd ../coldaisle-b && codex
```

役割分担の推奨（固定ではなく、うまくいかない方を切り替える）:

| パターン | 使い方 |
|---|---|
| 実装 → 相互レビュー | 片方が実装、もう片方に「このブランチの差分をレビューして」と投げる |
| 独立2案 | 同じIssueを別worktreeで独立に解かせ、実装を比較する |
| 並列 | 依存関係のない別Issueを同時に進める |

**レビュー役のエージェントにはファイルを編集させない。** 指摘のみを出力させ、
修正は実装側のブランチで行う。差分が混ざると原因の切り分けができなくなる。

後片付け:

```bash
git worktree remove ../coldaisle-a
git worktree list
```

注意点:

- worktree ごとに `uv sync` が必要（`.venv` は共有されない）
- `.env` は git 管理外なので worktree に自動では現れない。必要なら手動コピー
- **CIが最終的な裁定者**。エージェントの「動きました」を信用せず、`ruff` / `mypy` / `pytest` を通す
- CLIのオプションはバージョンで変わるため、`--help` で確認してから使う

## ファイル構成

```text
src/coldaisle/
  ingest/     # L0: Source実装（serial / mock / replay）、正規化
  store/      # L1: SQLite、ロールアップ、CSVエクスポート
  api/        # L2: FastAPI、WebSocket
  rules/      # L2: ルールエンジン（決定論的。AI非依存）
  ai/         # L3: LLM Provider抽象、ツール、プロンプト
  web/        # L4: 静的アセット
firmware/     # ESP32-S3 Arduino スケッチ
config/       # rules.yaml, calibration.json, coldaisle.toml
docs/         # 要件定義、仕様レビュー、ADR
tests/
```

## 迷ったら

- 仕様に書かれていない挙動を勝手に決めない。Issue にコメントして人間に聞く
- 安全性に関わる判断（アラート閾値、制御、電源）は必ず人間の承認を取る
