# 決定記録 0148: fand の EMERGENCY / DEGRADED と trace の途絶を、監視系が DB の decision trace を読んで Slack へ知らせる

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし（0012 のルールエンジンにルールの型を 2 つ足す。0013 / 0135 / 0142 の通知の経路・形は変えない）
- **関連**: [`0028`](0028-fan-control-contracts.md) §2.5 (d) / §2.7 /
  [`0029`](0029-telemetry-loss-classes.md) /
  [`0071`](0071-control-trace-read-api.md) §2.2 / §2.8 /
  [`0110`](0110-t-sensor-ceiling-and-enablement.md) /
  [`0137`](0137-trace-telemetry-health.md) /
  [`0012`](0012-rule-engine.md) / [`0013`](0013-notifications.md) / [`0135`](0135-critical-notifications-to-slack.md) /
  [`0142`](0142-japanese-notification-text.md) / [`0131`](0131-gpu-fan-service-alert.md) §2.3 /
  [`0139`](0139-rule-pending-reset-on-missing.md) /
  `config/rules.yaml`、`src/coldaisle/rules/`、`src/coldaisle/daemon.py`
- **対象 Issue**: #330

---

## 1. Context

fand（Critical Safety）が `EMERGENCY`（絶対温度の上限・Top の tach stall・書き込みの失敗の連続など。0028 §2.7・0110）や
`DEGRADED`（zone の fault・Telemetry の欠け）になっても、今は decision trace（`control_traces`）と fand のログにしか残らず、
Slack に届かない（2026-10-15 の所有者との確認）。0028 §2.7 の表は tach stall で「通知する」と書いているが、その経路が無い。

制約:

- 監視系は fand の状態を**読むだけ**。制御の経路・fand のプロセスに触らない（AGENTS.md ルール 2・6）。
  fand から監視系への新しい依存（ソケット・ファイル）を作らない
- trace は制御 ML と同じ粒度の時系列で、LLM のプロンプトへ直接入れない（ルール 8・0071 §2.8）

fand は tick ごとに `control_traces` へ 1 行を書く（0071）。`SqliteStore.latest_control_trace()`（`seq` が最大の行。
0071 §2.2）が既にあり、`GET /api/v1/control/latest` と同じ「最後に記録した tick」を返す。

---

## 2. Decision

### 2.1 経路: 取り込みのルールエンジンが DB の最新の trace を読む

- 取り込みデーモン（`coldaisle-daemon`）のルールエンジンが、評価のたび（サンプルごとと、サンプルが来ないときの tick ごと）に
  `latest_control_trace()` で**最後に記録した 1 行だけ**を読み、`state.safety_state` と `faults` の 2 つの欄だけを見る。
  0131 §2.3 の「DB の最新値を評価時刻で読む」ルールと同じ読み方
- fand・制御の経路・`control/` のコードは変えない。監視系から fand への経路も、fand から監視系への経路も足さない
- ルールのパッケージ（L2）は `control/` を import しない。trace の JSON を 2 つの欄だけ読む小さな読み手を `rules/` に置く
- **実時間で動くときだけ読む。** `--source replay` と `--speed` ≠ 1 では読まない（評価時刻が CSV・シナリオの時刻で、
  別のプロセスが壁時計で書いた trace と比べられないため。0007 §2.11）

### 2.2 3 つのルール

| ルール（`rule_id`） | 重大度 | 成立 | 発火まで | 解除 |
|---|---|---|---|---|
| `fand_emergency`（`FAND_EMERGENCY`） | critical | 最新の trace の `safety_state` が `emergency` | 0 s（即時） | `emergency` でない新しい trace が 10 s 続く |
| `fand_degraded`（`FAND_DEGRADED`） | warning | 最新の trace の `safety_state` が `degraded` | 120 s | `degraded` でも `emergency` でもない新しい trace が 10 s 続く |
| `fand_trace_silent`（`FAND_TRACE_SILENT`） | warning | 最後の trace から 60 s 以上新しい行が無い、または最新の trace を読めない | 0 s（60 s の無音そのものが待ち） | 読める新しい trace が 30 s 続く |

