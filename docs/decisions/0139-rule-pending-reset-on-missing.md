# 決定記録 0139: 既存のしきい値・範囲のルールで、入力が「欠けた」と判定されたら発火待ち（pending）を取り消す

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-15
- **Supersedes**: なし（0012 の状態機械に取り消しの条件を足す。0131 §2.3 の GPU の Fan の 2 つのルールの扱いは変えない）
- **関連**: [`0131`](0131-gpu-fan-service-alert.md) §2.3 / §5 の 4 /
  [`0012`](0012-rule-engine.md) §2.2 / §2.4 /
  [`0013`](0013-notifications.md) / [`0016`](0016-evidence-alerts.md) /
  [`0022`](0022-firmware-v1.md) §2.3 / §2.11 /
  [`0003`](0003-device-json-schema.md) §2.3 /
  [`0004`](0004-storage-read-contract.md) §2.12 /
  `config/rules.yaml`、`config/quality.yaml`、`src/coldaisle/rules/engine.py` /
  要件 FR-401〜409、AGENTS.md「迷ったら」
- **対象 Issue**: #302

---

## 1. Context

### 1.1 今の振る舞い

しきい値・範囲のルール（`recirculation`・`intake_high`・`airflow_degraded`・`room_high`・`humidity_out_of_range`）は、
取り込みの `Engine.on_sample()` がサンプルごとに評価する（`main` の `src/coldaisle/rules/engine.py`）。

- 入力の値は、そのサンプルの読み値の品質が ok のときだけ使い、ok でなければ `None` にする（`on_sample()`）。
  派生値（`d.intake_rise = air.front_intake − air.room`・`d.gpu_delta = air.gpu_exhaust − air.gpu_intake`）は、
  2 つの入力の両方が ok のときだけ計算し、どちらかが欠ければ `None`（`metrics.compute_derived()`）
- `_evaluate_threshold()` / `_evaluate_range()` は `value is None` で**何もせず返る**。`RuleState.since_ms`（条件が成立した時刻）と
  DB の pending の行はそのまま残る
- サンプルそのものが届かない間（無音）は、`on_tick()` が `SENSOR_FAULT` だけを評価し、しきい値・範囲のルールは評価しない。
  ここでも `since_ms` は残る
- 次に値が来て条件が成立していると、`_continue()` が `now − since_ms` を `fire_after_s` と比べる。
  **欠けの空白をまたいで「条件が続いた」とみなして発火しうる**（0131 §2.3 で GPU の Fan のルールについて指摘した問題と同じ）
- 再起動では、`_hydrate()` が DB の pending の行の `started_ms` を `since_ms` に戻す。停止していた時間もまたぐ
- 解除は閾値（`clear`・`low_clear` / `high_clear`）を下回った周期で即時なので、解除の側の問題は無い。
  `rapid_rise` は `fire_after_s: 0`、`sensor_fault`・`sensor_missing`・`probe_changed` は継続時間の pending を持たないので該当しない

### 1.2 空白をまたいだ発火が困る理由

- **観測していない継続を、継続として通知する。** 例: `room_high`（`fire_after_s: 600`）が 30 °C を 1 回超えて pending → 取り込みが
  2 時間止まる → 再開の最初のサンプルが 30 °C を超えている、だけで発火する。説明（0016 / #38 の `_rule_evidence()`）は
  `fired_ms − started_ms` から「条件が 7200 秒継続して発火」と書く。人も LLM の説明も、2 回の観測を 2 時間の継続と読む
- `fire_after_s` は「揺らぎで鳴らさない」ための待ち（0012 §2.4）なので、観測の無い時間で満たすと、その目的が崩れる

### 1.3 基板のセンサーの欠け方

- 送信周期は 2.5 s（ファームウェアの `INTERVAL_MS = 2500`）。1 サンプルに 7 チャネル（`air.*`）が載る
- DS18B20（`front_intake`・`gpu_intake`・`gpu_exhaust`・`top_exhaust`・`rear_exhaust`）は 1 本ずつ欠ける。読み取りの失敗・`-127`・
  ちょうど `85.00` は、そのチャネルだけが `null`（0022 §2.3、0003 §2.3）で、ホストでは `missing` / `suspect`
- AM2320 は `air.room` と `air.room_humidity` を**同時に**欠く（1 回の読み取りで両方）。読み取りの失敗が 5 回続くと、
  12 周期（約 30 s）読まない（0022 §2.11。`AM2320_FAIL_LIMIT = 5`・`AM2320_BACKOFF_CYCLES = 12`）。
  この 1 回の欠けは **5 + 12 = 17 サンプル ≈ 42.5 s** になる。起動の直後の 1 回目の読み取りは必ず失敗する（0022 §2.11）
