-- 0013_dataset_source_run_export_binding: #237 dataset 用の再生で照合した export の束縛。
-- 決定記録 0100 §2.3 / §2.8 / §2.10（段 2）。
--
-- dataset 用の再生（`coldaisle-daemon --source replay --dataset-run-alias`）が bind と同じ
-- transaction で、再生に使った timezone（入力の manifest の timezone）と、入力の export の束縛の
-- digest（`export_binding_sha256`）を記録する。manifest の無い入力の run は「照合していない」run で、
-- 両方 `NULL`（v1 の builder は受け入れ、v2 の builder と学習の入口は拒否する。段 3）。
--
-- 既存の行（この migration の前に bind した run）は両方 `NULL` のまま残る。後から推測で埋めない
-- （0100 §2.7 / §2.10）。行の不変（UPDATE / DELETE の拒否）は 0004 の trigger がそのまま守る。

ALTER TABLE dataset_source_run ADD COLUMN local_timezone TEXT
    CHECK (local_timezone IS NULL OR (typeof(local_timezone) = 'text' AND length(local_timezone) > 0));

ALTER TABLE dataset_source_run ADD COLUMN export_binding_sha256 TEXT
    CHECK (
        export_binding_sha256 IS NULL
        OR (length(export_binding_sha256) = 64 AND export_binding_sha256 NOT GLOB '*[^0-9a-f]*')
    );

-- 2つは組で、片方だけの行を作らない（照合した run は両方を持ち、照合していない run は両方 NULL）
CREATE TRIGGER dataset_source_run_export_binding_pair
BEFORE INSERT ON dataset_source_run
WHEN (NEW.local_timezone IS NULL) <> (NEW.export_binding_sha256 IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'dataset source run: local_timezone and export_binding_sha256 go together');
END;
