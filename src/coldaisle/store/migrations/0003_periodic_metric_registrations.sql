-- 0003_periodic_metric_registrations: #65 周期メトリクスの登録状態（決定記録 0038）。
--
-- ロールアップは周期メトリクス（Internal Telemetry など）の欠測を0行バケットで埋める。
-- 設定で有効な間の停止は欠測だが、無効にしていた期間は欠測ではない。
-- 有効・無効はロールアップの実行時に渡される登録からしか分からず、readings からは
-- 推測できない（全 source が静かな期間は痕跡が残らない）ため、明示的に保存する。
--
-- registered     : 直近のロールアップで登録されていたか（1 / 0）
-- interval_ms    : 登録時の収集周期。登録が外れた実行で、外れる前の停止を埋めるのに使う
-- since_ms       : 登録が再開した（または初めて登録された）区間の探索下限。
--                  この時刻以降の最初の観測から欠測を数える
-- active_from_ms : 欠測を数え始める分。再開後にまだ観測が無ければ NULL
-- changed_ms     : registered を最後に変えたロールアップの時刻

CREATE TABLE periodic_metric_registrations (
    metric         TEXT    PRIMARY KEY,
    registered     INTEGER NOT NULL,
    interval_ms    INTEGER NOT NULL,
    since_ms       INTEGER NOT NULL,
    active_from_ms INTEGER,
    changed_ms     INTEGER NOT NULL,
    CHECK (registered IN (0, 1)),
    CHECK (interval_ms > 0),
    CHECK (since_ms >= 0),
    CHECK (active_from_ms IS NULL OR active_from_ms >= 0)
) WITHOUT ROWID;
