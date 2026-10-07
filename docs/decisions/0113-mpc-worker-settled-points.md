# 決定記録 0113: 0077 段階 3 / 0107 の実装（Learned MPC worker。PR #260）で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-08、リポジトリ所有者が推奨案で承認）
- **Date**: 2026-10-08
- **Supersedes**: なし（0077 / 0101 / 0107 が決めていなかった点への追加と、文面の読み方の明記。どの記録の決定も置き換えない）
- **関連**: [0077](0077-learned-proposal-handoff.md) §2.3 / §2.6 / §2.7 / §2.10 /
  [0101](0101-calibration-via-learned-frame.md) / [0107](0107-mpc-worker-observed-window.md) §2.1 / §2.5 / §2.6 / §2.7 /
  [0095](0095-learned-channel-stage1-settled-points.md) / [0098](0098-registry-binding-settled-points.md) /
  `src/coldaisle/learned_worker/` / `src/coldaisle/control/config.py`（`ControlConfig.runtime_digest()`）/
  Issue #261
- **対象 Issue**: #86（実装 PR #260）

## 1. Context

0077 §2.10 の段階 3（MPC worker `coldaisle-learnd --role mpc`）と 0101（frame v3 の較正の運搬）を、0107 の規則で
PR #260 に実装した。その実装で、記録に書かれていない点を6点選び、PR #260 の「人間レビューが必要な点」で推奨案とともに示した。
加えて、Codex のレビューで記録の文言と食い違って見える指摘が3点残った。2026-10-08、所有者が次のとおり決めた（PR #260 は
マージ済み）。決定記録は追記のみなので、0077 / 0107 の本文は書き換えず、ここに残す。

## 2. Decision

### 2.1 実装で選んだ点（決着、2026-10-08 所有者の決定、推奨案）

| # | 点 | 決着 |
|---|---|---|
| 1 | fand との接続が切れたとき | **worker は終了する**（終了コード 3）。プロセスの中で再接続しない。再起動は worker の unit の `Restart=` に任せる（0077 §2.10 段階 6 のテンプレートで定める）。プロセスの中で再接続するなら、再試行の間隔を設定に置く別の記録が要る（AGENTS.md ルール9） |
| 2 | registry から読めない・frame の固定と SHA-256 が違う artifact | `model_load_failure`（`Reason.code` は `model_unusable`。`LearnedMpcRuntime.load` の読み込み失敗と同じ code）を返す。次に束縛を作る時点（`run_id` の変化・`authority_stage` の上昇。0101 §2.3）まで**毎周期**同じ失敗を返す（止まらない） |
| 3 | `hello` を待つ時間 | `config/learned-channel.yaml` の `worker_idle_timeout_ms` を使う（新しい設定値を足さない。fand はこの時間黙った接続を閉じるので、それより長く待つ意味が無い） |
| 4 | 終了コード | 2: `--role` が未対応（`supervisor` は 0077 段階 4 まで）、3: fand に接続できない・接続が切れた、5: Control Config・Metric Catalog・`learned-channel.yaml`・registry の設定を読めない |
| 5 | 0107 §2.5 の「目的関数の metric」 | `fan-policy.yaml` の `mpc.optimizer.cost_metrics` の `cpu_temperature` / `gpu_temperature` の metric を指す。artifact の feature metric と合わせて、束縛の時点で frame の `snapshot.signals` にあるかを確かめる |
| 6 | frame の `config` の作り方 | `ControlConfig.runtime_digest()` に1か所でまとめ、fand の loop と worker が同じ関数で作る（値は変えない。trace の `runtime.config` と同じ値） |

### 2.2 Codex の指摘のうち、いまのままにした点（決着、2026-10-08 所有者の決定、推奨案）

#### (1) 欠けの直前の frame が当たる格子時刻も missing にする

指摘: tick 8・10・11 を受け取り、格子が `ts(8)` を含むとき、`ts(8)` には tick 8 の frame がそのまま当たるのに、次に受け取った frame が
tick 10（tick 9 の欠け）なので、その格子時刻の cell がすべて missing になる。欠けた tick を使うはずだった時刻だけを missing にすべき。

**いまのまま**（0107 §2.1 の文言どおり。「当てる frame の後に受け取った frame のうち最も古いものの `tick_id` が `+1` でないなら、
その格子時刻の cell はすべて missing」）。理由:

