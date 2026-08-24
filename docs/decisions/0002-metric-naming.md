# 決定記録 0002: メトリクス命名規約とDBスキーマ（ロング形式）

- **種別**: Decision Record
- **Status**: FINAL（2026-08-24 マージ）
- **Date**: 2026-08-24
- **Supersedes**: なし
- **関連**: [`0001-initial-project-decisions.md`](0001-initial-project-decisions.md) D-02 /
  `docs/requirements.md` §5.1 / §5.3 / D-04 / `docs/spec-review.md` C-02
- **対象 Issue**: #3

以降のすべての実装（#5 ストレージ層、#9 API、#10 ロールアップ、#18 ルールエンジン、
#36 GPU Mode イベント）は本記録の DDL を唯一の参照先とする。

---

## 1. Context

将来 CPU / GPU / VRM / 12V-2x6 コネクタのセンサーを追加する予定がある（要件 S-10、元仕様 §18-8）。
ワイド形式（列＝センサー）では追加のたびに `ALTER TABLE` が必要になり、
その都度スキーマ移行とコード変更が発生する。

センサーの追加はこのプロジェクトの既定路線であって例外ではない。
**スキーマ変更を「時々起きる事故」ではなく「起きない前提」にする**ことを優先する。

---

## 2. Decision

### 2.1 メトリクス命名規約

`<domain>.<name>` のドット区切りとする。domain は名前空間として機能する。

| domain | 意味 | 例 |
|---|---|---|
| `air` | 外付けセンサーが測る空気の状態 | `air.room`, `air.gpu_intake` |
| `gpu` | NVML 由来。`gpu.<index>.<name>` の3段 | `gpu.0.core`, `gpu.0.hotspot` |
| `cpu` | lm-sensors 由来 | `cpu.package`, `cpu.vrm` |
| `power` | 消費電力 | `power.gpu.0`, `power.wall` |
| `sys` | システムの数値状態 | `sys.cuda_processes` |
| `d` | **派生値。予約済みで、`readings` には格納しない**（2.2） | `d.intake_rise` |

#### 文法

`gpu.0.core` や `power.gpu.0` のように**添字セグメント**を含む名前が要件 §5.1 に
含まれるため、「小文字とアンダースコアのみ」では必須メトリクスを弾いてしまう。
検証器が書ける形で文法を定義する。

```text
metric  := segment ("." segment)+     ; 2〜4 セグメント。ドメインなしの1語は不可
segment := name | index
name    := [a-z][a-z0-9_]*            ; 先頭は英字。数字とアンダースコアを含めてよい
index   := [0-9]+                     ; 0 始まりの装置番号など
```

検証用の正規表現:

```text
^[a-z][a-z0-9_]*(\.([a-z][a-z0-9_]*|[0-9]+)){1,3}$
```

規則:

- ハイフンと大文字は使わない
- 単位はメトリクス名に含めない（`air.room_c` としない）。単位はメトリクス定義側が持つ
- 一度公開した名前は変更しない。変更が必要なら新しい名前を足し、旧名は移行後に廃止する
- **カテゴリ値（状態）はメトリクスにしない。** `system_state` テーブルへ持つ（2.6）

#### 要件 §5.1 からの変更: `chipset` → `board.chipset` ★承認が必要

要件 §5.1 は lm-sensors 由来のメトリクスを `cpu.package` / `cpu.vrm` / **`chipset`** と
挙げているが、`chipset` だけドメインを持たず `<domain>.<name>` に反する。
このままでは命名規約の検証器が必須メトリクスを弾く。

`board` ドメインを新設して `board.chipset` とする。チップセットは CPU とは別の
シリコンであり、`cpu.*`（`cpu.package` はCPU自身、`cpu.vrm` はCPUへ給電するVRM）に
含めると意味がずれる。今後マザーボード側のセンサーが増えた際の置き場所にもなる。

**この名前は #34（NVML / lm-sensors 統合、実機待ち）でしか使わず、
まだ1行もデータが存在しない。** 変更するなら今が唯一のコストゼロの機会。

