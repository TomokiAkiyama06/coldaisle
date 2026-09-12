# AGENTS.md

このファイルが AI コーディングエージェント向け指示の**正本**です。
Claude Code は `CLAUDE.md` から本ファイルを参照します（内容を二重管理しないこと）。

## プロジェクト

GPUサーバーの温湿度・内部Telemetry監視 + 3系統Fan制御 + ローカルLLM運用アシスタント。
制御対象は Front / Rear / Top の3系統で、内部制御単位は PWM ではなく `demand = 0.0..1.0`。
制御アーキテクチャは **Supervisor + Learned MPC + Reactive Guard + Critical Safety** を採用する。
仕様は `docs/requirements.md`、ハードウェア指摘は `docs/spec-review.md`、確定済みの設計変更は `docs/decisions/` を参照する。

## コマンド

```bash
uvx pre-commit install               # 秘匿情報チェックの導入（clone 後1回だけ）
uv sync                              # 依存解決
uv run pytest                        # テスト
uv run pytest -k "not hardware"      # 実機不要のテストのみ（CIと同じ）
uv run ruff check . && uv run ruff format --check .
uv run mypy src
export UV_ENV_FILE=.env              # .env を読ませる（自動では読まれない）
uv run coldaisle-daemon --source mock # 実機なしでデーモン起動
uv run coldaisle-daemon --source mock --scenario ramp --speed 60  # 時間圧縮（下記の注意）
uv run coldaisle-daemon --source replay --csv ~/server_sensor_logs --bulk  # 既存CSVの再生
uv run coldaisle-daemon --source serial            # 実機から取り込む（ポートは自動検出）
uv run coldaisle-daemon --source serial --port /dev/cu.usbmodem1101  # ポートを明示する
uv run coldaisle-rollup             # ロールアップと保持期間の適用（1日1回）
uv run coldaisle-report             # 前日の日次レポート（ロールアップのあと）
uv run coldaisle-report --date 2026-08-24 --no-send --print  # 任意の日を作り直す
uv run coldaisle-escalate           # 故障疑いの案件資料（**送信はしない**）
uv run coldaisle-memory             # 運用メモリの更新案（**既定では書かない**）
uv run coldaisle-memory --apply --commit  # 確認してから書く
uv run coldaisle-calibrate          # 較正オフセットの算出（**既定では書かない**）
uv run coldaisle-calibrate --apply  # 確認してから書く。手順は docs/calibration.md
COLDAISLE_DB=var/coldaisle.db uv run uvicorn coldaisle.api:app --host 127.0.0.1 --port 8000
COLDAISLE_DB=var/coldaisle.db uv run uvicorn coldaisle.server:app --port 8000  # + AI ツールの窓口
```

API の設定は環境変数（`COLDAISLE_DB` / `COLDAISLE_METRICS` / `COLDAISLE_MAX_POINTS` ほか。
決定記録 0009 §2.9）。`uvicorn` に引数を渡せないため。

**`--speed` を付けている間は API / ダッシュボードを同時に使わない。**
圧縮再生ではホスト時刻がシナリオ時間で進むため、別プロセスから見ると
データが未来に見える（決定記録 0007 §2.11）。

## 絶対に守るルール

1. **LLMに書き込み・実行・アクチュエーション権限を与えない。** LLMレイヤのツールは読み取り専用のみ。
   `subprocess` / `eval` / 任意SQL を呼ぶツールを追加しない。LLMからFan DemandやPWMを直接変更しない。
2. **Fan制御は定義済みControl Pipelineを必ず通す。** Learned MPC / Supervisor が出せるのは `requested_demand` まで。
   `Reactive Guard` と `Critical Safety` を迂回してPWM・hwmonへ書き込む経路を作らない。
3. **Critical SafetyはMLから独立させる。** 絶対温度上限、最低安全Demand、CPU cooling floor、tach stall、
   telemetry loss、deadman、emergency Max、Manual safety override は決定論的に実装する。
4. **学習不足・未知状態を通常状態として扱わない。** Model Confidence / OOD 判定を持ち、低信頼時は
   Baseline / Fallback Controllerへ退避する。ML停止・optimizer timeout・モデル読込失敗でも安全運転を継続する。
