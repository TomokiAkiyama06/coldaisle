# 決定記録 0177: Workload Hint B1 の集計（0162 PR-D）は、基準の推論の識別子が要る 2 項目を後続に回して先に出す

- **種別**: Decision Record
- **Status**: FINAL（2026-10-29、リポジトリ所有者の承認）
- **Date**: 2026-10-29
- **Supersedes**: [`0162`](0162-hint-b1-shadow-solver-open-points.md) §2.4 の「読むもの」のうち 0053 の台帳（基準の plan が Gate に評価されたか）を
  PR-D で出す点のみ / [`0175`](0175-hint-shadow-implementation-points.md) §2.4 の「基準の解が無い周期は PR-D の集計で候補から除く」点のみ
  （どちらも後続の PR で出す。§2.2。0162・0175 のほかの点は変えない）
- **関連**: [`0053`](0053-control-shadow-mode-and-counterfactual-logging.md) / [`0133`](0133-workload-hint-stage-b-prior-rollout.md) /
  [`0162`](0162-hint-b1-shadow-solver-open-points.md) / [`0175`](0175-hint-shadow-implementation-points.md)
- **対象 Issue**: #288

## 1. Context

0162 §2.4 は集計の CLI `coldaisle-hint-shadow` に、`hint_shadow_records` と対応する trace のヒントの塊に加えて、
0053 の台帳（その周期の基準の plan が Gate に評価されたか）を読むことを求めている。0175 §2.4 は、基準の解が無い周期を
集計で候補から除くとしている。

PR-D の実装で、どちらも今の記録の形からは確かに引けないことが分かった。

- `hint_shadow_records` の行（`src/coldaisle/hint_shadow_record.py`）は `source_tick_id` / `source_ts_ms` を持つが、
  ヒント無しの基準の推論の識別子（`inference_id`）を持たない
- trace の `model_gate` は自分の `inference_id` を持つが、提案の元になった tick を持たない

そのため、Gate のどの判断が記録の基準の plan に当たるかは推測でしか結べない。予測の値の一致で推すと、Learned が
選ばれた tick では引けない。

## 2. Decision（2026-10-29 所有者の判断）

### 2.1 PR-D は 2 項目を出さずに先に出す

- 0053 の台帳と、基準の解が無い周期の除外は、PR-D では**出さない**。推測で埋めない
- CLI の出力と module の docstring に「まだ出さない」と明記する。候補 tick は 0162 の定義のまま
  （`stage: shadow_mpc` かつ `would_override`）で数え、**候補を多めに数える側に寄る**ことを出力に書く
- ほかの 0162 §2.4 の項目（段の数・差分の分布・`run_id` × epoch の成功率の上限・`retention_truncated`・合否を出さない）は PR-D で出す

### 2.2 後続の PR で足す

- fand が書く `record_json` に、その周期の基準の推論の識別子を足す（`record_json` の形の版を 2 に上げる）。
  集計は同じ run の trace の `model_gate.inference_id` と突き合わせ、台帳と「基準の解が無い周期」の除外を出す
- 版 1 の行は、2 項目を出さないまま今の集計で数える（版で分ける。版 1 の行から推して埋めない）
- 足す欄の名前・worker から fand への運び方・試験は、その PR で決める。0162・0175 の安全の境界（記録だけ・Demand に効かない・
  受け渡し口と Gate に入らない）は変えない。決めることが欄の追加を超えるなら、その PR の前に別の記録を立てる

### 2.3 保持期間の検査（実装のとおり）

- `retention_truncated` の検査は、`control_trace_prune.pruned_before_ms`（削除の記録）がある DB だけに当てる。
  削除の記録が無い DB で常に「最も古い行 + 余裕」で判定すると、B1 を始めた直後の区間が誤って拒まれる
- 引っかかった区間は、成功率だけでなく段の数・差分も出さない（欠けた区間の数が全体の数として読まれないため）。CLI は exit 0 で、区間ごとの status として出す

## 3. Consequences

- PR-D の集計で、Shadow の記録の段の数・差分・成功率の上限を見られるようになる。ただし候補はしばらく多めに数えられる
- 0162 §2.6 の B2 の判断に台帳が要る場合は、§2.2 の後続の PR が先に要る
