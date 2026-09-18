# 決定記録 0036: Workload Regime に TRANSIENT_CPU_GPU を加える

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-18
- **Supersedes**: なし
- **関連**: [`0027-fan-control-architecture.md`](0027-fan-control-architecture.md) §2、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2（State Estimator / Supervisor の出力）、
  [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md)、GitHub #87 / PR #124
- **対象 Issue**: #87

> 承認: リポジトリ所有者が 2026-09-18 に承認（PR #124 の Codex レビュー指摘への対応として）。
> PR のマージをもって FINAL とする。

## 1. Context

#87 の状態候補は `IDLE / TRANSIENT_CPU / TRANSIENT_GPU / SUSTAINED_CPU / SUSTAINED_GPU /
SUSTAINED_CPU_GPU / COOLDOWN / UNKNOWN` で、CPU と GPU が同時に active なのに
まだ SUSTAINED に達していない状態を表す値が無かった。

当初の実装は、この場合に閾値超過の大きい側だけを残して `TRANSIENT_CPU` または
`TRANSIENT_GPU` を返していた。decision trace（0030）に残るのは `workload_regime` と
`regime_confidence` だけなので、同時 burst が単独 burst と区別できなくなり、
下流の Supervisor policy や学習データのラベルを誤らせる（PR #124 の Codex レビュー指摘）。

## 2. Decision

1. `WorkloadRegime` に `TRANSIENT_CPU_GPU = "transient_cpu_gpu"` を加える。
2. CPU / GPU の両軸が Schmitt trigger 上 active で、**少なくとも一方**の active 継続が
   `sustained_after_ms` 未満のとき `TRANSIENT_CPU_GPU` とする。両軸とも達したら従来どおり
   `SUSTAINED_CPU_GPU`。片方だけ active の判定は変えない。
3. 閾値超過の大小による片側への丸め（tie-break）は行わない。
4. hysteresis / minimum transition の扱いは他の Regime と同じ。確認前の一時的な第2軸の
   spike は、既存の確定 Regime を保つ。
5. decision trace の形は変えない。`ControlTick` schema version 2 の `workload_regime` の
   取りうる値が1つ増えるだけである（v2 は #87 と同時に導入され、既存データは無い）。

## 3. Consequences

- 良くなること: 同時 burst を trace と学習データから非破壊に識別できる。
  `SUSTAINED_CPU_GPU` と対になり、語彙が一貫する。
- 悪くなること: Supervisor（RulePolicy / RLPolicy）が扱う Regime が1つ増える。
  緩和策: 未対応の Regime は Fallback 戦略を選べる（#87 受入基準）ので、安全側に倒れる。

## 4. 却下した代替案

- **同時 transient を `UNKNOWN` にする**: 状態候補の範囲に収まるが、実在する高負荷を
  「未知」と記録して情報を失い、Fallback へ不必要に寄せる。
- **enum は変えず、軸ごとの active 状態を trace に追加する**: 非破壊だが `ControlState` の
  フィールドと 0028 の出力契約を広げる。Regime 1値の追加より変更が大きい。
- **現状の tie-break を承認して残す**: 情報が失われる問題が解消しない。

## 5. 未決事項

- `TRANSIENT_CPU_GPU` に対する Supervisor の具体的な戦略・目的関数重みは、RulePolicy の
  Issue で決める。
