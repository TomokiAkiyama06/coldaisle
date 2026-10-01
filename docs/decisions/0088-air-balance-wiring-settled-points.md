# 決定記録 0088: 0078 段 3（Air Balance の協調の loop への配線）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-01、リポジトリ所有者が承認）
- **Date**: 2026-10-01
- **Supersedes**: なし（0078 / 0085 が決めていなかった点への追加と、0078 §2.3 の表の順の読み方の明記。
  どちらの記録の決定も置き換えない）
- **関連**: [0078](0078-air-balance-fallback-coordination.md) §2.2 / §2.3 / §2.5 / §2.7 / §2.11 /
  [0085](0085-coordination-failure-bypasses-gate.md) §2.1 / §2.3 /
  [0028](0028-fan-control-contracts.md) §2.7 /
  `src/coldaisle/control/loop.py` / `src/coldaisle/control/schema.py`
- **対象 Issue**: #81（実装 PR #214）

## 1. Context

0078 §2.11 の段 3（PR #214）で、Air Balance の協調を control loop へ配線し、`ControlTick` を v14 にした。
その実装で、0078 と 0085 のどちらにも書かれていない挙動が1点と、記録の文面だけでは
読み方が分かれうる点が2点あった。いずれも PR #214 の「人間レビューが必要な点」で示し、
2026-10-01 に所有者が実装のとおりで承認した。決定記録は追記のみなので、0078 / 0085 の本文には
手を入れず、ここに残す。記録だけを読んだ人が、実装と違う挙動を前提にしないためである。

## 2. Decision

### 2.1 Controller Gate 自身が例外を投げた tick の requested

**その tick の requested は、Gate へ渡すはずだった Baseline とする。** `mode: apply` で協調が値を上げた tick なら
coordinated baseline、それ以外（`off`・`shadow`・`skipped`・`not_needed`）なら raw baseline である。
実装は `ControlLoop._requested(mode, gate_baseline, selection=None)`。

- Gate の例外は、いままでどおり `fallback_exception`（detail `controller_gate: …`）へ翻訳し、次 tick の
  Critical Safety が `EMERGENCY` にする（0028 §2.7。変えない）
- 0085 の迂回（`mode: apply` で協調が `failed` の tick）とは別の経路である。迂回した tick は Gate を呼ばず、
  Gate へ渡すはずだった値はもともと raw baseline なので、0085 §2.1 の「requested = raw baseline」と矛盾しない
- 理由: 0078 §2.5 は「どの stage でも、Fallback の値は coordinated baseline」と決めている。また、
  raw baseline に戻すと 0078 §2.7 の不変条件「Fallback で回した tick の requested は `output` 以上」が
  破れ、その tick の trace を保存できない（`output` は Gate へ渡した値＝coordinated baseline）

### 2.2 `skipped` / `failed` の tick の trace の欄

0078 §2.7 は欄の型と不変条件を決めたが、`coordinate()` を呼ばなかった tick に、どの欄を `null` にするかは書いていない。
次のとおりとする。

| `status` | `projected_floors` / `projected_floor_basis` | `proposed` / `bounded_by_max_raise` / before・projected / `reasons` | `held` | `max_raise` |
|---|---|---|---|---|
| `skipped` | `null`（下限を見込まない） | `null` / 空 | 3 zone とも偽（保持を解いた） | 記録する |
| `failed` | `apply()` へ渡した値を記録する | `null` / 空 | 3 zone とも偽 | 記録する |

- `candidate` と `output` は、どちらも raw baseline（0078 §2.7・0085 §2.3）。Fallback が提案を返さなかった
  `skipped`（`baseline_unavailable`）では、どちらも `null`
- `skipped` の tick は下限の見込みを計算しない。条件が欠けた tick（Fan fault・tach 未確認など）の
  見込みは、計算しても意味を持たないためである
- trace の検証は、`skipped` の tick に `proposed`・`bounded_by_max_raise`・before / projected の状態・`reasons` が
  あれば拒む
- `skipped` / `failed` の `held` は **`null` を許さず、3 zone とも明示的な偽**を要る（0078 §2.7 の「`held` は
  すべて偽（保持を解いた）」のとおり）。`null` を許すと、「解いた」と「記録が無い・不明」を区別できない

### 2.3 Top の Fan fault の `skip_reason`

0078 §2.3 の表は**上の行から順に判定し、最初に欠けた条件を `skip_reason` にする**と読む。

- Top の Fan fault（`TACH_STALL`・`WRITE_FAILURE`・`READBACK_MISMATCH`・`ENABLE_REVERTED`）は、0028 §2.7 で
  無条件に `EMERGENCY` になる。そのため Top の Fan fault が確定した tick の `skip_reason` は、`zone_fan_fault` ではなく
  `safety_state` になる。**ただし表で `safety_state` より上の行（`air_balance_disabled`・`operating_mode`・
  `snapshot_unavailable`・`baseline_unavailable`）がすべて満たされている tick に限る。** 同じ tick にそれらの
  条件の欠け（例: `MANUAL`・snapshot の不在・Fallback の例外）が重なれば、その上の行の理由になる。
  Front / Rear の Fan fault（`EMERGENCY` にならない間）も同じく、上の行がすべて満たされた tick で `zone_fan_fault` である
- 確定前の tach 無応答（`tach_unconfirmed_zones`）は Safety の状態を変えないので、上の行がすべて満たされた tick では
  Top でも `tach_unconfirmed` になる
- どちらでも協調は `skipped` で raw baseline を使う（0078 §2.3 の帰結は変わらない）

## 3. Consequences

### 良くなること

- 記録だけを読んでも、Gate の例外の tick と協調の失敗の tick の requested、`skipped` の trace の形、
  Top の fault の理由を、実装と同じに読める
- Gate の例外の tick も 0078 §2.7 の不変条件を満たし、trace を保存できる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| Gate の例外の tick に、協調で上げた値（Learned MPC ではない決定論的な値）が requested に残る | 上げるだけ・`max_raise` 以内の値で、合成は迂回しない。次 tick は Safety が `EMERGENCY` にする（0028 §2.7） |
| `skipped` の tick から、その tick の下限の見込みを読めない | 合成の結果（`ZoneRecord.demand` の `safety_floor`・`guard_floor`・`bound_by`）は毎 tick 残る |
| Top の Fan fault を `zone_fan_fault` で数える集計は、Top の分を取りこぼす | 集計は `safety_state` と同じ tick の `faults` を合わせて読む。shadow の集計の道具（0078 §5）を作るときに扱う |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| A. Gate の例外の tick は raw baseline を requested にする（0085 の迂回と同じ値） | 0078 §2.5 の「Fallback の値は coordinated baseline」と食い違う。0078 §2.7 の不変条件（requested ≥ `output`）が破れて trace を保存できない。不変条件に例外を足すと、Gate を通った tick の検査が弱まる |
| B. `skipped` の tick も下限の見込みを計算して記録する | Fan fault・tach 未確認・STARTUP などの tick では見込みに意味が無く、計算そのものが不要な依存（合成の状態の読み出し）を増やす |
| C. Top の Fan fault を `safety_state` より先に判定し、`zone_fan_fault` にする | 0078 §2.3 の表の順を入れ替えることになる。協調の帰結（`skipped`）は同じで、理由の欄の差のために表を変える利益が小さい |

## 5. 未決事項

なし。協調の状態を `airflow.html` に出すかと shadow の集計の道具は、0078 §5 のとおり。
