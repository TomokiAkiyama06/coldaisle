# 決定記録 0093: Air Balance の協調の shadow 集計は新しい CLI、offline 評価の Baseline の arm は再計算しない

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が推奨案で承認）
- **Date**: 2026-10-07
- **Supersedes**: なし（0078 §5 の未決事項のうち2行を決着させる。0078 の決定は置き換えない）
- **関連**: [0078](0078-air-balance-fallback-coordination.md) §2.5 / §2.7 / §2.8 / §2.11 / §5、
  [0085](0085-coordination-failure-bypasses-gate.md)、
  [0088](0088-air-balance-wiring-settled-points.md) §2.2 / §2.3 / §3、
  [0054](0054-offline-evaluation-attribution-and-gates.md) §2.1 / §2.7、
  [0073](0073-air-balance-control-config-integration.md)、
  [0074](0074-supervisor-shadow-wiring-and-episode-evaluation.md) §2.1、
  `src/coldaisle/supervisor_shadow.py`（同じ形の手本）、`src/coldaisle/control/evaluation/evaluator.py`
- **対象 Issue**: #81

## 1. Context

0078 §5 は、実装の段 4（§2.11）で決める点として次の2行を残した。いずれも実測を待たない。

| 論点（0078 §5 の行） | どこで決めるか（0078） |
|---|---|
| shadow の集計の道具（新しい CLI か、#91 の offline 評価に足すか） | 実装の段 4 |
| offline 評価の Baseline の arm が coordinated baseline を再現する形（0054 の attribution との関係） | #91 |

段 3（PR #214）で loop は、適用側の requested と、0053 の shadow の Fallback の値の両方に
**協調後の値**（その tick の coordinated baseline。`shadow` / `off` / `skipped` などでは raw baseline）を
記録するようになった（0078 §2.5、`loop.py` の `_shadow_record(baseline=coordination.gate_baseline)`）。
段 4 の一部（PR #220）で、昇格の証拠は1つの `fan-policy.yaml` の trace に束縛された（評価報告 v4 の
`fan_policy_trace_binding`）。残る2点を、2026-10-07 に所有者が推奨案で決めた。

## 2. Decision

### 判断点

| 判断点 | 決定 | 状態 |
|---|---|---|
| (a) shadow の集計の道具 | **新しい CLI `coldaisle-air-balance-shadow`**。保存済みの decision trace を読むだけ。`coldaisle-supervisor-shadow` と同じ形。#91 の offline 評価には足さない | 決着（2026-10-07 所有者の決定、推奨案） |
| (b) offline 評価の Baseline の arm | **評価器で再計算しない**。trace に記録した値を読むだけで coordinated baseline の再現になる。arm の鍵に `mode` を足さない | 決着（2026-10-07 所有者の決定、推奨案） |

### 2.1 (a) shadow の集計は新しい CLI

`uv run coldaisle-air-balance-shadow --evidence <manifest.yaml> --control-config <dir> --out <file>`
（実装は `src/coldaisle/air_balance_shadow.py`）。

**出す内容は次だけで、合否は出さない。**

| 欄 | 内容 |
|---|---|
| `status` ごとの件数 | `off` / `skipped` / `not_needed` / `shadow` / `applied` / `failed` の6つの鍵を常に全部（0 も省かない） |
| `skip_reason` ごとの件数 | 0078 §2.3 の7つの鍵を常に全部 |
| zone ごとの引き上げ幅の分布 | `counterfactual_output - candidate`（zone ごと）。`counterfactual_output` を記録した tick（`shadow` / `not_needed` / `applied`）だけから作る |
| 引き上げの有無が切り替わった回数 | zone ごとと、「どれかの zone で上がっている」の有無について |
| zone の Fan fault で `skipped` になった tick の数 | zone ごと。0088 §3 のとおり、Top は `safety_state` と同じ tick の `faults` を合わせて数える（§2.1.2） |

- **合否・推奨・閾値を持たない。** shadow → apply の合否の基準は、0078 §5 のとおり shadow の実データを
  見てから所有者と決める（所有者の判断 10）。基準の無いまま「通った」に読める欄を出さない
