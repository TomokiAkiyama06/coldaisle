# 決定記録 0131: GPU の Fan の曲線（`coldaisle-gpu-fan`）が効いていないことを、集めている NVML の値から判定して warning と日次レポートに出す

- **種別**: Decision Record
- **Status**: FINAL（2026-10-14、リポジトリ所有者の承認）
- **Date**: 2026-10-14
- **Supersedes**: なし
- **関連**: [`0128`](0128-gpu-fan-curve-service.md) §2.2 / §2.4 /
  [`0012`](0012-rule-engine.md) §2.1 / §2.2 /
  [`0013`](0013-notifications.md) /
  [`0017`](0017-daily-report.md) §2.3 / §2.7 /
  [`0043`](0043-cpu-die-and-gpu-throttle-metrics.md)（`gpu.<index>.fan_speed` と flag の形） /
  [`0117`](0117-telemetry-unit.md) §2.1 /
  AGENTS.md ルール 1 / 6 / 9
- **対象 Issue**: #292

---

## 1. Context

`coldaisle-gpu-fan`（0128）は、次のときに何も書かずに終了コード 2 で止まり、unit は再起動しない（`RestartPreventExitStatus=2`）。

- 型番が `--gpu-name` と違う（journal の `gpu_fan_model_mismatch`。driver の更新で NVML の名前の表記が変わったときも含む）
- device が 2 個以上・引数の不正・NVML の library が無い

このとき GPU は自身の既定の制御に戻る（安全側）が、600 W の定常では約 80 s で 80 °C に届く状態に戻る（0128 §1）。
今はこれが journal にしか残らず、気づく手段が無い。

さらに 0128 §2.4 は、戻しの処理（`ExecStopPost=`）自体が NVML の無応答で打ち切られたとき、
**Fan が最後に書いた手動の回転のまま残りうる**ことを認めている。この場合サービスの状態は「再起動を繰り返している」で、
GPU の Fan は既定の制御にも曲線にも従っていない。

決める必要があるのは次の3点で、既存の記録に定めが無い。

1. **何を見て「効いていない」と判定するか**（systemd の状態・journal・集めている値のどれを読むか）
2. **どのプロセスが判定するか**（ルールエンジンは取り込み `coldaisle-daemon` の中にあり、0012 §2.1、
   基板のサンプルで評価する。GPU の値は別プロセスの `coldaisle-telemetry` が DB へ書いていて、今のルールエンジンには届かない）
3. **日次レポートにどう出すか**（0017 §2.7 は「その日に発火した件数」だけを数え、前日から発火したまま続いている状態は出ない）

## 2. Decision

### 2.1 判定は「集めている NVML の値」だけで行う。systemd・journal は読まない

監視系（`coldaisle` ユーザー）は、systemd の unit の状態も journal も読まない。代わりに次の 2 つの条件を、
`coldaisle-telemetry` が NVML から集めた値で判定する。

| ルール（`config/rules.yaml` の名前 / rule_id） | 条件 | 捉えるもの |
|---|---|---|
| `gpu_fan_not_manual` / `GPU_FAN_NOT_MANUAL` | GPU の Fan の制御方式が「手動」でない（＝GPU 自身の既定の制御）状態が `fire_after_s` 続く | サービスが止まっている（終了コード 2 の型番の不一致・device が 2 個以上・引数の不正・`systemctl stop`・未導入・`disable`）。再起動の途中（2 s）は `fire_after_s` で吸収する |
| `gpu_fan_below_curve` / `GPU_FAN_BELOW_CURVE` | `gpu.0.core` が `core_threshold`（70 °C）以上で、`gpu.0.fan_speed` が `fan_below`（90 %）未満の状態が `fire_after_s` 続く | 手動のまま曲線より低い回転で残った（0128 §2.4 の戻しの打ち切り）・既定の制御のまま熱い（上と重なる）・曲線の引数の誤り。サービスの状態を読まずに、曲線が守るべき結果だけを見る（Issue #292 の 2 つ目の案） |

