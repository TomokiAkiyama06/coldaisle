# 決定記録 0094: 0079 段 1 / 0087（Thermal Dataset v2）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が承認）
- **Date**: 2026-10-07
- **Supersedes**: なし（0079 / 0087 が決めていなかった点への追加。どちらの記録の決定も置き換えない）
- **関連**: [0079](0079-model-artifact-formats.md) §2.9 /
  [0087](0087-dataset-v2-action-grid.md) §2.1 / §2.2 / §2.7 / §5 #12 /
  [0031](0031-thermal-dataset-contract.md) /
  [0002](0002-metric-naming.md) §2.11 / [0071](0071-control-trace-read-api.md) §2.2a /
  migration `0007_control_trace_seq.sql` の `control_trace_prune_no_rewind` /
  `src/coldaisle/dataset.py` / `src/coldaisle/control/model/dataset.py` /
  `src/coldaisle/store/migrations/0010_control_trace_legacy_through_seq.sql`
- **対象 Issue**: #83（実装 PR #221）。切り出した Issue: #224

## 1. Context

0079 §2.9 の段 1（Thermal Dataset v2）と 0087（action 列を格子へ写す規則・`legacy_through_seq` の migration）を
PR #221 で実装した。その実装で、0079 / 0087 のどちらにも書かれていない挙動と、Codex のレビューを受けて
足した読み込み時の照合があった。いずれも PR #221 の「人間レビューが必要な点」とレビューのスレッドで示し、
2026-10-07 に所有者が推奨案のとおりすべて承認した。決定記録は追記のみなので、0079 / 0087 の本文には手を入れず、
ここに残す。記録だけを読んだ人が、実装と違う挙動を前提にしないためである。

## 2. Decision

### 2.1 anchor 候補が action の規則ですべて除外された run

**生成を拒否せず、`example_count = 0` と理由ごとの除外件数（`excluded`）を持つ dataset を返す。**

- anchor 候補（window と最大の horizon の target が run に収まる ControlTick）自体が無い run は、v1 と同じく
  生成を拒否する（`source run内に完全なwindow/targetを持つControlTickが無い`）。変えない
- 理由: 除外の件数は `action_stale_after_ms` と `step_ms` を選ぶ根拠に使う（0087 §2.2）。すべて除外された run こそ、
  値の選び方が実データに合っていないことを示す件数であり、拒否するとそれが残らない

### 2.2 除外の件数の分母

**window / target が run に収まらない端の tick は、除外の件数に数えない**（v1 と同じく、数えずに候補から外す）。
`excluded` の件数は「run に収まる anchor 候補のうち、action の規則（0087 §2.2 / §2.3 の 鮮度 → 連続 →
区間内の変化 → 再起動 → 欠番）で除いたもの」である。

- 理由: 端の tick は action の規則と無関係に作れない。混ぜると、件数から `action_stale_after_ms` / `step_ms` の
  選び方を読めなくなる

### 2.3 `legacy_through_seq` を下げさせない trigger は入れない

migration `0010_control_trace_legacy_through_seq.sql` は `control_trace_prune` に列を足して埋めるだけとし、
migration 0007 の `control_trace_prune_no_rewind` と同じ向きの「値を下げさせない」trigger は**入れない**。

- 理由: 0087 に無い DB オブジェクトであり、`tests/test_schema_matches_decision.py` は決定に無い DB オブジェクトを拒む。
  決定記録を先に作るという規則（AGENTS.md「決定記録」）を飛ばさない
- 必要になったら、別の決定記録を先に作ってから入れる

### 2.4 `coldaisle-dataset` CLI の v2 対応は別 PR

PR #221 は `ThermalDatasetV2Builder.build(source_run=, spec=, declared_changes=)` までとし、`coldaisle-dataset` は
v1 のままとする。

- 理由: `declared_changes`（`DeclaredChange` の tuple。空でも明示が必須。0087 §2.6）を CLI でどう渡すか
  （ファイルの形式など）が決まっていない
- CLI の v2 対応は別 PR で、その渡し方を決めてから行う

### 2.5 migration の可逆性

migration は**追記のみ**（0002 §2.11。down migration を持たない）のまま。可逆性を試験する代わりに、次を試験で確かめる。

- 既存の行（`control_traces` の行・`control_trace_prune` の行）が migration の前後で変わらない
- `legacy_until_ms` が変わらない（意味も変えない。0087 §2.8）
- 読み取り API（`ControlTracePruneState`・trace の page）の結果が前後で同じ
- 冪等: DB を開き直しても再適用しない（`legacy_through_seq` が同じ値のまま）
- `legacy_through_seq` の値: 行が無い DB では 0、0007 を適用済みで行がある DB では `ts_ms ≤ legacy_until_ms` の行の
  `MAX(seq)`（`legacy_until_ms` と同じ時刻で後から記録した行も含む安全側の上限。0087 §5 #12）

