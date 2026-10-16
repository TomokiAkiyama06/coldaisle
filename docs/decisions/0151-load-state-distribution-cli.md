# 決定記録 0151: 負荷の状態ごとの差温度（`d.*`）の分布を出す読み取り専用の CLI

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし
- **関連**: [0002](0002-metric-naming.md) §2.2（派生値は保存しない） / [0017](0017-daily-report.md) §2.4（生成物を `docs/` に置かない） /
  [0045](0045-local-socket-write-entry.md) §2.6（申告を根拠にしない） / [0063](0063-compute-mode-advisory.md) §5 #3 /
  [0070](0070-soak-acceptance-interpretation.md)（DB を読み取り専用で開く） /
  [0093](0093-air-balance-shadow-summary-and-baseline-arm.md)（合否を出さない集計の CLI） /
  [0096](0096-calibration-binding-digest.md) / [0099](0099-calibration-change-log.md)（較正の変更の記録） /
  [0119](0119-safety-values-first-host.md) §2.14（`workload_regime` は試験の値のまま `provisional`） /
  [0121](0121-hardware-test-procedure.md) §2.3（T1） / [0036](0036-transient-cpu-gpu-regime.md) /
  `config/metrics.yaml`、`config/rules.yaml`、`config/compute-mode-advisory.yaml`、`src/coldaisle/soak.py`、
  `src/coldaisle/control/supervisor/regime.py`
- **対象 Issue**: #50 / #68

## 1. Context

#50 の「監視アラート閾値（`config/rules.yaml`）の確定」は、無負荷・軽負荷（推論）・GPU フルロード・CPU フルロード・
CPU + GPU 同時の各状態で `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` / `d.case_delta` の分布を出し、
p99 + マージンを候補に暫定値を見直す、としている。#68 の Compute Mode アドバイザリ（0063）も、助言の上限と
フルロードの電力しきい値を #50 の実測で決める（0063 §5 #3）。どちらも「負荷の状態ごとの分布」を要る。

いま、それを出す道具が無い。

- `d.*` は**保存しない**（0002 §2.2。`readings` に `CHECK (metric NOT LIKE 'd.%')`）。参照のたびに
  `compute_derived()` で最新値から計算するだけで、時系列としての `d.*` を集計する経路は無い
- ロールアップ（`readings_1m` / `readings_1h`）にも `d.*` は無い。被減数と減数の分平均の差は、同じ時刻の差の
  分布と一致しない（分位点は差に対して線形ではない）
- `coldaisle-soak-report`（#47 / 0070）は受入基準の判定で、負荷の区別も `d.*` も持たない
- trace の `state.workload_regime` は fand が動いている期間にしか無く、導入先の `workload_regime` の値は
  試験の値のまま `provisional`（0119 §2.14）。fand は普段 disabled で、履歴の大半は BIOS の制御の期間である

2026-10-15 16:36 に始めた T1（24 h の連続運転。0121 §2.3）は、所有者の普段の使い方のまま fand が制御する
24 時間のデータで、#50 の材料としても使える。仕様に無い道具を足すので、実装より先に形を決める。

## 2. Decision

### 2.1 位置づけ: 合成の起点の新しい CLI。DB を読むだけで書かない

- モジュールは `src/coldaisle/load_distribution.py`、console script は `coldaisle-load-distribution`
  （`soak.py` / `air_balance_shadow.py` と同じ「合成の起点」）
- DB は `soak.py` の `SoakDatabase` と同じく **`mode=ro` の URI で開き、マイグレーションを当てない**。
  スキーマの版がコードと合わなければ開かずに終了する（0070）。同じ型を共有できるなら共有し、2 か所に書かない
- 書くのは `--out` で指定した出力ファイルだけ。DB・設定・`memory/`・`config/rules.yaml` は書かない
- **合否も閾値の提案も出さない**（0093 と同じ）。p99 + マージンの計算や `rules.yaml` の値の決定は人がする。
  確定値は別の決定記録に残す（#50 のとおり）
- API・AI ツール・日次レポートには足さない（§2.9）

### 2.2 負荷の状態: CPU と GPU の電力の帯の組み合わせ（fand が止まっていても使える）

状態の主軸は**観測された電力**から、この CLI の設定（§2.10）の帯で決める。

- 軸は CPU（既定 `power.cpu.package`）と GPU（既定 `power.gpu.0`）。metric 名は設定に書き、`metrics.yaml` に
  無い名前・単位が `W` でない名前は起動時に拒否する