- 稼働中のサービスは、周期ごとに Fan の制御方式を見て、手動でなければ書き直す（`gpu_fan_curve.py` の `step()`。
  アイドルの 30 % でも手動を保つ）。したがって「手動でない状態が数十秒続く」は、サービスが GPU の Fan を握っていないことと同じになる
- 初めの値（暫定）: `gpu_fan_not_manual` は `fire_after_s: 30`。`gpu_fan_below_curve` は `core_threshold: 70`・
  `fan_below: 90`・`fire_after_s: 30`。解除の閾値は発火と別に持つ（0012・要件 §6.4 のヒステリシス）: `gpu_fan_not_manual` は手動が `clear_s: 10` 続いたら、
  `gpu_fan_below_curve` は `gpu.0.core` が `core_clear: 67` 未満か `gpu.0.fan_speed` が `fan_clear: 95` 以上になったら解除する。
  30 s は、サービスの再起動（`RestartSec=2` と起動の待ち）を拾わず、600 W で 80 °C に届く約 80 s（0128 §1）より十分短い
- 曲線の上げはすぐ（0128 §2.2）なので、70 °C 以上では 100 % のはずである。`fan_below` を 100 ではなく 90 にするのは、
  NVML の目標値の読み取りと周期のずれで 1 周期だけ低く見えるのを拾わないため。`fire_after_s` と合わせて、値は
  `config/rules.yaml` に置く（AGENTS.md ルール 9）。値は他のルールと同じく暫定で、変えるときは決定記録に残す
- 終了コード 2 の理由（型番の不一致か、device の数か）は、この判定では分からない。通知の文に
  `systemctl status coldaisle-gpu-fan` と `journalctl -u coldaisle-gpu-fan`（`gpu_fan_model_mismatch` などの event）を
  見るよう書き、理由の切り分けは人が行う

### 2.2 `coldaisle-telemetry` に `gpu.<index>.fan_policy`（flag）を足す

- NVML の `nvmlDeviceGetFanControlPolicy_v2` を、GPU の各 Fan について読む。**すべての Fan が手動（`NVML_FAN_POLICY_MANUAL`）なら 1、
  1 つでも手動でなければ 0** とする。0043 の fan speed と同じく、Fan ごとのメトリクスにはしない
  （サービスはすべての Fan に同じ値を書くので、1 つでも外れていれば「握っていない」）
- 名前は 0002 §2.1 と 0043 の形に合わせ `gpu.<index>.fan_policy`、単位は `flag`。`config/metrics.yaml` に表示名を足す
- 関数が無い driver・`NVML_ERROR_NOT_SUPPORTED` は missing とする（0043 の T.Limit margin と同じ扱い）。missing の間は
  `gpu_fan_not_manual` を評価しない（発火も解除もしない）
- 制御の入力ではない（0043 §2.4 の Advisory。`gpu.0.fan_speed` と同じ）。fand・Learned・Supervisor の入力に足さない（0128 §2.1）
- 読むのは制御方式だけで、書かない。`coldaisle-telemetry` は今までどおり `coldaisle` ユーザーで、capability を足さない。
  0128 §2.5 で「capability なしでは読み取りだけ通る」ことは確かめたが、**制御方式の読み取りが通るかは導入先で確かめる**（2.6）

### 2.3 判定は取り込みのルールエンジンで、DB の最新値を読んで行う

- 0012 §2.1 のとおり、ルールエンジンは `coldaisle-daemon` の中に1つだけ置く。状態機械・ヒステリシス・アラートの行・
  通知の `Router`（0013）をそのまま使う。新しい通知の経路やプロセスを作らない
- 2 つのルールは、サンプルとティックの評価のたびに、`SqliteStore.latest(at_ms=評価時刻)` で `gpu.0.*` の最新値を読んで評価する
  （0012 §2.2 の評価時刻をそのまま使うので、圧縮再生・一括再生でも実運用と同じ判定になる。`rapid_rise` が DB の統計を読むのと同じ形）
- 最新値が無い・quality が OK でない（stale を含む）ときは、その周期で発火も解除もしない。収集が止まったことを「Fan が効いていない」と取り違えないため。
  収集の停止そのものの検知は本記録の範囲外（§5）