5. **Supervisor + Learned MPC + Reactive Guard + Critical Safety の役割を混ぜない。**
   - Supervisor: 数分〜長期の運転戦略・目的関数重み・Workload Regime
   - Learned MPC: 数十秒〜数分の未来予測とDemand最適化
   - Reactive Guard: 数秒の急変への即応floor / ceiling
   - Critical Safety: 即時の安全制約・最終裁定
6. **シリアルポートを開くのは ingest daemon だけ。** API層・UI層・AI層・control層から
   `serial.Serial(...)` を呼ぶコードを書かない。
7. **実機がなくてもテストが通ること。** 実機必須のテストには `@pytest.mark.hardware` を付ける。
   CIは `-k "not hardware"` で走る。Control系も Mock / Replay / simulated backend で検証できること。
8. **生の時系列をLLMのプロンプトに直接入れない。** 必ず集計してから渡す（FR-504）。
   ただし制御用MLモデルは時系列Windowを直接扱ってよい。LLMと制御MLを混同しない。
9. 閾値・ピン番号・保持期間・Safety floor・Ramp・目的関数重みなどの定数をコードにハードコードしない。
   `config/*.yaml` または環境変数へ。
10. **実機の個体識別子をコミットしない。** 本リポジトリは public で、
   一度 push した情報は履歴に残る。例に使うのは明らかな仮の値だけ。
   - IPアドレス: `127.0.0.1` / `0.0.0.0` / 文書用の予約範囲（RFC 5737）
   - DS18B20 の ROM: `28FF…` で始まる値（実物の2バイト目が `FF` になることはまず無い）
   - MACアドレス・ホスト名・実行環境の絶対パスは書かない

   `tests/test_repo_hygiene.py` が CI で走査する（決定記録 0021）。

## 制御アーキテクチャの固定前提

```text
Telemetry
  ↓
State Estimator / Workload Regime
  ↓
Supervisor（初期はRulePolicyをactive、RLPolicyはshadow可）
  ↓
Learned Thermal Model + Learned MPC
  ↓
Model Confidence / OOD Gate
  ↓
Requested Demand (Front / Rear / Top)
  ↓
Reactive Guard
  ↓
Critical Safety
  ↓
Effective Demand
  ↓
Fan Hardware Mapping
  ↓
PWM
```

- Front / Rear / Top は3系統独立制御。ただし推定実効風量とAir Balanceを共有して協調する。
- AIO Pump は通常制御対象外。BIOS / safety設定で高い安全側の固定運用を基本とする。
- 初期学習中でもアーキテクチャは完成形を使うが、制御権は段階的に解放する。
  Shadow → authority制限 → full authority の順とし、低Confidence時はFallbackへ退避する。
- Supervisorは「負荷があと何時間続くか」を断定しない。観測履歴から `IDLE / TRANSIENT_* / SUSTAINED_* / COOLDOWN / UNKNOWN`
  等のWorkload Regimeを推定する。外部のexpected duration等はhint/priorとしてのみ扱い、Telemetryを優先する。
- RL Supervisorを最終形とするが、学習が不十分な期間は同じInterfaceでRulePolicyをactiveにできること。
- NoiseはThermal Modelと分離する。初期はZone別の近似Penaltyでもよいが、RPM→dBAの単純比例を真値扱いしない。
  将来は独立したAcoustic Model / 実測SPL・周波数特性を追加できる設計にする。
- GPU AI ServiceがCompute Modeで停止しても制御は継続しなければならない。制御系はGPU AI Serviceに依存させない。

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

## 実装の担当

**ローカルLLMを第一候補とし、Claude は必要な場面に限定します。**
本プロジェクトの目的の1つが Claude 利用量の削減であるため、
「重要だから Claude」という切り分けはしません。

判断基準は重要度ではなく、**ローカルが1発で通せる確度**です。
やり直しが増えると、結局そちらのほうが高くつきます。

