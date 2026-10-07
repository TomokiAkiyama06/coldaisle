# 決定記録 0110: T_SENSOR（12V-2x6 コネクタ外装温度）を Critical Safety の入力として有効にする（専用の絶対温度上限 80 °C・妥当範囲の下限 0 °C・許容遅延 5000 ms）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-08、リポジトリ所有者の決定。§6）
- **Date**: 2026-10-08
- **Supersedes**（部分）:
  - [`0029`](0029-telemetry-loss-classes.md) §2.4 の「有効にするのは、設置と #50 の較正の確認の後」のうち、
    **#50 の較正（誤差の測定）を有効化の前提にする点のみ**。設置と実機の確認・所有者の承認は済んだ（§2.1）。
    有効化を `safety.yaml` の変更として所有者の承認点にすること、有効化後に使えなければ Critical とすることは変えない
  - [`0032`](0032-internal-telemetry-metric-names.md) §2 の T_SENSOR の段落の「設置・#50 の較正・妥当範囲の承認が
    終わるまで本番設定を `enabled: false` に保つ」「有効化には #50 の測定・較正と所有者承認を `status: confirmed` /
    `basis` として残す」のうち、**#50 の較正を前提にする点のみ**。metric 名 `board.connector_12v2x6` は変えない
- **関連**: [`0028`](0028-fan-control-contracts.md) §2.4 / §2.5 / §2.7 / §2.8 / §2.9 /
  [`0029`](0029-telemetry-loss-classes.md) §2.1〜§2.4 /
  [`0032`](0032-internal-telemetry-metric-names.md) /
  [`0038`](0038-internal-telemetry-outage-counting.md) /
  [`0071`](0071-control-trace-read-api.md) /
  `docs/critical-safety.md` / `docs/internal-telemetry.md` / `docs/control-config.md` /
  AGENTS.md「絶対に守るルール」2〜5・9・10
- **対象 Issue**: #78（Critical Safety）/ #65（Internal Telemetry Collector）/ #50（較正。未了）

---

## 1. Context

T_SENSOR は 0029 §2.4 と 0032 により、**設置と #50 の較正の確認の後**に有効にすることになっていた。
それまで `config/internal-telemetry.yaml` では無効、`safety.yaml` でも `telemetry.t_sensor.enabled: false` である。

2026-10-08、所有者が実機で T_SENSOR を読み取り、有効化を決めた。あわせて次の2点が問題になった。

1. **絶対温度上限が1つしかない。** いまの Critical Safety は、T_SENSOR を有効にすると、CPU / GPU / 空気の温度と
   同じ `absolute_temp_ceiling_c` で判定する（`_is_absolute_temperature_metric()`）。コネクタの外装温度は
   GPU のコアとは許せる温度が違い、共通の上限では T_SENSOR に合わせると他が厳しすぎ、他に合わせると T_SENSOR が緩すぎる
2. **妥当範囲の上限を決める根拠が無い。** collector の設定の検証は `minimum` と `maximum` を「両方指定する」ことを
   求めているが、所有者が分かっているのは断線時に負の値になることだけである

## 2. Decision（決着: 2026-10-08 所有者の決定）

### 2.1 実機の確認

- 2026-10-08、実機（ASUS ProArt X870E-CREATOR WIFI）で hwmon の driver `asusec`、label `T_Sensor` を読み取って確認した。
  同じ値が nct6799 の `AUXTIN5` にも出るが、**使うのは asusec の label** とする（label で特定でき、
  nct6799 の `AUXTINn` は用途が決まっていない汎用入力のため）
- GPU 600 W の負荷で 49〜50 °C
- 実機の識別子（`hwmonN`・絶対パス・シリアル）はリポジトリに書かない（AGENTS.md ルール 10）

### 2.2 T_SENSOR 専用の絶対温度上限（`telemetry.t_sensor.absolute_ceiling_c`）

- `safety.yaml` の `telemetry.t_sensor` に **`absolute_ceiling_c`** を足す。形はほかの Safety の値と同じ
  `{value, status, basis}`。名前は同じ塊の `stale_after_ms` に倣い、T_SENSOR の有効・遅延・上限を1か所に置く
- **有効なら必須、無効なら指定しない**（`stale_after_ms` と同じ規則。無効な T_SENSOR に上限があると、
  効いていない値を承認済みと読み違える）