- したがって 5 つのルールの入力の欠けは次のように依存する

| ルール | 入力 | `fire_after_s` | `fire_after_s` に要るサンプル数 | 欠ける原因 |
|---|---|---|---|---|
| `intake_high` | `air.gpu_intake` | 120 | 48 | DS18B20 1 本 |
| `recirculation` | `d.intake_rise`（`air.front_intake` − `air.room`） | 300 | 120 | DS18B20 1 本 **または AM2320** |
| `airflow_degraded` | `d.gpu_delta`（`air.gpu_exhaust` − `air.gpu_intake`） | 300 | 120 | DS18B20 2 本のどちらか |
| `room_high` | `air.room` | 600 | 240 | AM2320 |
| `humidity_out_of_range` | `air.room_humidity` | 600 | 240 | AM2320 |

- 既存の欠けの検知: `sensor_missing`（warning）は 1 つのメトリクスが **連続 5 サンプル**（`consecutive: 5` = 12.5 s）ok でないと
  即時に発火し、3 サンプル ok で解除する。`sensor_fault`（critical）はサンプルが **30 s**（`silence_s`）来ないと発火する。
  `config/quality.yaml` の `stale_after_ms` は 10 s（Store が読み出し時に stale とする閾値。0004 §2.12。取り込みのルールは
  サンプルの値で評価するので、今は使っていない）

## 2. Decision

### 2.1 選択肢と推奨

| 案 | 取り消す条件 | 発火が遅れる場面 |
|---|---|---|
| A | 入力が 1 回でも欠けたら（0131 §2.3 と同じ） | 1 サンプルの欠けでも数え直し。散発的な欠けで**発火しないまま続き、ほかの通知も出ない**（2.2） |
| B | 取り消さない（今のまま） | 遅れない。代わりに空白をまたいで発火する（1.2） |
| C | 入力の最後の ok から `stale_after_ms`（10 s）を超えた空白があったら | 4 サンプル以上の連続の欠け（12.5 s）で数え直し。4 サンプルちょうどの欠けでは `sensor_missing`（5 サンプル）が鳴らない |
| **D（推奨）** | 入力が `sensor_missing` の条件（連続 `consecutive` = 5 サンプル）に届いたら、またはサンプルの無音が `sensor_fault` の条件（`silence_s` = 30 s）に届いたら | 欠けの検知が成立したときだけ数え直す。**数え直しのたびに `SENSOR_MISSING` / `SENSOR_FAULT` が人に届く**（2.2） |

**D を採る。** 取り消す条件を、既存の「欠けている」の判定（`sensor_missing` / `sensor_fault`）に揃える。

- 1〜4 サンプルの短い欠け（DS18B20 の 1 回の読み損じ、AM2320 の 1〜4 回の失敗）では pending を保つ。遅れはほぼ 0（2.2）
- 欠けが `sensor_missing` / `sensor_fault` の条件に届いたら、pending を取り消し、回復の後に条件が成立した時刻から数え直す。
  **発火が遅れるときは、必ずその原因（センサーの欠け・無音）の通知が出ている**。黙って遅れることが無い
- 新しい閾値を足さない。数え直しの境界は `config/rules.yaml` の `sensor_missing.consecutive` と `sensor_fault.silence_s` を読む
  （AGENTS.md ルール 9。値を 2 か所に持たない）

### 2.2 発火が遅れる場面の評価（2.5 s 周期・`config/rules.yaml` の今の値）

条件は時刻 0 から成立し続けるとする。欠けが無ければ、発火は `fire_after_s` の時刻（`intake_high` 120 s、`recirculation`・
`airflow_degraded` 300 s、`room_high`・`humidity_out_of_range` 600 s）。

**(1) 1 回だけの短い欠け（1 サンプル）が、発火の直前に来たとき**

| 案 | `intake_high` | `room_high` / `humidity_out_of_range` |
|---|---|---|
| A | 117.5 s で取り消し → 120 s から数え直し → **240 s**（+120 s） | 597.5 s で取り消し → **1200 s**（+600 s） |
| B・C・D | 120 s（遅れない） | 600 s（遅れない） |

**(2) 散発的な 1 サンプルの欠けが独立に起きるとき（A だけの問題）**

