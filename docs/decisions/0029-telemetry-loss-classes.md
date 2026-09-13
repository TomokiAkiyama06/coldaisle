# 決定記録 0029: 制御入力の欠測の分類（Critical / Degraded / Advisory）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-13
- **Supersedes**: なし（決定記録 0028 §2.7 の「必須の `air.*`」を具体化する補足。0028 の対応そのものは変えない）
- **関連**: [`0028-fan-control-contracts.md`](0028-fan-control-contracts.md) /
  [`0026-three-zone-fan-control.md`](0026-three-zone-fan-control.md) /
  [`0004-storage-read-contract.md`](0004-storage-read-contract.md) /
  `docs/requirements.md` §5.1 / Q-19 /
  統合メモ（2026-09-13）§14
- **対象 Issue**: #78（Critical Safety）/ #102（Runtime State Estimator）。関連 #50 / #65 / #79 / #80 / #82（Issue 番号はすべて GitHub の番号）

---

## 1. Context

統合メモ（2026-09-13）§14 は、次を**決定済み**としている。

- 「センサー1本欠測 = 全 Fan 100%」にはしない
- zone と故障の種類ごとに fallback を定義する
- 例：Critical（CPU telemetry loss、Top tach stall、hwmon write failure）、Degraded（Rear Exhaust の DS18B20 だけの欠測、GPU Exhaust だけの欠測）、Advisory（室内湿度の欠測）

一方、決定記録 0028 §2.7 は次のように定めている。

- CPU 温度が stale → Top を Max
- GPU Telemetry / T_SENSOR / **必須の `air.*`** が stale → Front / Rear を `fault_demand`（暫定 1.0）

0028 は「必須の `air.*`」の中身を決めていない。そのままでは、**DS18B20 が1本欠けただけで Front / Rear が Max になる**と読め、メモ §14 と食い違う。

加えて、T_SENSOR（12V-2x6 外装温度）はまだ設置していない。0028 のままでは、設置までの間ずっと stale となり、Front / Rear が Max のままになる。

2026-09-13、リポジトリ所有者が分類の案を選んだ。本記録はその内容を残す。

---

## 2. Decision

### 2.1 3つの区分

| 区分 | 意味 | 動作 | safety state |
|---|---|---|---|
| **Critical** | 失うと安全に制御できない | 0028 §2.7 の対応（Max / `fault_demand`） | 0028 §2.5 (d) のまま（`DEGRADED` / `EMERGENCY`） |
| **Degraded** | 失っても、残りの入力で運転を続けられる | **`fault_demand` にしない。** 2.3 のとおり運転を続け、記録・通知する | **変えない** |
| **Advisory** | 安全に直結しない | 記録のみ | 変えない |

### 2.2 入力ごとの区分

| 入力 | 区分 | 対応 |
|---|---|---|
| CPU 温度（`cpu.package`） | Critical | Top を Max（0028 §2.7） |
| GPU 温度（`gpu.0.core`） | Critical | Front / Rear を `fault_demand`（0028 §2.7） |
| T_SENSOR（12V-2x6 外装温度） | Critical（**有効にした後**） | Front / Rear を `fault_demand`（0028 §2.7）。未設置の間は 2.4 |
| DS18B20 の `air.*` が**すべて** stale（取り込みデーモンの停止を含む） | Critical | Front / Rear を `fault_demand`。**0028 §2.7 の「必須の `air.*` が stale」はこの場合を指す** |
| DS18B20 の `air.*` の**一部**が stale（`front_intake` / `gpu_intake` / `gpu_exhaust` / `top_exhaust` / `rear_exhaust` のうち、すべてではない） | Degraded | 2.3 |
| `air.room`（AM2320 の温度） | Degraded | 室温を使う派生値（`d.intake_rise` / `d.rear_rise`）を外して運転を続ける |
| CPU / GPU の Power（`power.gpu.0` など） | Degraded | feed-forward を外し、温度の feedback だけで計算する |
| `air.room_humidity` | Advisory | 記録のみ |
| `gpu.0.hotspot` / `gpu.0.mem` / `gpu.0.vram_used` / GPU utilization / `sys.cuda_processes` / `cpu.vrm` / `chipset` / AIO Pump・VRM Fan の RPM | Advisory | 記録のみ（ML の confidence が下がりうる） |