- 本番の値は **80 °C**（所有者の承認。`status: confirmed`）
- Critical Safety は T_SENSOR を**この上限だけ**で判定する。**共通の `absolute_temp_ceiling_c` は T_SENSOR 以外の
  温度だけに使う**（T_SENSOR を共通の上限から外す）
- 超えたときの扱いは既存の `ABSOLUTE_TEMPERATURE_LIMIT` と同じ:
  - 判定は `値 >= 上限`（既存と同じ。80.0 °C で発火、79.9 °C では発火しない）。品質 `ok` の値だけを見る
  - fault code は `absolute_temperature_limit`、safety state は `EMERGENCY`、全 zone forced Max
  - 解除は既存の規則のまま。超えた metric の fresh（品質 `ok`）な上限未満の値を観測してから
    `fault_clear_hold_ms` 続いたときだけ。stale / missing / suspect / 消失の tick は解消に数えず hold をやり直す
- fault の `detail` には、超えた metric ごとに**値と使った上限**を残す（例: `board.connector_12v2x6=80.5C (ceiling 80C)`）。
  `detail` は毎 tick の decision trace（`ControlTick` の `faults`）に保存される

### 2.3 運用（所有者の方針）

- 緊急時はログ（decision trace）に残し、**何度も超えるようなら値を直す**。値を変えるのは `safety.yaml` の変更なので、
  0028 §2.9 の承認点 2 を経る
- 数え方: `GET /api/v1/control/traces` の各 tick の `faults` から、`code` が `absolute_temperature_limit` で
  `detail` に `board.connector_12v2x6` を含む tick を拾い、**直前の tick に無かったものを1回**と数える（続く tick は同じ1回）。
  trace は LLM のプロンプトへ直接入れない（AGENTS.md ルール 8）

### 2.4 妥当範囲（下限 0 °C・上限なし）

- 断線時は負の値になる（所有者の申告）。**0 °C 未満は「ありえない値」**とし、`config/internal-telemetry.yaml` の
  `board.connector_12v2x6` に `minimum: 0` を置く。0 °C ちょうどは範囲内
- **実現する場所は collector の `minimum`（Critical Safety ではない）。** collector は範囲外の値を `quality: suspect` で
  保存し、State Estimator はそれを「使えない」とし、`critical_unavailable` に T_SENSOR を出す。Critical Safety は
  0029 のとおり `t_sensor_stale`（Front / Rear を `fault_demand`、safety state `DEGRADED`）にする。
  Critical Safety 自身は値の下限を判定しない（範囲の定義を1か所にするため）
- 上限側の妥当範囲は根拠が無いので **`maximum: null`** のまま。80 °C の上限で緊急になるため、上限側の「ありえない値」は
  別に設けない
- このため collector の設定の検証を「`minimum` と `maximum` は両方指定する」から「**片側だけでもよい。両方あるときは
  `minimum < maximum`**」に改める。T_SENSOR を有効にするには、従来どおり `minimum` を必須とする

### 2.5 許容遅延（`telemetry.t_sensor.stale_after_ms`）

- **5000 ms**（collector の収集周期 `interval_ms: 2500` の2倍）。`status: confirmed`
- 既存の設定の検証は `stale_after_ms > 0` だけで、収集周期との下限の照合は無い。5000 はこれを満たす

### 2.6 `safety.yaml` の版は上げない（4 のまま）

- 足す欄は「T_SENSOR が有効なときだけ必須」である。版 4 で T_SENSOR を有効にした既存のファイルは、欄が無いので
  **検証で拒否され**（`config_invalid` の全 zone Max）、共通の上限で T_SENSOR を判定する旧い意味で黙って読まれることは無い
- 版 4 で T_SENSOR を無効にしたファイル（いまの本番を含む）は、旧い意味と新しい意味が同じである
- 版を上げると、無効のままの全環境に意味の変わらない移行を強いることになる。`ControlTick` の runtime は毎 tick
  `safety.yaml` の SHA-256 を残すので、どの上限で裁定したかは trace から追える

### 2.7 #50 の較正は未了

- 所有者は §2.1 の確認をもって有効化を承認した。**#50 の較正（T_SENSOR の誤差の測定）はまだ行っていない**
- 80 °C の上限と 0 °C の下限は較正誤差を織り込んでいない。#50 で誤差が分かったら、上限と下限を見直す（未決 1）

### 2.8 本番での有効化の手順

