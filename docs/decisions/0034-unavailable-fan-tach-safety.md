# 決定記録 0034: 制御対象 Fan の tach 読み取り不能を Safety fault にする

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-18
- **Supersedes**: なし（決定記録 0029 §5 の未決事項 5 を具体化する）
- **関連**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.5〜2.8 /
  [`0029-telemetry-loss-classes.md`](0029-telemetry-loss-classes.md) §2.2・§5 /
  決定記録 0032（#65、Proposed） / #50 / #75
- **対象 Issue**: #78

## 1. Context

決定記録 0029 は、制御対象 Fan の RPM が読めない場合の区分を #77 / #78 の
未決事項とした。#102 の `FanState.rpm` は `None` を表現できるが、これを
stall の timer 対象外にすると、次の経路で冷却の帰還を失ったままになる。

1. Startup 中に一度だけ tach の応答を確認する
2. その後 RPM の読み取りが不能になる
3. 指令 demand が `stall_check_min_demand` 以上でも stall 判定が解除され、
   `NORMAL` のまま運転する

RPM の読み取り不能は「Fan が回っているが tach だけ読めない」と
「Fan 停止や header 異常で読めない」をソフトウェアから区別できない。
実機がない現在は、前者だと楽観して通常運転を続ける根拠がない。

## 2. Decision

- zone の `effective_demand >= stall_check_min_demand` で、RPM が次のいずれかなら
  **tach の有効な応答が無い**とし、同じ stall timer を進める。
  - RPM が `stall_min_rpm` 未満
  - RPM が読み取れず `None`
- 上の2状態が切り替わっても timer を reset しない。有効な RPM が
  `stall_min_rpm` 以上へ戻ったときだけ reset する。
- `stall_window_ms` が経過するまでは transient として fault にしない。値は
  `safety.yaml` の provisional / confirmed 状態をそのまま使い、別の固定値を持たない。
- window 経過後は既存の `FaultCode.TACH_STALL` と同じ応答にする。
  - Front / Rear: 対象 zone を Max、他 zone を `fault_demand`、`DEGRADED`
  - Top: 全 zone を Max、`EMERGENCY`
- fault の detail に RPM が `unavailable` だったことを残し、低 RPM と調査時に
  区別できるようにする。
- Startup の tach 応答確認は、従来どおり有効な RPM が `stall_min_rpm` 以上に
  なった zone だけを確認済みとする。

T_SENSOR の metric contract は、決定記録 0032（#65、Proposed）が提案する
`board.connector_12v2x6` に依存する。Critical Safety は名前をハードコードせず、
T_SENSOR 有効時に `approved_t_sensor_metric` として承認済み metric contract の注入を
必須にする。0032 が FINAL になる前は本番設定で T_SENSOR を有効化しない。
0032 の名前がレビューで
変わる場合は、#65 / #78 へ同じ承認済み名を渡す。

## 3. Consequences

- tach の帰還を失ったまま `NORMAL` で低い demand へ移る経路を閉じる
- 一過性の読み取り失敗は `stall_window_ms` の間は fault にならない
- tach 配線だけの断線で Fan が実際に回っていても、window 後に安全側へ移り
  騒音が増える。実機で原因を切り分けられるまで冷却側を優先する
- 新しい fault code を増やさず、安全応答と detail の違いで表現する。将来、
  通知で両者を別集計する必要が出たら fault code の分離を検討する

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| RPM が `None` なら timer を reset して運転を続ける | 回転を確認できないのに正常とみなす楽観側の動作になる |
| RPM が1回でも `None` なら即時に fault | 短い読み取り競合なども即時 Max にする。既存の stall window で transient を吸収できる |
| tach 読み取り不能用の別 fault code を追加する | 安全応答は stall と同じ。実機で原因と通知の要件を確かめる前に schema を増やさない |
| Startup で一度見えた tach をプロセス停止まで正常とみなす | 運転中の tach 断線・Fan 停止を見逃す |

## 5. 未決事項

- `stall_check_min_demand` / `stall_min_rpm` / `stall_window_ms` の確定値は #75 の
  characterization と所有者の承認後に別の決定記録で確定する
- RPM 読み取り不能を専用 fault code に分けるかは、実機で原因と運用上の
  通知要件を確認してから決める