| タスクの性質 | 担当 |
|---|---|
| 仕様が明確 + テストがある + 既存パターンの模倣 | **ローカル** |
| 仕様が明確 + 既存にお手本がない | ローカル → 詰まったら別エージェント |
| 仕様が曖昧、設計判断を含む | Claude |
| セキュリティ・安全系（ルールエンジン、閾値、制御） | Claude |

ローカルLLM / Claude Code の担当分けは実測の成功率とやり直し回数で見直します。
安全系・制御系の設計変更は、実装担当モデルに関係なく人間レビューを必須とします。

### 記録のお願い

各 Issue の完了時に、以下を PR の説明に1行で残してください。
GPU 到着後のルーティング設計を、推測ではなく実データで決めるためです。

- 一発で通ったか / 何回やり直したか
- 人間の判断が必要な曖昧さがあったか
- 既存コードの模倣で済んだか

## 決定記録

設計・運用上の決定は `docs/decisions/NNNN-<slug>.md` に連番で残す。
`docs/adr/` は作らない。運用ルールとテンプレートは `docs/decisions/README.md`。

- **既存の記録を書き換えない。** 変更は新しい記録を作り `Supersedes` で旧記録を指す
  - 例外は**旧記録側への `Superseded by` の追記**だけ。これが無いと、旧記録だけを
    読んだ人が失効した決定に従ってしまう（`docs/decisions/README.md`「追記のみ」）
- 番号は再利用しない。取り下げた決定も `Status: Rejected` で残す
- 仕様に無い判断をした場合、実装より先にここへ記録して人間の承認を得る

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
  clock.py    # レイヤ横断: 時刻ソース（WallClock / SimulatedClock）。#42
  channels.py # レイヤ横断: チャネル名とメトリクス名の対応。#10
  metrics.py  # レイヤ横断: 単位・表示名・派生値の定義。#9
  daemon.py   # 合成の起点: Source→Normalizer→Store→Rules を束ねる。#8 / #18
  ingest/     # L0: Source実装（serial / mock / replay）、正規化
  report.py   # 合成の起点: 日次レポート（Store→AI→通知）。#25
  server.py   # 合成の起点: 読み取りAPI + AIツールの窓口。#23
  escalate.py # 合成の起点: 故障疑いの案件資料（AI非依存・送信しない）。#39
  memory.py   # 合成の起点: 運用メモリの記録（確認を経由する）。#40
  calibrate.py# 合成の起点: 較正オフセットの算出（確認を経由する）。#13
  store/      # L1: SQLite、ロールアップ、CSVエクスポート
  api/        # L2: FastAPI、WebSocket
  rules/      # L2: アラート用ルールエンジン（決定論的。LLM非依存）
  control/    # Fan制御。Supervisor / MPC / Guard / Safety / Fallback / hardware mapping
    supervisor/ # RulePolicy / RLPolicy / Workload Regime
    model/      # Learned Thermal Model、Confidence / OOD
    mpc/        # Optimizer / horizon制御
    reactive/   # 急変に対する決定論的Guard
    safety/     # Critical Safety。ML非依存・最終裁定
    fallback/   # Baseline / degraded運転
    hardware/   # Demand→PWM/RPM/flow mapping、mock backendを含む
    acoustic/   # 独立Acoustic Cost Model（初期は近似、将来実測対応）
  notify/     # L2: 通知（Slack / LINE / stdout）。秘匿情報は .env
  ai/         # L3: LLM Provider抽象、ツール、プロンプト。制御権限を持たない
  web/        # L4: 静的アセット
firmware/     # ESP32-S3 Arduino スケッチ。**コンパイルは人の手**（#11 / 決定記録 0022 §2.9）
config/       # rules.yaml, calibration.json, coldaisle.toml, fan-policy.yaml, fan-hardware.yaml, safety.yaml
memory/       # 運用メモリ（いまの閾値・較正値）。`coldaisle-memory` が更新案を出す
docs/         # 要件定義、仕様レビュー、ADR
tests/
```

## 迷ったら

- 仕様に書かれていない挙動を勝手に決めない。Issue にコメントして人間に聞く
- 安全性に関わる判断（アラート閾値、制御、電源）は必ず人間の承認を取る
