-- 0002_control_traces: #82 の制御判断ログ。
--
-- ControlTick は schema version を持つ。列へ展開すると schema を拡張するたびに
-- 保存済みの trace を読み違えるため、判断時点の JSON をそのまま残す。
-- これは readings の観測値ではなく、1 tick の意思決定を再現するための記録である。

CREATE TABLE control_traces (
    ts_ms          INTEGER NOT NULL,
    tick_id        INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    trace_json     TEXT    NOT NULL,
    PRIMARY KEY (ts_ms, tick_id),
    CHECK (ts_ms >= 0),
    CHECK (tick_id >= 0),
    CHECK (schema_version >= 1),
    CHECK (json_valid(trace_json)),
    CHECK (json_type(trace_json) = 'object')
) WITHOUT ROWID;

CREATE INDEX ix_control_traces_tick ON control_traces (tick_id, ts_ms);