制御対象ファンの tach stall・書き込み失敗・読み戻し不一致は、入力の欠測ではなく**ファンの故障**であり、0028 §2.7「Fan / Hardware」の表のまま扱う（本記録の対象外）。

### 2.3 Degraded のときの動作

- **safety state は変えない（`NORMAL` のまま）。** fault ではなく**入力の品質**として、State Snapshot（#102）と decision trace（#82）に残す
- Fallback Controller（#79）は、欠けた入力を使う項を外すか、代わりの入力へ切り替えて計算する。どの入力を何で代えるかは #79 が決め、#50 で確かめる
- Reactive Guard（#80）は、Degraded の入力があるあいだ、設定の保守側の閾値の組を使う
- Learned MPC（#86）は、学習時に無かった欠測の組み合わせなら OOD として Fallback へ退避する（0028 §2.5 (c)）
- 通知する（ルールエンジンのセンサー異常のルール）
- Degraded の DS18B20 が増えて**すべて**欠けたら Critical（2.2）

### 2.4 T_SENSOR が未設置の間

- `safety.yaml` で T_SENSOR を無効にできる。無効の間は Critical の入力として扱わず、decision trace と起動時のログに「無効（未設置）」と残す
- **有効にするのは、設置と #50 の較正の確認の後。** 有効化は `safety.yaml` の変更なので、所有者の承認を要する（0028 §2.9 の承認点 2）
- 有効にした後の断線・ありえない値・stale は Critical

### 2.5 決定記録 0028 との関係

- 0028 §2.7 の**対応**（何をどこまで上げるか）は変えない。本記録は「**どの入力の欠測がその対応を起こすか**」を決める
- `fault_demand` の暫定値 1.0 はそのまま（Critical にだけ効く）。下げてよいかは 0028 の未決 4（#50）
- #76 の `FaultCode.AIR_TELEMETRY_STALE` は「DS18B20 の `air.*` がすべて stale」を表す。一部の欠測は fault ではないので `FaultCode` にしない

---

## 3. Consequences

### 良くなること

- センサー1本の欠測で Front / Rear が Max にならない（統合メモ §14 と一致する）
- T_SENSOR を設置する前でも運転できる
- 0028 の「必須の `air.*`」の曖昧さが消え、#78 / #79 / #102 が同じ区分で実装できる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 一部の欠測中は、冷却の判断材料が減る | Reactive Guard を保守側へ寄せ、MPC は OOD で Fallback へ退避する。通知で人が気づく |
| 代わりの入力で計算することの妥当性は、実測するまで分からない | #50 で確かめる |
| T_SENSOR を無効にした設定のまま設置し忘れると、監視が抜ける | 無効の状態を起動時のログと decision trace に出す。有効化は承認点にする |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `air.*` をすべて Critical にする（1本の欠測でも `fault_demand`） | 統合メモ §14 に反する。センサー1本の不調でファンがうるさく回り続ける |
| すべての入力を Degraded にする | CPU / GPU の温度が見えないまま運転を続けることになる |
| #50 の実測まで分類を決めない | それまで「必須」が曖昧なまま #78 / #79 / #102 の実装が進む |
| 一部の欠測を safety state の `DEGRADED` にする | `DEGRADED` は zone の fault で demand を上げている状態（0028 §2.5 (d)）。入力の品質と安全状態を混ぜると、記録から原因を読み違える |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | どの入力が欠けたら何で代えるか | #79 / #50 |
| 2 | Degraded が長く続いたとき（例: 数時間）に Critical へ上げるか | #50 / #78 |
| 3 | Reactive Guard の保守側の閾値の値 | #80 / #50 |
| 4 | AIO Pump の RPM の低下をアラートにするか | ルールエンジン（#49 系） |
| 5 | 制御対象ファンの RPM が読めない（stall と区別できない）ときの区分 | #77 / #78 |
| 6 | 入力の品質を `ControlTick`（#76 の型）にどう載せるか | #82 / #102 |
