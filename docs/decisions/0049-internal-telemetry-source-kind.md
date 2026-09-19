# 決定記録 0049: 内部テレメトリの出どころの種類（実機 / 模擬）を記録し、画面で「実測」と書けるようにする

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-19
- **Supersedes**: なし（決定記録 0046 §2.7 は変えない。内部テレメトリの行を足す）
- **関連**: [`0002-metric-naming.md`](0002-metric-naming.md) §2.6 /
  [`0009-read-api.md`](0009-read-api.md) §2.8 /
  [`0040-server-health-api.md`](0040-server-health-api.md) /
  [`0046-airflow-ui.md`](0046-airflow-ui.md) §2.7 /
  [`0047-cpu-utilization-metric.md`](0047-cpu-utilization-metric.md) /
  #106 / PR #140 / PR #146
- **対象 Issue**: #147

## 1. Context

エアフロー画面（0046）は、値の札と注記を**経路ごとに**書き分けている。

| 経路 | 書くメトリクス | 出どころの手がかり | 画面の表示 |
|---|---|---|---|
| 取り込み（`coldaisle-daemon`） | `air.*`（`channels.py` の `CHANNEL_TO_METRIC`） | `sys.ingest_source` → `/api/v1/health` の `source`（serial / mock / replay） | 実測 / 模擬 / 再生 / 出どころ不明 |
| 内部テレメトリ（`coldaisle-telemetry`） | `fan.*.rpm` / `fan.*.pwm` / `cpu.*` / `gpu.*` など | **無い** | 中立な「読み取り値」 |

2026-09-19 時点のコード（origin/main）で確認したこと:

1. **内部テレメトリのメトリクスを書くのは `coldaisle-telemetry` だけ。** 取り込みの
   MockSource / ReplaySource は `SAMPLE_CHANNELS`（空気の温度・湿度）しか書かず、
   `control/hardware/simulated.py` も DB へ書かない。
2. `coldaisle-telemetry` の CLI には `--source` が無く、`build()` は既定で実 adapter
   （`NvmlAdapter` / `HwmonAdapter` / `ProcStatAdapter`）を組む。模擬の adapter は
   `build(adapters=...)` で差し込む試験用の経路にしか無い。
3. ただし「実 adapter のクラスである」ことは「実機を読んでいる」ことを保証しない。
   `NvmlAdapter(config, api=...)` は NVML の呼び出しを差し替えられ、`hwmon.root` と
   `proc_stat.path` は設定で任意の path を指せる（試験は偽の sysfs を使う）。
4. `sys.telemetry_source.<adapter>` は adapter ごとの**稼働状態**（ok / degraded /
   unavailable / disabled）で、0040 の Server Health（`sources.nvml` / `sources.lm_sensors`）が
   読む。出どころの種類は表さない。
5. `/api/v1/health` の `source` は `sys.ingest_source` だけを返す。画面（`airflow.js`）は
   この値を `air.*` の札にだけ使い、内部テレメトリは `TELEMETRY_KIND = "読み取り値"` 固定。

このため、実機で動かしていても回転数・PWM・CPU・GPU を「実測」と書けない。
逆に、根拠なく「実測」と書くと、模擬の DB を見ているときに誤解させる（0046 §2.7 の
「分からないときに実機と言わない」に反する）。**先にデーモンが出どころを記録する必要がある。**

仕様に無い挙動のため、実装より先にここで決める。

## 2. Decision

### 2.1 出どころの種類は、テレメトリデーモンを組む側（合成の起点）が決める

種類を決めるのは `telemetry_daemon.build()`（合成の起点）とする。adapter 自身や
設定ファイルには申告させない（§4 の案 C / D）。

- `adapters` を渡さない（CLI からの通常の起動）→ **`hardware`**
- `adapters` を渡す → 呼び出し側が種類を**必ず明示する**（引数 `source_kind`）。
  **既定値を持たせない。** 既定を `hardware` にすると、試験の偽 adapter が黙って
  「実機」を名乗る

`hardware` の意味は「実 adapter が、設定された OS の読み取り口（NVML / hwmon / `/proc/stat`）を
読んでいる」である。**センサーと物理部位の対応が正しいこと**までは表さない
（それは hwmon の `confirmation`（0029 §2.4）が担う）。

### 2.2 取りうる値

| 値 | 意味 |
|---|---|
| `hardware` | 実 adapter による読み取り（§2.1） |
| `mock` | 差し込まれた模擬の adapter（試験・将来の模擬デーモン） |

- 値は `internal_telemetry/models.py` の `StrEnum`（例: `TelemetrySourceKind`）に置く。
  運用で変える値ではなく実装の識別なので、設定ファイルには出さない（ルール9 の対象外）
- **`replay` は今は定義しない。** 内部テレメトリを再生する経路が存在しないため。
  作るときに別の決定記録で足す
- 「不明」は値として保存しない。**キーが無い／知らない値**を不明として扱う（§2.5）

### 2.3 記録先: デーモン全体で1つの `system_state` キー

- キーは **`sys.telemetry_kind`**（値は §2.2）。`sys.ingest_source`（0009 §2.8）と同じ形
- `run()` の最初の収集の前に1回書く。`set_system_state` の規則（0002 §2.6）どおり**変化時だけ**
  行が増える
- `sys.telemetry_source.` の下には置かない。その prefix は adapter 名ごとの稼働状態の
  名前空間で、0040 の Server Health が読んでいる。混ぜると「adapter 名 = kind」と区別が付かない
- キー名は `internal_telemetry/models.py` の定数にし、API はそれを import する
  （`server_health.py` が `SOURCE_STATE_PREFIX` を import しているのと同じ向き。
  L2 → 下位の一方向で、逆向きの import は生じない）