欠ける確率を 1 サンプルあたり p とすると、A では `fire_after_s / 2.5 + 1` サンプル連続で ok が要る。発火までの時間の期待値
（連続の成功を待つ回数の期待値 (1 − q^L) / (p·q^L)、q = 1 − p、L = 必要な連続数、× 2.5 s）:

| p | `intake_high`（欠け無しで 120 s） | `recirculation` / `airflow_degraded`（300 s） | `room_high` / `humidity_out_of_range`（600 s） |
|---|---|---|---|
| 0.1 % | 約 126 s | 約 322 s | 約 682 s（11 分） |
| 1 % | 約 159 s | 約 593 s（10 分） | 約 2567 s（43 分） |
| 2 % | 約 211 s | 約 1316 s（22 分） | 約 16146 s（4.5 時間） |
| 5 % | 約 567 s | 約 24748 s（6.9 時間） | 事実上発火しない（約 135 日） |

派生値のルールは 2 つの入力のどちらの欠けでも数え直すので、p は 2 つの和に近くなる（`recirculation` は DS18B20 1 本 + AM2320）。
**A では、AM2320 が 2 % 程度の散発的な読み損じを持つだけで、`room_high` と湿度の発火が数時間遅れ、そのあいだ `SENSOR_MISSING` も
鳴らない（5 回連続にならない）。** C と D では、独立な欠けが 4〜5 回続く確率は p^4〜p^5（p = 5 % で 1 サンプルあたり 6×10⁻⁶ 以下）で、
遅れは無視できる。

**(3) 発火が遅れる最悪の例（D）: AM2320 の 1 回の失敗の連続（17 サンプル ≈ 42.5 s）**

`room_high` が時刻 0 から成立 → 597.5 s まで ok → 600 s から AM2320 が 5 回失敗し約 30 s 読まない（600〜640 s の 17 サンプルが欠け）。

| 案 | 動き | 発火 |
|---|---|---|
| B | 642.5 s の最初の値で `642.5 − 0 ≥ 600` | 642.5 s（「642 秒継続」と記録。うち 42.5 s は観測していない） |
| C | 642.5 s で空白 42.5 s > 10 s → 取り消して 642.5 s から数え直し | **1242.5 s**（+642.5 s） |
| D | 610 s（5 回目の欠け）で `SENSOR_MISSING`（`air.room` と `air.room_humidity`）が発火、同時に取り消し → 642.5 s から数え直し | **1242.5 s（約 20.7 分。+642.5 s）**。610 s に `SENSOR_MISSING` が届き、647.5 s（3 サンプル ok）で解除される |

同じ形で `recirculation`（300 s）は 300 + 42.5 + 300 ≈ **642.5 s**、`intake_high`（120 s）は DS18B20 の 5 回の欠け（12.5 s）で
117.5 + 15 + 120 ≈ **252.5 s** になる。

**(4) 発火しないまま続く最悪の例（D）: 欠けが `fire_after_s` より短い間隔で繰り返す**

AM2320 が 10 分より短い間隔で「5 回失敗 → 30 s 休む」を繰り返すと、`room_high`・`humidity_out_of_range`（600 s）と
`recirculation`（300 s）は**一度も発火しない**。このとき `SENSOR_MISSING`（warning）が欠けのたびに発火・解除を繰り返すので、
人は「室内のセンサーが壊れかけている」ことを知る。部屋の温湿度の判定ができないことは、その通知で代わりに伝わる。
A では同じことが **1 サンプルの欠け**で起き、`SENSOR_MISSING` も鳴らない（(2)）。C では 4 サンプルちょうどの欠けの繰り返しで
起き、`SENSOR_MISSING` は鳴らない。

**(5) サンプルの無音**

| 無音の長さ | D | B |
|---|---|---|
| 30 s 未満 | pending を保つ（`sensor_fault` は鳴らない。遅れなし） | 保つ |
| 30 s 以上 | `SENSOR_FAULT`（critical）が鳴った時点で取り消し、再開の後に数え直す | 保つ。再開の最初のサンプルで発火しうる（1.2 の 2 時間の例） |

### 2.3 振る舞いの定め（D）

5 つのルール（`recirculation`・`intake_high`・`airflow_degraded`・`room_high`・`humidity_out_of_range`）に当てる。
`rapid_rise`・`sensor_fault`・`sensor_missing`・`probe_changed` と、0131 の GPU の Fan の 2 つのルール（0131 §2.3 のまま）は変えない。