値はすべて `config/rules.yaml` に置き（ルール 9）、**provisional**。

- **EMERGENCY は即時**: 全 Fan が Max で、原因は温度の上限・Top の stall など人が見るべきもの。fand 自身が
  `fault_clear_hold_ms`（導入先 30 s。0119）で状態を保つので、1 tick の揺れでは出入りしない
- **DEGRADED は 120 s 続いたら**: 1 tick だけの fault でも `fault_clear_hold_ms` の 30 s は `DEGRADED` が残る
  （T2 の「一時的」の試験で確認）。それを拾わず、Telemetry の収集の再起動（数秒 + 30 s）も拾わない長さ
- **DEGRADED の判定は EMERGENCY の間は止める**: `emergency` の trace の間、`fand_degraded` は始めも発火も解除もしない
  （発火中なら保ち、解除の待ちを数え直す。発火待ちは取り消す）。悪化（DEGRADED → EMERGENCY）で「DEGRADED が解けました」を出さず、
  EMERGENCY と DEGRADED の 2 通を同時に出さないため。EMERGENCY が解けて DEGRADED が残れば、そこから数え始める
- `startup` は通知しない（起動の takeover で全 Max の正常な段階）。`normal` と同じく「異常でない」として扱う
- **trace の新しさ**: 最新の trace の `ts_ms` が評価時刻より `fand_trace_silent.silence_s`（60 s）以上前なら、
  その trace は「いまの状態」として使わない。閾値はこの 1 か所に持つ
- 時計が戻って `ts_ms` が評価時刻より先にある trace は新しいとみなす（年齢は負。0071 §2.2 と同じく丸めない）
- **発火の待ち（DEGRADED の 120 s）と解除の待ち（10 / 10 / 30 s）は、取り込みのプロセスの単調時計で、新しい行を初めて
  読んだときの値の差で測る。** 新しい行かどうかは trace の `seq`（記録の順の番号。再利用しない。0071 §2.2a）が変わったかで
  決め、壁時計の大小に頼らない
  - 評価は約 1 s ごとに同じ最後の行を何度も読むので、評価の時刻で測ると、fand が 1 行書いて止まったときにその 1 行だけで
    待ちが満ち、発火中のアラートを解いてしまう。新しい行が来ない限り待ちは進まない
  - trace の `ts_ms` は fand が壁時計で付けるので、その差で測ると、NTP の補正で前へ跳んだとき隣り合う 2 行で待ちが満ち、
    後ろへ跳んだとき壁時計が追いつくまで待ちが止まる。単調時計ならどちらの向きの跳びにも影響されない
  - 途絶の判定そのもの（60 s）だけは、最新の行の `ts_ms` と評価時刻の差で測る（上の「trace の新しさ」）

### 2.3 欠け（trace が古い・読めない）のときの EMERGENCY / DEGRADED（0131 §2.3 と同じ扱い）

- 発火待ち（pending）は取り消す。発火中は**解除しない**（解除の待ちだけを数え直す）
- 理由: fand が止まった・書けないことは「状態が戻った」証拠ではない。止まったことは `FAND_TRACE_SILENT` が別に知らせる
- 0139 の取り消し（`SENSOR_MISSING` / `SENSOR_FAULT`）は当てない。入力が基板のサンプルではなく trace だから。
  取り込みの再起動での復元も 0131 と同じ（発火中は保ち、発火待ちは取り消す）

### 2.4 trace の版の読み分け

- **版ごとに分けない。** `state.safety_state` と `faults[].code` / `faults[].zone` は v1 から形が変わっていない
  （v4 で fault code が増えただけ）。v14 以前・v15・提案中の v16（#332 の `run_id`）を同じに読む
- 2 つの欄の形が想定と違う（`safety_state` が 4 つの値のどれでもない・`faults` が配列でない・`code` が文字列でない）
  trace は「読めない」とし、`FAND_TRACE_SILENT` で知らせる（`読めない（schema_version N）`）。将来の版が形を変えたとき、
  黙って「異常なし」にしないため
