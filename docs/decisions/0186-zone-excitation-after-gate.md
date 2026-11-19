# 決定記録 0186: 揺らぎ（0185）を Gate の後に掛け、温度の条件を設定の上限 2 つで書く

- **種別**: Decision Record
- **Status**: FINAL（2026-11-19、リポジトリ所有者の承認。§5 の 1・2 はどちらも推奨の案）
- **Date**: 2026-11-19
- **Supersedes**: [0185](0185-routine-zone-excitation.md) §2.1 の「位置」の行、§2.3 の「CPU と GPU の温度」の条件、§2.4 の `margin_c`
- **関連**: [0185](0185-routine-zone-excitation.md) / [0053](0053-control-shadow-mode-and-counterfactual-logging.md) /
  [0078](0078-air-balance-fallback-coordination.md) §2.5 / [0121](0121-hardware-test-procedure.md) §2.1 /
  [0130](0130-gpu-fan-quieter-curve.md) / AGENTS.md「絶対に守るルール」2・5・9
- **対象 Issue**: #84

## 1. Context

0185 の実装（PR #429 の後の PR-B）に入る前に、記録のままでは決めきれない点が 2 つ見つかった。

1. **位置と条件が噛み合わない。** §2.1 は揺らぎを「Air Balance の協調 → 揺らぎ → Gate」の順に置き、§2.3 は「Learned MPC が
   active の tick では掛けない」とした。ところが、その tick で Learned MPC が active かどうかは **Gate が選んだ後**でないと
   分からない。Gate の前に置くと、直前の tick の選択で推すしかない。さらに、揺らぎを足した Baseline が Gate に入ると、
   Gate の「Fallback からの変化の制限」（`authority_limits`）と Shadow の比較（0053。Fallback の値は Gate へ渡した Baseline。
   0078 §2.5）が、揺らいだ値を基準にしてしまう。
2. **温度の基準が設定に無い。** §2.3 の「0121 §2.1 の中止の基準（`cpu.package` 88 °C・`gpu.0.core` 85 °C。0130 の後）より
   `margin_c` 以上低い」の基準は、試験の手順の値で、Control Config のどこにも無い。コードに数値を書けない（ルール 9）。
   `safety.yaml` の `absolute_temp_ceiling_c` は CPU と GPU に共通の 1 つの値で、意味も違う（Critical Safety の絶対上限）。

## 2. Decision

### 2.1 位置

- 揺らぎは **Gate の後・Reactive Guard の前**に掛ける:
  Fallback → Air Balance の協調 → Model Confidence / OOD Gate → **揺らぎ** → Reactive Guard → Critical Safety
- 掛けるのは、**Gate がこの tick に Baseline（Fallback）を選んだ**ときの requested だけ（`active_controller` が `fallback`）。
  Learned MPC の提案を選んだ tick・Gate を迂回した tick（0085）・Gate が例外になった tick（0088）には掛けない
- Gate と Shadow の記録（0053）が使う Baseline は、**揺らぎを含まない**（協調の後の値のまま）。揺らぎの量は trace の
  `zone_excitation` にだけ残す（0185 §2.5）
- Reactive Guard と Critical Safety は今のとおり揺らぎの後に掛かる。揺らぎは Guard の ceiling も Safety の floor・
  `forced_max` も迂回しない（0185 §2.1 のまま）

### 2.2 温度の条件

- 0185 §2.3 の「CPU と GPU の温度が中止の基準より `margin_c` 以上低い」を、**`zone_excitation` の上限 2 つ**に置き換える:
  `cpu.package` が `cpu_max_c` より低く、かつ `gpu.0.core` が `gpu_max_c` より低いときだけ掛ける
- `margin_c` は持たない。`fan-policy.yaml` v14 の `zone_excitation` は `margin_c` の代わりに `cpu_max_c` / `gpu_max_c`
  （`{value, status}`。有限・正）を持つ
- 初めの値は、0185 で決めた余裕 8 °C を 0121 §2.1（0130 の後）の基準から引いた **`cpu_max_c` 80 °C・`gpu_max_c` 77 °C**
  （`provisional`）
- どちらかの温度が snapshot に無い・使えない（stale・欠測）の tick は掛けない（未知を「低い」と読まない。ルール 4）

## 3. Consequences

良くなること:

- 「MPC が active の tick では掛けない」を、その tick の選択そのもので判定できる
- Gate の制限と Shadow の比較の基準が、揺らぎで動かない。MPC の評価（#90 / #91）を揺らぎで乱さない
- 温度の条件の値が設定に直接書かれ、何 °C で止まるかが一目で分かる

悪くなること（と緩和策）:

| 悪くなること | 緩和策 |
|---|---|
| 揺らぎの requested は Gate の出力とずれる | 揺らぎの量と zone を毎 tick trace に残す（0185 §2.5）。effective は Guard と Safety を通る |
| 試験の手順（0121）の基準を変えても、揺らぎの上限は追わない | 上限は揺らぎだけの値として別に持つ。0121 を変えたら、ここも見直す（§5 の 2 の代替案は退けた） |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| 0185 のとおり Gate の前に置き、MPC が active かは直前の tick の選択で判定する | 切り替わりの tick で 1 tick ずれる。Gate の制限と Shadow の比較の基準が揺らぐ |
| `safety.yaml` の `absolute_temp_ceiling_c` から `margin_c` を引く | CPU と GPU を分けられない。Critical Safety の絶対上限の値次第で範囲が変わり、意味が混ざる |

## 5. 判断点（2026-11-19、どちらも決着）

| # | 判断点 | 決定（推奨案） | 代替案 |
|---|---|---|---|
| 1 | 揺らぎの位置 | **Gate の後・Guard の前。Gate が Baseline を選んだ tick だけ** | 0185 のとおり Gate の前 |
| 2 | 温度の条件の書き方 | **`zone_excitation` に `cpu_max_c` 80 °C・`gpu_max_c` 77 °C** | `safety.yaml` の `absolute_temp_ceiling_c` − `margin_c` |
