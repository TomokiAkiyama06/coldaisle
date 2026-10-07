# 決定記録 0092: worker へ渡す frame の `applied` は各 zone の `FanHardwareResult.applied_demand` から作り、確かめられないときは欠測のまま渡す

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が Codex P2 の推奨案で承認）
- **Date**: 2026-10-07
- **Supersedes**: [0077](0077-learned-proposal-handoff.md) の次の部分だけを置き換える。0077 の他の点は有効。
  - §2.3 の表の `applied` の行の「出どころ」（「その tick の effective demand（Hardware Backend へ渡した値）」）
- **関連**: [0077](0077-learned-proposal-handoff.md) §2.3（worker の入力 frame） /
  [0073](0073-air-balance-control-config-integration.md) §2.5 (a)（`FanHardwareResult.applied_demand` の定義） /
  [0087](0087-dataset-v2-action-grid.md)（学習側の action 列は ControlTick の effective を使う） /
  `AGENTS.md`「絶対に守るルール」2・4
- **対象 Issue**: #86（0077 段階 1 の実装の前に決める点として Issue のコメントに残したもの）

---

## 1. Context

0077 §2.3（FINAL）は、`coldaisle-fand` が毎 tick worker へ送る frame の欄 `applied` の出どころを
「その tick の effective demand（Hardware Backend へ渡した値）」とし、worker はこれを観測 window の
action（`ObservedFanAction`）に使うと決めた。

0077 のマージ後に残った Codex の指摘（P2）は、**Backend へ渡す前の effective demand は、実際に Fan に
効いた値と違うことがある**というものである。

- `FanHardwareResult.applied_demand`（0073 §2.5 (a)）は、profile の制約（`minimum_stable_demand` への
  引き上げ・未起動 zone の `startup_demand`）を掛けた後の、PWM へ写す直前の demand である。effective が
  最低安定の値より低い tick では、効いたのは引き上げた値の方である
- 書き込みや readback に失敗した zone では、effective の値は「渡した」だけで「効いた」とは言えない。
  `applied_demand` はこのとき `None` になる（`write_ok` と `readback_ok` の両方が真のときだけ値を持つ）
- Backend が例外で結果を返さなかった tick には、zone ごとの結果そのものが無い
- 起動直後（`STARTUP` の Max の書き込みを確かめる前）も、効いた値はまだ確かめられていない

worker が effective を「効いた action」として window に入れると、観測された温度の応答を実際とは違う
action に結び付けて推論することになる。0077 §2.3 は「届かなかった frame を補間しない。欠けは欠けとして
window に残し、Confidence / OOD と Gate が Fallback へ倒す」と決めており、確かめられない値を推測で
埋めることもこれと同じ理由で避けるべきである。

## 2. Decision

### 2.1 `applied` は各 zone の `FanHardwareResult.applied_demand` から作る

| frame の欄 | 出どころ（その tick の値） | 用途 |
|---|---|---|
| `applied` | その tick の **各 zone の `FanHardwareResult.applied_demand`**（Hardware Backend が返した、書き込みと readback を確かめた値） | 観測 window の action（`ObservedFanAction`） |

- zone ごとに独立に決める。ある zone の値が無くても、他の zone の値は渡す

### 2.2 確かめられないときは欠測のまま渡す（推測で埋めない）

次のときは、その zone の `applied` を**欠測（`null`）**にして frame を送る。effective demand・直前の
値・`requested` などで埋めない。

- その tick の結果が無い（Backend が例外で結果を返さなかった）
- その zone の `applied_demand` が `None`（書き込み・readback の失敗。起動時の確認前や、最低安定の写像の
  確認ができないときを含む）

worker は欠測を欠測として window に残す（0077 §2.3 の「補間しない」と同じ扱い）。その結果 Learned が
使えない tick は Confidence / OOD と Gate が Fallback へ倒す（AGENTS.md ルール4）。

### 2.3 変えないこと

- frame の他の欄（`snapshot`・`baseline`・`safety_floor` など）と、0077 の他の節は変えない
- decision trace（`ControlTick`）の `zones` の `effective` / `applied` の記録と版は変えない
- 学習データ（Thermal Dataset v2。0087）の action 列の作り方は変えない。本記録が決めるのは、走行中に
  `coldaisle-fand` が worker へ渡す frame の欄だけである

## 3. Consequences

### 良くなること

- worker の観測 window の action が、Fan に実際に効いた（確かめられた）値と一致する
- 書き込み・readback の失敗や最低安定の引き上げが、window の中で黙って別の値に化けない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 書き込みの失敗が続く間は action が欠測になり、Learned が使えない時間が増える | 安全側（Fallback）に倒れるだけ。書き込みの失敗そのものは Critical Safety が fault として扱う |
| 学習データの action（0087 は ControlTick の effective）と、推論時の action（applied）の出どころが違う | 最低安定の引き上げが起きない範囲では一致する。違いが効くかは段階 3 の shadow で見る（0087 は置き換えない） |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 0077 §2.3 のまま effective demand を渡す | 最低安定の引き上げ・書き込みの失敗のときに、効いていない値を「効いた action」として渡す（§1） |
| 結果が無いときは effective demand で埋める | 推測で埋めると、window の中で確かめられた値と区別できなくなる。0077 §2.3 の「補間しない」と食い違う |
| 結果が無い tick は frame を送らない | snapshot など他の欄は使えるのに、frame ごと捨てると window の欠けが増える。欠測は zone の欄だけで表せる |

## 5. 判断点

| 問い | 選択肢 | 決着 |
|---|---|---|
| frame の `applied` の出どころ | (a) **【採用】** 各 zone の `FanHardwareResult.applied_demand`。無い・確かめられないときは欠測 / (b) 0077 のまま effective demand | 決着（2026-10-07 所有者の決定、推奨案） |
