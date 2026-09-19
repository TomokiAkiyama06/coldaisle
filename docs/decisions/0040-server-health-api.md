# 決定記録 0040: Server Health API の契約

- **種別**: Decision Record
- **Status**: FINAL（2026-09-18、リポジトリ所有者が承認）
- **Date**: 2026-09-18
- **Supersedes**: [`0009-read-api.md`](0009-read-api.md) §5 未決事項1 のみ（0009 の他の節は有効）
- **Superseded by**: [0042](0042-server-health-signal-rules.md)（§2.4 のうち判定対象の metric・quality の扱い・source 状態の導出と、§2.6 のうち signal の判定対象を `config/server-health.yaml` だけで決めるとも読める部分のみ。他の節は有効）
- **関連**: [`0009-read-api.md`](0009-read-api.md) §2.9 / §2.12 /
  [`0032-internal-telemetry-metric-names.md`](0032-internal-telemetry-metric-names.md) /
  [`0006-gpu-mode-and-mixed-state.md`](0006-gpu-mode-and-mixed-state.md) /
  `docs/api-contract.md` §2〜§3 / `docs/requirements.md` FR-308 / 決定 D-08
- **対象 Issue**: #66（関連: #68 Compute Mode 切替時の環境条件アドバイザリ）

## 1. Context

決定記録 0009 §5 未決事項1 は、パネル向けの `/api/v1/health/summary` と
`/api/v1/thermal-gate` を後の Issue で決めるとしていた。その後 `docs/requirements.md`
FR-308 が `GET /api/v1/server-health` を Workspace GPU パネルの**唯一の窓口**とし、
signal / summary / gpu / environment / alerts / sources / compute_mode_advisory を
返すと定めた。#65 で NVML / hwmon の Internal Telemetry も保存されるようになった。

api-contract.md の旧記述（`/health/summary` と `/thermal-gate` の2本立て）は FR-308 と
食い違う。Workspace に複数エンドポイントを組み合わせさせると、同じ瞬間の値で
判断している保証が失われるため、ここで契約を確定する。

また実機（RTX PRO 6000）では NVML が hotspot / memory 温度を公開せず、collector は
毎回 `quality=missing` で保存する。「保存済みの周期メトリクスがすべて ok なら green」
という規則のままでは signal が恒常的に yellow になり、`compute_mode_advisory.safe` も
常に false になる。恒常的な yellow は本当の劣化を見分けられなくするため、
機種が公開しない値と、動いていたが止まった値を区別する必要がある。

## 2. Decision

2026-09-18、リポジトリ所有者が本記録の内容どおりに承認した（PR #135 のレビュー）。

### 2.1 `GET /api/v1/server-health` が `/api/v1/health/summary` を置き換える

- パネル1枚分を**1リクエスト**で返す。スキーマは `docs/api-contract.md` §3
- `gpu.metrics` / `environment.metrics` はキーを省略しない。値が無ければ
  `value: null` / `quality: "missing"` / `age_seconds: null`
- Workspace は `nvidia-smi` や hwmon を直接呼ばない。GPU の値は NVML collector が
  保存したものを返す
- `/api/v1/health`（デーモンの稼働状態、0009 §2.7）はそのまま残す

### 2.2 `/api/v1/thermal-gate` は作らず、`compute_mode_advisory` に含める

- `{"safe": bool, "warnings": [...], "blocking": false}`
- `safe` は**現在の決定論的 signal が green か**だけを示し、将来の高負荷時の安全を
  保証しない。実測フルロード履歴との比較は #50 の測定後に #68 で扱う
- `blocking` は型で常に `false`（決定 D-08。判断は人間）

### 2.3 `WS /api/v1/server-health/stream` は REST と同一 payload

- 封筒やフィールドを足さない。REST と同じ組み立て関数を使う
- 時計と `age_seconds` だけの変化では push しない（連続的な変化で帯域を使わない）

### 2.4 signal の判定規則（決定論的。AI は関与しない）

| 値 | 条件 |
|---|---|
| `green` | sensor_unit / nvml / lm_sensors がすべて `ok`、発生中アラート無し、周期メトリクスに signal を下げるものが無い |
| `yellow` | 情報源が `degraded`、critical 以外のアラートが発生中、または周期メトリクスの一部が `suspect` / `missing` / `stale` |
| `red` | 情報源が `unavailable` / `disabled` / `stopped`、または critical アラートが発生中 |

