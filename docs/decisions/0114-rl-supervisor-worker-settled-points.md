# 決定記録 0114: 0077 段階 4 の実装（RL Supervisor worker。PR #266）で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-08、リポジトリ所有者が決定。§2.1 の2は Codex の指摘を受けた所有者の決定、他の7点は推奨案で承認）
- **Date**: 2026-10-08
- **Supersedes**: [0113](0113-mpc-worker-settled-points.md)（§2.1 の表の4「終了コード」のうち、終了コード 2 を「`--role` が未対応（`supervisor` は 0077 段階 4 まで）」とした部分のみ。§2.1 の8。他の節は有効）
- **関連**: [0077](0077-learned-proposal-handoff.md) §2.3 / §2.5 / §2.6 / §2.8 / §2.10 /
  [0061](0061-rl-supervisor-policy-artifact-and-binding.md) §2.4 / [0074](0074-supervisor-shadow-wiring-and-episode-evaluation.md) /
  [0107](0107-mpc-worker-observed-window.md) / [0113](0113-mpc-worker-settled-points.md) /
  `src/coldaisle/learned_worker/supervisor.py` / `src/coldaisle/learned_worker/cli.py` /
  `src/coldaisle/control/supervisor/policy.py`（`DeliveredSupervisorOutput`）/ `src/coldaisle/control/loop.py`
- **対象 Issue**: #89（実装 PR #266）

## 1. Context

0077 §2.10 の段階 4（RL Supervisor worker `coldaisle-learnd --role supervisor`・`SupervisorOutputSource` の形を広げる・
`origin` は `unverified` のまま・active は閉じたまま）を PR #266 に実装した。0077 は RL worker について、固定した policy が
production でなくなったときに「出力を送らず再起動の frame まで止まる」ことと、経路で運ぶ型（出力と識別）を決めたが、
それ以外の失敗の扱い・入力・照合の範囲は MPC worker（0107 / 0113）のようには書いていない。実装で8点を選び、PR #266 の
「人間の判断が要る点」で推奨案とともに示した。そのうち1点（束縛の失敗を別の `run_id` まで覚えること）に Codex が
「0077 に無い運用上の挙動で、承認が要る。少なくとも一時的な失敗は読み直すべき」と指摘した。2026-10-08、所有者が次のとおり決めた。
決定記録は追記のみなので、0077 / 0113 の本文は書き換えず、ここに残す。

## 2. Decision

### 2.1 実装で選んだ点（決着、2026-10-08 所有者の決定）

| # | 点 | 決着 |
|---|---|---|
| 1 | 失敗の本文 | **RL worker は失敗を送る本文を持たない。** config の食い違い・`rl_version` が無い構成・固定の無い frame・束縛の失敗・production の移動・Workload Regime の欠けは、どれも「その周期は何も送らない（heartbeat だけ）」とし、理由は worker の構造化ログに残す（0077 §2.6 の「RL worker は出力を送らない」と §2.8 の型に揃える）。fand の trace では Coordinator の `supervisor_unavailable` になる。推奨案で承認 |
| 2 | policy の読み込みの失敗（**Codex の指摘への所有者の決定**） | **registry を読めない一時的な失敗だけは周期ごとに読み直す。** 読み込みの例外・`invalid_registry`（snapshot を検証できない）・`artifact_unavailable`（artifact の bytes を読めない）は束縛の失敗として覚えず、次の周期に読み直す（その周期は何も送らない）。**読めたうえで使えない**（登録が無い・checksum・schema・authority・形式・frame の固定との SHA-256 の食い違い・`for_shadow` の検査（kind・能力・metadata・`rl_version`・action 空間）に外れた）ときは、従来どおり別の `run_id`（fand の再起動）まで作り直さない。production でなくなった・registry の production を確かめられないときの停止（0077 §2.6）は変えない |
| 3 | `authority_stage` | **frame の `authority_stage` に追従して束縛を作り直さない。** RL の束縛は `for_shadow`（SHADOW 固定）で、経路の出力は `unverified` なので active slot に届かない。0077 §2.3 の作り直しは `MpcModelBinding` の規則である。推奨案で承認 |
| 4 | 入力 | **最新の frame 1つ**（snapshot と Workload Regime）から `SupervisorInput` を作り、`recent_history` / `recent_performance` は空にする。いまの policy（`RegimeTableRlPolicy`）は regime しか使わない。window を使う policy family を足すときは別の記録で決める。推奨案で承認 |
| 5 | Metric Catalog | **RL worker は Metric Catalog を読まず、frame の `metric_catalog_sha256` も照合しない**（RL policy は metric を使わない）。frame の `config` は照合する。推奨案で承認 |
| 6 | trace の detail | **経路が `connected` でない tick の RL の error は、Coordinator の既定の `supervisor_unavailable`（detail「未受信」）のまま**にし、経路の状態を detail に入れない。0077 §2.5 が detail の運び方を決めたのは MPC だけである。loop は RL を覗く前に経路の状態を確かめる（受付スレッドの死は役割をまたいで再起動まで覚える）。推奨案で承認 |
| 7 | 同じ frame への出力 | **周期ごとに最新の frame へ出力を作り直す**（MPC と同じ）。`computed_at_ms`（worker の壁時計）が変わるので loop は受信時刻を押し直すが、期限は元 snapshot の単調時刻から数える（`ReceivedSupervisorOutput.source_monotonic_ms`）ので、古い出力が新しく見えることはない。推奨案で承認 |
| 8 | 終了コード 2 | **廃止する。** `--role supervisor` を受け付けるので「未対応の役割」の終了コードは要らない。不正な `--role` は引数の解析（`choices`）で同じく 2 で終わる。0113 §2.1 の4の「2: `--role` が未対応」はこの部分だけ本記録で置き換える（3 と 5 は有効）。推奨案で承認 |

