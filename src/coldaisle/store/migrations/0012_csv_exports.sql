-- 0012_csv_exports: #237 日次 CSV の export の記録。決定記録 0100 §2.1 / §2.6。
--
-- `coldaisle-rollup --export-day` が1回の export ごとに、CSV の横の manifest
-- （`sensors_YYYY-MM-DD.export.json`）と同じ内容の行を、readings を読んだのと同じ DB に1行足す。
-- 書き手は export だけ（0100 §2.6 / §5 #12）。Dataset v2 の生成と学習の入口が、この行と manifest を
-- 全欄で照合して「この CSV はこの DB から export された」ことを確かめる（段 3）。
--
-- **追記のみ**を規約ではなく制約として持つ。更新・削除はトリガで拒否する。同じ日を書き直しても
-- 古い行は消さない（新しい export は別の export_id の行になる）。保持期間の削除（0008）の
-- 対象にしない。過去の export の行は埋めない（0100 §2.7）。
-- manifest の schema / schema_version（ファイルの形の定数）は列に持たない。この表の形が版 1 に当たる。

CREATE TABLE csv_exports (
    export_id          TEXT    PRIMARY KEY,  -- 'export-<32 hex>'。乱数で、機器の情報を含まない
    csv_name           TEXT    NOT NULL,     -- 対の CSV の basename だけ（'sensors_YYYY-MM-DD.csv'）
    csv_sha256         TEXT    NOT NULL,     -- 対の CSV の bytes の SHA-256
    day                TEXT    NOT NULL,     -- 'YYYY-MM-DD'（ローカルの日）
    timezone           TEXT    NOT NULL,     -- export に使った IANA の timezone 名（設定の文字列のまま）
    day_start_ms       INTEGER NOT NULL,     -- その日の開始 (Unix ms, UTC。含む)
    day_end_ms         INTEGER NOT NULL,     -- その日の終了 (Unix ms, UTC。含まない)
    timestamp_format   TEXT    NOT NULL,     -- CSV の時刻の書式
    row_count          INTEGER NOT NULL,     -- CSV のデータ行の数
    row_seconds_sha256 TEXT    NOT NULL,     -- 各行の絶対時刻（UTC の Unix 秒）の列の SHA-256
    exported_ms        INTEGER NOT NULL,     -- 行を書いた時刻 (Unix ms, UTC)
    CHECK (typeof(export_id) = 'text' AND length(export_id) = 39),
    CHECK (export_id GLOB 'export-*' AND substr(export_id, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(day) = 'text' AND day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (csv_name = 'sensors_' || day || '.csv'),
    CHECK (length(csv_sha256) = 64 AND csv_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(timezone) = 'text' AND length(timezone) > 0),
    CHECK (typeof(day_start_ms) = 'integer' AND typeof(day_end_ms) = 'integer'),
    CHECK (day_start_ms >= 0 AND day_start_ms < day_end_ms),
    CHECK (typeof(timestamp_format) = 'text' AND length(timestamp_format) > 0),
    CHECK (typeof(row_count) = 'integer' AND row_count >= 0),
    CHECK (length(row_seconds_sha256) = 64 AND row_seconds_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(exported_ms) = 'integer' AND exported_ms >= 0)
);

CREATE INDEX ix_csv_exports_day ON csv_exports (day);

CREATE TRIGGER csv_exports_no_update BEFORE UPDATE ON csv_exports
BEGIN
    SELECT RAISE(ABORT, 'csv_exports is append-only');
END;

CREATE TRIGGER csv_exports_no_delete BEFORE DELETE ON csv_exports
BEGIN
    SELECT RAISE(ABORT, 'csv_exports is append-only');
END;
