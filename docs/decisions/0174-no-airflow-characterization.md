# 決定記録 0174: 3 系統の Fan の風量の特性は測らない。Air Balance の協調は `mode: "off"` のまま運用する

- **種別**: Decision Record
- **Status**: FINAL（2026-10-26、リポジトリ所有者の承認）
- **Date**: 2026-10-26
- **Supersedes**: [`0153`](0153-airflow-measurement-method.md) のうち「導入先で掃引と応答の行列を測り、結果を承認する」予定の部分（§2.7 の 3・4 と §2.12 の EFU の定義を待つ部分）のみ（測り方の定義と解析の道具 `coldaisle-airflow-characterization` は有効）/
  [`0078`](0078-air-balance-fallback-coordination.md) §2.11 の段 5〜7（校正済み `air-balance.yaml`・shadow → apply）を行う予定の部分のみ（協調の仕組み・検証・`uncalibrated` との組み合わせの拒否は有効）
- **関連**: [`0073`](0073-air-balance-control-config-integration.md) / [`0078`](0078-air-balance-fallback-coordination.md) / [`0085`](0085-coordination-failure-bypasses-gate.md) /
  [`0093`](0093-air-balance-shadow-summary-and-baseline-arm.md) / [`0120`](0120-fan-hardware-first-host.md) / [`0153`](0153-airflow-measurement-method.md) /
  [`0164`](0164-no-sensor-calibration.md)（同じ形の判断）
- **対象 Issue**: #75 / #81

## 1. Context

#75 は Front / Rear / Top の風量の特性（回転数の掃引と応答の行列）を導入先で測り、Zone 間を共通の尺度（EFU）で比べられるようにする。
測り方は 0153 で決め、解析の道具（PR #356）もある。#81 の Air Balance は、その結果を校正済みの `air-balance.yaml` に写してから
shadow → apply へ進む（0078 §2.11 の段 5〜7）。

2026-10-26、所有者が「風量は測れなそうなので、クローズしてよい」と判断した。風量を直接測る機器が無く、
0153 の熱の応答から推す方法も、導入先で長い掃引の時間と条件の管理を要する。

#81 の残りはすべて #75 の結果を前提にしている。`mode: shadow` / `apply` は `air-balance.yaml` が `uncalibrated` なら
Control Config の不正として拒否される（`docs/control-config.md`、0078）ので、#75 を測らないなら協調は `mode: "off"` から先へ進めない。

## 2. Decision（2026-10-26 所有者の判断）

- **#75 の測定は行わない。** 0153 の測り方の定義と `coldaisle-airflow-characterization` は残すが、導入先で測る予定と、結果の承認・EFU の定義は追わない。
  #75 は not planned で閉じる
- **#81 の協調は `mode: "off"` のまま運用する。** 校正済みの `air-balance.yaml` を作らず、shadow・apply へ進めない。
  `air-balance.yaml` は `uncalibrated` のままで、Air Balance の trace（0073。applied demand から推した値を記録するだけで制御へ効かない）は今のまま残す。
  #81 は not planned で閉じる
- 実装済みの仕組み（協調の coordinator・検証・配線・trace の束縛・shadow の集計の CLI。PR #210 / #214 / #220 / #223）は消さない。
  `uncalibrated` と `shadow` / `apply` の組み合わせの拒否も今のまま（測らずに協調を有効にする経路を作らない）
- Front / Rear / Top の制御は今のまま、Baseline / Fallback の曲線（0119）・Reactive Guard・Critical Safety で決まる。CPU cooling floor は Top に残る
- 後で風量を測れるようになったら（機器を得た・別の方法を決めた）、新しい記録で #75 / #81 を開き直す

## 3. Consequences

### 良くなること

- 測れない前提の作業が Issue として残り続けない。#81 の段 5〜7 を待つ人がいなくなる

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| Zone 間の風量の釣り合い（吸気と排気）を制御で整えられない | Baseline の曲線と Guard・Critical Safety で冷却は保たれている（T1 24 時間・R3 で確認）。Air Balance の trace で傾向は見える |
| Learned MPC の目的関数の Air Balance の項（#86）が実測の尺度を持たない | #86 の重みは実データの後に決める（0052 §5）。Air Balance の項を使わない構成にできる |
| 0153 の解析の道具が使われないまま残る | 消すと開き直すときに作り直しになる。試験は CI で走り続ける |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| #75 だけ閉じ、#81 は温度差だけで協調する方法を探す | 方法が決まっておらず、新しい設計の記録が要る。所有者が両方を閉じることを選んだ |
| 記録を作らず Issue だけ閉じる | 0153・0078 だけを読んだ人が、測る前提のままだと思う（0164 と同じ理由） |
| 協調の仕組みのコードを消す | 開き直すときに作り直しになる。`mode: "off"` で制御へ効かない |

## 5. 未決事項

なし。
