# 決定記録 0169: `/opt/coldaisle` を `5ff6f96` から `5cd9820` へ更新するときにやり直す確認（区分 S）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-23、リポジトリ所有者の承認。§5 の 1〜3 はすべて推奨の案）
- **Date**: 2026-10-23
- **Supersedes**: なし（[`0167`](0167-opt-update-recheck-scope.md) §2.1 の区分 S が「更新の前に新しい決定記録で決める」とした、この 1 回の更新の確認を決める）
- **関連**: [`0121`](0121-hardware-test-procedure.md) §2.3 / §2.5 / [`0161`](0161-fand-enable-prerequisites.md) §2.4 / §2.5 /
  [`0162`](0162-hint-b1-shadow-solver-open-points.md) §2.1 / [`0164`](0164-no-sensor-calibration.md) / [`0165`](0165-r3-split-cpu-load-run.md) /
  [`0167`](0167-opt-update-recheck-scope.md) / [`0168`](0168-slack-mention-qa.md) / [`0099`](0099-calibration-change-log.md) /
  `docs/ubuntu-deploy.md` 4「コードを更新するとき」/ 4.3 / 6.5
- **対象 Issue**: #74 / #374

---

## 1. Context

導入先の `/opt/coldaisle` は `5ff6f96` で、fand は 2026-10-23 に `enable` した（0161）。所有者は Slack のメンションボット（#374 / 0168）を動かすため、
今の main（`5cd9820`）をまとめて入れることを選んだ（2026-10-23）。

`git diff --name-only 5ff6f96 5cd9820 -- src deploy config uv.lock pyproject.toml firmware` を 0167 §2.1 に当てた結果:

| 変わったもの | PR | 区分 | 中身 |
|---|---|---|---|
| `src/coldaisle/control/loop.py`・`control_daemon.py` | #373（0162 PR-A） | **S**（安全の配線） | frame v4 のヒントの欄を作る経路。**Critical Safety の呼び方と、その結果を出力の前に当てる部分は変わっていない**。ヒントの欄を埋めるのは実効の段階が `shadow_mpc` 以上の tick だけで、`IMPLEMENTED_STAGE` は `observe` のまま・導入先の設定は `stage: off` なので動かない |
| `config/calibration.json` | #367（0164） | **S**（較正の値と当てる経路） | `note` の文だけ。`offsets_c` は全チャネル 0.0 のまま変わらない |
| `src/coldaisle/control/` のほか（`hint_domain.py`・`learned_handoff.py`・`supervisor/policy.py`）・`learned_channel/`・`learned_worker/`・`hint_certification.py`・`store/db.py`（読み出しの関数を 1 つ足しただけ）・`pyproject.toml`（console script を 1 つ足した） | #373・#376 | F | frame の版（v3 → v4）・判定の関数・`get_stats` の読み出し・`coldaisle-slackbot` の入口 |
| `src/coldaisle/internal_telemetry/`（`nvml.py` を含む）・`telemetry_daemon.py`・`config/gpu_state.yaml` | #369 | I（値を作る部分を含む） | `sys.gpu_state` の記録 |
| `src/coldaisle/slackbot/`・`ai/`・`notify/slack_bot.py`・`config/ai.yaml`・`config/rules.yaml`（hint の文）・`config/learned-channel.yaml`（`hint_domain`。導入先の fand は `/etc/coldaisle/learned-channel.yaml` を読み、それは無い）・`web/index.html`・`deploy/systemd/coldaisle-slackbot.service`（新しい） | #367・#373・#376 | O | fand の閉包の外か、fand が読まない設定 |

- `store/migrations.py` と migration の SQL に差分は無い（DB の形は変わらない）
- `deploy/systemd/coldaisle-fand.service`・`coldaisle-daemon.service`・`coldaisle-telemetry.service`・`coldaisle-cpu-power.service`・`deploy/udev/`・`uv.lock`・`firmware/` に差分は無い
- `ControlTick` の版は 16 のまま（`src/coldaisle/control/schema.py` の `SCHEMA_VERSION`）。Control Config の版も v17 のまま

全体の区分は **S** になる（0167 §2.1）。

## 2. Decision（2026-10-23 所有者が §5 の推奨の案で承認）

### 2.1 S の部分の確認

**(a) 配線が変わっていないことを差分で確かめた**（本記録の作業。2026-10-23）: `5ff6f96..5cd9820` の `loop.py` の差分は、`_run_supervisor` の戻り値に
ヒント有りの Supervisor 出力を足すことと、frame を作るときにヒントの欄を入れるか決めることだけで、`CriticalSafety.evaluate()` の呼び出し・
`effective_demand` の決め方・hardware への出力の順は変わっていない。`control_daemon.py` の差分は、起動時の証明書の照合の結果を frame 用の値に写す
関数を足したことだけで、`CriticalSafety` の組み立てと渡し方は変わっていない。CI の試験 `tests/test_hint_frame_loop.py` が、`stage: off` と
ヒントの欄を埋める段階とで各 tick の `requested_demand` / `effective_demand` が一致することを固定している（0162 §2.1）。

**(b) 実機で安全の配線を 1 回通す: 0121 §2.5 の T3（データの遅れと復帰）を短く行う。** `coldaisle-telemetry` を 30 s 止めて戻し、続けて
`coldaisle-daemon` を 30 s 止めて戻す（各 1 回）。合格の条件は 0121 §2.5 のまま（stale が 5000 ms 以上・5000 ms + 1 tick 以内に上がり、
Top が Max・Front / Rear が `fault_demand` 1.0／air は Front / Rear だけ・Top は Max にならない／解消は 30 s 保持）。Critical Safety の判定が
loop を通って `effective_demand` と hardware まで届くことを、実際の fault で確かめる。所要は約 15 分。

