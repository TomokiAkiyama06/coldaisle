# 決定記録 0145: 12V-2x6 コネクタの温度を 2 段で通知する（警告 65 °C・重大 75 °C）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-15
- **Supersedes**: なし（0110 の Critical Safety の 80 °C と、0012 / 0131 §2.3 / 0139 の状態機械は変えない）
- **関連**: [`0110`](0110-t-sensor-ceiling-and-enablement.md)（T_SENSOR・80 °C・0 °C 未満は suspect） /
  [`0012`](0012-rule-engine.md) §2.2 / §2.4 /
  [`0131`](0131-gpu-fan-service-alert.md) §2.3（DB の最新値を読むルール） /
  [`0139`](0139-rule-pending-reset-on-missing.md)（欠けたときの発火待ちの取り消し） /
  [`0135`](0135-critical-notifications-to-slack.md)（critical も Slack・夜間は critical だけ） /
  [`0013`](0013-notifications.md) / [`0142`](0142-japanese-notification-text.md)（通知の文。ルールの `label`・`title`・`resolved_title`・`hint`） /
  `docs/fan-header-mapping.md` の T5・T7 /
  `config/rules.yaml`、`config/internal-telemetry.yaml`、`config/quality.yaml`、`src/coldaisle/rules/`
- **対象 Issue**: #329

---

## 1. Context

- `board.connector_12v2x6`（T_SENSOR。12V-2x6 コネクタの外装温度）は、`coldaisle-telemetry` が hwmon（asusec の `T_Sensor`）から
  2.5 s ごと（`config/internal-telemetry.yaml` の `interval_ms`）に読み、DB へ書いている。0 °C 未満は collector が
  `suspect` にする（0110 §2.4）
- 今この値を見ているのは Critical Safety（`coldaisle-fand`）だけで、80 °C（`telemetry.t_sensor.absolute_ceiling_c`）で
  EMERGENCY・全 zone Max にする。**それまで人に知らせる手段が無い。** fand が動いていない間（BIOS の制御）は、80 °C の Max も効かない
- Fan を全開にしてもコネクタの発熱（接触不良・差し込みの不足）は止まらない。早く気づいて**人が負荷を止める**ことが目的
- 実測は GPU 600 W の定常で 48〜51 °C（`docs/fan-header-mapping.md` の T5・T7、0110 §2.1）。アイドルは 26〜27 °C。
  T5 の各試行では、負荷をかけてから Max の後 30 s までの上がりは 0〜2 °C で、コネクタの温度はゆっくり動く
- 所有者は 2026-10-15、2 段の通知を選んだ（値は provisional）: 警告は 65 °C 以上が 60 s 続く、重大は 75 °C 以上。
  重大の発火までの時間・解除のヒステリシス・欠けたときの扱いは本記録で決める

### 1.1 既存のしきい値のルールでは足りない

`ThresholdRule`（`intake_high` など）は、取り込みのサンプル（基板の値）を `Engine.on_sample()` で評価する。
`board.connector_12v2x6` は基板のサンプルに含まれない（別のプロセスが DB へ書く internal telemetry）ので、
`ThresholdRule` に書いても**一度も評価されない**。DB の最新値を読む形は、GPU の Fan の 2 つのルール（0131 §2.3）にだけある。

---

## 2. Decision

### 2.1 ルールの型: DB の最新値を読むしきい値のルール（`LatestThresholdRule`）を足す

- 項目は `ThresholdRule` と同じ（`enabled`・`severity`・`metric`・`threshold`・`clear`・`fire_after_s`）。読み込み時に
  `clear < threshold` を確かめる（`ThresholdRule` と同じ）
- 値は 0131 §2.3 のとおり、サンプルとティックの評価のたびに `SqliteStore.latest(at_ms=評価時刻)` で読む。
  基板のサンプルが止まっていても `on_tick()` で評価される
- **日次 CSV の再生（`--source replay --csv … --bulk` など）では評価されない。** 日次 CSV はチャンネルの対応（`channels.py`）が
  あるメトリクスだけを書き（`store/csv_export.py`）、`board.connector_12v2x6` には対応が無いので、再生の DB にこの値が入らない。
  値が無いので発火待ちも始まらない。内部の telemetry を再生できるようにするのは本記録の範囲外（§5 の 7）
- **比較は「以上」**: 成立は `値 >= threshold`、成立中は `値 >= clear` の間続き、`値 < clear` で解除する。
  所有者の選択（「65 °C 以上」）と 0110 §2.2 の Critical Safety の判定（`値 >= 上限`）に合わせる。
  既存の `ThresholdRule` の `値 > threshold` は変えない
- 通知の文（`detail`）は持たない（`ThresholdRule` と同じ。値は通知の `値:` に出る）。ルール名で分岐する文を足さない
- 型はコネクタに限らない。ほかの internal telemetry のしきい値の通知にも使えるが、足すのは本記録の 2 つだけ

### 2.2 2 つのルールと値（provisional）