- 軸ごとに、名前つきの帯を下限の昇順で並べる（例: GPU `idle` / `light` / `high` / `full`）。帯は `[下限, 次の帯の下限)`
  で隙間も重なりも作らない。最初の帯の下限は 0
- 各評価時刻 `t`（§2.4）の電力は、`(t - power_window_ms, t]` の `quality = ok` の行の**平均**。窓の中の ok の行が
  `power_window_ms / 期待周期 × power_min_ok_ratio` に満たない軸は `unknown`
- 状態のラベルは `cpu:<帯>/gpu:<帯>`。どちらかが `unknown` なら状態は `unknown` で、分布に入れず件数だけ数える
- **GPU Mode の申告（`events` / `sys.gpu_mode`）や Workload Hint は使わない**（0045 §2.6・0063 の I-3 と同じ。
  申告は観測ではない）

Workload Regime（`control/supervisor/regime.py`）を主軸にしない理由は §4。ただし trace に `workload_regime` が
あれば、**副の切り口**として `--group-by trace-regime` で同じ統計を Regime ごとにも出す（§2.6）。

### 2.3 移り変わり直後と欠けの扱い

- 状態が変わった時刻から `settle_ms` の間の評価時刻は **`transition`** とし、主の分布に入れない。
  `transition` は行き先の状態ごとに件数と分布を別に出す（Reactive Guard の材料になりうるため捨てない）
- 電力の軸が `unknown` になった・評価時刻の間隔が `max_gap_ms` を超えた後は、状態が同じでも落ち着きの計時を
  やり直す（欠けの前後を同じ定常として繋がない）
- 定常の連続した区間（segment）ごとに数え、状態ごとに **segment の数と最長の長さ**を出す。1 回の長い負荷だけで
  決まった p99 を、複数回の負荷の p99 と見分けるため

### 2.4 評価の時刻と `d.*` の組み立て

- 各 metric は**自分の `quality = ok` の行の時刻**で評価する。値を補間しない・リサンプルしない
- `d.*` の定義（被減数・減数）は `config/metrics.yaml` の `derived` だけを読む（コードに複製しない）。
  被減数の ok の行の時刻 `t` に、減数の ok の行を次の規則で対にする:
  - 同じ `ts_ms` の行があればそれ（基板の `air.*` は1サンプルで同じ時刻。`gpu.0.core` / `gpu.0.hotspot` も同じ収集の周期）
  - 無ければ `[t - pair_max_age_ms, t]` の最も新しい ok の行（`d.connector_rise` のように収集元が違う組）
  - どちらも無ければ `unpaired` として数え、分布に入れない
  - 差は `compute_derived()` と同じく `round(被減数 - 減数, 3)`
- 状態のラベルは評価時刻 `t` で §2.2 を当てる
- 1 行は 1 件。周期はほぼ一定（2500 ms）なので件数がおおよそ時間に比例する。状態ごとの観測時間は
  「件数 × 期待周期」ではなく、segment の端の時刻から出す

### 2.5 集計する metric と統計量

**metric**（設定の `metrics` に並べる。既定の一覧）:

- `config/metrics.yaml` のすべての `d.*`（いまは `d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` / `d.top_rise` /
  `d.rear_rise` / `d.gpu_internal_delta` / `d.case_delta` / `d.connector_rise`）
- 関連の温度: `air.room` / `air.front_intake` / `air.gpu_intake` / `air.gpu_exhaust` / `air.rear_exhaust` /
  `air.top_exhaust` / `gpu.0.core` / `cpu.package` / `board.connector_12v2x6`、湿度 `air.room_humidity`
  （#68 の助言の上限の材料）
- 状態を決めた電力そのもの: `power.gpu.0` / `power.cpu.package`（帯の見直しの材料。0063 の `min_value` 400 W も）
- Fan の状態: `fan.front.pwm` / `fan.rear.pwm` / `fan.top.pwm`（同じ負荷でも Fan で温度差が変わるため並べて読む）

一覧に無い metric・`metrics.yaml` に無い名前は起動時に拒否する。

**統計量**（状態ごと・metric ごと）: 件数 `n`、`min` / `max` / `mean` / 標準偏差、設定の分位点
（既定 0.01 / 0.05 / 0.25 / 0.5 / 0.75 / 0.95 / 0.99）、segment の数と最長。

