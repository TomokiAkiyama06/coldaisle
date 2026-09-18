-- 0005_dataset_source_run_complete: #83 のdataset Replayが入力を最後まで取り込んだ印。
--
-- dataset_source_run は取り込み開始前に入力全体のSHA-256をbindする。途中で停止した
-- runやsampleを捨てたrunではDBが入力の一部になるため、EOFまで欠けなく取り込めたときだけ
-- 1回INSERTする。
-- builderはこの行が無いDBを拒否する。0004と同じくsingletonで上書き不能にする。

-- 完了時のreadings件数とSHA-256を封印として持ち、builderは再計算して一致を要求する。
-- さらに完了後のreadingsへのINSERT / UPDATE / DELETEをtriggerで拒否し、別のwriter
-- （coldaisle-telemetryの追記、coldaisle-rollupのretention削除）を黙って通さず失敗させる。

CREATE TABLE dataset_source_run_complete (
    singleton       INTEGER PRIMARY KEY,
    completed_ms    INTEGER NOT NULL,
    readings_count  INTEGER NOT NULL,
    readings_sha256 TEXT    NOT NULL,
    CHECK (singleton = 1),
    CHECK (completed_ms >= 0),
    CHECK (readings_count >= 0),
    CHECK (length(readings_sha256) = 64)
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

CREATE TRIGGER readings_sealed_no_insert
BEFORE INSERT ON readings
WHEN EXISTS (SELECT 1 FROM dataset_source_run_complete)
BEGIN
    SELECT RAISE(ABORT, 'dataset readings are sealed');
END;

CREATE TRIGGER readings_sealed_no_update
BEFORE UPDATE ON readings
WHEN EXISTS (SELECT 1 FROM dataset_source_run_complete)
BEGIN
    SELECT RAISE(ABORT, 'dataset readings are sealed');
END;

CREATE TRIGGER readings_sealed_no_delete
BEFORE DELETE ON readings
WHEN EXISTS (SELECT 1 FROM dataset_source_run_complete)
BEGIN
    SELECT RAISE(ABORT, 'dataset readings are sealed');
END;
