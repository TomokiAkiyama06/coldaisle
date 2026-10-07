-- 0011_calibration_activations: #233 較正の変更の記録先。決定記録 0099 §2.1 / §2.4 / §2.5。
--
-- 取り込み（`coldaisle-daemon` の serial / mock）が、新しい較正で起動した時点を1行残す。
-- 行は実効の offset の写像（0099 §2.3）が**変わったときだけ**足す。書き手は取り込みだけで、
-- `coldaisle-calibrate --apply`・API・AI・control は書かない（0099 §2.2）。
--
-- **追記のみ**を規約ではなく制約として持つ。更新・削除はトリガで拒否し、INSERT は
-- 時刻の単調・直前の行への鎖・写像の変化をトリガで検査する。`row_sha256` 自体の計算は
-- SQLite では検査せず、読む側（`store/calibration_history.py`）が全行を検証する（0099 §2.5）。
-- 過去の履歴は埋めない（0099 §2.8）。保持期間の削除（0008）の対象にしない。

CREATE TABLE calibration_activations (
    id                      INTEGER PRIMARY KEY,
    ts_ms                   INTEGER NOT NULL,   -- 取り込みが新しい較正で起動した時刻 (Unix ms, UTC)
    source_kind             TEXT    NOT NULL,   -- 'serial' / 'mock'（replay は書かない）
    offsets_json            TEXT    NOT NULL,   -- 実効の写像の canonical JSON（末尾の改行を含む）
    offsets_sha256          TEXT    NOT NULL,   -- offsets_json の UTF-8 の bytes の SHA-256
    previous_row_sha256     TEXT,               -- 直前の行の row_sha256。最初の行だけ NULL
    calibrated_at           TEXT,               -- 読んだ較正ファイルの calibrated_at（説明用）
    calibration_file_sha256 TEXT    NOT NULL,   -- 読んだ較正ファイルの bytes の SHA-256（説明用）
    row_sha256              TEXT    NOT NULL,   -- id と自身を除く全欄の canonical JSON の SHA-256
    CHECK (typeof(ts_ms) = 'integer' AND ts_ms >= 0),
    CHECK (source_kind IN ('serial', 'mock')),
    CHECK (typeof(offsets_json) = 'text'),
    CHECK (json_valid(offsets_json) AND json_type(offsets_json) = 'object'),
    CHECK (length(offsets_sha256) = 64 AND offsets_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (
        previous_row_sha256 IS NULL
        OR (length(previous_row_sha256) = 64 AND previous_row_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    CHECK (calibrated_at IS NULL OR typeof(calibrated_at) = 'text'),
    CHECK (
        length(calibration_file_sha256) = 64
        AND calibration_file_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    CHECK (length(row_sha256) = 64 AND row_sha256 NOT GLOB '*[^0-9a-f]*')
);

-- 直前の行は id の最大の行。時刻は直前より厳密に大きく、鎖は直前の行の row_sha256 を指し
-- （表が空なら NULL）、写像は直前の行と違う（同じ値の行を足さない。0099 R2）
CREATE TRIGGER calibration_activations_chain BEFORE INSERT ON calibration_activations
BEGIN
    SELECT RAISE(ABORT, 'calibration_activations: ts_ms must be greater than the previous row')
    WHERE NEW.ts_ms <= (SELECT ts_ms FROM calibration_activations ORDER BY id DESC LIMIT 1);
    SELECT RAISE(ABORT, 'calibration_activations: previous_row_sha256 must chain to the previous row')
    WHERE NEW.previous_row_sha256 IS NOT
        (SELECT row_sha256 FROM calibration_activations ORDER BY id DESC LIMIT 1);
    SELECT RAISE(ABORT, 'calibration_activations: offsets must differ from the previous row')
    WHERE NEW.offsets_sha256 =
        (SELECT offsets_sha256 FROM calibration_activations ORDER BY id DESC LIMIT 1);
    SELECT RAISE(ABORT, 'calibration_activations: id must be greater than the previous row')
    -- 採番を任せた INSERT では BEFORE INSERT の NEW.id は -1 になる
    WHERE NEW.id <> -1 AND NEW.id <= (SELECT MAX(id) FROM calibration_activations);
END;

CREATE TRIGGER calibration_activations_no_update BEFORE UPDATE ON calibration_activations
BEGIN
    SELECT RAISE(ABORT, 'calibration_activations is append-only');
END;

CREATE TRIGGER calibration_activations_no_delete BEFORE DELETE ON calibration_activations
BEGIN
    SELECT RAISE(ABORT, 'calibration_activations is append-only');
END;
