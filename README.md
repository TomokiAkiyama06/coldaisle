# coldaisle

GPUサーバーの温湿度・内部Telemetry監視 + 3系統Fan制御 + ローカルLLM（Qwen3.8-27B）による運用アシスタント。

> リポジトリ名は変更できますが、`coldaisle` は Python パッケージ名でもあります（`src/coldaisle/`、`pyproject.toml` のエントリポイント、import）。**`grep | sed` による一括置換はしないでください。** `.git` や `.venv` まで書き換わり、ディレクトリ名とも食い違って起動できなくなります。改名はディレクトリの移動・`pyproject.toml`・import をまとめて行います。

> **2026-09-12 設計更新**: Personal AI Workspace との統合に加え、Fan control / ML control の方針を反映しました。
> 本システムは独立アプリではなく **Workspace の Core Service** です。
> 境界の定義は [`docs/api-contract.md`](docs/api-contract.md) を参照してください。

## これは何か

XIAO ESP32-S3 に接続した DS18B20 ×5 と AM2320、ASUS T_SENSOR、GPU / CPU / Board Telemetryを使い、
GPUサーバー周辺の空気・発熱・Fan状態を時系列で記録し、監視・異常検知・冷却最適化を行います。

- 単一のデーモンがUSBシリアルを占有し、SQLiteへ時系列を蓄積
- NVML / lm-sensors / hwmon 等から GPU / CPU / Board / Fan Telemetry を取得
- Front / Rear / Top を3系統独立の `demand = 0.0..1.0` で制御し、推定実効風量とAir Balanceで協調
- **Supervisor + Learned MPC + Reactive Guard + Critical Safety** で冷却制御
- 決定論的なルールエンジンが異常を検知し、Slack / LINE へ通知
- ローカルLLMが履歴の自然言語問い合わせ・異常説明・日次レポートを担当

外付け温度センサーにより、マザーボード内蔵センサーだけでは分からない「排気の再循環」「室温上昇」
「GPU吸排気温度差」「ケース吸排気温度差」なども評価します。

## 設計上の柱

### 1. LLMと制御MLを分離する

LLMは従来どおり**読み取り専用・提案のみ**で、Fan制御権限を持ちません。
一方、Learned MPC / RL Supervisor は制御専用MLとして利用しますが、出力できるのは `requested_demand` までです。

```text
Telemetry
  ↓
State Estimator / Workload Regime
  ↓
Supervisor
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

| 層 | 主な時間軸 | 責務 | ML |
|---|---:|---|---|
| Critical Safety | 即時 | 絶対温度、最低安全Demand、CPU cooling floor、tach stall、telemetry loss、deadman、emergency Max | なし |
| Reactive Guard | 数秒 | 急激な温度上昇・Power上昇に対する即応floor / ceiling | 原則なし |
| Learned MPC | 数十秒〜数分 | 将来温度・Airflowを予測してFront / Rear / Top Demandを最適化 | あり |
| Supervisor | 数分〜長期 | Workload Regime、運転戦略、MPC目的関数重みの調整 | 最終形はRL |
| Advisory LLM | 任意 | 説明・診断・要約・チャット | あり、読み取り専用 |

**MLが安全装置になることはありません。** Reactive Guard と Critical Safety が最終裁定し、
Learned MPC / RL Supervisor からPWMやhwmonへ直接書き込む経路は作りません。

### 2. 学習不足・未知状態でも安全に動く

アーキテクチャは最初から完成形で実装しますが、学習モデルへ最初から全制御権は与えません。

```text
Shadow Mode
  ↓
制限付きAuthority
  ↓
