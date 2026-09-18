-- 0004_dataset_source_run_complete: #83 のdataset Replayが入力を最後まで取り込んだ印。
--
-- dataset_source_run は取り込み開始前に入力全体のSHA-256をbindする。途中で停止した
-- runではDBが入力の先頭だけになるため、EOFまで取り込めたときだけ1回INSERTする。
-- builderはこの行が無いDBを拒否する。0003と同じくsingletonで上書き不能にする。

CREATE TABLE dataset_source_run_complete (
    singleton    INTEGER PRIMARY KEY,
    completed_ms INTEGER NOT NULL,
    CHECK (singleton = 1),
    CHECK (completed_ms >= 0)
) WITHOUT ROWID;

CREATE TRIGGER dataset_source_run_complete_requires_bind
BEFORE INSERT ON dataset_source_run_complete
WHEN NOT EXISTS (SELECT 1 FROM dataset_source_run)
BEGIN
    SELECT RAISE(ABORT, 'dataset source run is not bound');
END;

CREATE TRIGGER dataset_source_run_complete_no_second_insert
BEFORE INSERT ON dataset_source_run_complete
WHEN EXISTS (SELECT 1 FROM dataset_source_run_complete)
BEGIN
    SELECT RAISE(ABORT, 'dataset source run is already complete');
END;

CREATE TRIGGER dataset_source_run_complete_no_update
BEFORE UPDATE ON dataset_source_run_complete
BEGIN
    SELECT RAISE(ABORT, 'dataset source run completion is immutable');
END;

CREATE TRIGGER dataset_source_run_complete_no_delete
BEFORE DELETE ON dataset_source_run_complete
BEGIN
    SELECT RAISE(ABORT, 'dataset source run completion is immutable');
END;