- `ControlTick` の検証（pydantic）は通さない。監視系が制御の型に依存せず、未知の版でも 2 つの欄が読めれば判定を続けるため

### 2.5 本文（0142 の形）

文は `config/rules.yaml` の `label` / `title` / `resolved_title` / `hint`。値の行の先頭（`detail`）は新しい値の種類 `control`
（出来事のルールと同じく `detail` をそのまま出す）。

- `FAND_EMERGENCY` / `FAND_DEGRADED` の `detail`: `原因: <日本語の名前>（<fault code>[ / <zone>]）、…`。
  名前は `config/rules.yaml` の `fault_labels`（2 つのルールで YAML のアンカーで共有）。無い code は code のまま出す。
  `faults[].detail`（fand の自由記述の英語。値を含む）は**載せない**（2.6）。詳しくは `/api/v1/control/latest`・
  `coldaisle-control status`・`journalctl -u coldaisle-fand` で見る（`hint` に書く）
- `FAND_TRACE_SILENT` の `detail`: `最後の decision trace から N 秒（tick T）` / `最新の decision trace を読めない（schema_version N・tick T）`
- メトリクスを持たない（ID の行は `FAND_EMERGENCY` だけ）。値の行の残りは `context_always`（室温）

例（EMERGENCY の発火）:

```text
🔴 [重大] Fan 制御が緊急状態です（すべての Fan を最大で回しています）
原因: 絶対温度の上限に達した（absolute_temperature_limit）・室温 26.1 °C
GPU・CPU・コネクタの温度と Fan の回転を確かめてください。理由は /api/v1/control/latest の faults と journalctl -u coldaisle-fand にあります。
FAND_EMERGENCY
```

### 2.6 LLM へ渡すもの（ルール 8・0071 §2.8）

- trace（JSON・列）はプロンプトへ入れない。ルールエンジンが作るのは**閉じた語彙の 1 行**（状態・fault code・zone・
  設定の日本語の名前・無音の秒数と tick 番号）だけで、それが `alerts.detail` に残り、通知と自動の説明（#38）の材料になる
- fault の自由記述（`faults[].detail`）は DB の `alerts` にも通知にも LLM にも写さない（値の列を含みうるため）
- 3 つのルールは `llm_excluded_rules` に足さない（0146 の一覧は空のまま）。説明に渡るのは上の 1 行と、既存の
  `_rule_evidence`（継続の秒数）・`get_latest` の品質の集計だけ（メトリクスを持たないので直近の統計は付かない）

### 2.7 夜間・連投・解除（0013 / 0135 のまま）

- 夜間（22〜7 時）は `FAND_EMERGENCY`（critical）だけが届く。`FAND_DEGRADED` / `FAND_TRACE_SILENT`（warning）は
  `night.warning_enabled` が false の間は送らない（0001 D-04）。夜間に発火した warning は朝に送り直さない（既存の振る舞い）
- 遷移だけを送る（発火と解除で 1 通ずつ）。同じアラートの連投は `repeat_after_s` で抑える。発火を届けたものの解除は必ず届く
- critical の解除（`✅ [重大 解除]`）は fand の状態が `emergency` を抜けたこと。DEGRADED が残っていれば 120 s 後に
  `FAND_DEGRADED` が別に届く

### 2.8 意図して fand を止めているとき（T1 の後・保守）

- 監視系は「意図した停止」と「落ちた」を見分けない（systemd・journal を読まない。0131 と同じ）
- **DB に trace が 1 行も無いときは評価しない**（fand を導入していない機械で鳴らさない）
- trace がある機械で fand を意図して止めるときは、`FAND_TRACE_SILENT` が 1 回だけ warning で届く（遷移だけを送るので
  繰り返さない。取り込みを再起動しても発火中のアラートを復元するので出し直さない）。止めている間も鳴らしたくなければ、
  `config/rules.yaml` の `fand_trace_silent.enabled` を false にして取り込みを再起動する（`hint` に書く）
