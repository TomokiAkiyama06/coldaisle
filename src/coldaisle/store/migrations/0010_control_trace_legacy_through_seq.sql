-- 0010_control_trace_legacy_through_seq: #83 Thermal Dataset v2。決定記録 0087 §2.1 / §5 #12。
--
-- 移行前の行（`seq` を 0007 が `(ts_ms, tick_id)` の順に振った行）を、時刻ではなく `seq` で見分ける。
-- `legacy_until_ms` は空の DB でもストアの時計（`:now_ms`）で書かれるので、`SimulatedClock` が進む前に
-- 記録した正しい tick がその時刻と等しくなり、時刻では移行前の行と区別できない（0087 §2.1）。
--
-- 列を1つ足すだけで、`control_traces` の表と `legacy_until_ms` の意味は変えない（0087 §2.8）。
-- 適用済みの 0007 は書き換えない（決定記録 0002 §2.11「追記のみ」）。

-- 移行前の行の `seq` の上限。移行前の行が無ければ 0
ALTER TABLE control_trace_prune
    ADD COLUMN legacy_through_seq INTEGER NOT NULL DEFAULT 0 CHECK (legacy_through_seq >= 0);

-- 行が無い DB（0007 と同時に適用する新しい DB を含む）では 0 のまま。
-- 0007 を適用済みの DB では `ts_ms ≤ legacy_until_ms` の行の `MAX(seq)` で埋める。0007 の後に同じ時刻で
-- 記録された行を含みうるが、狭める向き（Dataset v2 の拒否が増える向き）にだけずれる安全側の上限である
UPDATE control_trace_prune
SET legacy_through_seq = COALESCE(
    (SELECT MAX(seq) FROM control_traces
     WHERE control_traces.ts_ms <= control_trace_prune.legacy_until_ms),
    0
);
