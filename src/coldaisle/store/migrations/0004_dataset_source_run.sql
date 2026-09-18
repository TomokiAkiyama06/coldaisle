-- 0004_dataset_source_run: #83 の1 run専用dataset DB provenance。
--
-- singleton=1 の1行だけを INSERT し、UPDATE / REPLACE はStore APIから提供しない。
-- 同じ開始時刻の別Replayが system_state を置換し、旧readingへ新しいsource hashを
-- 結び付けることを防ぐ。別runには新しいDBを使う。

CREATE TABLE dataset_source_run (
    singleton     INTEGER PRIMARY KEY,
    run_alias     TEXT    NOT NULL,
    source_kind   TEXT    NOT NULL,
    source_sha256 TEXT    NOT NULL,
    bound_ms      INTEGER NOT NULL,
    CHECK (singleton = 1),
    CHECK (source_kind IN ('serial', 'replay', 'mock', 'import')),
    CHECK (length(source_sha256) = 64),
    CHECK (bound_ms >= 0)
) WITHOUT ROWID;

CREATE TRIGGER dataset_source_run_no_second_insert
BEFORE INSERT ON dataset_source_run
WHEN EXISTS (SELECT 1 FROM dataset_source_run)
BEGIN
    SELECT RAISE(ABORT, 'dataset source run is already bound');
END;

CREATE TRIGGER dataset_source_run_no_update
BEFORE UPDATE ON dataset_source_run
BEGIN
    SELECT RAISE(ABORT, 'dataset source run is immutable');
END;

CREATE TRIGGER dataset_source_run_no_delete
BEFORE DELETE ON dataset_source_run
BEGIN
    SELECT RAISE(ABORT, 'dataset source run is immutable');
END;