v1 で使用する具体的なメトリクスは、上記1点を除き要件 §5.1 の表に従う。

### 2.2 派生メトリクスは保存しない

`d.intake_rise` / `d.gpu_preheat` / `d.gpu_delta` / `d.top_rise` / `d.gpu_internal_delta` は
**保存せず、参照のたびに計算する。**

理由:

- 保存すると、元メトリクスの較正オフセット（FR-107）を後から変更したときに
  **保存済みの派生値と再計算した値が食い違う。** 二つの真実を持つことになる
- 引き算は安価で、行数を倍にする価値がない
- 派生値の定義（何から何を引くか）はコード側の一箇所で管理できる

`d.` を予約プレフィクスとし、`readings` への INSERT は CHECK 制約で拒否する（2.3）。

### 2.3 `readings`（生データ）

```sql
CREATE TABLE readings (
    metric  TEXT    NOT NULL,          -- 'air.front_intake'
    ts_ms   INTEGER NOT NULL,          -- ホスト受信時刻 (Unix ms, UTC)
    value   REAL,                      -- NULL 可（欠測）
    quality TEXT    NOT NULL,          -- ok | missing | suspect | stale
    PRIMARY KEY (metric, ts_ms),
    -- 派生値は保存しない（2.2）。規約ではなく制約として持つ
    CHECK (metric NOT LIKE 'd.%'),
    CHECK (quality IN ('ok', 'missing', 'suspect', 'stale'))
) WITHOUT ROWID;
```

**CHECK 制約で不変条件を実体化している。** 「`d.` を保存しない」「`quality` は4値」は
コメントに書くだけでは守られない。取り込み層の不具合で `quality` に綴り違いが
混ざると、ダッシュボードのフィルタも欠測率も静かに間違った値を出し続ける。
書き込みの時点で落とすほうが早く気づける。

代償として、値の集合を変えるにはテーブル再構築が必要になる（SQLite は
CHECK 制約を後から変更できない）。4値と `d.` 予約は要件 §5.3 と本記録 2.2 で
確定した内容であり、頻繁に変わる想定はない。

**`ts_ms` はホスト受信時刻**であり、デバイス時刻ではない（D-05）。
**1サンプルから生成される全行は同一の `ts_ms` を持つ。** この規則が破れると
同時刻の横串（ピボット）ができなくなるため、取り込み側は1サンプルにつき
時刻を1回だけ確定し、全メトリクスへ配る。

`WITHOUT ROWID` は要件 D-04 の判断を踏襲する。行が小さく（実測見込み40バイト前後）、
主キーがそのまま本体になるため rowid 分の間接参照とインデックスを節約できる。

### 2.4 主キーの並びを `(metric, ts_ms)` とする ★要件 D-04 からの変更

要件 D-04 は `PRIMARY KEY (ts_ms, metric)` と記載しているが、**逆順を提案する。**

読み出しの支配的なパターンは FR-302 の「1メトリクスを期間で取る」であり、
この並びなら主キーだけで完結する。逆順にすると同じ用途に
`(metric, ts_ms)` の二次インデックスが必要になり、`WITHOUT ROWID` では
二次インデックスが主キー全体を含むため**保存量がほぼ倍**になる。

| 用途 | `(metric, ts_ms)` | `(ts_ms, metric)` + 二次インデックス |
|---|---|---|
| 1メトリクスの期間取得（FR-302） | 主キーの範囲走査 | 二次インデックス経由 |
| 最新値（`v_latest`） | メトリクスごとに1シーク | 同等 |
| 1分ロールアップ（FR-202） | メトリクス数ぶんのシーク（20回程度） | 時刻範囲の連続走査 |
| 保持期間超過の削除（FR-203） | **メトリクスごとにループして削除する必要がある** | 時刻範囲の連続走査 |
| 保存量 | 基準 | **約1.8倍** |

削除だけは不利になるが、`DELETE FROM readings WHERE metric = ? AND ts_ms < ?` を
既知のメトリクスでループすれば主キーを使える。1日1回の処理であり、
常時発生する読み出しと保存量を優先する。