- EMERGENCY / DEGRADED が発火中のまま fand を止めると、そのアラートは次に fand が動いて状態が戻るまで発火中のまま残る（2.3）

---

## 3. Consequences

### 良くなること

- fand の EMERGENCY が夜間も Slack に届く。原因の fault code が日本語の名前と一緒に本文に出る
- fand が止まった・trace を書けないことに気づける（いまは画面を開くまで分からない）
- fand・制御の経路は変わらない。監視系は既存の読み出し（0071）だけを使う

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 最新の 1 行だけを見るので、評価の間（約 1 s）にだけ現れた状態は見ない | fand は `fault_clear_hold_ms`（30 s）の間状態を保つ。`fault_clear_hold_ms` を評価の間隔より短くする設定は想定しない（§5 の 3） |
| 基板の無音（`SENSOR_FAULT`）は fand でも `air_telemetry_stale` の DEGRADED になり、2 通届く | 原因の行（`air_telemetry_stale`）で同じ事象と分かる。まとめるかは §5 の 4 |
| 取り込みデーモンが止まると、この通知も止まる | 取り込みの停止は fand の DEGRADED（`air_telemetry_stale`）になるが、それを知らせる側が止まっている。外からの死活監視は範囲外（§5 の 5） |
| fand を意図して止めると warning が 1 回届く | 2.8。導入前の機械では鳴らない |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| fand が直接 Slack へ送る | 制御のプロセスにネットワークと秘密を持たせる。fand の tick に通知の失敗・遅延を混ぜる（0028 の期限） |
| fand の状態をメトリクス（`sys.fand_state` など）として readings へ書く | fand に新しい書き込みの経路を足す。trace に同じ情報が既にある |
| 監視系が fand の管理ソケットに状態を問い合わせる | 監視系から fand への新しい依存。管理ソケットは人の操作の入口（0072） |
| 前回から増えた trace をすべて読む | 起動時や遅れたときに大量の JSON を読む。fand が状態を保つので最新の 1 行で足りる（3） |
| `ControlTick` で検証して読む | ルールのパッケージが制御の型に依存し、未知の版で判定が止まる（2.4） |
| DEGRADED も即時 | 1 tick の fault でも 30 s 残るので、Telemetry の再起動のたびに鳴る |
| `faults[].detail` を本文に出す | 英語の自由記述で、値を含み、LLM の説明の材料になる（2.6） |
| 意図した停止を見分ける（systemd を読む・印のファイル） | 監視系が systemd・fand の置き場所に依存する。warning 1 回で足りる（2.8） |

---

## 5. 未決事項・仕様に無かった判断

1. **値（provisional）**: EMERGENCY 即時・DEGRADED 120 s・無音 60 s・解除 10 / 10 / 30 s は Claude の案。所有者の承認が要る
2. **`fand_trace_silent` を出荷で有効にした**（2.8）。fand を常駐させるまでは、T1 の後に 1 回 warning が届く。
   常駐の前は false で出す案もある（所有者が選ぶ）
3. **最新の 1 行だけを見る**（3）。`fault_clear_hold_ms` が取り込みの評価の間隔（約 1 s）より短いと、短い EMERGENCY を見落としうる
4. **基板の無音と fand の DEGRADED（`air_telemetry_stale`）の 2 通をまとめない**（3）
5. **取り込みデーモン自身の停止**は知らせられない（範囲外。別の Issue）
6. **Front / Rear の tach stall は DEGRADED（warning）なので夜間は届かない**。0028 §2.7 の「通知する」を夜間も含むと読むなら、
   tach stall を含む DEGRADED を critical にする別の判断が要る
7. **fault の日本語の名前**（`fault_labels`）は Claude の案。所有者が `config/rules.yaml` で直してよい
8. **`startup` を通知しない**（2.2）。`startup` のまま進まない状態を知らせるかは別に決める
9. **自動の説明（LLM）を求める**（2.6）。閉じた語彙の 1 行だけが渡る。求めないなら `llm_excluded_rules` に足す（0146 の一覧の変更になる）
