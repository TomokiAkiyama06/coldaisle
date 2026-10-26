# 決定記録 0175: Workload Hint B1 の Shadow の実装（0162 PR-B / PR-C）で記録に無かった点の決定

- **種別**: Decision Record
- **Status**: FINAL（2026-10-26、リポジトリ所有者の承認。§2 の 1〜4 はすべて推奨の案）
- **Date**: 2026-10-26
- **Supersedes**: なし（0162・0133 の決定は変えない。記録に無かった点を足す）
- **関連**: [`0133`](0133-workload-hint-stage-b-prior-rollout.md) §2.5 / §2.5a / [`0152`](0152-hint-monotonic-gate-certificate.md) / [`0157`](0157-hint-runtime-launch-form.md) /
  [`0160`](0160-hint-conditions-digest-scope.md) / [`0162`](0162-hint-b1-shadow-solver-open-points.md) §2.2・§2.3・§2.5 / [`0167`](0167-opt-update-recheck-scope.md)
- **対象 Issue**: #288

## 1. Context

0162 §2.5 の PR-B（worker 側の Shadow solver、ブランチ `feat/288-hint-shadow-worker`）と PR-C（fand 側の受信・検証・記録と ControlTick v17、
`feat/288-hint-shadow-fand`）を実装したところ、0162・0133 に書かれていない点が出た。Shadow は記録だけで、受け渡し口・Gate・Fan の Demand には入らない
（0162 §2.2）ので、どれも制御の出力は変えない。所有者が 2026-10-26 に以下を決めた。

## 2. Decision

### 2.1 ヒントの鍵は設定から導く（frame の版は上げない）

frame v4（0162 PR-A、#373）は、どの workload のヒントかを運ばない。worker と fand は判定 1 の鍵を、Control Config の `context_overrides` のうち
「Regime が同じで、上書き先の context の `target_band` がヒント有りの band と等しい行」から導く（`hint_keys_by_band`）。
引けた鍵はどれも判定 1 を通り、同じ band に複数の workload が当たっても結果は変わらない。frame に鍵を足す（v5）のは却下（§4）。

### 2.2 記録の新しさは基準の提案と同じ寿命

fand は受け取った Shadow の記録の元の snapshot の単調時刻からの寿命を、基準の提案と同じ `mpc.max_source_age_ms` で見て、超えれば `stale` として拒否する。
Shadow 専用の閾値は足さない。0162 のとおり、保留が切れた記録も `recorded` のまま残す（`pending_domain_result_ms` は新しさの閾値にしない）。

### 2.3 migration 0017 は `hint_shadow_records` を作り直す

`worker_instance_id` と累積の数 3 つを NOT NULL の列にするため表を作り直し、0016 の行は写さない（0016 には書き手が無かった）。
仮の既定値で埋めると「値が無い」と「0 件」を取り違えるため。**`/opt` へ入れる手順で、本番の DB の `hint_shadow_records` の行が 0 であることを確かめ、
backup を取ってから当てる**（0167 §2.3 の migration の扱い）。

### 2.4 実装のとおりに決める細かい点

