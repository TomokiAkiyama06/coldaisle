-- 0017_hint_shadow_records_epoch: #288 Workload Hint B1 の fand の書き手（決定記録 0162 §2.5 の PR-C）。
--
-- `hint_shadow_records` のすべての行に、そのメッセージを送った worker のプロセスの epoch
-- （`worker_instance_id`。`run_id` と同じ形の乱数の識別子）と、メッセージが運んだ**累積の数**
-- （`shadow_dropped_backpressure` / `shadow_record_too_large` / `shadow_unavailable`）を残す
-- （0162 §2.3）。`recorded` 以外の行は `record_json` を持たないので、行そのものに epoch と数が無いと、
-- `skipped` / `rejected` の行を集計（0162 §2.4）の epoch と区間に振り分けられない。
--
-- 列は**必須**（NOT NULL）。ALTER TABLE ADD COLUMN では既定値なしの NOT NULL を足せず、
-- 仮の既定値（空文字・0）を置くと「値が無い」と「0 件」を取り違える行を作りうるので、表を作り直す。
-- **0016 の表には書き手がいなかった**（書き手はこの migration と同じ PR で足す。0016 の注記）ので、
-- 旧表の行は写さない。推測で `worker_instance_id` や数を埋めた行を作らない（0162 §2.3「既存の行を
-- 推測で埋める必要は無い」）。適用済みの 0016 は書き換えない（決定記録 0002 §2.11「追記のみ」）。
-- DROP TABLE は旧表のトリガ・索引も一緒に消すので、下で作り直す。

DROP TABLE hint_shadow_records;

CREATE TABLE hint_shadow_records (
    -- 追記順の番号。AUTOINCREMENT で、保持で消した番号も再利用しない（巻き戻りなし）
    seq                         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      TEXT    NOT NULL,  -- fand の起動ごとの識別子（32桁の16進。経路の封筒が運ぶもの。0077）
    source_tick_id              INTEGER NOT NULL,  -- ヒント有りの solve の元になった frame の snapshot の tick_id
    source_ts_ms                INTEGER NOT NULL,  -- 同じ snapshot の ts_ms（壁時計。Unix ms, UTC）
    source_monotonic_ms         INTEGER NOT NULL,  -- 同じ snapshot の monotonic_ms（鮮度と突き合わせの検算）
    received_monotonic_ms       INTEGER NOT NULL,  -- fand が受け取った時刻（単調時計）
    received_ts_ms              INTEGER NOT NULL,  -- fand が受け取った時刻（壁時計。Unix ms, UTC）
    schema_version              INTEGER NOT NULL,  -- この記録の形の版
    status                      TEXT    NOT NULL,  -- 'recorded' / 'skipped' / 'rejected'
    reason                      TEXT,              -- status が recorded 以外のときだけ。閉じた語彙（検証は書き手）
    record_json                 TEXT,              -- status が recorded のときだけ。両 plan・再検査の結果など
    worker_instance_id          TEXT    NOT NULL,  -- worker のプロセスの起動ごとの識別子（32桁の16進。0162 §2.3）
    shadow_dropped_backpressure INTEGER NOT NULL,  -- メッセージが運んだ累積の数（その epoch の中で増える向き）
    shadow_record_too_large     INTEGER NOT NULL,
    shadow_unavailable          INTEGER NOT NULL,
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
    CHECK (record_json IS NULL OR (json_valid(record_json) AND json_type(record_json) = 'object')),
    CHECK (length(worker_instance_id) = 32 AND worker_instance_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (shadow_dropped_backpressure >= 0),
    CHECK (shadow_record_too_large >= 0),
    CHECK (shadow_unavailable >= 0)
);

-- 突き合わせ（`control_traces` の主キーと同じ並び）と、保持期間の削除（`source_ts_ms` の範囲）の両方に使う
CREATE INDEX ix_hint_shadow_records_source ON hint_shadow_records (source_ts_ms, source_tick_id);

-- **追記専用**を規約ではなく制約として持つ。UPDATE はトリガで拒む。
-- DELETE は保持期間の適用（`coldaisle-rollup`）だけが行う（`control_traces` と同じ扱い。0030）
CREATE TRIGGER hint_shadow_records_no_update BEFORE UPDATE ON hint_shadow_records
BEGIN
    SELECT RAISE(ABORT, 'hint_shadow_records is append-only');
END;