- 分位点は順序統計量の線形補間（Hyndman–Fan の type 7。NumPy の既定と同じ）。標準ライブラリで書き、依存を足さない
- `n` が `min_samples` 未満の組は分位点・標準偏差を `null` にし、理由 `insufficient_samples` を添える
  （少ない件数の p99 を数字として出さない）
- 値は小数 3 桁に丸める（`compute_derived()` と同じ）

### 2.6 切り口（グループの鍵）

分布は次の鍵の組ごとに出す。

1. **`cooling`**（常に付ける）: 評価時刻 `t` の `[t - trace_max_age_ms, t]` に `control_traces` の行があれば `fand`、
   無ければ `no_fand`（BIOS の制御）。trace の `state.operating_mode` が `auto` でない tick（`manual` / `max` /
   `calibration`。試験の操作）は `fand_override` とし、**既定では分布から除いて件数だけ数える**
   （`--include-override` で含める）。Fan の制御が違う期間を混ぜると、同じ負荷でも温度差の分布がずれるため
2. **状態**（§2.2 の電力の帯）
3. **`period`**（任意）: `--periods <yaml>` に名前つきの区間（例: `ac_on` / `ac_off`、試験の区間）と除外する区間を
   書くと、区間の名前を鍵に足す。区間の外の評価時刻は使わない。エアコンの ON / OFF は DB に記録が無いので、
   人が区間で渡す（推測しない）
4. **`--group-by trace-regime`**（任意）: 状態の代わりに trace の `state.workload_regime` を鍵にする。trace は
   既存の `ControlTick` の検証を通ったものだけを使い、通らない行・Regime の無い tick は件数だけ数える。
   出力には「Regime の設定は `provisional`（0119 §2.14）」を必ず書く

### 2.7 品質と較正

- 使うのは **`quality = ok` かつ値が `NULL` でない行だけ**。`missing` / `suspect` / `stale` は metric ごと・グループ
  ごとに品質別の件数として出し、`d.*` では `unpaired` も別に数える。黙って捨てない
- DB の値は取り込みで較正を当てた後の値である（0096）。期間の中に較正の変更（0099 の記録）があれば、その時刻と
  digest を出力の先頭に一覧で出し、`calibration_changed: true` を立てる。期間を自動では分けない（§5 で扱う余地は
  残さない。分けたければ人が `--periods` で分ける）
- 生データ（`readings`）だけを読む。ロールアップは使わない（§1）。そのため**生データの保持期間（`raw_days`）の
  うちに**走らせる。期間が `max_period_days` を超える指定は拒否する

### 2.8 出力の形

- `--format json`（既定）と `--format markdown`。`--out <path>` に書き、省けば標準出力
- JSON は版つき（`version: 1`）で、先頭に入力の来歴を置く: 期間（UTC の ms とタイムゾーンつきの表示）、
  設定ファイルと `metrics.yaml` の SHA-256、DB のスキーマの版、`--periods` の SHA-256、除いた件数の内訳、
  較正の変更の一覧。DB のパスは出さない（実行環境の絶対パスを残さない。AGENTS.md ルール 10）
- Markdown は metric ごとの表（行 = グループ、列 = `n`・p50・p95・p99・max・segment の数）と、除いた件数の表
- 既定の出力先の例は `var/load-distribution/`。**`docs/` には書かない**（室温の推移は生活パターン。0017 §2.4）。
  #50 の `docs/baseline-YYYY-MM-DD.md` は、人が中身を確かめてから必要な数字だけを写す

### 2.9 LLM へ渡さない

- AI 層（`ai/`）・API（`api/`）・`server.py` からこのモジュールを import しない。AI ツールにも日次レポートにも足さない
- 出力は集計値だけだが、それでも LLM のプロンプトへの経路を作らない（ルール 8 の「生の時系列を渡さない」に加え、
  閾値の判断を LLM に寄せないため）。import の向きは AST で import を走査するテスト（§2.11）で CI が見張る

### 2.10 設定は `config/load-distribution.yaml`

次の値をすべてここに置き、コードに既定値を持たない（ルール 9）。鍵の欠け・未知の鍵・型の違いは起動時に拒否する。