- 理由: この集計は shadow → apply（0078 §2.8 の段 1 → 2、§2.11 の段 6 → 7）を判断する人の材料で、
  #91 の制御器の比較・昇格の証拠（`_check_evidence()` が読む報告）とは用途が違う。#91 の報告に足すと、
  合否の基準がまだ無い数が昇格の証拠の形（報告の版・digest）に入り、基準を決めたときに報告の版を
  もう一度上げることになる
- **書き込み経路・制御へ届く経路を持たない。** 証拠の DB は `coldaisle-evaluate` の `EvidenceDatabase`
  （`immutable=1`）で開き、書くのは `--out` の1ファイルだけ。制御側（`control_daemon.py` / `control/`）は
  この CLI を import しない。LLM のツールにしない（AGENTS.md ルール 1 / 8）
- **同じ入力からは同じ bytes を出す。** 生成時刻を持たない（0054 §2.7 と同じ）

§2.1.1〜§2.1.3 は、(a) の範囲で実装が `coldaisle-supervisor-shadow` / `coldaisle-drift` に倣って選んだ形である。
所有者が決めたのは (a) の表の内容までで、ここから下は実装 PR のレビューで所有者が確認する。

#### 2.1.1 入力と拒否（`coldaisle-supervisor-shadow` と同じ規律）

- manifest（`--evidence`）は `schema_version` と期間 `[start_ms, end_ms)` だけを持つ。相対指定を置かない
- 分布の percentile（nearest-rank。`coldaisle.control.evaluation.stats.summarize`）は
  `config/air-balance-shadow.yaml` から読む（AGENTS.md ルール 9。コードに既定値を持たない）
- 次のどれかがあれば **run 全体を拒否し、何も書かずに 1 を返す**（落とした行を除いて続けない）
  - period の外の行・索引（`ts_ms` / `tick_id` / `schema_version`）と本文の食い違い・本文を読めない
  - `runtime` の無い trace、`air_balance_coordination` の無い（v14 未満の）trace
  - `runtime.config` の `fan-policy.yaml` / `air-balance.yaml` / `fan-hardware.yaml` の hash が、渡した
    Control Config の hash と違う tick（PR #220 の束縛と同じ3つ。`max_raise`・`mode`・較正・stable demand が
    違う区間を1つの分布に混ぜない。期間を分けて渡す）
  - `--out` が入力（証拠の DB とその添え file・manifest・`config/air-balance-shadow.yaml`・Control Config の
    4ファイル）を指す（symlink を含む）。書き出しは置き換えなので、読むだけの CLI が証拠を壊さないよう読む前に拒む

#### 2.1.2 数え方

- **引き上げの有無**は、0078 §2.7 の読み方（「apply なら上げていたかは `counterfactual_output > candidate`
  で読む」）のとおり、その zone で `counterfactual_output > candidate`（厳密な不等号。不感帯を置かない）
- **切り替わりの回数**は、`counterfactual_output` を記録した tick だけを `(ts_ms, tick_id)` の順に並べ、
  隣り合う2つで有無が変わった回数とする。`skipped` / `failed` / `off` の tick は列に入れない（有無の情報を
  持たないため）。それらの tick の数は `status` / `skip_reason` の件数で別に読む。再起動を跨ぐ区間も
  1つの列として数える
- **zone の Fan fault による `skipped`** は、`status: skipped` で `skip_reason` が `zone_fan_fault` または
  `safety_state` の tick のうち、同じ tick の `faults` にその zone の Fan fault（`TACH_STALL` /
  `WRITE_FAILURE` / `READBACK_MISMATCH` / `ENABLE_REVERTED`）がある tick の数を zone ごとに数える。
  Top の Fan fault は無条件の `EMERGENCY` なので `skip_reason` が `safety_state` になる（0088 §2.3）。
  `zone_fan_fault` だけで数えると Top を取りこぼす（0088 §3）。1つの tick に複数の zone の fault があれば
  それぞれの zone に数える
- 引き上げ幅の分布は、全件（0 を含む）と、上げた tick だけの2つを zone ごとに出す。値が1つも無ければ
  `null`（0 で埋めない）

#### 2.1.3 出力の形式

`--format json`（既定。保存と比較の正本、canonical JSON）と `--format markdown`（同じ報告を人が読む表に
写すだけで、数を足さない）。`coldaisle-drift` と同じ。