### 2.6 読み込み時に元の tick を照合する（PR #221 の Codex の指摘で入れたもの）

Dataset v2 の読み込み（`DatasetExampleV2` / `ThermalDatasetV2` の検証）で、次を求める。外れた dataset は読み込みで拒む。
どれも 0087 の規則から導かれる性質を、builder を通らずに作った・書き換えた artifact に対しても確かめるものである。

1. **同じ元の tick を使う step の一致**: 1つの example の中で同じ `source_ts_ms` を使う step は、`source_tick_id` と
   `effective_demand` が一致する（549c253）
2. **dataset 全体での一致**: `(source_run_id, 元の tick の時刻)` をキーに、全 example を通して、anchor（`action_ts_ms` /
   `control_tick_id` / `action` の effective）・`prior_action`・各 step の元の tick の `tick_id` と値が一致する。
   1つの ControlTick は1つの `tick_id` と effective しか持たない（0087 §2.1）ためである（eb5d14e）
3. **`prior_action` の `tick_id`**: `prior_action.source_tick_id = control_tick_id − 1`。builder は `prior_action` の tick から
   `tick_id` がちょうど 1 ずつ増える anchor だけを example にする（0087 §2.2）ので、それ以外の値は作りえない（2dd551a）
4. **step の元の `tick_id` の順**: step の元の tick の `tick_id` が、元の tick の時刻の順に増える
   （時刻が進めば `tick_id` も進み、時刻が同じなら同じ tick）（2dd551a）

### 2.7 v1 / v2 共通の loader の残り2件は別 Issue（#224）

PR #221 の Codex の指摘のうち、次の2件は PR #221 では直さず、v1 / v2 共通の loader の強化として #224 で扱う。

- (a) 同じ anchor を別の `example_id` で複製した example を拒否していない（一意性は `example_id` だけ）
- (b) window で同じ `(metric, source_ts_ms)` を複数の frame が使うとき、値・quality（・missing）の食い違いを拒否していない
  （metric ごとの観測時刻が逆行しないことだけを見る、v1 と共通の検査）

- 理由: どちらも改ざん・破損した artifact への読み込み時の防御で、builder が DB から作る dataset では起きない。
  同じ欠落が v1 の loader（0031）にもあり、直すと v1 の契約も変わる。v2 だけを直すと v1 / v2 で検査が食い違う
- 契約が変わるので、#224 の実装の前に決定記録を作って承認を得る

## 3. Consequences

### 良くなること

- 記録だけを読んでも、すべて除外された run の扱い・除外の件数の意味・migration の範囲・CLI の範囲・読み込み時の
  照合を、実装と同じに読める
- すべて除外された run でも件数が残り、`action_stale_after_ms` / `step_ms` を実データから選べる
- 読み込み時の照合で、元の tick について矛盾する action の軌跡を持つ artifact を学習へ入れない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| `example_count = 0` の dataset が作れて、そのまま学習へ渡りうる | 学習・artifact v2（#84）の側で example が足りない dataset を拒む。件数は manifest の `excluded` に残る |
| `legacy_through_seq` を誤って下げる書き込みを DB が拒まない | 書くのは migration だけで、store に書き込みの API は無い。必要になれば別の決定記録で trigger を足す |
| v2 の dataset を CLI から作れない | `ThermalDatasetV2Builder` を合成の起点から呼べる。CLI は `DeclaredChange` の渡し方を決めてから |
| 同じ anchor の複製・window 内の同一観測の食い違いを、読み込みでまだ拒まない | builder の出力では起きない。#224 で v1 / v2 共通に塞ぐ |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| A. anchor 候補がすべて action の規則で除外されたら生成を拒否する | `action_stale_after_ms` / `step_ms` を選ぶ根拠の件数（0087 §2.2）が残らない |
| B. 端の tick も除外の件数に数える | action の規則と無関係な件数が混ざり、件数から値の選び方を読めなくなる |
| C. `legacy_through_seq` を下げさせない trigger を PR #221 で入れる | 0087 に無い DB オブジェクトを決定記録より先に入れることになる |
| D. 同じ anchor の複製と window 内の食い違いを PR #221 で v2 だけ直す | v1 の契約（0031）と食い違い、v1 / v2 の両方を変える判断を PR の中でしてしまう |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | `coldaisle-dataset` の v2 対応で `DeclaredChange` を CLI でどう渡すか | CLI の v2 対応の PR（決定が要れば先に記録） |
| 2 | 同じ anchor の複製と、window 内の同じ `(metric, source_ts_ms)` の食い違いを、v1 / v2 共通に拒むか・そのキーと範囲 | #224（実装の前に決定記録） |