- 0107 は FINAL で、実装はその文言どおりである。変えるなら 0107 を置き換える記録が要る
- missing が増える向きにしか効かない。Confidence / OOD と Gate が Fallback へ倒すだけで、安全側である
- 欠けが起きる頻度と、それで Learned が使えない時間は、段階 3 の shadow の集計で見る（0077 §3）。問題になれば、その実測を
  根拠に別の記録で扱う

#### (2) 推論の後に production を照合し直さない

指摘: `propose()` の直前に照合した後、推論の間に promotion / rollback が起きると、置き換えられた artifact の結果を送りうる。
fand は受付スレッドの監視（`safety.tick_ms` ごと）で役割を閉じるまで古い期待値のままなので、推論の後にも照合すべき。

**いまのまま**（0077 §2.6 の文言どおり、照合は `propose()` の**直前**）。理由:

- 0077 §2.6 は、受付スレッドの確認と次の tick の `poll()` の間に最大1 tick（`safety.tick_ms`）だけ結果が受け渡し口に残りうることを
  記録に書いた上で受け入れている。その結果は、worker が照合した時点では production の artifact で作られたものである
- 推論の後の照合を足しても、送信から fand の監視までの間の競合は残る。間隔を縮めるだけで、性質は変わらない
- 速く止めたいときの経路は、これまでどおり管理ソケットの authority の降格（0072）である

### 2.3 worker が `coldaisle.store` を間接的に読み込む件は別 Issue で直す（決着、2026-10-08 所有者の決定、推奨案）

`coldaisle.learned_worker` → `coldaisle.control.config` → `coldaisle.store.models` の経路で、Python が `coldaisle/store/__init__.py` を
実行し、`store.db`（と `sqlite3`）を読み込む。直接の import は試験で止めているが、0077 の「worker は frame だけを入力にする」を
import の構造で保証できていない。worker から書き込む経路は無く、制御の安全性はいまも変わらない。

**直し方は Issue #261 で扱う。** 層をまたいで使う型と検証（`Quality`・`validate_metric` など）を store package の外の「レイヤ横断」の
module へ移し、worker の import 試験をプロセス全体の `sys.modules` で確かめる形に強める。本記録は方針だけを決め、実装と試験は
#261 の PR で行う。

## 3. Consequences

### 良くなること

- 記録だけを読んだ人が、実装と違う挙動（worker が再接続する・読み込み失敗で止まる・推論の後にも照合する、など）を前提にしない
- Codex の指摘を「記録と違う」ではなく「記録どおりで、変えるなら記録が要る」として扱った理由が残る

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 接続が切れるたびに worker が終了し、再起動まで Learned が使えない（§2.1 の1） | fand の再起動でも接続は切れる（新しい `run_id`）。どちらも Fallback に倒れるだけ。unit の `Restart=` は段階 6 で決める |
| 欠けの直前の格子時刻まで missing になり、Learned が使えない時間が増える（§2.2 (1)） | 安全側に倒れるだけ。頻度は shadow で数える |
| 推論の間の promotion で、最大1 tick 古い artifact の結果が残りうる（§2.2 (2)） | 0077 §2.6 が受け入れた残余と同じ。authority の降格で先に止められる |
| #261 が入るまで、worker のプロセスに SQLite の層が読み込まれる（§2.3） | 書き込む経路は無い。直接の import は試験で止めている |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| worker がプロセスの中で再接続する | 再試行の間隔という新しい設定値が要る。プロセスの再起動で十分で、状態（束縛・window）は新しい `run_id` で作り直すので失うものが無い |
| 読み込みの失敗で `artifact_not_production` と同じく止まる | 0077 §2.3 は stage の上昇で束縛を作り直すと決めており、止まると昇格後に回復できない |
| hello を待つ時間を新しい設定値にする | `worker_idle_timeout_ms` より長く待っても fand が接続を閉じる。値が増えるだけ |
| 欠けた tick を使うはずだった時刻だけを missing にする（Codex の案） | 0107 §2.1（FINAL）を置き換える記録が要る。実測（shadow）の前に変える根拠が無い |
| 推論の後にも production を照合する（Codex の案） | 0077 §2.6 の残余を縮めるだけで無くせない。0077 に無い挙動を足すことになる |
| `coldaisle/store/__init__.py` の再 export を遅延にする | 変更は小さいが型検査と読みやすさが落ちる。#261 で共有の型を移す方を推奨とした |

## 5. 未決事項

なし（§2.3 の実装は Issue #261。worker の unit の `Restart=` は 0077 §2.10 段階 6）。
