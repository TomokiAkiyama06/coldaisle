# 決定記録 0120: 導入先の `fan-hardware.yaml`（header の特定と Fan profile）を承認する

- **種別**: Decision Record
- **Status**: FINAL（2026-10-13、リポジトリ所有者の承認）
- **Date**: 2026-10-13
- **Supersedes**: なし
- **関連**: [`0028`](0028-fan-control-contracts.md) §2.4 / §2.9 /
  [`0073`](0073-air-balance-control-config-integration.md) §2.3 /
  [`0118`](0118-hwmon-backend-label-less-headers.md) §2.1 /
  [`0119`](0119-safety-values-first-host.md) /
  `docs/fan-header-mapping.md` / `docs/control-config.md`
- **対象 Issue**: #75（Fan のキャラクタライズ）/ #74（実機の確認）

---

## 1. Context

fand は `fan-hardware.yaml` の `approval.status` が `confirmed` で根拠 `basis` を持つまで、実機の Fan へ書かない。
0119 で `safety.yaml` と `fan-policy.yaml` の Baseline の値を決めたので、0028 §2.9 の承認点 3（Top から）へ進むには
導入先の `fan-hardware.yaml` の承認が残っていた。

使える実測は2つある。

- `docs/fan-header-mapping.md` の 2026-09-12 頃の実測（PWM 25 / 50 / 75 / 100% の RPM・停止と再始動の PWM。概数）
- 2026-10-10〜13 に `coldaisle-telemetry` が BIOS の自動制御のもとで集めた `fan.<zone>.pwm` と `fan.<zone>.rpm`
  （zone ごとに約 10 万組。GPU の 600 W の負荷を含む）

運転記録を PWM の 5% 刻みで束ねた RPM の中央値（抜粋）:

| PWM | Front | Rear | Top |
|---:|---:|---:|---:|
| 40% | 687 | 748 | 1074 |
| 50% | 890 | 938 | 1376 |
| 60% | 1067 | 1091 | 1582 |
| 75% | 1326 | 1277 | 1875 |
| 90% | 1626 | 1462 | 2166 |
| 100% | 1792 | 1555 | 2323 |
| 観測の最大 | 1800 | 1603 | 2406 |

9月の概数の表とおおむね合う。ただし Front の 100% は運転記録の方が高い（1650 に対し 1792）。
BIOS は PWM を約 39% より下げないため、それより低い側は運転記録に無い。

## 2. Decision

### 2.1 header

| zone | driver | label | pwm | tach | enable |
|---|---|---|---|---|---|
| Front | `nct6799` | `null` | `pwm6` | `fan6_input` | `pwm6_enable` |
| Rear | `nct6799` | `null` | `pwm5` | `fan5_input` | `pwm5_enable` |
| Top | `nct6799` | `null` | `pwm2` | `fan2_input` | `pwm2_enable` |

`nct6799` は Fan の label を持たない（0118 §2.1）。導入先で `name` が `nct6799` の hwmon は1つだけで、一意に特定できる。
対応は `docs/fan-header-mapping.md` のとおり。

### 2.2 Fan profile

| 値 | Front | Rear | Top | 根拠 |
|---|---:|---:|---:|---|
| `startup_demand` | 0.50 | 0.50 | 0.50 | 再始動の PWM 24（≈0.09）の約5倍。止まった Fan を確実に回す |
| `minimum_stable_demand` | 0.15 | 0.15 | 0.15 | 下限の候補 PWM 32（≈0.125）に余裕を足す。Rear は PWM 0 でも止まらない |
| `maximum_rpm` | 1900 | 1700 | 2500 | 観測の最大に約 5% を足す |

`pwm_to_rpm`（demand → RPM）:

| demand | 0.25 | 0.40 | 0.50 | 0.60 | 0.75 | 0.90 | 1.00 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Front | 439 | 687 | 890 | 1067 | 1326 | 1626 | 1792 |
| Rear | 525 | 748 | 938 | 1091 | 1277 | 1462 | 1555 |
| Top | 721 | 1074 | 1376 | 1582 | 1875 | 2166 | 2323 |

0.25 は 9月の実測、0.40 以上は運転記録の中央値。

`airflow_index` は各 zone の RPM を demand 1.0 の RPM で割った値（風量は回転数におおむね比例するという近似）にする。
Air Balance は無効のまま（0119 §2.14）なので、いまは trace に残るだけで Demand には効かない。

`approval.status` は `confirmed`。`basis` に本記録を指す。

### 2.3 運転への効き方

- `safety.yaml` の最低安全 Demand 0.35（0119 §2.2）が常に上にあるため、`minimum_stable_demand` 0.15 はいまの運転に効かない。
  Fan そのものの性質の記録として置く
- `maximum_rpm` は、いまのコードでは表の上限の検査にしか使わない
- 実運用の `fan-hardware.yaml` はリポジトリに置かない（AGENTS.md ルール 10。値は本記録に残す）

## 3. Consequences

### 良くなること

- 承認点 3（Top から）に必要な設定がそろう
- 表が運転記録の中央値なので、`target_rpm` が実際の回転数に近い

### 悪くなること・その緩和

- 0.40 未満は 9月の概数の1点だけで、0.25 未満の表は無い。最低安全 Demand 0.35 の下へは書かないので、いまは効かない。
  Safety の下限を下げる前に測り直す（§5 の 1）
- 運転記録は BIOS が PWM を動かしている途中の点も含む（Fan の回転は PWM に遅れる）。中央値を取り、上下の裾は使わない

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| `minimum_stable_demand` を Safety と同じ 0.35 にする | いまの運転は変わらない。Fan の性質と Safety の判断を混ぜることになる |
| 低い側を実測し直してから承認する | fand で測るには先に承認が要る。いまの運転に効かない範囲のために承認点 3 を止めない |
| 9月の概数の表だけを使う | 3日間の運転記録の方が点が多く、Front の高い側が概数と合わない |

## 5. 未決事項

| # | 内容 | 時期 |
|---|---|---|
| 1 | 0.35 未満の表・停止と再始動の点の測り直し | Safety の最低 Demand を下げる前（#75） |
| 2 | `airflow_index` の実測（風量の比例の近似を置き換える） | Air Balance を有効にする前（#75 / 0073） |
| 3 | 0119 §5 の 5（試験の手順）は、別の決定記録で決める | 承認点 3 で時間の値を確定する前 |
