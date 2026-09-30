-- 0009_control_admin_audit_authority: #92 管理ソケットの authority の降格。決定記録 0072 §2.2 / §2.7 / §2.10 段階 2。
--
-- 0008 は `superseded_by > command_id`（置き換えるのは後から受け付けた指令だけ）を CHECK にした。
-- これはモードの枠（command_id が最大の1件を採る）の規則である。authority の枠は置き換えずに
-- **最も低い行き先を採る**（0072 §2.2）ので、後から届いた浅い降格は、先に届いてまだ枠にある
-- 深い降格に置き換えられる（`superseded_by < command_id`）。その行を CHECK が拒むと、採られなかった
-- 指令を `superseded` として監査に残せない（0072 §2.7）。
--
-- CHECK は ALTER TABLE で変えられないので、表を作り直して移す。条件は「自分自身には置き換えられない」
-- だけに緩め、ほかの CHECK・一意制約・追記のみのトリガは 0008 と同じにする。
-- 適用済みの 0008 は書き換えない（決定記録 0002 §2.11「追記のみ」）。
-- DROP TABLE は DELETE のトリガを起動しない（旧表のトリガは表と一緒に消え、下で作り直す）。

CREATE TABLE control_admin_audit_v2 (
    id             INTEGER PRIMARY KEY,
    run_id         TEXT    NOT NULL,   -- coldaisle-fand の起動ごとの識別子（32桁の16進）
    command_id     INTEGER NOT NULL,   -- 受付スレッドが採番した、起動の中で単調に増える番号
    event          TEXT    NOT NULL,   -- 'accepted' / 'applied' / 'superseded' / 'lease_expired'
    ts_ms          INTEGER NOT NULL,   -- 記録時刻 (Unix ms, UTC。coldaisle-fand の壁時計)。期限の計算には使わない
    peer_uid       INTEGER,            -- accepted だけ。接続の uid（SO_PEERCRED）
    op             TEXT,               -- accepted だけ。'set_mode' / 'lower_authority' / 'rollback_authority'
    body           TEXT,               -- accepted だけ。検証済みの本文の JSON
    tick_id        INTEGER,            -- applied / lease_expired だけ
    superseded_by  INTEGER,            -- superseded だけ。採られた指令の command_id
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
    CHECK (superseded_by IS NULL OR (superseded_by >= 1 AND superseded_by <> command_id))
);

INSERT INTO control_admin_audit_v2
    (id, run_id, command_id, event, ts_ms, peer_uid, op, body, tick_id, superseded_by)
SELECT id, run_id, command_id, event, ts_ms, peer_uid, op, body, tick_id, superseded_by
FROM control_admin_audit;

DROP TABLE control_admin_audit;

ALTER TABLE control_admin_audit_v2 RENAME TO control_admin_audit;

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
