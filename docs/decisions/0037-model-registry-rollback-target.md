# 決定記録 0037: Model Registry の promotion 時の rollback target

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-18
- **Supersedes**: なし
- **関連**: [`docs/model-registry.md`](../model-registry.md)、
  [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md)、PR #123（Codex review）
- **対象 Issue**: #104

オーナーが 2026-09-18 に PR #123 の Codex review への対応として承認した。
Status は PR のマージをもって `FINAL` になる。

## 1. Context

`ModelRegistry.promote()` は昇格対象の artifact だけを検証し、切り替え前の production を
無条件で rollback target（`ProductionSlot.previous`）にしていた。切り替え前の production の
bytes がすでに壊れていると、壊れた artifact が rollback target になる。それまでの正常な
rollback target は失われ、以後の `rollback()` は必ず `ArtifactVerificationError` になる。
promotion は成功するため、既知の正常な戻り先がなくなったことに運用者は気付けない。

## 2. Decision

promotion 時の rollback target を、次の順で最初に検証を通ったものにする。

1. 切り替え前の production（`slot.active`）
2. それまでの rollback target（`slot.previous`）
3. どちらも通らなければ `None`（rollback 不可）

- ここでの検証は **checksum + format** だけにする（`_verify(..., compatibility=None)`）。
  feature / target schema と authority の互換性は、`rollback()` 実行時に、その時点の
  runtime contract で検証する（従来どおり）
- rollback target が決まらなくても promotion は続行する。新しい artifact 自体は検証済みであり、
  壊れた production を置き換える復旧経路を塞がない
- 切り替え前の production は、検証の結果にかかわらず `retired` にする。audit の
  `previous_artifact` は従来どおり「切り替え前の production」を記録する
- `RegistryAuditEvent` に `rollback_target: ArtifactRef | None` を追加する。promotion event
  だけが値を持ち、kind は昇格対象と同じで、昇格対象自身は指せない
- snapshot 読込時の audit replay は、promotion の `rollback_target` が
  `{None, 切り替え前の production, 直前の rollback target}` のいずれかであることを検証し、
  その値を `ProductionSlot.previous` として再現する。これ以外の artifact を rollback target に
  した snapshot は拒否する

## 3. Consequences

- 壊れた artifact が rollback target にならず、既知の正常な戻り先が残る
- audit から、どの artifact が戻り先として残ったか、なぜ `previous_artifact` と異なるのかを
  追跡できる
- rollback target が `None` になった場合、rollback はできない。緩和策: `rollback_target` が
  `None` であることは audit に残るため、運用者は検証済みの別 version を通常の promotion で
  戻せる
- persisted snapshot の audit event にフィールドが増える。本 Registry は PR #123 で導入され、
  マージ前に永続化された snapshot は存在しないため、`schema_version` は 1 のまま据え置く

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| rollback target を常に `None` にする | 正常な古い rollback target が残っていても捨ててしまう |
| 切り替え前の production が壊れていれば promotion を拒否する | 壊れた production を正常な artifact に置き換える復旧経路を塞ぐ |
| promotion 時に compatibility まで検証する | schema 更新の直後に、健全な旧 artifact が戻り先から外れる。互換性は rollback 時の runtime contract で判断すべき |
| 監査 field を追加せず、replay 側で bytes を再検証する | snapshot の妥当性がファイルの読み取りに依存し、読み取り専用の load 経路で副作用や I/O 失敗の扱いが増える |

## 5. 未決事項

- rollback target が `None` になったときの通知（Rule Engine / Notification への接続）は、
  Control Logging（#82）と Fallback（#79）の統合時に決める
