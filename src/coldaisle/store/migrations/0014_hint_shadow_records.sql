-- 0014_hint_shadow_records: #288 Workload Hint Stage B の B1 で、ヒント有りの solve の記録を残す表。
-- 決定記録 0133 §2.5a。
--
-- ヒント有りの記録は基準の `MpcProposal` より後に届き、Gate も評価しないので、不変の
-- `ControlTick` に埋め込む 0053 の Shadow の台帳には書けない。**0053 は変えず**、別の追記専用の表にする。
--
-- 書くのは `coldaisle-fand` だけ（0066 §2.2 の「制御デーモンが自分の表にだけ書く別の接続」）。
-- `events` には書かない。読み取り API・AI ツール・Thermal Dataset には出さない（0133 §2.5a「公開しない」）。
--
-- **この migration の時点では書き手がいない。** 書き手（fand の tick の外の書き手）と記録を運ぶ経路は
-- B1 の実装で足す。表と保持と突き合わせの契約を先に固定する。
--
-- 突き合わせ（join の契約。0133 §2.5a）: 1行は `control_traces` の主キー
-- `(ts_ms, tick_id) = (source_ts_ms, source_tick_id)` の行と、その trace の最上位の `run_id`
-- （ControlTick v16。0133 §2.9）が `run_id` と等しいときだけ対応する。`ts_ms` だけ・`tick_id` だけでは
-- 突き合わせない（再起動で `tick_id` が戻り、壁時計は飛ぶため）。

CREATE TABLE hint_shadow_records (
    -- 追記順の番号。AUTOINCREMENT で、保持で消した番号も再利用しない（巻き戻りなし）
    seq                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                TEXT    NOT NULL,  -- fand の起動ごとの識別子（32桁の16進。経路の封筒が運ぶもの。0077）
    source_tick_id        INTEGER NOT NULL,  -- ヒント有りの solve の元になった frame の snapshot の tick_id
    source_ts_ms          INTEGER NOT NULL,  -- 同じ snapshot の ts_ms（壁時計。Unix ms, UTC）
    source_monotonic_ms   INTEGER NOT NULL,  -- 同じ snapshot の monotonic_ms（鮮度と突き合わせの検算）
    received_monotonic_ms INTEGER NOT NULL,  -- fand が受け取った時刻（単調時計）
    received_ts_ms        INTEGER NOT NULL,  -- fand が受け取った時刻（壁時計。Unix ms, UTC）
    schema_version        INTEGER NOT NULL,  -- この記録の形の版
    status                TEXT    NOT NULL,  -- 'recorded' / 'skipped' / 'rejected'
    reason                TEXT,              -- status が recorded 以外のときだけ。閉じた語彙（検証は書き手）
    record_json           TEXT,              -- status が recorded のときだけ。両 plan・再検査の結果など
    CHECK (length(run_id) = 32 AND run_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (source_tick_id >= 0),
    CHECK (source_ts_ms >= 0),
    CHECK (source_monotonic_ms >= 0),
    CHECK (received_monotonic_ms >= 0),
    CHECK (received_ts_ms >= 0),
    CHECK (schema_version >= 1),
    CHECK (status IN ('recorded', 'skipped', 'rejected')),
    CHECK ((status = 'recorded') = (reason IS NULL)),
    CHECK (reason IS NULL OR (length(reason) BETWEEN 1 AND 64 AND reason NOT GLOB '*[^a-z0-9_]*')),
    CHECK ((status = 'recorded') = (record_json IS NOT NULL)),
    CHECK (record_json IS NULL OR (json_valid(record_json) AND json_type(record_json) = 'object'))
);

-- 突き合わせ（`control_traces` の主キーと同じ並び）と、保持期間の削除（`source_ts_ms` の範囲）の両方に使う
CREATE INDEX ix_hint_shadow_records_source ON hint_shadow_records (source_ts_ms, source_tick_id);

-- **追記専用**を規約ではなく制約として持つ。UPDATE はトリガで拒む。
-- DELETE は保持期間の適用（`coldaisle-rollup`）だけが行う（`control_traces` と同じ扱い。0030）
CREATE TRIGGER hint_shadow_records_no_update BEFORE UPDATE ON hint_shadow_records
BEGIN
    SELECT RAISE(ABORT, 'hint_shadow_records is append-only');
END;
