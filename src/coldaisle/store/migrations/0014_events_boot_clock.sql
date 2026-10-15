-- 0014_events_boot_clock: #318 events に書いた時点の boot id と CLOCK_BOOTTIME を持たせる。
-- 決定記録 0066 §2.2（0045 §2.5 の列の定義を限定的に置き換える）。
--
-- Workload Hint の期限と起動時の取り込みの経過を、壁時計ではなく同じ boot の中の
-- CLOCK_BOOTTIME の差で数えるため（壁時計は戻り、CLOCK_MONOTONIC は suspend 中に進まない）。
--
-- **既存の行は NULL のまま残す。** 現在の boot id や既定値で埋めない（移行前のヒントを
-- 確かめられる行として再び採ってしまうため）。移行後に `coldaisle-eventd` が書く行は両方を必ず入れる。
-- 2つは組で、片方だけの行を作らない。ALTER TABLE は既存の UPDATE / DELETE の拒否のトリガを発火させない。

ALTER TABLE events ADD COLUMN boot_id TEXT
    CHECK (boot_id IS NULL OR length(boot_id) = 36);

ALTER TABLE events ADD COLUMN boottime_ns INTEGER
    CHECK (boottime_ns IS NULL OR (typeof(boottime_ns) = 'integer' AND boottime_ns >= 0))
    CHECK ((boot_id IS NULL) = (boottime_ns IS NULL));