| 鍵 | 内容 | 初期値（**すべて `provisional`**） |
|---|---|---|
| `version` | 設定の版 | 1 |
| `timezone` | `--start` / `--end` の解釈と表示 | `Asia/Tokyo` |
| `max_period_days` | 1 回で集計できる期間の上限 | 30（`raw_days` と同じ） |
| `expected_interval_ms` | 窓の中の ok の件数の基準 | 2500 |
| `axes.cpu` / `axes.gpu` | metric 名と帯（名前と下限 W） | §5 #2 |
| `power_window_ms` | 電力の平均の窓 | 30000 |
| `power_min_ok_ratio` | 窓の中の ok の割合の下限 | 0.5 |
| `settle_ms` | 移り変わり直後とみなす長さ | 300000（§5 #3） |
| `max_gap_ms` | これを超える間隔で落ち着きの計時をやり直す | 10000（`stale_after_ms` と同じ） |
| `pair_max_age_ms` | `d.*` の減数を対にする古さの上限 | 5000（2 周期） |
| `trace_max_age_ms` | `cooling: fand` とみなす trace の古さの上限 | 2000（tick 1000 ms の 2 倍） |
| `quantiles` | 出す分位点 | 0.01 / 0.05 / 0.25 / 0.5 / 0.75 / 0.95 / 0.99 |
| `min_samples` | 分位点を出す件数の下限 | 120（約 5 分） |
| `metrics` | 集計する metric の一覧 | §2.5 |

### 2.11 テストの方針（実機不要）

すべて CI（`-m "not hardware"`）で走る。DB は一時ディレクトリに既存のマイグレーションで作り、合成の行を入れる。

- 読み取り専用: 集計の前後で DB のバイト列とスキーマの版が変わらない。版の合わない DB は開かずに終了する
- 帯: 境界の値（下限ちょうど）が上の帯に入る。窓の中の ok が足りない軸は `unknown`
- 落ち着き: 状態の変化から `settle_ms` 未満は `transition`、以上は主の分布。`max_gap_ms` を超える欠けで計時をやり直す
- `d.*` の対: 同じ時刻の対・`pair_max_age_ms` 以内の対・超えた `unpaired`・減数が `suspect` の行を使わない
- 品質: `missing` / `suspect` / `stale` / `NULL` が分布に入らず件数に出る
- 分位点: 手で計算した type 7 の値と一致する。`min_samples` 未満で `null` と理由
- 切り口: trace の有無で `fand` / `no_fand`、`manual` の tick が `fand_override` で既定では除かれる。
  `--periods` の区間の外を使わない。`--group-by trace-regime` で検証を通らない trace を件数だけ数える
- 設定: 未知の metric・`W` でない電力の metric・帯の下限が昇順でない・鍵の欠けを拒否する
- 出力: JSON の版と来歴の欄、DB のパスが出ないこと、Markdown の表の形
- import の向き: `ai/` / `api/` / `server.py` がこのモジュールを import しない

### 2.12 T1 のデータへの使い方

導入先の DB は作業者の uid から読めないので、root で SQLite の `.backup` で写しを作り、その写しに対して走らせる
（書いている最中の DB をファイルの複写で写さない）。期間は T1 の開始から 24 h を `--start` / `--end` で渡す。
T1 は全体が fand の制御なので `cooling: fand` のグループだけになる。BIOS の期間との比較は、同じ DB の
T1 より前の期間を別に走らせて並べる。

## 3. Consequences

### 良くなること

- #50 の「状態ごとの `d.*` の分布」と、#68（0063 §5 #3）の助言の上限・フルロードの電力の材料が、同じ道具・同じ
  規則で出る。後で閾値を見直すときも同じ手順で作り直せる
- fand が止まっている期間（履歴の大半）にも使える。fand / BIOS / 試験の操作を分けて出すので、制御の違いが
  分布に混ざらない