### 2.5 `quality` の値

要件 §5.3 の判定規則に対応する4値のみを許す。

| 値 | 条件 |
|---|---|
| `ok` | 正常値 |
| `missing` | JSON が `null`、または `err` に理由が付いている |
| `suspect` | DS18B20 の `-127.00` / **ちょうど `85.00`**、AM2320 の物理的にありえない値 |
| `stale` | 前サンプルから10秒以上更新がない |

**`85.00` ちょうどを `suspect` にすることは必須**（spec-review C-02）。
`-127.00` と違い「排気温度としてありえる値」であるため、見逃すと誤警報ではなく
**誤った安心**を生む。判定の実装とテストは #5 で行う。

### 2.6 カテゴリ値は `readings` に入れず `system_state` へ持つ

NVML から観測した GPU の実状態のようなカテゴリ値を、
`readings.value`（REAL）へ整数コードで入れることはしない。

**理由1: ロールアップが存在しない状態を生む。**
`readings_1m` は min / max / mean を計算する。状態コードの平均値は意味を持たない。
`idle`(1) と `compute`(3) の平均は 2 で `ai` を指すが、その間 `ai` だった事実はない。

**理由2: コードと名前の写像を設定へ外出しすると、写像を変えた瞬間に過去データの
解釈が変わる。** 半年後に「3 が何だったか」を設定ファイルの履歴から復元することになる。

```sql
CREATE TABLE system_state (
    ts_ms INTEGER NOT NULL,
    key   TEXT    NOT NULL,   -- 'sys.gpu_state'
    value TEXT    NOT NULL,   -- 'compute'
    PRIMARY KEY (ts_ms, key)
) WITHOUT ROWID;
```

規則:

- **TEXT で直接保存する。** コード化しない
- **変化したときだけ書く。** 2.5秒ごとに同じ値を書かない。状態遷移だけを記録する
- これにより #36 が求める「タイムライン注釈」がそのまま得られる
- 保持は無期限。行数は状態遷移の回数しかない

主キーの並びが `readings`（2.4）と逆なのは意図的ではなく、行数が桁違いに少なく
並び順が性能に影響しないため、指定された形をそのまま採ったもの。

#### `sys.gpu_state` の値

| 値 | 条件 |
|---|---|
| `unknown` | NVML 利用不可 / 判定不能。**最も保守的な閾値セットを使う** |
| `idle` | CUDA プロセスが存在しない |
| `ai` | 推論サーバのプロセスのみが存在する |
| `compute` | 推論サーバ以外の CUDA プロセスが存在する |
| `mixed` | 両方が存在する |

- プロセスの識別規則は `config/gpu_state.yaml` へ外出しする。ハードコードしない
- **値の追加は可。既存値の意味変更・削除は不可。** 過去データの解釈が変わるため

#### 用語の区別（重要）

| 概念 | 所有者 | 意味 | coldaisle が知る必要 |
|---|---|---|---|
| **GPU Mode** | Workspace | 利用者が選んだ**指令値**（AI / Shared / Compute） | **なし** |
| **`gpu_state`** | coldaisle | NVML から観測した**実際の状態** | あり |

閾値の切り替えに必要なのは「利用者が何を選んだか」ではなく
**「実際に GPU で何が動いているか」**である。指令と実態はずれる
（Compute へ切り替えたが学習がまだ始まっていない、など）。
決定記録 D-19 の AI / Shared / Compute は Workspace 側の概念であり、
coldaisle のデータモデルには現れない。

なお `sys.cuda_processes` は個数（数値）なので通常どおり `readings` へ格納する。

> 保存する `key` は **`sys.gpu_state`** とする（`sys.` を付ける）。
> `readings.metric` と `system_state.key` で同じ対象を別表記にすると、
> API のレスポンスやログを読むときに毎回対応付けが必要になる。
> 名前空間の表記は格納先に関わらず一つにそろえる。

### 2.7 `node_id` は列を作らず名前だけ予約する