| 点 | 決定 |
|---|---|
| 同じ元の tick の 2 件目 | fand は `rejected` / `duplicate_source`（0162 §2.2 の「2 件目は拒否」の reason。語彙を 1 つ足す） |
| 元の frame の config が fand の設定と違う・記録の基準の plan が受け渡し口の基準の解と違う | `rejected` / `base_mismatch` |
| `conditions_sha256` が無い `skipped`（子が束縛を作る前に止まった） | conditions を照らさずに受け入れ、証明書の照合だけ行う |
| 書けなかった件数（0133 §2.5a） | trace の欄は 0162 のとおり `certificate` と `shadow_counters` だけにし、件数は構造化ログに出す |
| `mpc_unavailable`（0162 §2.3） | 今回は配線しない（PR-D の集計の後に要否を決める） |
| `learned-channel.yaml` の `hint_domain` | `pending_domain_result_ms`・`max_pending_domain_results`・`limits.max_shadow_record_bytes` を必須にする。欠ければ Learned の経路だけを開かない（今の不正な設定の扱い）。行を書く待ち行列の上限は `max_pending_domain_results` を使う |
| 基準の解が無い周期 | worker は Shadow の依頼も記録も作らない（対にする基準が無い）。PR-D の集計ではこの周期を候補から除く |
| 子が artifact を読めない・digest が違う・L9 で束縛できない | `skipped` / `artifact_mismatch`。solve の例外は `budget_exhausted`（0133 §2.8 の「解けない」） |
| 記録に無い 2 つの検査（厳しい側だけ） | ヒント有りの解の anchor が基準と違えば `base_mismatch`、子が読んだ Control Config と Metric Catalog の digest が frame と違えば `certification_mismatch` |
| 子の設定の読み方 | 子は設定を path から自分で読む（pydantic の generic が pickle できないため）。親子の経路はどちらの向きも検証する JSON で、unpickle しない |
| 子を作る時機 | 最初のヒントの依頼で作る（B0 の間は作らない）。起動中（`ready` 待ち）は時間の上限を置かない（B1 が使えないだけで安全側） |
| `PR_SET_PDEATHSIG` を設定できない | SCHED_IDLE と同じく何もせず終了し、そのプロセスの間は作り直さない |
| 暫定の設定値 | `hint_shadow.respawn_interval_ms` 30 s・`hint_shadow.unresponsive_after_ms`（打ち切り時刻から）60 s・`limits.max_shadow_record_bytes` 32768・`limits.packet_overhead_bytes` 8192（開発機の `SIOCOUTQ` の実測で 4096 では足りなかった） |
| 実時間の試験 | 遅い側への差（子と計算ループがある条件 − 無い条件）だけを許容幅と比べる（片側）。marker は `hardware` と `realtime` の両方 |

### 2.5 本番のホストで確かめること（`/opt` へ入れるときの手順に入れる）

- `packet_overhead_bytes` の測り直し（`SIOCOUTQ`）
- 実時間の試験（`-m realtime`）を本番のホストで走らせる
- 子の部品 `environment`（spawn の `-I -c` の子の指紋）が fand・認証ランナーと一致するか（`-I -m` で起動したときだけ確かめられる）
- `hint_shadow_records` の行が 0（§2.3）・既存の `learned-channel.yaml` に必須の欄があるか（§2.4）

## 3. Consequences

### 良くなること

- PR-B / PR-C の判断が記録に残り、レビューと PR-D の集計の前提がそろう

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| ヒントの鍵を設定から導くので、設定の書き方によっては鍵が曖昧になりうる | 引けた鍵はどれも判定 1 を通り、結果が変わらないことを試験で固定した。Shadow は記録だけ |
| 必須の欄が増え、古い `learned-channel.yaml` では Learned の経路が開かない | Learned が開かなくても Fallback / RulePolicy で運転する（今の扱い）。§2.5 で更新の前に確かめる |
| 暫定の値が多い | §2.5 で本番のホストで測り直す。Shadow の段（B1）は記録だけ |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| frame に鍵を足す（v5） | frame の版上げと PR-A の作り直しが要る。今の導き方で判定の結果が変わらない |
| Shadow 専用の新しさの閾値 | 設定が増える。基準と同じ寿命で足りる |
| 0016 の行を仮の既定値で写す | 「値が無い」と「0 件」を取り違える。0016 に書き手は無かった |
| 細かい点を 1 つずつ別の記録にする | どれも記録だけの経路の中の話で、制御の出力を変えない |

## 5. 未決事項

- `mpc_unavailable` の配線の要否（PR-D の集計の後）
- §2.4 の暫定の値の確定（本番のホストでの測定の後）
