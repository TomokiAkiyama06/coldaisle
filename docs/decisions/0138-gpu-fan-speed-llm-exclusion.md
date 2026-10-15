# 決定記録 0138: 既存の `gpu.<index>.fan_speed` も LLM へ渡すものから除く（0131 §2.4a の除外の一覧に足す）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-15
- **Supersedes**: なし（0131 §2.4a の一覧に項目を足す。0131 の原則と経路の閉じ方は変えない）
- **関連**: [`0131`](0131-gpu-fan-service-alert.md) §2.4a / §5 の 1 /
  [`0128`](0128-gpu-fan-curve-service.md) §2.1 /
  [`0043`](0043-cpu-die-and-gpu-throttle-metrics.md) §2.1 / §2.4 /
  [`0017`](0017-daily-report.md) §2.2 / §2.3 /
  [`0015`](0015-llm-tools.md) /
  [`0040`](0040-server-health-api.md) /
  AGENTS.md ルール 1 / 8 / 9、「制御アーキテクチャの固定前提」
- **対象 Issue**: #301

---

## 1. Context

0131 §2.4a は、0128 §2.1 と AGENTS.md の固定前提（「GPU の Fan も fand の制御対象外。…fand・Learned・LLM から
読まない・書かない」）を安全側に読み、新しい `gpu.*.fan_policy` と `GPU_FAN_NOT_MANUAL` / `GPU_FAN_BELOW_CURVE` を、
LLM へ渡すものすべてから除くと決めた。除く対象の一覧は `config/report.yaml`（`llm_excluded_metrics` /
`llm_excluded_rules`）に置く。

一方、0043 で足した既存の `gpu.<index>.fan_speed`（NVML の `nvmlDeviceGetFanSpeed`。GPU の Fan の**目標値**の %）は、
0131 の範囲の外として残り、今も LLM の入力に入っている（0131 §5 の 1）。`main`（1a0b36e）でコードを読んだ結果:

| 経路 | `gpu.0.fan_speed` が LLM に届く形 |
|---|---|
| 日次レポートの所見（`report.py` の `facts_markdown()` → `_facts()` → `ai.summary.summarise`） | 「測定値」の表の 1 行（最低・平均・最高・前日比・欠測・収録）。収録が 1440 分に満たなければ「集計の欠け」の注記の判定にも入る |
| AI ツール（`ai/tools.py` の `ToolRegistry`。`server.py` と API の `/tools`、外の LLM の agent が呼ぶ） | `get_latest` の `metrics`（値・単位・品質・age）と上位の `ts_ms`、`query_series` / `get_stats` の集計、`describe_system` の `metrics`（`config/metrics.yaml` の表示名「GPU Fan速度（NVMLの目標値）」） |
| アラートの自動の説明（`ai/explain.py` の `_quality_evidence()`） | `get_latest` 経由で、品質が ok でないメトリクスの名前の一覧に入る |
| Server Health の AI 要約（`api/server_health.py` → `ai/health.py`） | `NvmlAdapter.expected_metrics` に入るので、`gpu.0.fan_speed` が stale / suspect になると `signal` が下がり、そのテンプレートが `Provider.chat()` に渡る（名前と値は渡らない） |

制御の側（fand・Learned MPC・Supervisor・Reactive Guard・Critical Safety）は、`gpu.0.fan_speed` を読んでいない
（`src/coldaisle/control`・`learned_worker`・`control_daemon.py` を grep して確かめた。0043 §2.4 の Advisory のまま）。
問題は LLM の側だけである。

0043 §2.4 は `gpu.0.fan_speed` を **Advisory（記録のみ）**、理由を「GPU 自身の fan で、本プロジェクトの制御対象外」とし、
用途を記録と事後の分析とした。0043 は LLM に渡すかどうかを定めていない。その後 0128 §2.1 が、GPU の Fan を
「LLM は読まない」と定めた。GPU の Fan は今 `coldaisle-gpu-fan` が曲線で書いているので、`gpu.0.fan_speed` は
**そのサービスが書いた値（の NVML の目標値）そのもの**であり、0128 §2.1 の「GPU の Fan を読まない」の対象に入る。

## 2. Decision

### 2.1 `gpu.*.fan_speed` を 0131 §2.4a の `llm_excluded_metrics` に足す

- `config/report.yaml` の `llm_excluded_metrics` に `"gpu.*.fan_speed"` を足す（0131 の実装で一覧ができた後）。
  一覧の形・検査・当て方は 0131 §2.4a のまま変えない。コードでメトリクス名を分岐しない（AGENTS.md ルール 9）
- 0131 §2.4a の**原則（LLM へ渡すものは除外の一覧を当てた後の集合だけから作る。派生した値を含む）**を、そのまま
  `gpu.*.fan_speed` にも当てる。経路ごとの閉じ方も 0131 §2.4a の表と同じで、新しい閉じ方は作らない
- **人が読むものには残す。** 日次レポートの本文（LLM を通さない、コードが作る表）・ダッシュボード・
  読み取り API（`/api/v1/*`）・CSV・エアフロー画面には今までどおり出す。0043 §2.4 の「記録と事後の分析」は
  人が行うもので、LLM の所見を経由しない