- **欠けた・stale の入力では、連続の時間を数え直す。** 今のエンジンは、条件が成立した時刻（`RuleState.since_ms`）を保ったまま
  値の無い周期を飛ばし（`_evaluate_threshold()` は `value is None` で何もせず返る）、次に値が来たとき `_continue()` が
  `now - since_ms` を `fire_after_s` と比べる。このままだと、条件が 1 回成立 → 収集が数分止まる → 回復の直後に 1 回成立、
  だけで「30 s 続いた」とみなして発火する。解除の待ち（`gpu_fan_not_manual` の `clear_s`）も同じく空白をまたいで満たされうる。
  そこで 2 つのルールでは次のようにする:
  - **発火待ち（pending）** の間に欠けた・stale の入力が来たら、pending を取り消す（行を消し `since_ms` を戻す。
    0012 §2.4 の「継続時間に届かなかった揺らぎは消す」と同じ扱い。遷移は配らない）。回復後に条件が成立したら、その時刻から数え直す
  - **発火中（firing）** のアラートは、欠けた・stale の入力では**解除しない**（そのまま保つ）。解除の待ちの起点
    （手動が続いた時刻）だけを戻し、回復後に手動が `clear_s` 続いてから解除する。`gpu_fan_below_curve` の解除は
    閾値を下回った周期ですぐ（待ちが無い）なので、欠けた周期では何もしない
  - 「欠けた・stale」は、評価のたびに読む最新値（`gpu.0.fan_policy`、または `gpu.0.core` と `gpu.0.fan_speed` のどちらか）が
    無い・OK でないこと。`gpu_fan_below_curve` は 2 つの値のどちらかが欠ければ欠けとみなす
- **当てる範囲はこの 2 つのルールだけ**で、エンジン全体の既存のルールの振る舞いは変えない。コードを読んだ結果:
  - しきい値・範囲のルール（`recirculation`・`intake_high`・`airflow_degraded`・`room_high`・`humidity_out_of_range`。
    `_evaluate_threshold()`・`_evaluate_range()`）は、今も欠けた値で `since_ms` を保ち、同じく空白をまたいで発火しうる。
    解除は閾値で即時なので、解除の側の問題は無い
  - `rapid_rise`（`_evaluate_slope()`）も傾きが出ない周期は `since_ms` を保って返るが、`fire_after_s: 0` で成立と同時に発火するので、
    発火待ちの空白の問題は起きない
  - `sensor_fault`（`_evaluate_silence()`）は空白そのものを見るルールで、`clear_s` は `resumed_ms` から数える
  - `sensor_missing` は連続の回数（`streak`）で数え、欠けそのものを数える
  - 既存のしきい値・範囲のルールの同じ問題を直すかは、基板のセンサーの欠け方（1 本ずつ・数秒）で通知の振る舞いが変わるので、
    別に決める（§5 の 4）。実装では、2 つのルールのリセットを共通の部品（例: `RuleState` を pending から戻す関数）として書き、
    既存のルールからは呼ばない
- stale の閾値はルールに持たない。`SqliteStore.latest()` が `config/quality.yaml` の `stale_after_ms`（Store の唯一の情報源。
  0004 §2.12）で stale に落とした値を、そのまま「評価しない」に使う。閾値を 2 か所に持つと、片方だけを変えたときに
  Store では stale なのにルールは新しいとみなす（またはその逆の）食い違いが起きるため
- 取り込みのルールエンジンが Telemetry を読むのは DB 経由だけで、`coldaisle-telemetry` を import しない
  （レイヤの向きは 0012 §2.1 のまま）。シリアルを開くのは今までどおり取り込みだけ（AGENTS.md ルール 6）

### 2.4 通知と日次レポート

- 重大度は 2 つとも `warning`（Issue #292）。通知は既存の `Router` の重大度の振り分け・連投の抑制に従う（0013）
- 日次レポートには、次の 2 つで出る
  1. その日に発火した件数（0017 §2.7。今のまま `GPU_FAN_NOT_MANUAL` / `GPU_FAN_BELOW_CURVE` の行が出る）
  2. **「日の終わりに発火したまま続いているアラート」の節を、日次レポートに足す**（rule_id・重大度・発火の時刻）。
     型番の不一致は人が直すまで続くので、発火は 1 回きりで、2 日目以降の件数には出ない。この節で毎日出るようにする。
     節は GPU の Fan に限らず全ルールに効く（ルール名で分岐しない。0017 §2.3 と同じ方針）