1. `config/internal-telemetry.yaml`（本 PR でリポジトリの値を有効にした）で `coldaisle-telemetry` を再起動し、
   `board.connector_12v2x6` が `ok` で記録されることを確かめる
2. 運用の `safety.yaml` の `telemetry.t_sensor` を次にする（版は 4 のまま）
   ```yaml
   t_sensor:
     enabled: {value: true, status: confirmed, basis: "決定記録 0110。2026-10-08 所有者が承認"}
     stale_after_ms: {value: 5000, status: confirmed, basis: "決定記録 0110 §2.5"}
     absolute_ceiling_c: {value: 80.0, status: confirmed, basis: "決定記録 0110 §2.2"}
   ```
3. `coldaisle-fand` を `--t-sensor-metric board.connector_12v2x6` を付けて再起動する（設定は再起動時だけ反映する）。
   `safety.yaml` で有効にしたのにこの引数が無い、またはその逆は起動時に拒否される

---

## 3. Consequences

### 良くなること

- 12V-2x6 コネクタの過熱を、GPU や空気の温度と別の値で緊急 Max にできる
- T_SENSOR の断線（負の値）・collector の停止（stale）を Critical として Front / Rear を上げる
- 共通の上限を T_SENSOR に合わせて動かす必要が無くなる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| #50 の較正前なので、80 °C / 0 °C が誤差の分だけずれうる | 600 W で 49〜50 °C と上限まで余裕がある。超えた記録を数え（§2.3）、#50 の後に見直す（未決 1） |
| 下限の判定が collector の設定にあり、Critical Safety だけを読んでも分からない | 本記録と `docs/critical-safety.md` に場所を書く。`internal-telemetry.yaml` の T_SENSOR は `minimum` を必須にしてある |
| 版を上げないので、版だけを見ても上限の意味の変化が分からない | 有効な旧いファイルは検証で拒否される（§2.6）。trace に `safety.yaml` の SHA-256 と fault の `detail` の上限が残る |
| T_SENSOR の誤作動（接触不良で一時的に負）で Front / Rear が `fault_demand` になる | 0029 の Critical の定義どおり。頻発するなら trace で数えて配線を直す |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 共通の `absolute_temp_ceiling_c` で T_SENSOR も判定し続ける | 外装温度とコア温度で許せる温度が違う（§1） |
| 専用の上限を最上位（`t_sensor_absolute_ceiling_c`）に置く | T_SENSOR の有効・遅延と別の場所になり、無効のまま上限だけ残る組み合わせを作りやすい |
| `safety.yaml` を版 5 に上げる | §2.6。旧い意味で黙って読まれる経路が無く、無効の環境に意味の無い移行を強いる |
| 負の値を Critical Safety の中で判定する | 妥当範囲の定義が collector と Safety の2か所になる。collector の `suspect` で同じ結果（Critical）になる |
| 上限側の妥当範囲に仮の値（例: 125 °C）を置く | 根拠の無い値を置かない（AGENTS.md「仕様に書かれていない挙動を勝手に決めない」）。80 °C で緊急になる |
| nct6799 の `AUXTIN5` を使う | 汎用入力で label が用途を表さない。asusec の `T_Sensor` は label で特定できる |
| #50 の較正まで有効にしない | 所有者が実機確認をもって有効化を決めた。較正は後から値を見直す |

---

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | #50 の較正誤差を受けて、80 °C の上限と 0 °C の下限を見直すか | #50 |
| 2 | 上限側の妥当範囲（`maximum`）を置くか | #50 |
| 3 | Offline Evaluation（`config/evaluation.yaml`）や RL の screen（`config/rl-training.yaml`）の `temperature_metrics` に T_SENSOR を加える場合、共通の上限ではなく専用の上限と比べること（いまはどちらにも T_SENSOR は無い） | #91 / #105 |

---

## 6. 所有者の決定（2026-10-08）

所有者は 2026-10-08 に §2.1〜§2.5・§2.7 を決めた（実機の確認、専用の上限 80 °C とその扱い、共通の上限を T_SENSOR 以外に
限ること、運用の方針、下限 0 °C と上限 null、許容遅延 5000 ms、#50 の較正前の有効化）。§2.2 の欄の名前、§2.4 の
collector の検証の変更、§2.6 の版を上げない判断は、所有者の決定を実装に移すための選択であり、本記録の PR のレビューで確認する。