Full Authority
```

Model Confidence / OOD（Out-of-Distribution）判定を持ち、低信頼・未学習領域・optimizer timeout・モデル停止時は
Baseline / Fallback Controllerへ退避します。

Supervisorは「このGPU負荷があと何時間続くか」をTelemetryだけから断定しません。
観測履歴から `IDLE / TRANSIENT_* / SUSTAINED_* / COOLDOWN / UNKNOWN` 等の **Workload Regime** を推定します。
ジョブ側からexpected duration等を受け取れる場合も、それはhint/priorとして扱い、実Telemetryを優先します。

RL Supervisorを最終形としますが、学習初期は同じSupervisor Interface上で `RulePolicy` をactive、`RLPolicy` をshadowにできます。

### 3. 騒音はThermal Modelと分離する

騒音は単純な `RPM → dBA` 換算を真値として扱いません。
Fan径・Fan枚数・ラジエーター・フィルター・ケース共振・beat・周波数特性などで、同じ音圧でも感じ方が変わるためです。

- 初期: Front / Rear / Topごとの非線形Acoustic Penalty（実測または主観評価）
- 将来: 固定位置のSPL / マイク測定、周波数スペクトル、annoyance scoreを利用可能

Thermal Modelと独立した **Acoustic Cost Model** としてMPCの目的関数へ入力します。

### 4. Core Service は GPU AI Service に依存しない

Kaggle・研究がGPUを使う「Compute Mode」では、LLM用GPU AI Serviceを完全停止してVRAMを解放できます。
しかし **Compute ModeこそGPUが最も熱くなる時間帯** です。

| | Core Service（取り込み・監視・安全・制御・API・UI） | GPU AI Service（説明・チャット） |
|---|---|---|
| AI Mode | 稼働 | 稼働 |
| **Compute Mode** | **稼働** | **完全停止可能** |

Telemetry、Reactive Guard、Critical Safety、Fallback、Fan controlはGPU AI Service停止中も継続します。
制御モデルもGPU AI Serviceを必須依存にしません。

### 5. ハードウェア非依存で開発する

データソースを `serial` / `mock` / `replay` で抽象化し、Fan backendも実機 / simulated backendに分離します。
実機がなくてもControl Pipeline、Shadow Mode、Safety、UIの大部分を開発・テストできる構成にします。

```bash
uv run coldaisle-daemon --source mock --scenario ramp
```

実機でしか確定できないのは、PWM→RPM、minimum stable PWM、Effective Airflow、Thermal Effectiveness、
Safetyの最終閾値などのキャラクタライズ値です。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/requirements.md`](docs/requirements.md) | 要件定義書。スコープ、ユースケース、アーキテクチャ、機能/非機能要件、フェーズ計画 |
| [`docs/api-contract.md`](docs/api-contract.md) | **Personal AI Workspace との境界。**この契約だけが2リポジトリの接点 |
| [`docs/spec-review.md`](docs/spec-review.md) | ハードウェア仕様のレビューと改訂提案 |
| [`docs/decisions/`](docs/decisions/) | 決定記録。追記のみ。変更は新しい記録を作り `Supersedes` で参照する |
| [`ISSUES.md`](ISSUES.md) | Issue一覧と着手順 |
| [`issues/`](issues/) | M0〜M7 の個別Issue定義（フロントマター付き）。**M8 以降は GitHub の Issue が正本** |
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
3. `AGENTS.md` のControl PipelineとSafety境界を確認する
4. Mock / Replay / simulated fan backendを使い、実機なしでControl Pipelineをテスト可能にする
5. 実機ではAirflow CharacterizationとBaseline測定を先に行い、その後Dataset蓄積 → Thermal Model → Shadow Modeへ進む

## スコープ外

本リポジトリは**ソフトウェアとファームウェアのみ**を扱います。
物理的な組み立て、部品調達、設置作業そのものは管理対象外です。
ただし、それらがFan control、Safety、Airflow characterization、Acoustic Model等のソフトウェア要件に影響する場合は、
`docs/spec-review.md` や設計文書・Issueの技術的前提として記述します。

## ライセンス

Apache License 2.0

Copyright 2026 TomokiAkiyama06