### 2.2 0043 §2.4 との関係

- 0043 §2.4 の区分（Advisory）・名前・単位・収集・保存・ロールアップ・保持は**変えない**。
  `coldaisle-telemetry` は今までどおり集め、Server Health の監視の対象（0043 §2.4 の「欠測を隠さない」）も
  人が見る応答では変えない
- 本記録が決めるのは「LLM へ渡すか」だけで、0043 は置き換えない。0043 の Advisory は「制御とアラートの入力にしない」
  という区分で、「LLM に渡す」ことを定めたものではない。0128 §2.1（後の記録）が LLM について定めたので、それに従う
- 事後の分析は、人が本文の表・ダッシュボード・CSV で行う。LLM の所見に「GPU の Fan の回転」を論じさせる用途は、
  今の設計に無い（0017 の所見は 2〜3 文の要約）

### 2.3 Server Health の AI 用の signal から、一覧のメトリクスの品質を除く

0131 §2.4a は、Server Health の AI 用の signal を「一覧の rule_id を除いた重大度の数」で決め直すとした。
`gpu.0.fan_speed` は `NvmlAdapter.expected_metrics` に入っていて、**品質（stale / suspect / missing）でも `signal` を下げる**
（`_signal()` の `monitored`）。0131 の `gpu.*.fan_policy` も同じく NVML の入力なので、同じ経路を持つ。

- AI 用の signal は、`monitored` から `llm_excluded_metrics` に当たるメトリクスを除いた集合で決め直す
  （アラートの側は 0131 §2.4a のとおり一覧の rule_id を除く）
- AI 用の signal と応答の `signal` が違うときは、0131 §2.4a と同じく要約を求めず、決定論的なテンプレートを返す
  （`summary_source: "template"`）
- 人が見る応答の `signal`・`sources`・テンプレートは、除外を当てない（今のまま）
- これは 0131 §2.4a の原則（派生の値も除く前の集合から作らない）を、0131 の経路の表に無かった品質の経路に当てたもので、
  `gpu.*.fan_policy` にも同じに効く。制限を増やす向きの変更である

### 2.4 変わるコードの場所（実装は本記録の承認の後。0131 の実装の PR の後か、同じ PR に含める）

| 場所 | 変えること |
|---|---|
| `config/report.yaml` | `llm_excluded_metrics` に `"gpu.*.fan_speed"` を足す |
| `src/coldaisle/report.py`（`facts_markdown()` / `_facts()`） | 0131 の実装で、LLM 向けの facts が除外を当てた後のメトリクスから作られる。本記録ではコードは足さない（一覧の項目が増えるだけ）。「集計の欠け」の注記も除外の後のメトリクスで判定される |
| `src/coldaisle/ai/tools.py`（`get_latest`・`query_series`・`get_stats`・`describe_system`） | 0131 の実装の除外がそのまま効く（コードは足さない）。`describe_system` の `metrics` からも消える |
| `src/coldaisle/ai/explain.py`（`_quality_evidence()`） | `get_latest` 経由なので、ツールの除外で `gpu.0.fan_speed` の名前が Evidence に出なくなる（コードは足さない） |
| `src/coldaisle/api/server_health.py`（`build_server_health()` / `_signal()`）と AI 要約の呼び出し | AI 用の signal を、`monitored` から一覧のメトリクスを除いた集合で決め直す（2.3）。0131 の実装に AI 用の signal の関数ができるので、そこに品質の側を足す |
| `src/coldaisle/escalate.py` | 0131 §2.4a の「一覧のメトリクスは案件資料から除く」がそのまま効く（今は critical の `metric` に `gpu.0.fan_speed` を持つルールは無い） |
| `tests/` | 2.5 |

`gpu.0.fan_speed` を入力に持つルールは `gpu_fan_below_curve`（0131）だけで、その rule_id は既に一覧にある。
アラートの `detail` / `trigger_value` からの漏れは 0131 の除外で閉じている。

### 2.5 テストで確かめること（実機なし。AGENTS.md ルール 7）

- 日次レポート: `gpu.0.fan_speed` のロールアップがある日に、`facts_markdown()` の「測定値」の表に
  `gpu.0.fan_speed` の行が無く、`as_markdown()`（人が読む本文）には行があること
- 日次レポート: `gpu.0.fan_speed` **だけ**が収録 1440 分に満たない日に、`facts_markdown()` に「集計の欠け」の注記が出ず、
  本文には出ること
- AI ツール: `get_latest` の `metrics` に `gpu.0.fan_speed` が無いこと。`gpu.0.fan_speed` が最も新しい読み値のとき、
  上位の `ts_ms` が除外の後の最新の時刻になること。`describe_system` の `metrics` に無いこと。
  `query_series` / `get_stats` に `gpu.0.fan_speed` を指定したときの応答が、0131 の実装で決めた除外の当て方
  （0131 §5 の 5）と同じであり、値・件数が返らないこと