- **欠けの数え方**: ルールが評価する値（派生値ならその値）が `None` のサンプルを連続で数える（`RuleState` に連続の欠けの数を持つ）。
  値が来たら 0 に戻す。`sensor_missing` の状態（`streak`）は使わない（`sensor_missing.enabled: false` のときも数えるため。
  派生値は 2 つの入力の欠けが交互に来ても数える）
- **pending（未発火）の取り消し**: 連続の欠けが `sensor_missing.consecutive` に届いたサンプルで、pending の行を消し `since_ms` を戻す
  （0012 §2.4 の「継続時間に届かなかった揺らぎは消す」と同じ。遷移は配らない）。0131 §2.3 の実装で作る共通の部品
  （`RuleState` を pending から戻す関数）を使う
- **無音**: `on_tick()` で `sensor_fault` の条件（無音 ≥ `silence_s`）が成立したとき、5 つのルールの pending をすべて取り消す
- **発火中（firing）は取り消さない・解除しない。** 欠けの間は今までどおり何もせず、回復後に解除の閾値で解除する（解除は即時で待ちが無い）
- **再起動**: `_hydrate()` で読み戻した 5 つのルールの pending は、起動の前に DB に残る最後の `air.*` の読み値の時刻と、
  起動の後の最初のサンプルの時刻の差が `sensor_fault.silence_s` 以上なら取り消す（無音と同じに扱う）。未満なら今までどおり引き継ぐ
- `sensor_missing.consecutive` と `sensor_fault.silence_s` は、そのルールの `enabled` によらず値を読む
- 評価時刻は 0012 §2.2 のまま（サンプルの `ts_ms`）。圧縮再生・一括再生でも同じに動く

### 2.4 変わるコードの場所（実装は本記録の承認の後。0131 の実装（#292）の共通の部品の後）

| 場所 | 変えること |
|---|---|
| `src/coldaisle/rules/engine.py` の `RuleState` | 連続の欠けの数を持つ |
| `_evaluate_threshold()` / `_evaluate_range()` | `value is None` の分岐で欠けを数え、`consecutive` に届いたら pending を取り消す（firing なら何もしない）。値が来たら 0 に戻す |
| `on_tick()` / `_evaluate_silence()` | 無音が `silence_s` に届いた時点で 5 つのルールの pending を取り消す |
| `_hydrate()` / `begin()` / 最初の `on_sample()` | 2.3 の再起動の扱い（起動前の最後の読み値の時刻を控え、最初のサンプルで比べる） |
| `src/coldaisle/rules/models.py`（ルールの設定の型） | 変えない（新しい項目を足さない） |
| `config/rules.yaml` | 値は変えない。冒頭の説明に「欠けが `sensor_missing` / `sensor_fault` の条件に届いたら発火待ちを数え直す」を 1 行足す |
| `docs/notifications.md` など運用の文書 | 発火の遅れの条件を書く |

### 2.5 テストで確かめること（実機なし。SimulatedClock と 2.5 s のサンプル列。AGENTS.md ルール 7）

- `intake_high`: pending の途中で `air.gpu_intake` が 4 サンプル欠けても、最初の成立から 120 s で発火すること（今と同じ）
- `intake_high`: 5 サンプル欠けると pending の行が消え、遷移が配られず、回復の時刻から 120 s 続いて初めて発火すること。
  同じ周期に `SENSOR_MISSING` が発火していること
- `recirculation`: `air.room` だけ、`air.front_intake` だけ、2 つが交互に欠けて派生値が 5 サンプル連続で `None` のとき、どれでも取り消すこと
- `humidity_out_of_range`（範囲）: 低い側・高い側で同じに動くこと
- AM2320 の形（5 回失敗 + 12 周期休む = 17 サンプルの欠け）で、`room_high` の発火が 2.2 (3) の時刻（1242.5 s）になること
- 発火中のアラートが 5 サンプル以上欠けても解除されず、回復後に解除の閾値で解除されること
- 無音: 20 s の無音では pending を保つこと。30 s の無音で `SENSOR_FAULT` と同時に取り消され、再開の後に数え直すこと
- `sensor_missing.enabled: false` でも 5 サンプルで取り消すこと
- 再起動: 起動の前の最後の読み値から 30 s 以上たって最初のサンプルが来たら、読み戻した pending を取り消すこと。30 s 未満なら引き継ぎ、
  元の `started_ms` から数えて発火すること