要件 N-04 は「1台前提。ただしスキーマは `node_id` を予約しておく」としている。

**決定: 現時点では列を作らない。** 単一ノード運用で全行に同じ値を持つ列を
主キーへ加えると、`WITHOUT ROWID` では主キーが本体であるため
**全データの保存量に定数コストが乗り続ける。**

代わりに以下を予約とする。

- メトリクス名として `node_id` および `node.*` を使わない
- 複数ノードへ拡張する際は、新テーブルを作って移し替える移行を行う
  （SQLite は主キーの変更ができないため、いずれにせよテーブル再構築が必要）
- 移行機構は #5 で用意する `schema_version` に載せる

### 2.8 ロールアップは3段（生 / 1分 / 1時間）

決定記録 D-02 に従う。**要件 §6.2 FR-203 の「既定14日」は D-02 に上書きされている。**

| 粒度 | 保持 | テーブル |
|---|---|---|
| 生（2.5秒） | 30日 | `readings` |
| 1分 | 無期限 | `readings_1m` |
| 1時間 | 無期限 | `readings_1h` |

```sql
CREATE TABLE readings_1m (
    metric         TEXT    NOT NULL,
    bucket_ms      INTEGER NOT NULL,   -- 分の開始時刻 (Unix ms, UTC)
    min_value      REAL,               -- quality='ok' の行のみから計算
    max_value      REAL,
    mean_value     REAL,
    ok_value_count INTEGER NOT NULL,   -- quality='ok' の行数。上の3値の母数
    row_count      INTEGER NOT NULL,   -- バケット内の全行数（品質を問わない）
    expected_count INTEGER,            -- そのバケットで期待されるサンプル数
    PRIMARY KEY (metric, bucket_ms)
) WITHOUT ROWID;

CREATE TABLE readings_1h (
    metric         TEXT    NOT NULL,
    bucket_ms      INTEGER NOT NULL,   -- 時の開始時刻 (Unix ms, UTC)
    min_value      REAL,
    max_value      REAL,
    mean_value     REAL,
    ok_value_count INTEGER NOT NULL,
    row_count      INTEGER NOT NULL,
    expected_count INTEGER,
    PRIMARY KEY (metric, bucket_ms)
) WITHOUT ROWID;
```

#### 集計は `quality='ok'` の行だけで行う

`suspect`（`-127.00` や ちょうど `85.00`）を平均や最大値に混ぜてはならない。
センサーの人工物がそのままグラフと統計に乗り、**生データを消した後は
取り除けなくなる。** 品質の情報は `ok_value_count` と `row_count` の差として残る。

`ok_value_count = 0` のバケットでは `min_value` / `max_value` / `mean_value` は NULL。

#### 3つの計数を持つ理由（重なりのない母数を残す）

当初は `sample_count`（値があった数）と `missing_count`（`ok` 以外の数）の2つにしていたが、
**この2つは重なるため欠測率を復元できない。**
数値を持つ `suspect` の行は両方を増やすので、

| 実際に起きたこと | 旧定義での記録 | 本来の欠測率 |
|---|---|---|
| 数値付きの `suspect` が1行 | `sample_count=1, missing_count=1` | 100% |
| `ok` が1行 + 値なし `missing` が1行 | `sample_count=1, missing_count=1` | 50% |

**同じ記録から異なる欠測率が導かれてしまう。** 生データを30日で消したあとは
どちらだったか判別できず、FR-303 と NFR-02 が満たせない。

そこで、入れ子の関係が明確な3つを持つ。

```text
ok_value_count  ≦  row_count  ≦  expected_count（通常）
        欠測率 = 1 - ok_value_count / COALESCE(expected_count, row_count)
```

| 列 | 何を数えるか | これで分かること |
|---|---|---|
| `ok_value_count` | `quality='ok'` の行 | 集計値の重み。欠測率の分子 |
| `row_count` | バケット内の全行 | **サンプルは届いたが値が異常**（センサー故障、FR-402） |
| `expected_count` | 期待サンプル数 | **サンプル自体が届かなかった**（通信断、FR-401） |