- アラートの説明: `gpu.0.fan_speed` の品質が ok でないとき、ほかのアラートの説明の Evidence（`_quality_evidence()`）に
  その名前が出ず、ほかのメトリクスは今までどおり出ること
- Server Health: `gpu.0.fan_speed` だけが stale で、ほかの監視対象とアラートが正常なとき、応答の `signal` は今のとおり下がり、
  summarizer は呼ばれず `summary_source` が `template` になること。`gpu.0.core` など一覧に無い監視対象も stale のとき
  （AI 用の signal が応答と同じ）は、今までどおり要約を求めること。`gpu.0.fan_policy` でも同じになること
- 一覧の漏れの見張り: `config/metrics.yaml` に定義された `gpu.<index>.fan` で始まるメトリクスが、すべて
  `llm_excluded_metrics` のどれかのパターンに当たること（GPU の Fan のメトリクスを後で足したときの除き忘れを CI で止める）
- 制御の側が `gpu.*.fan_speed` を読まないこと（今の状態の固定）: Control Config の feature metric・RL の設定・
  Fallback の入力の検査に、このメトリクスを入れたら拒否する、までは本記録では求めない（§5 の 2）

## 3. Consequences

### 良くなること

- 0128 §2.1 と AGENTS.md の固定前提（LLM は GPU の Fan を読まない）が、例外なく守られる。
  `fan_policy` は除き `fan_speed` は渡す、という食い違いが無くなる
- LLM の所見が「GPU の Fan の回転」を論じなくなり、制御の対象外の装置について根拠の弱い推測（「Fan の回転が低い」
  など）を書く余地が無くなる
- 除外の一覧の漏れを CI で見張る（2.5）

### 悪くなること・その緩和

- **日次レポートの所見の質が少し下がりうる。** GPU の温度（`gpu.0.core`）が高い日に、LLM は「GPU の Fan は既に高い回転だった」
  という文脈を使えない。緩和: 所見は 2〜3 文の要約で、根拠の表は本文（除く前の値）に残る。GPU の Fan の異常は
  0131 の warning と本文の表で人に届く。GPU の温度・電力・throttle・筐体の Fan の値は今までどおり LLM に渡る
- AI ツールを使う外の LLM の agent が、GPU の Fan の回転を答えられなくなる。緩和: 人は API・ダッシュボードで読める
- Server Health で `gpu.0.fan_speed`（または `fan_policy`）の品質だけが下がったとき、AI の要約が出ずテンプレートになる。
  緩和: 応答の `signal` と `sources` は今のまま人に見える。テンプレートは決定論的な文で、誤りは無い

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 足さない（Advisory として LLM に渡し続ける） | 0128 §2.1 と AGENTS.md の固定前提（LLM は GPU の Fan を読まない）と、明文で食い違ったまま残る。0131 で `fan_policy` を除いた理由（安全側に読む）が `fan_speed` にもそのまま当たる。`fan_speed` は `coldaisle-gpu-fan` が書いた値そのもので、`fan_policy` より直接に「GPU の Fan を読む」ことになる |
| 日次レポートの所見からだけ除き、AI ツールでは返す（またはその逆） | 0131 §2.4a の原則（LLM へ渡すものすべて）と一覧を経路ごとに分けることになり、一覧が 2 つになる。ツールの応答も LLM の入力である |
| 足したうえで、LLM 向けの facts に「GPU の Fan の値は除いてある」と注記する | 0131 §2.4a は LLM が「GPU の Fan について何も知らない」前提で書く、とした。注記は除いたものの存在を伝える |
| 一覧を `gpu.*.fan_*` の 1 つのパターンにまとめる | 漏れは防げるが、何を除いているかが一覧から読めなくなる。名前を 1 つずつ書き、漏れは 2.5 の CI の見張りで止める |
| Server Health の品質の経路（2.3）は本記録で扱わない | 0131 の経路の表に無い経路が残り、`gpu.0.fan_speed` の stale がテンプレートを通して LLM に届く。0131 §2.4a の原則（派生の値も除く）に照らすと閉じるべきで、閉じ方は 0131 と同じ（要約を求めない）なので新しい判断を要しない |

## 5. 未決事項

| # | 内容 | 時期 |
|---|---|---|
| 1 | 実装の順序: 0131 の実装の PR（#292）に本記録の 1 行と試験を含めるか、0131 の実装の後に別の PR にするか | 本記録の承認の後、#292 の実装の担当が決める |
| 2 | 制御の側（Control Config の feature metric・RL の設定・Fallback の入力）が GPU の Fan のメトリクスを入力に取らないことを、設定の読み込みで拒否するか。今は読んでいないことを grep で確かめただけで、機械的な見張りは無い | 別の Issue |
| 3 | `gpu.0.fan_speed` 以外の GPU の値（`gpu.0.core`・電力・throttle）を LLM に渡すことは変えない。GPU の Fan の制御の結果として間接に表れるが、0128 §2.1 は GPU の温度を読むことを禁じていない | 決めない（今のまま） |
