# 決定記録 0041: 非同期 RL Supervisor 提案の有効性（期限の起点と Regime の一致）

- **種別**: Decision Record
- **Status**: FINAL（2026-09-18、リポジトリ所有者が承認）
- **Date**: 2026-09-18
- **Supersedes**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) の一部
  （§2.3「層の入出力」表の Supervisor 行の有効期限、§2.6「時計」の Supervisor 出力の期限の起点）
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md)、
  [`0036-transient-cpu-gpu-regime.md`](0036-transient-cpu-gpu-regime.md)、
  [`docs/supervisor.md`](../supervisor.md)、GitHub #88 / PR #137
- **対象 Issue**: #88

> 承認: リポジトリ所有者が 2026-09-18 に承認（PR #137 の Codex レビュー指摘への対応として）。

## 1. Context

0028 §2.2 により、RL Supervisor は制御ループの外の worker で動き、ループは最新の提案を読むだけである。
推論は非同期なので、受け取った提案は通常、現在より前の tick の snapshot から作られている。

0028 §2.3 / §2.6 は Supervisor 出力の期限を「`coldaisle-fand` が受け取った時刻（単調時計）から
`supervisor.valid_ms`」と定めていた。この起点では、推論やキューで時間がかかった提案も受信した瞬間に
新鮮と扱われ、元にした snapshot が `valid_ms` より古くても採用されうる（PR #137 のレビュー）。

また、過去 tick の提案をそのまま使うと、提案後に Workload Regime が変わった場合に
古い Regime 向けの戦略が選ばれうる。

## 2. Decision

1. **期限の起点は提案の元 snapshot の単調時刻とする。** 制御ループは worker へ渡した snapshot の
   `monotonic_ms`（`coldaisle-fand` 自身の単調時計）を `source_monotonic_ms` として保持し、
   `現在の単調時刻 - source_monotonic_ms > supervisor.valid_ms` の提案を期限切れにする。
   worker の時計や壁時計（`computed_at_ms`）とは比べない（0028 §2.6 の原則は変えない）。
2. **受信時刻も trace に残す。** `received_monotonic_ms` と `source_monotonic_ms` の両方を
   decision trace に記録する。受信時刻が現在より未来のもの、元 snapshot 時刻が受信時刻より後のものは拒否する。
3. **元 tick が現在より未来の提案は拒否する。** 採用した提案の元 tick の識別子（`tick_id` / `ts_ms`）は
   書き換えずに trace に残す。
4. **過去tickのRL提案は、valid_ms 以内かつ提案時の Workload Regime が現在の Regime と一致する場合のみ
   利用する。異なる場合は RulePolicy へ fallback する。**
   - 比較するのは提案時と現在の Regime の2点だけとする。途中で Regime が変わって戻った場合（A→B→A）は、
     `valid_ms` が鮮度を制限しているため、設計上そのまま利用する。
   - 過去 tick の提案には confidence の一致を求めない。同じ tick の提案は Regime と confidence の両方が
     一致すること。
5. これは **Supervisor proposal の有効性判定であり、Safety State は変えない。** 不採用の提案は
   RulePolicy への fallback として記録し、Critical Safety・Reactive Guard・Fallback Controller の判断には
   影響しない。

## 3. Consequences

- 良くなること: 推論や受け渡しが遅れた古い提案を新鮮と扱わない。採用した提案の出所（元 tick と時刻）が
  trace から追える。Regime が変わった後に古い戦略を使わない。
- 悪くなること: 推論と受け渡しにかかる時間だけ、受信後に使える時間が短くなる。
  `valid_ms` が推論時間に比べて短いと、active RL が頻繁に RulePolicy へ fallback し、shadow の比較データも減る。
  緩和策: `supervisor.valid_ms` は設定値であり、推論時間の実測に合わせて見直す。fallback の件数は trace で数えられる。

## 4. 却下した代替案

- **0028 どおり受信時刻から数える**: 遅れて届いた古い提案を新鮮と扱う。
- **Regime epoch（Regime が変わるたびに増える番号）で提案を束ね、A→B→A も拒否する**: より厳密だが
  入出力の契約が広がる。`valid_ms` が鮮度を制限するため、端点の比較で十分と判断した。
- **過去 tick の提案にも confidence の一致を求める**: confidence は tick ごとに変わるため、
  非同期の提案がほぼすべて不採用になる。

## 5. 未決事項

- `supervisor.valid_ms` の本番値（推論時間の実測後に決める）。