- source 状態は collector が `system_state` に残す値（`sys.telemetry_source.*`）と、
  監視必須 metric の quality の両方から決める。必須 metric が全滅なら `unavailable`、
  一部なら `degraded`
- 事象メトリクス（`sys.dropped_samples` 等）は鮮度判定から外す（0009 §2.12）
- AI 停止は監視停止ではないため signal 判定から外す

### 2.5 機種が公開しない metric の `missing` は signal を下げない

- `config/server-health.yaml` の `missing_tolerated` に列挙した metric に限り、
  `quality=missing` を signal 判定から外す。初期値は `gpu.0.hotspot` / `gpu.0.mem`
- `suspect` / `stale` は外さない（値はあるが疑わしい・古いのは劣化である）
- 監視必須 metric は `missing_tolerated` に入れられない（設定読込時に拒否）
- 値そのものは payload に `missing` として載り、Workspace から見える

### 2.6 監視対象 metric は `config/server-health.yaml` に置く

- source ごとの監視必須 metric、パネルに載せる metric、`missing_tolerated`
- 閾値ではなく「どれを見るか」の宣言。コードに持たない（AGENTS.md ルール9）
- 設定は `COLDAISLE_SERVER_HEALTH` で差し替えられる（0009 §2.9）。metrics.yaml に
  無い名前は起動時に拒否する（誤記は常に missing に見え、黙って監視から外れるため）

### 2.7 AI の summary は閉じた候補から選ぶだけ

- AI は確定済みテンプレートと同じ signal の許可済み文面を選べるだけで、自由文・
  数値・操作の提案は捨てる。生成はバックグラウンドで、REST / WS は待たない
- AI 未設定・停止・不達・不正出力ではテンプレートを返し、
  `summary_source: "template"`、`sources.ai_layer.status: "stopped"`

## 3. Consequences

### 良くなること

- Workspace は1回の GET（または WS）で、同じ瞬間の値に基づくパネルを描ける
- 機種差による恒常的な yellow が消え、yellow が「何かが劣化した」を意味し続ける
- 監視対象の変更（T_SENSOR 設置など）がコード変更なしで済む

### 悪くなること・その緩和

- `missing_tolerated` の metric が本当に止まっても（公開されていたものが消えても）
  signal は下がらない。緩和: 値は payload に `missing` として常に見える。
  許容するのは設定で明示した metric だけで、必須 metric は許容できない
- `/api/v1/thermal-gate` を前提にした設計があれば変更が必要。現時点で実装は無い

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `/health/summary` と `/thermal-gate` を残して2本立て | FR-308 の「唯一の窓口」に反し、2回の取得で時刻がずれる |
| signal を監視必須 metric だけで判定する | ファン回転数など必須でない metric の劣化が signal に出なくなる |
| 「一度も ok になったことが無い metric」を自動で除外する | 履歴の保持期間に依存し、再起動や保持期間の経過で結果が変わる。決定論的でない |
| internal-telemetry.yaml の NVML 設定に optional フラグを足す | collector の設定（何を読むか）と表示の判定（何を問題とみなすか）が混ざる |
| AI に summary を自由に書かせる | 数値の捏造や「安全です」の断言を防げない |

## 5. 未決事項

以下は本決定の対象外とし、実測のあとで所有者が決める。決まるまでは §2 の規則で運用する。

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | lm_sensors source の判定。現状は有効な hwmon sensor の**いずれか1つ**が ok なら ok（`require_all=False`）。有効な sensor は現在ほぼファン rpm / pwm で、1系統の入力が読めなくなっても source は ok のまま（その metric が missing になれば周期メトリクスの規則で yellow にはなる。回転数 0 の実測値は quality ok のため signal には出ない。tach stall は Critical Safety 側の責務）。T_SENSOR 設置後に `required: true` の sensor だけは全件必須にするか | #50 の測定後 |
| 2 | 監視必須 metric の選び方。sensor_unit は外付けデバイスの `air.*` 7本すべて、nvml は NVML adapter が source 状態の判定に使う core 温度と電力の2本（adapter と一致させるため）。utilization / VRAM / CUDA プロセス数を必須にするか | #50 の測定後 |
| 3 | パネルに載せる metric。gpu は 0032 の GPU 系7本、environment は `air.*` と CPU / board 系。ファン回転数（`fan.*.rpm`）をパネルに載せるか | #48 / Workspace 側 |
| 4 | `compute_mode_advisory` にフルロード履歴との比較を加える時期と形 | #68（#50 の測定後） |
