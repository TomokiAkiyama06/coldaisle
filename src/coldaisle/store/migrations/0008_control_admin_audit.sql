-- 0008_control_admin_audit: #74 管理ソケット（control-admin）の指令の監査。決定記録 0072 §2.7。
--
-- 書くのは `coldaisle-fand` の監査書き込みスレッドだけ（自分の表だけを書く別の接続。0066 の前例）。
-- 読み取り API は書かない。**追記のみ**を規約ではなく制約として持つ。更新・削除はトリガで拒否する。
--
-- 1つの指令の経過は、行を書き換えずに「事象の行」を足して表す（0072 §2.7）。
--   accepted      : 受付。受付時刻・peer_uid・op・検証済みの本文
--   applied       : 結末。loop が適用した tick_id
--   superseded    : 結末。置き換えた指令の command_id
--   lease_expired : MANUAL の期限切れ。結末とは別の事象で、判定した tick_id
-- command_id は起動ごとに 1 から振り直すので、一意性は run_id との組で持つ。

CREATE TABLE control_admin_audit (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT    NOT NULL,   -- coldaisle-fand の起動ごとの識別子（32桁の16進）
    command_id     INTEGER NOT NULL,   -- 受付スレッドが採番した、起動の中で単調に増える番号
    event          TEXT    NOT NULL,   -- 'accepted' / 'applied' / 'superseded' / 'lease_expired'
    ts_ms          INTEGER NOT NULL,   -- 記録時刻 (Unix ms, UTC。coldaisle-fand の壁時計)。期限の計算には使わない
    peer_uid       INTEGER,            -- accepted だけ。接続の uid（SO_PEERCRED）
    op             TEXT,               -- accepted だけ。'set_mode' など
    body           TEXT,               -- accepted だけ。検証済みの本文の JSON
    tick_id        INTEGER,            -- applied / lease_expired だけ
    superseded_by  INTEGER,            -- superseded だけ。置き換えた指令の command_id
    CHECK (length(run_id) = 32 AND run_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (command_id >= 1),
    CHECK (ts_ms >= 0),
    CHECK (event IN ('accepted', 'applied', 'superseded', 'lease_expired')),
    CHECK ((event = 'accepted') = (peer_uid IS NOT NULL AND op IS NOT NULL AND body IS NOT NULL)),
    CHECK (event = 'accepted' OR (peer_uid IS NULL AND op IS NULL AND body IS NULL)),
    CHECK (body IS NULL OR (json_valid(body) AND json_type(body) = 'object')),
    CHECK ((event IN ('applied', 'lease_expired')) = (tick_id IS NOT NULL)),
    CHECK (tick_id IS NULL OR tick_id >= 0),
    CHECK ((event = 'superseded') = (superseded_by IS NOT NULL)),
    CHECK (superseded_by IS NULL OR superseded_by > command_id)
);

CREATE UNIQUE INDEX ux_control_admin_audit_accepted
    ON control_admin_audit (run_id, command_id) WHERE event = 'accepted';

-- 1つの command_id に結末の行は高々1つ
CREATE UNIQUE INDEX ux_control_admin_audit_outcome
    ON control_admin_audit (run_id, command_id) WHERE event IN ('applied', 'superseded');

-- lease 切れは結末とは別の一意制約
CREATE UNIQUE INDEX ux_control_admin_audit_lease
    ON control_admin_audit (run_id, command_id) WHERE event = 'lease_expired';

CREATE TRIGGER control_admin_audit_no_update BEFORE UPDATE ON control_admin_audit
BEGIN
    SELECT RAISE(ABORT, 'control_admin_audit is append-only');
END;

CREATE TRIGGER control_admin_audit_no_delete BEFORE DELETE ON control_admin_audit
BEGIN
    SELECT RAISE(ABORT, 'control_admin_audit is append-only');
END;