`row_count` と `expected_count` を分けているのは、この2つが違う障害を指すため。
どちらも欠測だが、対処は「センサーを見る」と「ケーブルとデーモンを見る」で異なる。

`expected_count` は取り込み側が `devices.interval_ms`（起動バナー由来）から算出する。
ファームウェアの更新で送信周期が変わっても、**バケット単位で当時の期待値が残る。**
起動バナーを受け取れていない場合のみ NULL とし、そのときの欠測率は
`row_count` を母数とした下限値として扱う。

#### 1分 → 1時間の再集計

**平均の単純平均を取ってはならない。** `ok_value_count` で重み付けする。

```text
min_value      = MIN(min_value)
max_value      = MAX(max_value)
mean_value     = SUM(mean_value * ok_value_count) / SUM(ok_value_count)
ok_value_count = SUM(ok_value_count)
row_count      = SUM(row_count)
expected_count = SUM(expected_count)    -- いずれかが NULL なら NULL
```

`min_value` ではなく `min` としないのは、SQL の集約関数名と紛れるため。

### 2.9 `alerts`

```sql
CREATE TABLE alerts (
    id            INTEGER PRIMARY KEY,
    rule_id       TEXT    NOT NULL,   -- 'RECIRCULATION'（FR-401〜409）
    severity      TEXT    NOT NULL,   -- info | warning | critical
    state         TEXT    NOT NULL,   -- pending | firing | resolved
    metric        TEXT,               -- 対象メトリクス。複数にまたがる規則では NULL
    started_ms    INTEGER NOT NULL,   -- 条件が成立した時刻
    fired_ms      INTEGER,            -- 継続時間を満たして発火した時刻
    resolved_ms   INTEGER,
    trigger_value REAL,               -- 発火時の値
    threshold     REAL,               -- 発火時に適用されていた閾値
    detail        TEXT,               -- 人間向けの補足
    CHECK (severity IN ('info', 'warning', 'critical')),
    CHECK (state IN ('pending', 'firing', 'resolved'))
);

CREATE INDEX ix_alerts_state   ON alerts(state);
CREATE INDEX ix_alerts_started ON alerts(started_ms);
```

状態機械 `OK → PENDING → FIRING → RESOLVED`（要件 §6.4）を
`state` と3つの時刻列で表現する。`started_ms` と `fired_ms` を分けているのは、
**「条件はいつ成立し、継続時間の要件をいつ満たしたか」を後から検証できるようにする**ため。
閾値を実測で見直す #19 でこの差分が判断材料になる。

`threshold` を記録するのは、閾値が設定ファイルで変わるため。
過去のアラートを「当時の閾値」で解釈できないと、履歴が読めなくなる。

このテーブルだけは `WITHOUT ROWID` にしない。行数が桁違いに少なく、
連番の `id` が必要なため。

### 2.10 `devices` と `device_sensors`

起動バナー（`type:"hello"`、要件 §5.2）を受けて更新する。

```sql
CREATE TABLE devices (
    device_id     TEXT PRIMARY KEY,   -- hello.dev 'xiao-esp32s3'
    fw            TEXT,               -- hello.fw
    schema_v      INTEGER,            -- hello.v
    interval_ms   INTEGER,            -- hello.interval_ms
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL,
    last_hello_ms INTEGER             -- 直近の起動バナー受信時刻
);

CREATE TABLE device_sensors (
    device_id  TEXT    NOT NULL,
    channel    TEXT    NOT NULL,      -- 'front_intake'
    kind       TEXT    NOT NULL,      -- 'ds18b20' | 'am2320'
    gpio       INTEGER,
    rom        TEXT,                  -- DS18B20 の64bit ROM ID。AM2320 は NULL
    resolution INTEGER,               -- ビット数。11 を想定（spec-review C-01）
    updated_ms INTEGER NOT NULL,
    PRIMARY KEY (device_id, channel)
) WITHOUT ROWID;
```

