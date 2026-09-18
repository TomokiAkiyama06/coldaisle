# 決定記録 0034: 制御対象 Fan の tach 読み取り不能を Safety fault にする

- **種別**: Decision Record
- **Status**: FINAL（2026-09-18、リポジトリ所有者が承認）
- **Date**: 2026-09-18
- **Supersedes**: なし（決定記録 0029 §5 の未決事項 5 を具体化する）
- **関連**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) §2.5〜2.8 /
  [`0029-telemetry-loss-classes.md`](0029-telemetry-loss-classes.md) §2.2・§5 /
  [`0032-internal-telemetry-metric-names.md`](0032-internal-telemetry-metric-names.md)（FINAL） / #50 / #75
- **対象 Issue**: #78

## 1. Context

決定記録 0029 は、制御対象 Fan の RPM が読めない場合の区分を #77 / #78 の
未決事項とした。#102 の `FanState.rpm` は `None` を表現できるが、これを
stall の timer 対象外にすると、次の経路で冷却の帰還を失ったままになる。

1. Startup 中に一度だけ tach の応答を確認する
2. その後 RPM の読み取りが不能になる
3. 指令 demand が `stall_check_min_demand` 以上でも stall 判定が解除され、
   `NORMAL` のまま運転する

同じ抜けは `ControlStateSnapshot.fans` 全体が `None` になった場合にも生じる。
個別 RPM だけでなく Fan readback 自体を失っても、直前までの指令を Safety は
冷却確認済みとみなせない。

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
- `snapshot.fans` 全体が無い場合は、zone ごとに最後に観測できた
  `FanState.effective_demand` を直前 command の記録として使い、RPM unavailable と同じ
  timer を進める。最初の readback より前は STARTUP の command が Max であるため
  demand `1.0` として扱う。値を0や閾値未満で補って判定を解除しない。
- `stall_window_ms` が経過するまでは transient として fault にしない。値は
  `safety.yaml` の provisional / confirmed 状態をそのまま使い、別の固定値を持たない。
- window 経過後は既存の `FaultCode.TACH_STALL` と同じ応答にする。
  - Front / Rear: 対象 zone を Max、他 zone を `fault_demand`、`DEGRADED`
  - Top: 全 zone を Max、`EMERGENCY`
- fault の detail に RPM が `unavailable` だったことを残し、低 RPM と調査時に
  区別できるようにする。
- Startup の tach 応答確認は、従来どおり有効な RPM が `stall_min_rpm` 以上に
  なった zone だけを確認済みとする。

T_SENSOR の metric 名は決定記録 0032（FINAL）の `board.connector_12v2x6` とする。
Critical Safety は名前をハードコードせず、T_SENSOR 有効時に `approved_t_sensor_metric`
として承認済み metric contract の注入を必須にし、Metric Catalog に単位 `C` で存在し、
既存 Safety 入力名と衝突しないことを検証する。T_SENSOR の本番有効化は、0029 §2.4 と
0032 のとおり設置・#50 の較正・所有者承認の後に行う。

**承認**: 2026-09-18、リポジトリ所有者が #78 PR（#131）の Safety review で本記録を承認した。
`stall_check_min_demand` / `stall_min_rpm` / `stall_window_ms` などの閾値は本記録の対象外で、
#50 / #75 の実測後に別の PR で本番の `safety.yaml` として確定する。それまでテストの値は
provisional のまま扱う。

## 3. Consequences

- tach の帰還を失ったまま `NORMAL` で低い demand へ移る経路を閉じる
- Fan readback 全体の喪失でも last effective command を基準に fault へ移り、timer を
  消して `NORMAL` に戻る経路を閉じる
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
