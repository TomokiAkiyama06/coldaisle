-- 0007_control_trace_seq: #106 decision trace の読み取り API。決定記録 0071 §2.2a。
--
-- 記録した順を表す `seq` と、保持期間の削除の境界を表す1行を足す。
-- `ts_ms` は壁時計で、時刻合わせで前後に飛ぶ。`(ts_ms, tick_id)` で並べて cursor にすると、
-- 読んでいる途中に時計が戻ったとき新しい行を黙って飛ばす（0071 §4 N）。
-- 主キー `(ts_ms, tick_id)` と `INSERT OR IGNORE` は変えない（0030 §2）。
--
-- `:now_ms` は `migrations.apply_pending` が渡すストアの時計（本番は壁時計）。

-- 削除の境界。1行だけ。`pruned_*` を書くのは `coldaisle-rollup` だけ。
-- `legacy_until_ms` は移行前の削除で欠けたかもしれない範囲の上限で、ここで1回だけ書く。
-- 移行前の削除がどこまで消したかは記録が無いため、移行前の期間はまとめて
-- 「欠けているかもしれない」と扱う（0071 §2.2a / §4 R）。
CREATE TABLE control_trace_prune (
    singleton          INTEGER PRIMARY KEY,
    pruned_through_seq INTEGER NOT NULL,   -- 消した行の seq の上限。消していなければ 0
    pruned_before_ms   INTEGER,            -- これより前の ts_ms の行は消したことがある
    legacy_until_ms    INTEGER NOT NULL,   -- 移行前の期間の上限（Unix ms, UTC）
    CHECK (singleton = 1),
    CHECK (pruned_through_seq >= 0),
    CHECK (pruned_before_ms IS NULL OR pruned_before_ms >= 0),
    CHECK (legacy_until_ms >= 0)
);

INSERT INTO control_trace_prune (singleton, pruned_through_seq, pruned_before_ms, legacy_until_ms)
VALUES (1, 0, NULL, MAX(:now_ms, COALESCE((SELECT MAX(ts_ms) FROM control_traces), :now_ms)));

-- 境界は前へ戻さない。戻すと、消えた範囲を「残っている」と返し、古い cursor が 409 にならない。
-- `legacy_until_ms` を運用者が後ろへずらすのは安全側なので許す（0071 §2.2a）
CREATE TRIGGER control_trace_prune_no_rewind
BEFORE UPDATE ON control_trace_prune
WHEN NEW.singleton IS NOT OLD.singleton
  OR NEW.pruned_through_seq < OLD.pruned_through_seq
  OR (OLD.pruned_before_ms IS NOT NULL
      AND (NEW.pruned_before_ms IS NULL OR NEW.pruned_before_ms < OLD.pruned_before_ms))
  OR NEW.legacy_until_ms < OLD.legacy_until_ms
BEGIN
    SELECT RAISE(ABORT, 'control trace prune boundary cannot move backwards');
END;

CREATE TRIGGER control_trace_prune_no_delete
BEFORE DELETE ON control_trace_prune
BEGIN
    SELECT RAISE(ABORT, 'control trace prune boundary cannot be deleted');
END;

-- `NOT NULL UNIQUE` の列は ALTER TABLE ADD COLUMN で足せないので、表を作り直して移す。
-- 既存の行には `(ts_ms, tick_id)` の順に 1 から振る（移行前の記録の順は残っていない）
CREATE TABLE control_traces_seq (
    seq            INTEGER NOT NULL UNIQUE,
    ts_ms          INTEGER NOT NULL,
    tick_id        INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    trace_json     TEXT    NOT NULL,
    PRIMARY KEY (ts_ms, tick_id),
    CHECK (seq >= 1),
    CHECK (ts_ms >= 0),
    CHECK (tick_id >= 0),
    CHECK (schema_version >= 1),
    CHECK (json_valid(trace_json)),
    CHECK (json_type(trace_json) = 'object')
) WITHOUT ROWID;

INSERT INTO control_traces_seq (seq, ts_ms, tick_id, schema_version, trace_json)
SELECT ROW_NUMBER() OVER (ORDER BY ts_ms, tick_id), ts_ms, tick_id, schema_version, trace_json
FROM control_traces;

DROP INDEX ix_control_traces_tick;
DROP TABLE control_traces;
ALTER TABLE control_traces_seq RENAME TO control_traces;
CREATE INDEX ix_control_traces_tick ON control_traces (tick_id, ts_ms);
