-- 0001_initial: 決定記録 0002 で確定したスキーマ
--
-- このファイルは docs/decisions/0002-metric-naming.md の SQL をそのまま写したもの。
-- 内容を変更する場合は決定記録を先に更新し、新しい決定記録を作ること（追記のみ）。
-- tests/test_schema_matches_decision.py が両者の一致を検証している。
--
-- 注: `v_latest` は ad-hoc 参照用。GROUP BY のため保持期間に比例して遅くなるので、
-- `SqliteStore.latest()` はこのビューを使わない（決定記録 0004 §2.11）。

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

CREATE TABLE system_state (
    ts_ms INTEGER NOT NULL,
    key   TEXT    NOT NULL,   -- 'sys.gpu_state'
    value TEXT    NOT NULL,   -- 'compute'
    PRIMARY KEY (ts_ms, key)
) WITHOUT ROWID;

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

CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_ms INTEGER NOT NULL
);

CREATE VIEW v_latest AS
SELECT metric, MAX(ts_ms) AS ts_ms, value, quality
FROM readings
GROUP BY metric;