| ルール（`rule_id`） | 重大度 | 発火 | 発火までの時間 | 解除 |
|---|---|---|---|---|
| `connector_temp_high`（`CONNECTOR_TEMP_HIGH`） | warning | 65 °C 以上 | 60 s | 62 °C 未満 |
| `connector_temp_critical`（`CONNECTOR_TEMP_CRITICAL`） | critical | 75 °C 以上 | **10 s（推奨）** | 72 °C 未満 |

- **重大の 10 s**: 2.5 s 周期で 4〜5 回の読み値が続いたことになり、1 回だけの読み違いで夜間の critical を
  鳴らさない。コネクタの温度はゆっくり動く（1）ので、10 s 待っても 80 °C（Critical Safety）までの余裕はほとんど減らない。
  `stale_after_ms`（`config/quality.yaml` の 10 s）とも同じ長さで、収集が 1 回抜けた程度では待ちが切れない
- **解除は発火より 3 °C 低く**: 発火の閾値のちょうど上下を 1 °C ほど揺れても、発火と解除を繰り返さない幅。
  定常（48〜51 °C）から十分に離れている。重大の解除（72 °C）は警告の発火（65 °C）より上なので、重大が解けても
  警告は残り、「下がり始めたが、まだ高い」ことが分かる
- 75 °C 以上が 60 s 続けば、警告と重大の両方が発火する（rule_id が違うので別の通知。連投の抑制（0013）は rule_id ごと）。
  重大だけにまとめない（警告の発火の履歴を残すため。§4）
- 値は `config/rules.yaml` にだけ置く（AGENTS.md ルール 9）。コードに既定値を持たない

### 2.3 欠け・stale・suspect のときは 0131 §2.3 の扱い（0139 ではない）

- 評価のたびに読む最新値が、**無い・`ok` でない（`missing`・`stale`・`suspect`）** 周期は、発火も解除もしない
  - **発火待ち（pending）は取り消す**（行を消す。遷移は配らない）。回復後に成立したら、その時刻から数え直す
  - **発火中（firing）は解除しない**（そのまま保つ）。解除は回復後に `clear` を下回った周期で行う
  - stale の閾値は `config/quality.yaml` の `stale_after_ms`（10 s。Store の唯一の情報源。0004 §2.12）。
    fand の `telemetry.t_sensor.stale_after_ms`（5000 ms。0110）とは別の値のまま
  - 0 °C 未満（断線。0110 §2.4 の `suspect`）は、この扱いで「評価しない」。**断線を高温と取り違えない**
- 再起動では、DB の発火待ちを取り消す（止まっていた間を連続とみなさない）。発火中は保ち、回復後に解除する（0131 §2.3 と同じ）
- **0139（`SENSOR_MISSING` / `SENSOR_FAULT` が発火したときだけ取り消す）は当てない。** 2 つの故障ルールは基板のサンプルだけを
  数え、`board.connector_12v2x6` は基板のサンプルに含まれないので、取り消しの根拠になる故障アラートが出ない。
  0139 の扱いにすると、収集が止まって回復した直後の 1 回で「60 s 続いた」とみなしうる（0131 §2.3 の問題そのもの）
- 逆に、基板の `SENSOR_FAULT`（基板の無音）では、このルールの発火待ちを取り消さない。値は別のプロセスが書き続けている

### 2.4 通知

- 経路は既存の `Router` のまま（0013・0135）。critical は夜間も Slack へ届く。**warning は夜間に届かない**
  （`night.warning_enabled: false`。0135 §2.3）。夜間に人へ届くのは 75 °C の重大だけ
- LLM の説明（#38）は既存のルールと同じく求める（除外の一覧 `llm_excluded_rules` に足さない。GPU の Fan と違い、
  LLM に渡さない理由が無い。渡すのは集計だけ（AGENTS.md ルール 8））
- 通知の文は 0142 の形に従う（`config/rules.yaml` の 4 つの必須の項目）。重大の見出しは「12V-2x6 コネクタが危険な温度です」、
  確かめることは警告が「GPU の負荷を下げ、電源ケーブルの差し込みを確認してください。」、重大が「すぐに GPU の負荷を止め、
  電源ケーブルとコネクタを確認してください。」。値の行は比較に合わせて「（閾値 75.0 °C 以上）」と書く（値の意味の種類
  `at_or_above` を足す。0142 §2.4 の「超過」は `>` のルールの言い方のため）

### 2.5 テスト（実機なし。AGENTS.md ルール 7）

DB へ直接書く（`coldaisle-telemetry` の行を模す）か、偽の hwmon を通した収集で書き、`Engine` を `SimulatedClock` で動かす。

- 設定: 出荷の値（2.2）、`clear >= threshold` の拒否、未知のメトリクスの拒否
- 警告: 65 °C で 60 s 後に発火、64.9 °C では発火しない、60 s 未満の揺れは履歴に残さない、62〜65 °C では解除しない・62 °C 未満で解除
- 重大: 75 °C で 10 s 後に発火（警告も 60 s で発火）、72 °C 未満で解除（警告は残る）
- 欠け: stale・missing・suspect（負の値）で発火待ちを取り消し、回復から数え直す。発火中は欠けで解除しない
- 再起動: 発火待ちは取り消し、発火中は保つ
- 基板の `SENSOR_FAULT` が発火しても、コネクタのルールは評価され続ける
- 日次 CSV の書き出しと一括再生では、コネクタの値が再生の DB に入らず、2 つのルールの行が作られない
- 収集（偽の hwmon）→ DB → 取り込みのルールエンジン → 通知（critical）