- `gpu.0.fan_policy` は flag なので、ロールアップの平均が「手動だった割合」になり、人が読む本文の「測定値」の表に出る

### 2.4a LLM には GPU の Fan の事実を渡さない

0128 §2.1 と AGENTS.md の固定前提（fand・Learned・LLM は GPU の Fan を読まない）を、安全側に読んでそのまま守る。

**原則: LLM へ渡すものは、除外の一覧を当てた後の集合だけから作る（件数・時刻・欠けの注記・省略の状態など、派生した値を含む）。**
除く前の集合から作った派生の値を渡すと、その差から除いたメトリクスやアラートの存在が分かるため。人が見る決定論的な出力は、
除く前の集合から作る。以下の項は、この原則を各経路に当てたものである。

- 日次レポートの LLM の所見の入力（`facts_markdown()` の経路。0017 §2.2）からは、`gpu.*.fan_policy` と
  `GPU_FAN_NOT_MANUAL` / `GPU_FAN_BELOW_CURVE` の 2 つのアラート（その日の件数の行と、続いているアラートの節の行）を除く。
  これらは**人が読む本文**（LLM を通さない、コードが作る表）にだけ載せる
- `facts_markdown()` の中の**アラートの集計**（発火の件数 `alert_total`・前日の件数 `prev_alert_total`・
  ルールごとの件数の表・`ALERT_SCAN_LIMIT` を超えた省略の注記）は、除外の一覧の rule_id を除いた後で数え直す。
  除く前の件数を LLM に渡すと、件数の差から除いたアラートの存在が分かるため。省略の注記も、除いた後の件数で判定する。
  **人が読む本文の件数・表・注記は、除く前の値のまま**（GPU の Fan のアラートも数える）
- `facts_markdown()` の**「集計の欠け」の注記**（収録が 1440 分に満たないメトリクスがあるか。`report.py` の `_facts()`）も、
  除外の一覧のメトリクスを除いた後のメトリクスから判定する。`gpu.*.fan_policy` だけが欠けている日は、LLM 向けには注記を出さない。
  人が読む本文の注記は除く前のメトリクスから作る
- 除く対象は、コードでメトリクス名・rule_id を分岐せず、`config/report.yaml` に一覧（例: `llm_excluded_metrics: ["gpu.*.fan_policy"]`・
  `llm_excluded_rules: ["GPU_FAN_NOT_MANUAL", "GPU_FAN_BELOW_CURVE"]`）として置く（AGENTS.md ルール 9。0017 §2.3 の
  「メトリクス名で分岐しない」と同じ方針）。一覧の形の検査は読み込み時に行い、不正なら LLM の所見を作らない（安全側）
- AI ツールの窓口（0015 / 0018 の読み取り専用ツール。`get_latest`・`query_series`・`get_stats`・`list_alerts`・`describe_system`）も、
  同じ一覧のメトリクスとアラートを返さない。`describe_system` のメトリクスの一覧と閾値（`thresholds`）からも、一覧のメトリクスと
  rule_id の行を除く。`get_latest` の上位の `ts_ms`（最新の時刻）・`derived` も、除外を当てた後の読み値の集合から作る
  （除外したメトリクスが最も新しいとき、除く前の集合の `ts_ms` を返すとその存在が分かるため）
