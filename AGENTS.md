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
uv run pytest -m "not hardware"      # 実機不要のテストのみ（CIと同じ）
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
uv run coldaisle-soak-report --start 2026-08-24T09:00  # 連続運転テストの集計（**DB を読むだけ**）
uv run coldaisle-escalate           # 故障疑いの案件資料（**送信はしない**）
uv run coldaisle-memory             # 運用メモリの更新案（**既定では書かない**）
uv run coldaisle-memory --apply --commit  # 確認してから書く
uv run coldaisle-calibrate          # 較正オフセットの算出（**既定では書かない**）
uv run coldaisle-calibrate --apply  # 確認してから書く。手順は docs/calibration.md
uv run coldaisle-evaluate --runs var/evaluation-runs.yaml --out var/evaluation.json  # Controller構成の比較（読み取りのみ）
uv run coldaisle-drift --evidence var/drift-evidence.yaml --profile var/confidence-profile.json  # Model driftの検知（読み取りのみ・書き込みはしない。`--format markdown` で trend を人が読む表に）
uv run coldaisle-supervisor-shadow --evidence var/supervisor-shadow-runs.yaml --registry-root var/model-registry --out var/supervisor-shadow.json  # Supervisor の Shadow 集計（DB と Registry を読むだけ。昇格は CLI の外）
uv run coldaisle-air-balance-shadow --evidence var/air-balance-shadow-evidence.yaml --control-config var/control-config --out var/air-balance-shadow.json  # Air Balance の協調の shadow 集計（DB を読むだけ。**合否は出さない**。`--format markdown` で表に。決定記録 0093）
uv run coldaisle-eventd             # 書き込み専用の Unix ソケット入口（決定記録 0045。API とは別）
uv run coldaisle-event gpu-mode compute  # GPU Mode の切り替えを記録する（#67）
uv run coldaisle-event workload-hint training --expected-duration 4h  # Workload Hint を記録する（記録のみ。#107 / 決定記録 0064）
uv run coldaisle-telemetry --once   # NVML / hwmon を1回収集（#65）
uv run coldaisle-fand --max-ticks 5 # 3系統Fan制御デーモン（simulated backend。#74 / 決定記録 0028）
uv run coldaisle-fand --config-dir var/control-config  # 4ファイル（air-balance.yaml を含む）を置いた設定で起動。無ければ全 zone Max（決定記録 0073）
#   管理ソケットは config/control-admin.yaml（--admin-config）。不正なら開かず AUTO で運転。--no-admin で開かない（決定記録 0072）
#   既定の設定は同じ uid を認めない（socket.group は仮の名前。配置先の専用グループへ置き換える）
uv run coldaisle-fand --admin-config config/control-admin.dev.yaml  # 開発用: 同じ uid から操作できる（**本番で使わない**）
uv run coldaisle-fand --authority-root var/authority  # Authority Stage の journal（authority.json）の場所。起動時に読めなければ制御を取らない（#92 / 決定記録 0057 / 0072 §2.6）
uv run coldaisle-fand --calibration config/calibration.json  # 較正を起動時に1回だけ読む（反実仮想 artifact の L9。読めなくても起動は止めない。決定記録 0079 §2.4 / 0096）
uv run coldaisle-fand --learned-channel-config config/learned-channel.yaml  # Learned worker との経路を開く（役割ごとの SOCK_SEQPACKET。省くと開かない。不正なら Learned だけ無効で運転。#86 / 決定記録 0077）
uv run coldaisle-fand --registry-root var/model-registry  # 起動時に1回だけ Model Registry を読み Gate・trace・frame・authority を束縛（省くと読まない。読めなければ Learned だけ無効。production が動いた役割は再起動まで閉じる。#104 / 決定記録 0077 §2.6 / 0098）
uv run coldaisle-control status     # coldaisle-fand の運転モードを読む（管理ソケット。#74 / 決定記録 0072。**人が使う**）
uv run coldaisle-control max --reason "負荷試験の前に全開"  # MAX（期限なし）。manual は --front/--rear/--top と --lease が必須
uv run coldaisle-control rollback-authority --reason "挙動を見直す"  # Authority を Baseline へ（lower-authority --to-stage も。**上げる操作は無い**。#92）
uv run coldaisle-authority raise --authority-root <dir> --approval <承認.json> --report <報告.json> --config-dir var/control-config --registry-root var/model-registry  # Authority を1段上げる。**人が自分の uid で実行する唯一の昇格の入口**。承認者は実行した uid（`--approver` は無い。#92 / 決定記録 0086）
uv run coldaisle-authority rollback --authority-root <dir> --reason "fand 停止中に戻す"  # fand が止まっているときの rollback（uid で拒まない）
COLDAISLE_DB=var/coldaisle.db uv run uvicorn coldaisle.api:app --host 127.0.0.1 --port 8000
COLDAISLE_DB=var/coldaisle.db uv run uvicorn coldaisle.server:app --port 8000  # + AI ツールの窓口
# エアフロー画面: http://127.0.0.1:8000/airflow.html（制御の状態は /api/v1/control/latest の trace から。
#   ?mock=normal|override|throttle で模擬データだけを表示。#106 / 決定記録 0046 / 0071）
```

API の設定は環境変数（`COLDAISLE_DB` / `COLDAISLE_METRICS` / `COLDAISLE_MAX_POINTS` ほか。
決定記録 0009 §2.9）。`uvicorn` に引数を渡せないため。decision trace の読み出し
（`/api/v1/control/*`）の件数は `COLDAISLE_CONTROL_TRACE_LIMIT` / `COLDAISLE_CONTROL_TRACE_MAX_LIMIT`
（決定記録 0071）。**trace の応答を LLM のプロンプトへ直接入れない**（ルール 8）。

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
   CIは `-m "not hardware"` で走る。Control系も Mock / Replay / simulated backend で検証できること。
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
  - 決定の内容ではない付随情報（Status の遷移、改名に伴うリンクの追従）の更新は
    書き換えに当たらない（同じ節）
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
  calibration_offsets.py # レイヤ横断: 較正を当てる metric の判定と実効の offset の写像（Normalizer と較正の digest が共有）。決定記録 0096
  metrics.py  # レイヤ横断: 単位・表示名・派生値の定義。#9
  daemon.py   # 合成の起点: Source→Normalizer→Store→Rules を束ねる。#8 / #18
  ingest/     # L0: Source実装（serial / mock / replay）、正規化
  report.py   # 合成の起点: 日次レポート（Store→AI→通知）。#25
  soak.py     # 合成の起点: 連続運転テストの集計（Store を読むだけ）。#47
  server.py   # 合成の起点: 読み取りAPI + AIツールの窓口。#23
  escalate.py # 合成の起点: 故障疑いの案件資料（AI非依存・送信しない）。#39
  memory.py   # 合成の起点: 運用メモリの記録（確認を経由する）。#40
  calibrate.py# 合成の起点: 較正オフセットの算出（確認を経由する）。#13
  evaluate.py # 合成の起点: Controller構成の比較レポート（読み取りのみ）。#91
  supervisor_shadow.py # 合成の起点: 保存済み trace から Supervisor の Shadow 集計（読み取りのみ。制御へ届かない）。#89
  air_balance_shadow.py # 合成の起点: 保存済み trace から Air Balance の協調の shadow 集計（読み取りのみ・合否なし。制御へ届かない）。#81 / 決定記録 0093
  event_entry/ # 合成の起点: 書き込み専用の Unix ソケット入口。AI 層・API から import しない。#67
  control_admin/ # 合成の起点: coldaisle-fand の管理ソケット（運転モードと Authority の降格。昇格は受けない）。AI 層・API・eventd・control から import しない。#74 / #92 / 決定記録 0072
  learned_channel/ # 合成の起点: Learned worker と coldaisle-fand の経路（役割ごとのソケット・受付スレッド・frame の送り出し・registry の production の監視）。control・AI 層・API・control_admin から import しない。#86 / #104 / 決定記録 0077
  local_socket.py # レイヤ横断: Unix ソケット入口に共通の門（SO_PEERCRED・権限・起動時の検査）。0045 / 0072 §2.5
  authority_cli.py # 合成の起点: `coldaisle-authority raise` / `rollback`。`raise_stage()` を呼ぶ唯一の場所。**どこからも import しない**。#92 / 決定記録 0086
  rollup_job.py # 合成の起点: `coldaisle-rollup` の入口（周期メトリクスを Store へ渡す）。#65
  control_daemon.py # 合成の起点: Fan制御デーモン。**hwmonへ書くのはこのプロセスだけ**。#74
  store/      # L1: SQLite、ロールアップ、CSVエクスポート
  api/        # L2: FastAPI、WebSocket
  rules/      # L2: アラート用ルールエンジン（決定論的。LLM非依存）
  control/    # Fan制御。Supervisor / MPC / Guard / Safety / Fallback / hardware mapping
    loop.py     # 1 tickの順序・期限・例外の翻訳（閾値もdemandの計算も持たない）。#74
    operating_mode.py # 管理ソケットの受け渡し口（モードと authority の2枠）から tick の先頭でモードと降格を取り出す（lease・受付の死で MAX）。#74 / #92 / 決定記録 0072
    authority.py      # Authority Stage の journal（authority.json）と AuthorityRuntime（降格は即時・書き残しは heartbeat の後・journal の変化を毎 tick 検知。**上げる経路を持たない**）。#92 / 決定記録 0057 / 0072 §2.6
    registry_binding.py # 起動時に読んだ registry の snapshot から Gate の期待値・RL の識別・frame の固定・trace の provenance を作る（registry は読まない）。#104 / 決定記録 0077 §2.6 / 0098
    air_balance.py       # Air Balance Model と air-balance.yaml（v2）の形。Safety ではない。#81
    air_balance_trace.py # applied demand から Air Balance を trace へ記録するだけ（制御へ効かない）。#81 / 決定記録 0073
    air_balance_coordination.py # Baseline（Fallback）の requested への Air Balance の協調（上げるだけ・max_raise・保持。Gate の前・合成の前）。#81 / 決定記録 0078 / 0085
    supervisor/ # RulePolicy / RLPolicy / Workload Regime
    model/      # Learned Thermal Model、Confidence / OOD
    mpc/        # Optimizer / horizon制御
    reactive/   # 急変に対する決定論的Guard
    safety/     # Critical Safety。ML非依存・最終裁定
    fallback/   # Baseline / degraded運転
    hardware/   # Demand→PWM/RPM/flow mapping、mock backendを含む
    acoustic/   # 独立Acoustic Cost Model（初期は近似、将来実測対応）
    rl/         # RL Supervisorの学習・評価環境・episode・探索（#105 / #89 / 決定記録 0058）。**制御権を持たない**
    shadow/     # 適用しなかった提案の記録と突き合わせ（制御へ届かない）。#90
    evaluation/ # Offline Evaluation と RL episode の報告 PolicyEpisodeReport（読み取り専用。制御へ届かない）。#91 / #105
    drift/      # Model Drift 検知と再学習の推奨（読み取り専用。制御へ届かない）。#93
    shadow/     # Shadow Mode。適用しなかった提案の記録と実測照合（書き込み経路を持たない）
  notify/     # L2: 通知（Slack / LINE / stdout）。秘匿情報は .env
  ai/         # L3: LLM Provider抽象、ツール、プロンプト。制御権限を持たない
  web/        # L4: 静的アセット。airflow-trace.js が decision trace の版の解釈を1か所で持つ（0071 §2.3）
firmware/     # ESP32-S3 Arduino スケッチ。**コンパイルは人の手**（#11 / 決定記録 0022 §2.9）
deploy/       # Ubuntu 常駐化のテンプレート（systemd / udev）。**仮の値だけ**。手順は docs/ubuntu-deploy.md（#57）
config/       # rules.yaml, calibration.json, coldaisle.toml, evaluation.yaml, drift.yaml, air-balance-shadow.yaml, rl-training.yaml, soak.yaml, control-admin.yaml / control-admin.dev.yaml, learned-channel.yaml（Control Config の4ファイル fan-hardware / safety / fan-policy / air-balance は実運用のものを置かない。docs/control-config.md）
memory/       # 運用メモリ（いまの閾値・較正値）。`coldaisle-memory` が更新案を出す
docs/         # 要件定義、仕様レビュー、ADR
tests/
```

## 迷ったら

- 仕様に書かれていない挙動を勝手に決めない。Issue にコメントして人間に聞く
- 安全性に関わる判断（アラート閾値、制御、電源）は必ず人間の承認を取る