- **スキーマの変更は無い**（既存の `system_state` を使う）

1つのデーモンの adapter は同じ合成で組まれるので、種類はデーモン全体で1つに決まる。
adapter ごとに分ける（§4 の案 B）のは、実機と模擬を混ぜる合成が必要になってからにする。

### 2.4 API: `/api/v1/health` に `telemetry_source` を足す

- `HealthResponse` に `telemetry_source: str | None` を足す。`sys.telemetry_kind` の現在値を
  そのまま返す（無ければ `null`）。既存の `source`（取り込み）は意味を変えない
- エアフロー画面はすでに `/api/v1/health` を読んでいるので、取得を増やさない
- **Server Health（`/api/v1/server-health`、0040 / 0042）の契約は変えない。** あちらは
  稼働状態の契約で、PR #138 でも議論中のため。必要になれば別の記録で足す

### 2.5 画面の対応

内部テレメトリ（回転数・PWM・CPU・GPU）の札と注記を次のようにする。

| `telemetry_source` | 札 | 注記 |
|---|---|---|
| `hardware` | 実測 | 回転数・PWM・CPU・GPU は実機の読み取り値です（内部テレメトリ） |
| `mock` | 模擬 | …は模擬の値です。実機の値ではありません |
| `null`・知らない値 | **読み取り値**（今と同じ） | 今と同じ中立な注記 |
| health が未着 | 確認中 | 確認中 |
| `?mock=` 表示中 | 模擬（今と同じ） | 今と同じ |

- **分からないときに「実測」と言わない。** 古いデーモン（キーを書かない版）の DB でも、
  今と同じ表示に留まる
- データの鮮度（古い・未来）は従来どおり health の `stale` / 各値の quality で別に言う。
  `hardware` は「最後に記録したデーモンの合成」を表すだけで、値が今も届いていることは表さない
  （`sys.ingest_source` と同じ性質。0046 で受け入れ済み）
- 札と注記の語は画面側（`airflow-status.js`）に持つ。`air.*` 用の `INGEST_KINDS` と同じ扱い

### 2.6 範囲の外

- シリアルポートには触れない（取り込みデーモンだけが開く規則は変わらない）
- 制御（Supervisor / MPC / Guard / Critical Safety）は `sys.telemetry_kind` を読まない。
  これは表示のための情報で、制御の入力にしない
- 実装は本記録の承認後に #147 の別 PR で行う

## 3. Consequences

良くなること:

- 実機で動かしているとき、回転数・PWM・CPU・GPU を「実測」と書ける
- 模擬の adapter で書いた DB を画面で見たとき、「模擬」と明示できる
- 既存のスキーマ・Server Health の契約・`source`（取り込み）の意味を変えない

悪くなること（と緩和策）:

- **`hardware` は偽の sysfs を指す設定でも名乗れる**（§1 の 3）。
  → 意味を「実 adapter が設定された読み取り口を読んでいる」に限定して明記する（§2.1）。
  偽の sysfs を使うのは試験だけで、試験は `build(adapters=...)` か adapter を直接使う
- **デーモンが止まっても `hardware` のまま残る。**
  → 鮮度は従来どおり `stale` / quality で別に表示する（§2.5）
- **合成の起点の関数に引数が1つ増え、既定値を持たない**ため、`build(adapters=...)` を呼ぶ
  既存の試験はすべて `source_kind` の追記が要る。
  → 既定を持たせないことが目的（偽 adapter が黙って実機を名乗らない）なので受け入れる
- `/api/v1/health` の応答にフィールドが1つ増える。追加だけなので既存の読み手は壊れない

## 4. 却下した代替案

- **A. 画面が Server Health の稼働状態（`nvml: ok` など）を見て「実測」と書く。**
  稼働状態は「読めたか」であって「何を読んだか」ではない。偽 adapter でも `ok` になる
- **B. adapter ごとのキー（`sys.telemetry_kind.nvml` など）で記録する。**
  今は1つのデーモンの adapter がすべて同じ合成で組まれ、種類が混ざらない。画面の札も
  内部テレメトリ全体で1つ。画面側にメトリクス → adapter の対応表を持つ必要も生じ、
  設定（`internal-telemetry.yaml`）と二重管理になる。混ぜる合成が必要になったら改めて決める
- **C. adapter のクラスが種類を申告する（Protocol に `kind` を足す）。**
  `NvmlAdapter(api=...)` のように実クラスのまま中身を差し替えられるため、クラスでは
  判別できない（§1 の 3）
- **D. `internal-telemetry.yaml` に `source_kind: hardware` と書く。**
  設定は運用で変える値の置き場で、「実機かどうか」を人が申告すると、試験用の設定を
  流用したときに誤った値が残る。合成の起点のほうが事実に近い
- **E. `sys.telemetry_source.<adapter>` の値に種類を混ぜる（`ok:mock` など）。**
  0040 の `HealthSourceStatus` の解釈が壊れ、知らない値は `unavailable` に落ちる
- **F. readings の各行に出どころの列を足す。**
  マイグレーションが要り、全行に同じ値が並ぶ。表示の札のためには過剰

## 5. 未決事項

- **`replay` の追加**: 内部テレメトリを再生する経路（dataset 取り込みなど）を作るときに、
  その記録で値と画面の表示を決める
- **Thermal dataset（0031）での利用**: 学習用データを `hardware` の区間に限るかどうか。
  学習パイプラインの側で決める
- **Server Health への公開**: Workspace で出どころを見せる必要が出たら、0040 / 0042 の後続で決める
- **内部テレメトリだけが止まったときの鮮度表示**: 取り込みは新しいが内部テレメトリが古い場合の
  画面の言い方は本記録の範囲外。必要なら #106 の後続で扱う