- **Server Health の AI 要約にも渡さない。** `build_server_health()`（`api/server_health.py`）は、発火中のアラートの重大度の数
  （`alert_severity_counts`）から `signal` を決め、`signal` のテンプレートを `AiHealthSummarizer.summarize()`（`ai/health.py`）が
  `Provider.chat()` に渡す。GPU の Fan の warning は `signal` を変えるので、テンプレートを通して LLM に届く。
  - **人が見る決定論的な応答**（`signal`・`active_alerts`・テンプレートの文）は、除外の一覧を当てない（GPU の Fan のアラートも数え、見せる）
  - **AI 向けの入力**は、除外の一覧の rule_id を除いた重大度の数で `signal` を決め直し（AI 用の signal）、そのテンプレートだけを渡す
  - AI 用の signal と応答の `signal` が違うとき（＝除いたアラートが signal を変えているとき）は、**要約を求めず**決定論的なテンプレートを返す
    （`summary_source: "template"`）。AI 用の signal で作った要約を出すと、応答の `signal` と要約の文が食い違って人を誤らせるため。
    要約を求めないこと自体は LLM に何も渡さない
- **アラートの自動の説明（0016 / #38）も要求しない。** `explain_alerts: true`（今の既定）では、通知を送った発火の遷移が
  `Daemon._record()` → `_request_explanation()` → 説明の待ち行列 → `Explainer.explain()` → `Explainer._ask()` と進み、
  rule_id・閾値・`detail`・直近の統計が LLM に渡る。除外の一覧に入った rule_id の発火は、`_request_explanation()` を呼ばず
  **通知だけを送る**（素のアラート。#38 のデグレードと同じ見え方）。一覧は日次レポートと同じものを読む（2 か所に持たない）
- LLM へ届く経路を洗った結果と、それぞれの閉じ方。2026-10-14 に `src/coldaisle` で `Provider.chat()` の呼び出し元
  （`ai/summary.py`・`ai/explain.py`・`ai/health.py`・`ai/provider.py` の `probe()`）と、それを組み立てる箇所
  （`coldaisle.ai` を import する `report.py`・`daemon.py`・`server.py`）、`ToolRegistry` を作る箇所（`daemon.py`・`server.py`）を grep し、
  呼び出し元までたどって確かめた。これで全部である:

  | 経路 | LLM に渡るもの | 閉じ方 |
  |---|---|---|
  | `daemon.py` の自動の説明（`Explainer`） | 発火したアラート1件の rule_id・閾値・detail、`get_stats` の統計、`get_latest` の品質が ok でないメトリクスの名前 | 一覧の rule_id は説明を要求しない（上）。ほかのアラートの説明の `get_latest`（`_quality_evidence()`）・`get_stats` も `ToolRegistry` を通るので、ツールの除外で `gpu.*.fan_policy` の名前と値が出ない |
  | `report.py` の所見（`ai.summary.summarise`） | `facts_markdown()` | 一覧のメトリクスとアラートの行を除く（上） |
  | `report.py` の所見（`ai.summary.summarise`）のアラートの集計 | `facts_markdown()` の件数・前日の件数・省略の注記 | 一覧の rule_id を除いた後で数え直す（上）。本文は除く前のまま |
  | `server.py` の AI ツールの窓口（`ToolRegistry`。API の `/tools` も同じ実体。外の LLM の agent が呼ぶ） | ツールの応答（`describe_system` のメトリクスの一覧と閾値を含む） | 一覧のメトリクスとアラートを返さない（上） |
  | `server.py` → `build_server_health()` → `BackgroundHealthSummarizer` → `AiHealthSummarizer.summarize()` | 発火中のアラートの重大度の数で決まる `signal` のテンプレート | AI 用の signal は一覧の rule_id を除いて決め、応答の `signal` と違えば要約を求めない（上）。応答は除く前のまま |
  | `ai/provider.py` の `probe()`（疎通の確認） | 固定の `"ping"` だけ | 該当なし（データを渡さない） |
  | `escalate.py` の案件資料（人が Claude へ持ち込む） | critical のアラートの rule_id と関係するメトリクス | 今の 2 つは warning なので載らない。重大度を後で変えても載らないよう、一覧の rule_id とメトリクスは案件資料から除く |

  `memory.py`・`calibrate.py` などほかの合成の起点は `coldaisle.ai` を import せず、LLM を呼ばない。新しく LLM を呼ぶ経路を足すときは、同じ一覧を当てる
- LLM の所見は「GPU の Fan について何も知らない」前提で書かれる。GPU の Fan の異常は、通知（Router。LLM を通らない）と
  レポートの本文の表で人に届く

### 2.5 有効化