---

## 3. Consequences

### 良くなること

- 80 °C の Critical Safety（と fand が止まっている間）より前に、コネクタの発熱に人が気づける
- 断線（負の値）・収集の停止を高温と取り違えない。空白をまたいで「続いた」とみなさない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 夜間の 65〜75 °C は通知されない（warning が夜間に届かない） | 夜間も 75 °C の重大は届く。夜間の warning の方針は 0135 §2.3 / #19 の範囲で、本記録では変えない（§5 の 2） |
| センサーが欠けている（断線・収集の停止）ことは、このルールでは知らせない | §5 の 1 で別に決める。発火中のアラートは欠けでは解除しないので、高温の通知が「解除」で消えることはない |
| 重大の発火が 10 s 遅れる | 温度の上がりはゆっくりで（1）、80 °C までの余裕は数分のオーダー。即時（0 s）が要るなら値を変えるだけ |
| 警告と重大が同じ温度で 2 通届く | rule_id が違い、先頭の行の重大度で区別できる（0135 §2.2） |
| `config/rules.yaml` の 75 °C と `safety.yaml` の 80 °C の大小を機械で確かめていない | ルールエンジンは `safety.yaml` を読まない（レイヤを混ぜない）。§5 の 3 |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| `ThresholdRule` に `board.connector_12v2x6` を書く | 基板のサンプルに含まれないので一度も評価されない（1.1） |
| `ThresholdRule` に「DB から読む」の切り替えを足す | 欠けたときの扱い（0139 と 0131 §2.3）と比較（`>` と `>=`）が型の中で分岐する。型を分けたほうが、どちらの規則に従うかが型で決まる |
| 重大を即時（0 s） | 1 回の読み違いで夜間の critical が鳴る。温度はゆっくり動くので、10 s の確認の費用は小さい |
| 重大を 60 s（警告と同じ） | 75 °C から 80 °C までの余裕を不要に削る |
| 解除を発火と同じ値（ヒステリシスなし） | 閾値の上下の揺れで発火と解除を繰り返す（要件 §6.4） |
| 欠けたときに 0139 の扱い（故障アラートが出たときだけ取り消す） | コネクタの値には故障アラートが出ないので、空白をまたいで発火しうる（2.3） |
| 断線（負の値）で通知する | 高温ではない。欠けの通知として別に決める（§5 の 1） |
| 75 °C 以上では警告を解除して重大だけにする | 状態機械に「別のルールを見て解く」依存が入る。警告の履歴（いつ 65 °C を越えたか）も消える |

---

## 5. 未決事項（仕様に無い判断を含む）

本記録で仕様に無い判断をしたもの（所有者の承認が要る）:

- 重大の発火までの時間を **10 s** にした（2.2。推奨）
- 解除を **発火 − 3 °C**（62 °C・72 °C）にした（2.2）
- 比較を **以上（`>=`）** にした（2.1。既存の `ThresholdRule` は `>`）
- 欠け・stale・suspect は **0131 §2.3 の扱い**（0139 ではない）にした（2.3）
- 警告と重大を独立の 2 つのルールにし、両方が発火しうる形にした（2.2）
- LLM の説明の対象に含めた（2.4）

先送りした論点:

1. **センサーの欠けの通知。** `board.connector_12v2x6` が missing・stale・suspect のまま続いても、取り込みのルールは何も出さない
   （`SENSOR_MISSING` は基板のサンプルだけを数える。fand の `t_sensor_stale` は fand が動いているときだけ）。
   internal telemetry の必須の入力（`config/internal-telemetry.yaml` の `required: true`）の欠けを通知するルールを、別の Issue で決める
2. **夜間の warning。** 65 °C の警告を夜間にも届けるか（このルールだけ critical 扱いにするか・`night.warning_enabled` を見直すか）。
   0135 §2.3 / #19 の範囲
3. **`safety.yaml` の上限との整合。** 80 °C を変えたときに 75 °C を見直す手順（今は人の確認）。`safety.yaml` は導入先にだけ置かれる
   （`docs/control-config.md`）ので、CI では確かめられない
4. **値の確定。** 65 / 75 °C・60 / 10 s・3 °C の幅は provisional。#50 の較正（T_SENSOR の誤差）と、運転の記録（日次レポートの最高値）で見直す
5. **案件資料（`coldaisle-escalate`）の「24 時間の p95 が閾値の内側か」の行。** 今は `ThresholdRule` / `RangeRule` だけで、
   本記録のルールは含まない。要るなら別に足す
6. **通知の文の言い回し。** 2.4 の文は provisional（0142 の後に足した）。運用で見直す
7. **内部の telemetry の再生。** 日次 CSV に `board.connector_12v2x6` が無いので、過去の記録でこのルールを検証できない。
   要るなら CSV の列か別の再生の経路を別に決める