### 2.2 (b) offline 評価の Baseline の arm は再計算しない

- 評価器（`coldaisle.control.evaluation`）は Fallback も協調器も import せず、Baseline の値を
  **作り直さない**。適用側の arm は trace の `ZoneRecord`（requested / effective）を、counterfactual 側は
  0053 の shadow の Fallback の値を、記録されたまま読む。段 3 で loop がどちらにも協調後の値を記録して
  いるので、読むだけで coordinated baseline の再現になる（0054 §2.1「trace の証拠から決める」のとおり）
- **arm の鍵（`AppliedArm` / `CounterfactualArm`）に協調の `mode` を足さない。** PR #220 の束縛で、
  昇格に使える報告は1つの `fan-policy.yaml`（したがって1つの `mode`）の trace に限られ、1つの報告の中で
  raw baseline と coordinated baseline の区間が混ざらないため。束縛の外で `mode` の違う区間を混ぜた報告は
  `fan_policy_trace_binding` の不一致として `_check_evidence()` が拒む
- コードの変更は要らない。評価器が Fallback・協調器を import しないことを試験で固定する
- 評価器で再計算すると、記録時の設定・保持の状態・下限の見込みを評価器が持ち直すことになり、
  制御と評価で同じ規則を2か所に持つ（0054 §2.6「ほかの契約が持つ値を写さない」に反する）

## 3. Consequences

### 良くなること

- shadow の期間を、制御プロセスの外で、何度でも同じ bytes で集計できる
- 合否の基準の無い数が昇格の証拠に入らない。基準を決めるときに、#91 の報告の版を動かさずに済む
- 評価器が制御の規則を持ち直さないので、協調の規則を変えても評価器が古い規則で再計算することがない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| shadow の集計と #91 の報告が別のファイルになり、同じ期間を2回読む | どちらも読むだけで、期間は manifest で揃えられる |
| 切り替わりの回数は `skipped` を跨いで数えるので、`skipped` による実際の上げ下げ（apply では raw baseline に戻る）は含まない | `skipped` の件数と理由は別に出る。合否の基準を決めるとき（0078 §5）に、必要なら数え方を足す |
| 開ループの値（0078 §5 の6行目）で、apply の閉ループは再現しない | 0078 §5 のとおり、その差の扱いは合否の基準と一緒に決める。本記録は集計の道具だけを決める |
| 評価器が再計算しないので、記録の誤りを評価器が検出できない | 記録の不変条件は `AirBalanceCoordinationRecord` / `ControlTick` の検証が読み書きの両方で持つ（0078 §2.7） |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| #91 の `coldaisle-evaluate` の報告に shadow の集計を足す | 用途（shadow → apply の判断材料）と昇格の証拠が混ざる。合否の基準が無いまま報告の版と digest に入る |
| CLI に合否（閾値と判定）を持たせる | 基準は shadow の実データを見てから決めると決定済み（0078 §5、所有者の判断 10） |
| 評価器で Baseline の arm を `mode` に応じて再計算する | 記録済みの値と同じものを、制御の規則を写して作り直すだけになる（§2.2） |
| arm の鍵に `mode` を足す | PR #220 の束縛で、1つの報告に `mode` の違う区間が入らない。鍵を足すと報告の版が上がるだけで得るものが無い |

## 5. 未決事項

| 論点 | どこで決めるか | 実測待ちか |
|---|---|---|
| shadow → apply の合否の基準と、開ループと閉ループの差の扱い（0078 §5 のまま） | 段 6 の前に所有者と | **shadow の実データを待つ** |
| 切り替わりの数え方に `skipped` を跨ぐ上げ下げを含めるか | 合否の基準と一緒に | **shadow の実データを待つ** |
| 協調の側の帯のヒステリシス（0078 §5 のまま） | 本 CLI の集計で `release_hold_ms` だけでは切り替わりが多いと分かった場合、別の記録 | **shadow の実データを待つ** |
| 束縛した `fan-policy.yaml` の下で `mode` が変わるときの arm の扱い | `fan-policy.yaml` の束縛（PR #220）を緩める記録を作るとき | いいえ |