- `config/rules.yaml` の 2 つのルールは `enabled: true` で置く。導入先で `coldaisle-gpu-fan` を動かしていないときは
  `GPU_FAN_NOT_MANUAL` が発火し続けるが、0128 の方針（本番で GPU の Fan を曲線で回す）から外れた状態を知らせるのは目的どおりである
- GPU の無い開発環境・Mock の Telemetry では `gpu.0.fan_policy` が missing なので評価されない

### 2.6 テストと導入先での確認

- 実機なしのテスト（AGENTS.md ルール 7）: 偽の NVML で `fan_policy` の 0 / 1 / missing（関数が無い・NOT_SUPPORTED）、
  SimulatedClock と DB に入れた `gpu.0.*` で 2 つのルールの pending → firing → resolved・stale（`config/quality.yaml` の閾値）と missing で評価しないこと・
  LLM の入力（`facts_markdown()`）と AI ツールの応答に除外の一覧のメトリクスとアラートが入らず、人が読む本文には出ること・
  除外の一覧の rule_id の発火で、通知は送られ `Explainer`（説明の待ち行列）が呼ばれないこと（一覧に無い rule_id では今までどおり呼ばれること）・
  ほかのアラートの説明の Evidence に `gpu.*.fan_policy` の名前が出ないこと・案件資料に一覧の rule_id とメトリクスが載らないこと・
  欠けた・stale の入力のリセット: pending の途中で `gpu.0.fan_policy` が stale になり、回復の直後に 0 が来ても発火せず、
  回復の時刻から `fire_after_s` 続いて初めて発火すること・firing のまま stale になっても解除されず、回復後に手動が `clear_s`
  続いて初めて解除されること（空白の前の手動の時間を数えない）・`gpu_fan_below_curve` で `gpu.0.core` か `gpu.0.fan_speed`
  の片方だけが stale でも同じに扱うこと・既存のしきい値のルール（例: `intake_high`）の振る舞いが変わらないこと・
  LLM 向けの「集計の欠け」の注記が除外を当てた後のメトリクスから作られること（`gpu.0.fan_policy` だけが欠けた日に LLM 向けには出ず、
  本文には出ること）・`get_latest` で除外したメトリクスが最も新しいとき、上位の `ts_ms` が除外を当てた後の最新の時刻になること・
  `describe_system` の応答に一覧のメトリクスと rule_id の閾値が出ないこと・
  日次レポートで、一覧の rule_id の発火がある日に `facts_markdown()` の件数・前日の件数・表・省略の注記が除いた後の値になり、
  人が読む本文は除く前の値のままであること（`ALERT_SCAN_LIMIT` の境界を含む）・
  Server Health で、一覧の rule_id の warning だけが発火しているとき、応答の `signal` と `active_alerts` はそれを含み、
  summarizer は呼ばれず `summary_source` が `template` になること。一覧に無い warning も発火しているとき（AI 用の signal が同じ）は、
  summarizer に渡るテンプレートが除いた後の signal のものであること・
  一括再生で評価時刻が DB の時刻に従うこと、日次レポートの「続いているアラート」の節
- Mock の Telemetry に `fan_policy` を足し、シナリオで「サービスが止まった」（0 が続く）を作れるようにする
- 導入先（実装の後・所有者の承認の後）: `coldaisle` ユーザーで `fan_policy` が読めること。
  `systemctl stop coldaisle-gpu-fan` で数十秒後に warning が届き、`start` で解除されること

## 3. Consequences

### 良くなること

- 型番の不一致・device の数・停止・未導入のどれでも、journal を見に行かずに warning で気づける
- 戻しの処理が打ち切られて手動の低い回転で残る場合（0128 §2.4）も、熱いときに `GPU_FAN_BELOW_CURVE` で気づける
- 監視系の権限は増えない（systemd の D-Bus・journal のグループ・subprocess を使わない）
- 既存のルールエンジン・通知・日次レポートの仕組みに乗るので、Mock / Replay で確かめられる

### 悪くなること・その緩和