### 2.2 0115（worker の unit。FINAL）との関係（注記）

0115 は本記録より前に FINAL になり、次の2か所で終了コード 2 を「`--role` が未対応（RL は #89 まで）」と書いている。
本記録は 0115 の本文を書き換えず、読み方だけをここに残す（決定の内容は変わらない）。

- **§2.5 の `RestartPreventExitStatus=2` は有効なまま。** 2 は §2.1 の8のとおり引数の誤り（未知の `--role` を含む。
  argparse の終了コード）で、人が直すまで結果が変わらない点は同じである。CLI はこの値を `EXIT_USAGE` として持ち、
  `tests/test_deploy_templates.py` が unit の値と照合する
- **§2.9 の「`--role supervisor` が終了コード 2 で拒まれる間」は PR #266 で終わる。** RL worker の unit を導入先へ
  置かない理由のうち残るのは「RL のユーザーが無いまま置くと 217/USER で再試行を続ける」だけで、ユーザー・ACL・
  unit を同時に置く規則（0115 §2.2 / §2.9）は変わらない。テンプレートの注記と `docs/ubuntu-deploy.md` 6.8 の
  文言はこの読み方に合わせた

## 3. Consequences

### 良くなること

- 記録だけを読んだ人が、実装と違う挙動（RL worker が失敗を送る・stage で作り直す・window を作る、など）を前提にしない
- registry の一時的な読み込みの失敗で、fand か worker を再起動するまで RL の shadow の証拠が止まることが無い（§2.1 の2）
- 使えない policy を毎周期読み直してログを溢れさせない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| RL が使えない理由が trace からは読めない（§2.1 の1・6） | worker の構造化ログに理由の code を残す。trace で要るなら 0077 §2.9 の選択肢 B を別の記録で |
| 一時的な失敗が続くと毎周期読み直す（§2.1 の2） | 読み込みは `supervisor.period_ms` ごとに1回だけ。registry が壊れていれば、その前の production の照合（0077 §2.6）で止まる |
| 一時的な失敗のログは `run_id` ごとに1回だけ | 溢れさせないため。回復は「束縛した」の info ログで分かる |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 束縛の失敗をすべて別の `run_id` まで覚える（PR #266 の最初の実装） | 一時的な読み込みの失敗でも再起動まで RL が止まる（Codex の指摘）。所有者が §2.1 の2 を選んだ |
| 束縛の失敗をすべて周期ごとに読み直す | 使えない policy を毎周期読み直し、同じ失敗を繰り返すだけになる |
| RL worker に失敗の本文（MPC の `model_load_failure` 相当）を足す | 経路の型と `SupervisorOutputSource` の形が増える。0077 §2.6 は「出力を送らない」と決めている |
| RL の経路の状態を `supervisor_unavailable` の detail に入れる | 0077 §2.5 に無い挙動。必要なら別の記録で |

## 5. 未決事項

なし（worker の unit は 0077 §2.10 段階 6、`for_active` の門は 0061 §5）。