`device_sensors` を別テーブルにするのは、FR-403 `PROBE_CHANGED` が
**「どのチャネルの ROM が変わったか」**を必要とするため。
JSON を1列に丸めて保存すると、差分の特定がアプリ側の文字列処理になる。

### 2.11 `schema_version`

```sql
CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_ms INTEGER NOT NULL
);
```

連番の SQL を順に適用し、適用済みの番号を記録する。実装は #5。

### 2.12 `v_latest` ビュー

ロング形式の弱点（同時刻の横串が面倒）を吸収する。

```sql
CREATE VIEW v_latest AS
SELECT metric, MAX(ts_ms) AS ts_ms, value, quality
FROM readings
GROUP BY metric;
```

`MAX()` と同じ行から他の列を取る挙動は SQLite が明示的に保証している
（bare columns in an aggregate query）。**本プロジェクトは SQLite に限定するため
この仕様に依存してよい**が、他の DBMS へ移す場合はここを書き換える必要がある。

`system_state` の現在値は行数が少ないため
`SELECT value FROM system_state WHERE key = ? ORDER BY ts_ms DESC LIMIT 1` で足りる。
ビューは設けない。

---

## 3. Consequences

### 良くなること

- センサーを追加しても `ALTER TABLE` が発生しない。行が増えるだけになる
- メトリクスごとに欠測・品質を独立して持てる。ワイド形式では
  「この行のこの列だけ suspect」を表現できない
- 保持期間の異なる3段の粒度を、同じ形のテーブルで扱える
- 状態を別テーブルに分けたことで、ロールアップが数値だけを相手にすればよくなった

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 同時刻の全メトリクスを1行で取るクエリが複雑になる | `v_latest` ビュー（2.12）と、API 層でのピボット |
| メトリクス名の文字列が全行に繰り返され、保存量が増える | 現実的な行数では問題にならない（下記の見積り）。将来必要になれば `metric` を整数 ID へ正規化する移行を行う |
| 時系列と状態が別テーブルに分かれ、突き合わせに結合が要る | 状態は遷移時のみの数行。`ts_ms` の範囲で引き当てる |
| 削除が主キー順に沿わない | メトリクスごとのループ削除（2.4） |

### 保存量の見積り（要件 §6.2 の数値を訂正）

要件 §6.2 は「2.5秒周期 × 7メトリクス = 約 2.4M行/日」「60〜80MB/日」としているが、
**この行数は約10倍過大である。**

```text
1日のサンプル数 = 86400秒 ÷ 2.5秒 = 34,560
1日の行数       = 34,560 × 7メトリクス = 241,920 行/日   （2.4M ではない）
1行あたり       ≈ 40 バイト
1日             ≈ 10 MB
30日保持        ≈ 290 MB
```

v1 で GPU / CPU / 電力（S-10）を加えて20メトリクスになった場合でも、
30日で 830MB 程度に収まる。

**この訂正は保持方針を変えるものではない**（ロールアップと削除は引き続き必要）が、
「14日で1GB」という前提で容量を心配する必要はない。

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| ワイド形式（列＝センサー） | センサー追加のたびに `ALTER TABLE`。本プロジェクトでは追加が既定路線 |
| **カテゴリ値を整数コードで `readings` に格納** | ロールアップが存在しない状態（平均値）を生む。写像を変えると過去データの解釈が変わる（2.6） |
| `metric` を整数 ID へ正規化 | 全クエリに JOIN が乗る。上記の見積りでは保存量の節約が見合わない。必要になってからでも移行できる |
| メトリクスごとにテーブルを分ける | テーブル数がメトリクス数に比例し、横断クエリが破綻する |
| 派生値も保存する | 較正オフセット変更時に過去の派生値と再計算値が食い違う（2.2） |
| Prometheus 等の時系列DBを併設 | 決定記録 D-09 で不採用 |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | メトリクスの単位・表示名の定義をどこに置くか（`config/metrics.yaml` を想定） | #5 |
| 2 | `gpu_state` 別に閾値を切り替える場合の `alerts.threshold` の扱い | #18 / #36 |