- 終了コード 2 の理由は通知に出ない。通知の文で `systemctl status` と `journalctl` を案内する
- 取り込みのデーモンが止まっていると判定されない。他のルールと同じ制約で、`sensor_fault` や日次レポートの欠けで気づく
- 収集（`coldaisle-telemetry`）が止まっていると判定されない（stale で評価を止めるため）。§5 の 2
- `GPU_FAN_BELOW_CURVE` は 70 °C 以上にならないと出ない。アイドルでサービスが握っていないことは `GPU_FAN_NOT_MANUAL` が捉える
- ルールエンジンが DB を読む回数が増える（評価ごとに最新値を 1 回）。最新値の読み出しは索引で引ける

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 監視系が `systemctl is-active` / `systemctl show -p ExecMainStatus` を subprocess で呼ぶ | 監視系に subprocess を持ち込む。開発環境（systemd が無い）とテストで偽物が要る。「動いているが曲線どおりでない」を捉えない |
| systemd の D-Bus を読む | 依存（D-Bus のライブラリ）が増え、上と同じく曲線どおりかは分からない |
| journal の `gpu_fan_model_mismatch` を読む | `coldaisle` ユーザーを `systemd-journal` グループに入れることになり、全サービスのログが読める。理由まで分かるが権限が広すぎる |
| `coldaisle-gpu-fan` に `/run` の状態ファイルを書かせる（0122 と同じ形） | 0128 §2.2 の「ファイルや socket を持たない」を変え、`CAP_SYS_ADMIN` を持つ root のサービスに書き込み先を足す。止まったときは書けないので、結局「古い」の判定が要る |
| `coldaisle-telemetry` の中で判定し、通知も出す | 通知の経路・アラートの状態機械が 2 か所になる（0012 §2.1 の「ルールエンジンは 1 つ」に反する） |
| `gpu_fan_below_curve` だけにする（`fan_policy` を足さない） | アイドルや低負荷でサービスが止まっていても 70 °C までは気づけない。型番の不一致は運転中ずっと続くので、熱くなる前に知らせたい |
| 曲線の全点で偏差を見る（温度から期待の回転を引いて比べる） | 曲線の点を `config/rules.yaml` と unit の 2 か所に持つことになる。下げのヒステリシスと 1 %/s で、下げの途中は曲線より高いのが正しい。100 % になるはずの領域だけを見れば十分 |
| ルールに固有の `stale_after_s` を `config/rules.yaml` に持つ | stale の閾値が `config/quality.yaml` と 2 か所になり、片方だけを変えると Store とルールの判定が食い違う（§2.3） |
| 集計済みの `gpu.*.fan_policy` と `GPU_FAN_*` のアラートを LLM の所見に渡す | 0128 §2.1 と AGENTS.md の固定前提（LLM は GPU の Fan を読まない）を緩めることになる。人には通知と本文の表で届くので、LLM に渡す利点が小さい（§2.4a） |

## 5. 未決事項

| # | 内容 | 時期 |
|---|---|---|
| 1 | ~~日次レポートの所見（LLM）に `gpu.0.fan_policy` の集計と 2 つのアラートを渡してよいか~~ → 渡さない（§2.4a。0128 §2.1 と AGENTS.md の固定前提どおり。2026-10-14 承認）。既存の `gpu.0.fan_speed`（0043）も今は LLM の入力に入っている。同じ一覧に足して除くかは別に決める | #301 |
| 2 | `coldaisle-telemetry` の停止を通知するルール（GPU・CPU の値が一定時間来ない） | 別の Issue |
| 3 | `fire_after_s`・`fan_below`・`core_threshold`・`clear_s`・`core_clear`・`fan_clear` の値の確定（§2.1 の暫定値は 2026-10-14 に承認） | 導入先で 2.6 の確認の後 |
| 4 | 既存のしきい値・範囲のルール（`intake_high` など）も、欠けた値で pending を取り消すか（今は `since_ms` を保ち、空白をまたいで発火しうる。§2.3）。通知の振る舞いが変わるので別に決める | #302 |
| 5 | LLM の経路の細部（各経路の派生の値・除外の当て方）は、実装の PR とその試験で確定する。決定記録の段階のレビューは閉じた（2026-10-14 所有者の判断） | #292 の実装の PR |
