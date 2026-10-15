-- 0015_workload_hint_state: #318 Workload Hint の終端状態・run の記録・取り込みの境界。
-- 決定記録 0066 §2.2（0133 §2.4 の失効の事由を含む）。
--
-- 書き手は `coldaisle-fand`（合成の起点）だけで、`events` とは別の接続で**自分の表にだけ**書く。
-- `events` は読み取り専用のまま（書き込みの入口は 0045 のソケットだけ）。
-- 4つの表はどれも**追記のみ**を制約として持つ（更新・削除はトリガで拒否）。保持期間の削除の対象にしない。
-- 時刻の欄（ts_ms）は記録のためだけに持ち、順序にも判定にも使わない。順序は追記順の番号で決める。

-- run の開始記録。run の順序は時計に依存しない追記順の番号（run_seq）で決める。
-- AUTOINCREMENT: 番号を再利用しない・巻き戻さない（最大の行が消えても次の番号は戻らない）
CREATE TABLE workload_hint_runs (
    run_seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT    NOT NULL UNIQUE,  -- fand の起動ごとの識別子（0072 §2.7 の 32 桁の16進）
    started_ts_ms       INTEGER NOT NULL,         -- 記録のみ（壁時計）
    boot_id             TEXT    NOT NULL,
    started_boottime_ns INTEGER NOT NULL,
    CHECK (length(run_id) = 32 AND run_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(started_ts_ms) = 'integer' AND started_ts_ms >= 0),
    CHECK (length(boot_id) = 36),
    CHECK (typeof(started_boottime_ns) = 'integer' AND started_boottime_ns >= 0)
);

-- run の終了記録。1つの run に高々1行（主キー）。終端状態の書き込みに1回でも失敗した run は
-- 'incomplete'（欠けあり）で残す。行が無い run は異常終了と扱う
CREATE TABLE workload_hint_run_ends (
    run_seq           INTEGER PRIMARY KEY,
    status            TEXT    NOT NULL,
    ended_ts_ms       INTEGER NOT NULL,           -- 記録のみ（壁時計）
    boot_id           TEXT    NOT NULL,
    ended_boottime_ns INTEGER NOT NULL,
    CHECK (status IN ('clean', 'incomplete')),
    CHECK (typeof(ended_ts_ms) = 'integer' AND ended_ts_ms >= 0),
    CHECK (length(boot_id) = 36),
    CHECK (typeof(ended_boottime_ns) = 'integer' AND ended_boottime_ns >= 0)
);

-- 取り込みの境界。直前の run が正常に終わったと確かめられない起動で、その時点の events.id の最大値を
-- run の開始記録と同じトランザクションで足す。実効の境界は全行の最大値で、増えるだけで戻さない
CREATE TABLE workload_hint_backfill_boundaries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_seq             INTEGER NOT NULL UNIQUE,  -- 境界を作った run
    previous_run_seq    INTEGER,                  -- 確かめられなかった直前の run（無ければ NULL）
    previous_run_state  TEXT    NOT NULL,         -- 'none' / 'abnormal' / 'incomplete'
    through_event_id    INTEGER NOT NULL,         -- これ以下の event_id のヒントは取り込まない
    CHECK (previous_run_state IN ('none', 'abnormal', 'incomplete')),
    CHECK ((previous_run_seq IS NULL) = (previous_run_state = 'none')),
    CHECK (typeof(through_event_id) = 'integer' AND through_event_id >= 0)
);

-- ヒントの終端状態。event_id ごとに高々1行（失効は一方向。同じ event_id は再び active にならない）
CREATE TABLE workload_hint_terminal_states (
    event_id    INTEGER PRIMARY KEY,
    state       TEXT    NOT NULL,
    run_seq     INTEGER NOT NULL,                 -- 書いた run
    ts_ms       INTEGER NOT NULL,                 -- 記録のみ（壁時計）
    boot_id     TEXT    NOT NULL,
    boottime_ns INTEGER NOT NULL,
    CHECK (state IN ('contradicted', 'expired', 'ended', 'superseded', 'unsupported_version')),
    CHECK (typeof(ts_ms) = 'integer' AND ts_ms >= 0),
    CHECK (length(boot_id) = 36),
    CHECK (typeof(boottime_ns) = 'integer' AND boottime_ns >= 0)
);

-- 参照先の存在はトリガで確かめる（接続ごとの PRAGMA foreign_keys に依らない）
CREATE TRIGGER workload_hint_run_ends_refs BEFORE INSERT ON workload_hint_run_ends
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_run_ends: run_seq must refer to a started run')
    WHERE NOT EXISTS (SELECT 1 FROM workload_hint_runs WHERE run_seq = NEW.run_seq);
END;

CREATE TRIGGER workload_hint_backfill_boundaries_refs
BEFORE INSERT ON workload_hint_backfill_boundaries
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_backfill_boundaries: run_seq must refer to a started run')
    WHERE NOT EXISTS (SELECT 1 FROM workload_hint_runs WHERE run_seq = NEW.run_seq);
    SELECT RAISE(ABORT, 'workload_hint_backfill_boundaries: previous_run_seq must precede run_seq')
    WHERE NEW.previous_run_seq IS NOT NULL AND (
        NEW.previous_run_seq >= NEW.run_seq
        OR NOT EXISTS (SELECT 1 FROM workload_hint_runs WHERE run_seq = NEW.previous_run_seq)
    );
    SELECT RAISE(ABORT, 'workload_hint_backfill_boundaries: the boundary only increases')
    WHERE NEW.through_event_id <
        (SELECT MAX(through_event_id) FROM workload_hint_backfill_boundaries);
END;

CREATE TRIGGER workload_hint_terminal_states_refs BEFORE INSERT ON workload_hint_terminal_states
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_terminal_states: event_id must refer to an event')
    WHERE NOT EXISTS (SELECT 1 FROM events WHERE id = NEW.event_id);
    SELECT RAISE(ABORT, 'workload_hint_terminal_states: run_seq must refer to a started run')
    WHERE NOT EXISTS (SELECT 1 FROM workload_hint_runs WHERE run_seq = NEW.run_seq);
END;

CREATE TRIGGER workload_hint_runs_no_update BEFORE UPDATE ON workload_hint_runs
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_runs is append-only');
END;

CREATE TRIGGER workload_hint_runs_no_delete BEFORE DELETE ON workload_hint_runs
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_runs is append-only');
END;

CREATE TRIGGER workload_hint_run_ends_no_update BEFORE UPDATE ON workload_hint_run_ends
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_run_ends is append-only');
END;

CREATE TRIGGER workload_hint_run_ends_no_delete BEFORE DELETE ON workload_hint_run_ends
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_run_ends is append-only');
END;

CREATE TRIGGER workload_hint_backfill_boundaries_no_update
BEFORE UPDATE ON workload_hint_backfill_boundaries
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_backfill_boundaries is append-only');
END;

CREATE TRIGGER workload_hint_backfill_boundaries_no_delete
BEFORE DELETE ON workload_hint_backfill_boundaries
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_backfill_boundaries is append-only');
END;

CREATE TRIGGER workload_hint_terminal_states_no_update
BEFORE UPDATE ON workload_hint_terminal_states
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_terminal_states is append-only');
END;

CREATE TRIGGER workload_hint_terminal_states_no_delete
BEFORE DELETE ON workload_hint_terminal_states
BEGIN
    SELECT RAISE(ABORT, 'workload_hint_terminal_states is append-only');
END;