**(c) 較正**: `offsets_c` が変わらないことを、更新の前に `git diff 5ff6f96 5cd9820 -- config/calibration.json` で確かめる（`note` の行だけが違うこと）。
ファイルの bytes が変わるので、取り込みの起動時に `calibration_activations` へ 1 行が足される（0099。offset は同じ）。これは想定どおりで、
Dataset v2 の較正の区切りになる。fand の frame の較正（offset の写像）と、Workload Hint の部品 `calibration`（`stage: off` では使わない）は
offset から作るので変わらない。

### 2.2 F の部分の確認: 0161 §2.4 の R1〜R3

0167 §2.1 の F のとおり、R1〜R3 をそのままの手順と合格の条件で行う（`stage: off`。R3 の負荷の回は 0165 §2.3 の直したスクリプトで、
trace の `schema_version` は 16）。R3 の 2 時間の soak は常駐の運転とつなげてよい（0167 §2.3）。

### 2.3 I の部分の確認: 0167 §2.4 の I1

基準との照合を含める（`nvml.py` が変わったので、GPU の温度と電力を `nvidia-smi` と比べる。CPU 温度と CPU 電力も比べる）。
R3 の最初の 30 分と重ねてよい（どちらもアイドルの区間で行う）。`sys.gpu_state` の新しい sample が DB に届くことも見る。

### 2.4 順序

1. 本記録の承認
2. 0167 §2.3 の手順で書き手と fand を止め、`/opt/coldaisle` を `5cd9820` へ更新し、root で venv を同期する（migration は無いが、`coldaisle-rollup` を
   1 回走らせる手順はそのまま行う）。`coldaisle-slackbot.service` を `/etc/systemd/system/` に置いて `daemon-reload` する（この時点では起動しない）
3. 共通の確認のうち C2（`fand_trace_silent.enabled` は出荷の `false` に戻るので `true` に直す）と C3。**確認の間は fand を `disabled` にしておく**
   （T3・R1〜R3 は `systemctl start` / `stop` で動かし、R2 の再起動で fand が勝手に起動しないようにする。0167 §2.3 の「R3 に合格するまで常駐の状態に戻さない」）。
   止まっている間は BIOS の制御
4. §2.1 (b) の T3 の短い回
5. R1・R2（R1 は所有者が現地。0161 §2.4）
6. R3（2 時間）と、その最初の 30 分に I1。基準との照合は、R3 が CPU の負荷を始める 20 分より前のアイドルで 2 分ごとに 10 組とる
7. すべて合格したら fand を `enable --now` して C1・C4（0167 §2.3）を確かめ、続けて `coldaisle-slackbot` を `enable --now` し、許可したチャンネルでメンションして答えが返ることを確かめる（0168 / `docs/ubuntu-deploy.md` 4.3）
8. 結果を #74 にコメントで残す（0167 §5 の 3）。#374 は 7 を確かめてから閉じる

どれかが条件を満たさなければ、`/opt` を `5ff6f96` へ戻して C1 からやり直す（migration が無いので DB は戻さなくてよい）。

## 3. Consequences

### 良くなること

- S の更新を、変わった部分ごとの確認で入れられる。T1（24 h）や全部の T をやり直さない
- 安全の配線は差分の確認と、実際の fault（T3）の 2 つで確かめる

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| T2（書き込みの失敗）・T5（watchdog）・T6（tach stall）は実機でやり直さない | その経路のコード（`safety/`・`hardware/`・`safety_handoff.py`・unit）は変わっていない。R1 が `kill -STOP` / `kill -KILL` と引き継ぎを通す |
| 約 4 時間（T3 15 分・R1〜R2 約 1 時間・R3 2 時間）と所有者の立ち会いが要る | R3 は常駐の運転とつなげる。I1 は R3 と重ねる |
| ボットの起動は最後になる | 確認がすべて済んでから起動する。ボットは fand と取り込みに依存しないので、後から起動しても影響しない |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| ボットだけを `/opt` とは別の場所に置いて先に動かす | 所有者が `/opt` をまとめて更新することを選んだ（2026-10-23） |
| R1〜R3 だけで済ませ、T3 をやり直さない | 0167 は安全の配線の変更を R1〜R3 では見つけられないとして S にした。差分の確認だけでなく、実際の fault を 1 回通す |
| T2・T3・T5・T6 をすべてやり直す | 変わったのは配線のうちヒントの経路だけで、各 fault の検出と出力の経路は変わっていない。T3 で loop を通ることを確かめれば足りる |

## 5. 判断点（決定済み。推奨を先頭に）

**所有者の決定（2026-10-23）**: 1〜3 のすべてを推奨の案で決めた。

| # | 内容 | 選択肢（推奨を先に） |
|---|---|---|
| 1 | S の部分の実機の確認 | **(推奨) 差分の確認 ＋ T3 の短い回（各 1 回）** / 差分の確認だけ（R1〜R3 に任せる）/ T2・T3・T5・T6 をすべて |
| 2 | I1 の時間 | **(推奨) R3 の最初の 30 分と重ねる** / R3 とは別に 30 分 |
| 3 | ボットの起動 | **(推奨) すべての確認の後** / 更新の直後（ボットは fand に依存しない） |