- 除いた件数と理由・segment の数を出すので、「1 回の負荷で決まった p99」や「欠けの多い期間の分布」を数字の
  見た目だけで信用しない

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 電力の帯は Workload Regime と別の分け方で、語彙が 2 つになる | 帯は集計の切り口で、制御には使わない。trace があれば `--group-by trace-regime` で Regime の語彙でも出せる |
| 帯の初期値が根拠の薄い `provisional` | 電力そのものの分布も出すので、1 回走らせてから帯を見直せる（§5 #2） |
| 生データだけを読むので、保持期間（30 日）より古い期間は集計できない | `d.*` は保存しないので、ロールアップからは正しい分布が出ない。必要な期間は保持期間のうちに走らせ、出力を残す |
| 24 h × 2.5 s で metric あたり約 3.5 万件をメモリに持つ | 期間の上限 `max_period_days` で抑える。30 日でも metric あたり約 100 万件の float で、1 回の手動の集計として許容できる |
| エアコンの ON / OFF を自動で区別できない | DB に記録が無いものを推測しない。人が `--periods` で区間を渡す |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| trace の `workload_regime` だけで分ける | fand が動いている期間にしか無い（履歴の大半は BIOS）。導入先の `workload_regime` の値は試験の値のまま `provisional`（0119 §2.14）。Regime は idle / active の 2 段で、#50 の「軽負荷（推論）」と「フルロード」を分けられない |
| Regime の推定器（`WorkloadRegimeEstimator.step`）を DB の上で再生して分ける | fand が無くても使え語彙も揃うが、`ControlStateSnapshot` を readings から組み立てる別の経路が要り、推定器の設定が `provisional` なのは同じ。§5 #1 で将来の拡張として残す |
| ロールアップ（`readings_1m`）から計算する | `d.*` が無く、被減数と減数の分平均の差は同じ時刻の差の分布と一致しない。`suspect` などの品質の内訳も残らない |
| API のエンドポイントにする | 生データを広い期間で読むので重く、AI ツールの経路に近づく。手動で時々走らせる用途に常駐の口は要らない |
| `coldaisle-soak-report` に足す | soak は受入基準の判定（0070）で、期間も目的も違う。判定の CLI に分布を混ぜると、判定の再現性の範囲が広がる |
| GPU Mode の申告（`events` / `sys.gpu_mode`）で負荷を分ける | 申告は観測ではない（0045 §2.6・0063 の I-3）。申告と実際の負荷がずれた期間を誤った状態に入れる |
| p99 + マージンを CLI が計算して閾値の候補を出す | 閾値は安全・通知に関わる人の判断（AGENTS.md「迷ったら」）。マージンの決め方も未決で、数字が出ると候補が既定値のように扱われる |

## 5. 未決事項

人の判断が要る点。いずれも推奨案を先頭に置く。

| # | 論点 | 選択肢（先頭が推奨） | 決める場所 |
|---|---|---|---|
| 1 | 負荷の状態の主軸 | **(a) 電力の帯を主軸・trace の Regime は副の切り口（§2.2 / §2.6。推奨）**／(b) trace の Regime だけ／(c) Regime の推定器を DB の上で再生する（`workload_regime` の値が承認された後の拡張として残す）／(d) (a) と (c) の両方を最初から | 本記録の承認 |
| 2 | 帯の初期値 | **(a) 次の `provisional` で始め、T1 の電力の分布を見て見直す（推奨）**: GPU `idle` 0 / `light` 30 / `high` 250 / `full` 450 W（アイドル約 6 W・TGP 600 W・0063 の 400 W と 0127 の 450 W の定常を跨がない）、CPU `idle` 0 / `light` 50 / `full` 120 W（全スレッドで約 200 W・0123）／(b) 先に T1 の電力の分布だけを出して帯を決めてから実装する／(c) 0063 の 400 W に揃えて GPU を `low` / `full` の 2 段にする | 本記録の承認（値は設定。変えるときは設定の差分と出力の来歴で追える） |
| 3 | `settle_ms`（移り変わり直後を除く長さ） | **(a) 300 s（推奨。2026-10-13 の 5 分の負荷で排気の温度がほぼ上がりきったため）**／(b) 600 s（筐体の空気がより落ち着くが、短い負荷が定常に入らない）／(c) 120 s | 本記録の承認 |
| 4 | `config/rules.yaml` の現在の閾値を並べ、超えた割合を出すか | **(a) 出さない（推奨。人が表と `rules.yaml` を並べて読む。CLI をルールの形の変化から切り離す）**／(b) `d.*` を対象にする `threshold` 型のルールだけ、閾値と超過の割合を参考に出す | 本記録の承認 |
| 5 | `fand_override`（`manual` / `max` / `calibration` の tick）の既定の扱い | **(a) 既定で除き件数だけ出す・`--include-override` で含める（推奨。試験の操作は普段の運転ではない）**／(b) 既定で含め別のグループにする | 本記録の承認 |
| 6 | 実装の担当（AGENTS.md「実装の担当」） | **(a) ローカル LLM（推奨。仕様が本記録で明確・`soak.py` の読み取り専用の型を模倣でき・実機不要のテストで確かめられる）**／(b) Claude | 本記録の承認の後、Issue で |