- 発火の説明の「条件が N 秒継続して発火」（`explain.py` の `_rule_evidence()`）が、取り消した後は数え直した時刻からの秒数になること
- 変わらないこと: `rapid_rise`・`sensor_fault`・`sensor_missing`・`probe_changed` の遷移の列が今と同じこと。
  0131 の GPU の Fan の 2 つのルール（1 回の欠けで取り消す）が今と同じこと
- 一括再生（`--bulk`）で、評価時刻がサンプルの時刻に従うこと

## 3. Consequences

### 良くなること

- 観測していない時間を「条件が続いた」として通知しない。長い欠け・無音・停止をまたいだ発火が無くなり、説明の継続秒数が観測と合う
- 短い読み損じでは今と同じ時刻に鳴る（A のように散発的な欠けで黙って遅れることが無い）
- 発火が遅れるときは必ず `SENSOR_MISSING` か `SENSOR_FAULT` が出ており、遅れの原因が人に見える
- 新しい閾値を足さない

### 悪くなること・その緩和

- **センサーが繰り返し欠けると、部屋の温湿度・再循環のルールが発火しないまま続く**（2.2 (4)）。緩和: そのとき `SENSOR_MISSING` が
  欠けのたびに届く。センサーの欠けを直すことが先で、欠けた値で部屋の判定を続けることはもともとできない
- 長い欠けの直後の発火が、最大で `fire_after_s` + 欠けの長さだけ遅れる（`room_high` で約 20.7 分。2.2 (3)）。緩和: 5 つはすべて
  warning（`rapid_rise`・`sensor_fault` の critical は変わらない）。急な上昇は `rapid_rise`（`fire_after_s: 0`）が捉える
- 夜間（0013 §2.5）は warning が届かない設定なので、夜間の `SENSOR_MISSING` も届かない。緩和: 夜間の扱いは 5 つのルール自身も同じで、
  本記録で悪くならない
- 0131 の GPU の Fan のルール（1 回の欠けで取り消す）と、取り消しの境界が違う。緩和: GPU の値は DB の最新値の stale で判定し、
  欠けは収集の停止なので 1 回で十分長い（0131 §2.3）。基板の値はサンプルごとの読み損じがあり、性質が違う。違いを本記録に残す

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| A: 1 回の欠けでも取り消す（0131 §2.3 と同じ） | 散発的な欠けで発火が黙って大きく遅れる（AM2320 の 2 % の読み損じで `room_high` が約 4.5 時間）。そのあいだ `SENSOR_MISSING` も鳴らず、遅れの原因が人に見えない（2.2 (2)） |
| B: 取り消さない（今のまま） | 観測していない時間を継続として通知し、説明の継続秒数が誤る（1.2）。数時間の無音・停止の後でも、再開の 1 サンプルで鳴る |
| C: `stale_after_ms`（10 s）を超える空白で取り消す | D とほぼ同じ振る舞いだが、4 サンプルちょうどの欠けで数え直し、そのとき `SENSOR_MISSING`（5 サンプル）が鳴らない。「黙って遅れない」が成り立たない。取り込みのルールは Store の stale を使っておらず、閾値の出どころを足すことになる |
| 欠けの長さを `config/rules.yaml` にルールごとに持つ | 「欠けている」の判定が `sensor_missing` と 2 か所になり、片方だけを変えると食い違う（0131 §2.3 の stale の閾値を 2 か所に持たない理由と同じ） |
| 欠けの間は継続の時間を止め（時計を止め）、回復後に続きから数える | 観測した時間だけを足すので継続の主張は正しいが、`since_ms` が 1 つの時刻でなくなり、DB の `started_ms`・説明の秒数・再起動の引き継ぎの意味が変わる。変更が大きく、利点は D との差が小さい |

## 5. 未決事項

| # | 内容 | 時期 |
|---|---|---|
| 1 | 実機の AM2320・DS18B20 の欠けの頻度（2.2 の p）を、導入先の DB の `quality` から数えて評価を確かめる。D の推奨は p によらないが、A を選び直すかの判断材料になる | 本記録の承認の前でも後でもよい。所有者が導入先で数える |
| 2 | `sensor_missing.consecutive`・`sensor_fault.silence_s` の値を変えるときは、本記録の数え直しの境界も同時に変わる。値の変更の決定記録でそれを書く | 値を変えるとき |
| 3 | 0131 の GPU の Fan の 2 つのルールの取り消しの条件を D に揃えるか（今は 1 回の欠け） | 揃える必要が出たとき（今は性質が違うので揃えない。§3） |
