-- 0006_events: #67 外部から届いた事象（GPU Mode の切り替えなど）。決定記録 0045 §2.5。
--
-- 書き込みの入口は `coldaisle-eventd`（Unix ソケット）だけ。読み取り API は書かない。
-- **追記のみ**を規約ではなく制約として持つ。更新・削除はトリガで拒否する。
-- `kind` の許可リストはコードが持つ（種類を足すたびに表を作り直さないため）。

CREATE TABLE events (
    id        INTEGER PRIMARY KEY,
    ts_ms     INTEGER NOT NULL,   -- ホスト受信時刻 (Unix ms, UTC)。書き手は時刻を渡せない
    kind      TEXT    NOT NULL,   -- 'gpu_mode'
    payload   TEXT    NOT NULL,   -- 検証済みメッセージの JSON（v / type を含む）
    peer_uid  INTEGER,            -- 書き込んだ接続の uid（SO_PEERCRED）
    CHECK (ts_ms >= 0),
    CHECK (kind GLOB '[a-z]*' AND kind NOT GLOB '*[^a-z0-9_]*'),
    CHECK (json_valid(payload)),
    CHECK (json_type(payload) = 'object')
);

CREATE INDEX ix_events_ts ON events (ts_ms);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events is append-only');
END;
