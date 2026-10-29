# 決定記録 0178: `/opt/coldaisle` を `5cd9820` から `750f05a` へ更新するときにやり直す確認（区分 S・migration あり）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-29、リポジトリ所有者の承認。§5 の 1〜6 はすべて推奨の案）
- **Date**: 2026-10-29
- **Supersedes**: なし（[`0167`](0167-opt-update-recheck-scope.md) §2.1 の区分 S が「更新の前に新しい決定記録で決める」とした、この 1 回の更新の確認を決める）
- **関連**: [`0121`](0121-hardware-test-procedure.md) §2.3 / §2.5 / [`0161`](0161-fand-enable-prerequisites.md) §2.4 / §2.5 /
  [`0162`](0162-hint-b1-shadow-solver-open-points.md) §2.1 / §2.5 / [`0165`](0165-r3-split-cpu-load-run.md) /
  [`0167`](0167-opt-update-recheck-scope.md) / [`0169`](0169-opt-update-5ff6f96-to-5cd9820.md) / [`0170`](0170-telemetry-db-lock.md) /
  [`0171`](0171-daily-report-thread.md) / [`0172`](0172-thermal-offline-evaluation.md) / [`0173`](0173-ingest-rss-trend-check.md) /
  [`0176`](0176-no-gpu-hotspot-mem.md) / `docs/ubuntu-deploy.md` 4「コードを更新するとき」/ 4.4
- **対象 Issue**: #74 / #47 / #378 / #380 / #394

---

## 1. Context

導入先の `/opt/coldaisle` は `5cd9820`（0169 で更新。fand・slackbot が常駐）。その後の main（`750f05a`）には、
#378（telemetry の DB lock。0170）・#380（日次レポートのスレッド。0171）・#394（GPU の hotspot / mem を記録しない。0176）・
#47（RSS の採取。0173）が入った。#47 の残りの 24 時間の RSS の測定（0173）は、この版を入れた後に始める。

`git diff --name-only 5cd9820 750f05a -- src deploy config uv.lock pyproject.toml firmware`（51 path）を 0167 §2.1 に当てた結果
（PR は `git log --first-parent -m 5cd9820..750f05a` の merge で対応を取った。「閉包」は 0167 §2.2 の一行を `750f05a` の
`uv sync --no-dev` の venv で実行した結果で、119 module。checkout の外の `coldaisle` は読んでいない）:

| 変わったもの | PR | 区分 | 中身 |
|---|---|---|---|
| `src/coldaisle/control/loop.py`・`src/coldaisle/control_daemon.py` | #392（0162 PR-C） | **S**（安全の配線） | Workload Hint B1 の Shadow の記録の受け口と書き手の配線。**Critical Safety の呼び方・`effective_demand` の決め方・hardware への出力の順は変わっていない**（§2.1 (a)）。導入先の `stage: off` では新しい経路は開かない |
| `src/coldaisle/control/schema.py`（`ControlTick` の `SCHEMA_VERSION` 16 → **17**、`WORKLOAD_HINT_RECORD_SCHEMA_VERSION` 1 → **2**）・`control/supervisor/workload_hint.py`（`IMPLEMENTED_STAGE` を `observe` → `shadow_mpc`）・`control/hint_shadow.py`（新しい）・`control/hint_monotonicity.py`（新しい）・`control/model_registry.py` | #392・#391・#402 | F（`control/` の S 以外。閉包に入る） | trace の版の上げ・ヒントの塊 v2（`certificate` / `shadow_counters`）・B1 の 2 条件の再検査・Registry の `validate` が反実仮想 thermal model の評価の報告を写す（書く側だけ。fand の読み込みの検証は変えない） |
| `src/coldaisle/learned_channel/`（`config.py`・`hint_shadow.py`（新しい）・`mailbox.py`・`messages.py`・`server.py`） | #391・#392 | F（閉包） | `hint_shadow` の本文の受け取りと検証・保留の表・書き手のスレッド。frame の版は変わらない |
| `src/coldaisle/hint_certification.py`・`hint_shadow_record.py`・`thermal_evaluation_report.py`（`control/model_registry.py` が import する） | #392・#400 | F（閉包） | trace の `certificate` の写し・行の形・評価の報告の ref の形 |
| `src/coldaisle/store/db.py`・`store/models.py`・**`store/migrations/0017_hint_shadow_records_epoch.sql`**・`store/rollup.py` | #392・#389 | F（`store/`） | **DB の migration が 1 つある**（`hint_shadow_records` を作り直し、epoch と累積の数の列を足す。§2.5）。`apply_retention` の削除を metric ごとのトランザクションにした（0170 §2.2） |
| `src/coldaisle/learned_worker/`（`cli.py`・`client.py`・`mpc.py`・`shadow.py`（新しい）・`shadow_solver.py`（新しい）・`__init__.py`） | #391 | F（0169 に倣う。閉包の外） | MPC worker の Shadow solver の子。導入先には learnd の unit を置いていないので動かない |
| `uv.lock`・`pyproject.toml` | #393・#391・#399・#400 | F | **Pillow 12.3.0 が依存に入る**（`coldaisle.report_table` だけが使う。0171 §2.3）。console script 2 つ（`coldaisle-hint-shadow`・`coldaisle-thermal-evaluate`）と pytest の marker `realtime` |
| `src/coldaisle/telemetry_daemon.py`・`src/coldaisle/internal_telemetry/nvml.py`・`internal_telemetry/config.py`・`config/internal-telemetry.yaml` | #389・#396 | I（値を作る部分を含む） | BUSY / LOCKED の周期だけ書き込みを捨てる（0170 §2.1）・`nvml.excluded_metrics` に書いた `gpu.0.hotspot` / `gpu.0.mem` を読まず行も書かない・起動後に 1 回だけ読んで値が返れば `excluded_metric_available` を warning に出す（0176 §2.1） |
| `src/coldaisle/report.py`・`report_table.py`（新しい）・`notify/models.py`・`notify/slack_bot.py`・`config/report.yaml` | #393 | O | 日次レポートを Bot のスレッド（親の題・要点・表の PNG）にする。ファイルに保存しない（0171） |
| `src/coldaisle/slackbot/reply.py` | #383 | O | Slack の太字の mrkdwn の直し |
| `src/coldaisle/soak.py`・`config/soak.yaml`・`deploy/soak/rss-sample.sh`（新しい） | #397 | O | `coldaisle-soak-report --rss-csv` と採取のスクリプト（0173） |
| `src/coldaisle/thermal_evaluate.py`・`config/thermal-evaluation.example.yaml`・`src/coldaisle/registry.py` | #400・#402 | O（閉包の外。人が手で実行する CLI） | 反実仮想 Thermal Model の offline 評価（0172） |
| `src/coldaisle/hint_shadow.py` | #399 | O（閉包の外） | B1 の記録の集計の CLI（読むだけ） |
| `src/coldaisle/hint_certify.py`・`config/hint-shadow-realtime.yaml` | #391 | O（閉包の外） | 認証ランナーと実時間の試験の設定（どちらも導入先の常駐から読まない） |
| `config/learned-channel.yaml` | #391・#392 | O（fand が読むのは `/etc/coldaisle/learned-channel.yaml`。0169 §1 の時点では置かれていない） | `limits.max_shadow_record_bytes` 等と `hint_shadow` の塊 |
| `config/server-health.yaml`・`config/evaluation.yaml` | #396 | O（ただし I の設定と組で効く。§2.3） | `panels.gpu` と `missing_tolerated` から hotspot / mem を外した |
| `deploy/systemd/coldaisle-learnd-mpc.service` | #391 | O（導入先に置いていない unit） | `--hint-lockfile` を足した。置き直すものは無い |
| `src/coldaisle/web/airflow-trace.js` | #392 | O | v17 を知っている版にした（画面は変わらない） |

- `deploy/systemd/` のうち導入先に置いた unit（`coldaisle-fand`・`coldaisle-daemon`・`coldaisle-telemetry`・`coldaisle-cpu-power`・
  `coldaisle-api`・`coldaisle-rollup`・`coldaisle-report`・`coldaisle-slackbot`・`coldaisle-gpu-fan`）・`deploy/udev/`・`firmware/`・
  `config/calibration.json`・`config/rules.yaml`・`src/coldaisle/control/safety/`・`reactive/`・`fallback/`・`hardware/`・
  `control/config.py`・`safety_handoff.py`・`daemon.py`・`ingest/`・`gpu_fan_curve.py` に差分は無い
- Control Config の版は v17 のまま、Learned の frame の版も変わらない

全体の区分は **S** になる（0167 §2.1）。migration があるので 0167 §2.3 の DB の backup の規定も当たる。

## 2. Decision（2026-10-29 所有者が §5 の推奨の案で承認）

### 2.1 S の部分の確認

**(a) 配線が変わっていないことを差分で確かめた**（本記録の作業。2026-10-29。行番号は `750f05a`）

- `control/loop.py` の `5cd9820..750f05a` の差分は **1 か所 4 行だけ**: `ControlLoop._hint_record`（L1271〜）の例外の経路で、
  塊を自分で組み立てていた 4 行を `tracker.lost_record(...)`（`workload_hint.py` に足した同じ形の関数）に置き換えた。
  `_run_tick`（L833〜）は変わっていない。順は今までどおり、`self._safety.evaluate(...)`（L871。`CriticalSafety.evaluate()`）→
  `_coordinate`（Safety の評価の後・Gate の前）→ `_select`（Gate）→ `_requested`（L907）→ `self._composer.compose(..., safety=safety_decision, ...)`
  （L908。`effective_demand` は合成の結果）→ `hardware = self._apply(composed)`（L917。`_apply` は L1966 で `self._backend.apply(composed)`）→
  `self._beat()`（L926）で、trace の組み立てはその後
- `control_daemon.py` の差分は `build()`（L1041〜）と `ControlDaemon.close()`（L830〜）だけ。`CriticalSafety(...)` を組み立てる
  `_build_loop()`（L1580〜。組み立ては L1603）・`hand_back_hardware()`・`run()` は変わっていない
  - `build()`: 証明書の照合を `_open_workload_hint` から `_verify_workload_hint()`（L1377）へ出して先に呼び、B1 の受け口と書き手を開く
    `_open_hint_shadow()`（L1472）を足した。`_verify_workload_hint` は `hint_latch is None`（L1390）なら何もせず None を返す。
    `hint_latch` は設定の `stage` が `observe` より上のときだけ作る（L1094）
  - `_open_hint_shadow` は、Learned の経路が開いている・`hint_domain` がある・照合が通った・実効の段階が `shadow_mpc` 以上、の**すべて**を満たすときだけ
    受け口を作り `learned.server.attach_hint_shadow()`（L1546）を呼ぶ。そうでなければ None
  - `close()`: Learned の経路を止めた後に B1 の書き手を止める行を足した（書き手が無ければ何もしない）。ハードウェアを返す手順とは別の関数で、順は変わらない
- `learned_channel/server.py`: 受け口が無ければ `hint_shadow` の本文を `unsupported_body` として捨てる（`_on_hint_shadow` L535〜541）。
  frame を送った後の環状の列への追加も受け口があるときだけ（L620）。どちらも生存の知らせ・受け渡し口・Gate に入らない
- `control/hint_shadow.py`・`control/hint_monotonicity.py` は値を作らず、`control.safety` / `reactive` / `fallback` / `hardware` から import されない
  （CI の試験 `tests/test_hint_shadow_fand.py::test_safety_guard_fallback_and_hardware_do_not_import_the_shadow_parts`）
- CI の試験 `tests/test_hint_frame_loop.py::test_stage_off_and_b1_give_the_same_demands_every_tick` が、`stage: off` と B1 とで各 tick の
  `requested_demand` / `effective_demand` と、ヒントの塊の外の trace（Supervisor・Gate・Guard・Safety の結果）が 1 ビットも変わらないことを固定している
  （本記録の作業で `750f05a` で通ることを確かめた）

**導入先の設定でこの経路が動くか**: 0161 §5 の 5 と 0169 §1 のとおり、導入先の Control Config の Workload Hint は `stage: off`。
`stage: off` では `hint_latch` が None → 照合しない → `_open_hint_shadow` が None → 受け口は無い。`_open_workload_hint` も tracker を作らず、
`_hint_record` は `WorkloadHintRecord.off()` を返す。`IMPLEMENTED_STAGE` が `shadow_mpc` に上がっても、`effective_stage()` は設定の `off` を超えない
（`workload_hint.py` L612〜625）。導入先は learnd の unit を置いておらず、0169 §1 の時点で `/etc/coldaisle/learned-channel.yaml` も無い
（あっても `stage: off` なら上の条件で開かない）。変わるのは trace の形だけで、v17 の `workload_hint` は
`{"schema_version": 2, "stage": "off", ..., "certificate": null, "shadow_counters": null}` になる。

導入先の root だけが読める設定（`/etc/coldaisle/control-config/` と `/etc/coldaisle/learned-channel.yaml`）は本記録の作業では読めなかったので、
**更新の前に root で確かめる**（§2.4 の手順 2）。`stage` が `off` でなければ更新を止め、本記録を見直す。

**(b) 実機で安全の配線を 1 回通す: 0121 §2.5 の T3（データの遅れと復帰）を短く行う**（0169 §2.1 (b) と同じ手順と合格の条件）。
`coldaisle-telemetry` を 30 s 止めて戻し、続けて `coldaisle-daemon` を 30 s 止めて戻す（各 1 回）。合格は 0121 §2.5 のまま
（stale が 5000 ms 以上・5000 ms + 1 tick 以内に上がり、Top が Max・Front / Rear が `fault_demand` 1.0／air は Front / Rear だけ・Top は Max にならない／解消は 30 s 保持）。
配線は変わっていないが、trace の版が上がり `ControlTick` の検証（`_check_workload_hint`）が tick ごとに増えたので、fault の tick も v17 の trace として
書けることを実際の fault で 1 回見る。所要は約 15 分。

**(c) 較正**: `config/calibration.json`・`calibration_offsets.py`・`ingest/`・`daemon.py` に差分は無いので、較正の確認はしない。

### 2.2 F の部分の確認: 0161 §2.4 の R1〜R3

0167 §2.1 の F のとおり、R1〜R3 をそのままの手順と合格の条件で行う（`stage: off`。R3 の負荷の回は 0165 §2.3 の直したスクリプトで）。
**R3 の trace の `schema_version` は 17**（0167 §2.1 の「`<target>` の `SCHEMA_VERSION` と一致」）。R3 の判定のスクリプトが 16 を固定で見ていれば 17 に直してから使う。
R3 の 2 時間の soak は常駐の運転とつなげてよい（0167 §2.3）。

`store/rollup.py`（0170 §2.2）は `coldaisle-rollup` のタイマーでだけ動く。R3 の 2 時間に 03:00 が入らなければ、R3 では通らない。
§2.4 の手順 4 の migration で `coldaisle-rollup` を 1 回手で走らせるので、そこで落ちずに終わることを見る（`systemctl status` が `status=0/SUCCESS`）。

### 2.3 I の部分の確認: 0167 §2.4 の I1 と 0176 の確かめ

**I1** は基準との照合を含めて行う（`nvml.py` と `telemetry_daemon.py` が変わったので、GPU の温度と電力を `nvidia-smi` と、CPU 温度と CPU 電力も比べる）。
R3 の最初の 30 分と重ねてよい（どちらもアイドルの区間で行う）。

**導入先の `coldaisle-telemetry` と API は `/etc/coldaisle/internal-telemetry.yaml` を読む**（unit の drop-in の `--config`・API の
`COLDAISLE_INTERNAL_TELEMETRY`）。repo の `config/internal-telemetry.yaml` に足した `excluded_metrics` は、そのままでは効かない。
一方 `config/server-health.yaml`（`/opt` のものを読む）は `missing_tolerated` から hotspot / mem を外したので、**`/etc` の写しに
`excluded_metrics` を足さずに更新すると、`missing` の hotspot / mem の行が監視の対象のまま `signal` を恒常的に下げる**
（`api/server_health.py` の `_degrades_signal`。監視の対象は `/etc` の設定から作る `NvmlAdapter.expected_metrics`）。そのため C3 で次を行う（§5 の 4）:

- `/etc/coldaisle/internal-telemetry.yaml` の `nvml:` に `excluded_metrics: [gpu.0.hotspot, gpu.0.mem]` を足す（repo の yaml と同じ行）
- 戻すとき（§2.4 の最後）は**この行を消してから**古いコードを起動する。古いコードの `NvmlConfig` は未知の鍵を拒む（`extra="forbid"`）ので、
  消さないと `coldaisle-telemetry` と API が起動しない

確かめること（I1 の「変わった metric」の行に足す）:

| 確認 | 合格 |
|---|---|
| hotspot / mem の行 | `coldaisle-telemetry` を起動し直した後、`gpu.0.hotspot` / `gpu.0.mem` の新しい行（`missing` を含む）が DB に増えない。`gpu.0.core`・`power.gpu.0`・`gpu.0.utilization` ほかの GPU の行は今までどおり届く |
| 起動後の 1 回の読み（0176 §2.1） | `journalctl -u coldaisle-telemetry` に `excluded_metric_available` が**出ない**（この GPU は 2 つを公開しないので、出ないのが期待どおり）。出たら driver が値を返すようになったということで、不合格ではない。所有者が `excluded_metrics` から外すかを決める（0176 §3） |
| API の状態 | `/api/v1/health` の `stale` が false。状態の画面（server health）の `signal` が hotspot / mem のせいで下がらない |
| #378 の書き込みの捨て方 | 確認の間の `journalctl -u coldaisle-telemetry` に「DB の書き込みロックを取れず」の warning が出ても、`NRestarts` が増えない（出なければそれでよい。0170 §2.1） |

### 2.4 順序

1. 本記録の承認
2. **止める前に読むだけの確認**（root で。書き換えない）:
   Control Config の Workload Hint の `stage` が `off`（`/etc/coldaisle/control-config/` の `fan-policy.yaml`）／`/etc/coldaisle/learned-channel.yaml` の有無
   （あれば中身を所有者が見る。repo の古い写しで `hint_domain` があり `limits.max_shadow_record_bytes` が無いと、新しい検証で Learned の経路だけが無効になる。fand の運転は続く）／
   `/opt/coldaisle` の `git status --short` が ` M config/rules.yaml` と `?? var` だけ／DB の大きさと backup の置き場所の空き（`df`）／
   `SELECT count(*) FROM hint_shadow_records` が 0（`5cd9820` には書き手がいない。0017 はこの表を作り直す）
3. 0167 §2.3 の手順で書き手と fand を止める（`coldaisle-rollup.timer` / `coldaisle-report.timer`・`coldaisle-fand`・`coldaisle-daemon`・`coldaisle-api`・
   `coldaisle-telemetry`・`coldaisle-slackbot`、常駐していれば `coldaisle-eventd`）。**確認の間は fand を `disabled` にしておく**（0169 §2.4 の 3 と同じ理由）。止まっている間は BIOS の制御
4. **DB の backup を取る**（0167 §2.3。§5 の 2）: 書き手がすべて止まったことを確かめてから、root で venv の `python -I` から `sqlite3` の `Connection.backup()` で
   root だけが読める場所へ写し、`PRAGMA integrity_check` が `ok` であることを見る。続けて `/opt/coldaisle` を `750f05a` へ更新する:
   `git fetch` の後 **`git merge --ff-only 750f05a`**（`git pull` は使わない。その時点の main が `750f05a` より進んでいると、本記録の区分の外のものまで入る）。
   `config/rules.yaml` は `5cd9820..750f05a` で変わっていないので、`fand_trace_silent.enabled: true` のローカルの変更は衝突せずに残る。
   **`git stash`・`git checkout -- .`・`git reset --hard` を使わない**（使うと出荷の `false` に戻る。0169 §2.4 の 3 で直したのはこれ）。
   更新の後に `git rev-parse --short HEAD` が `750f05a`、`git status --short` が ` M config/rules.yaml` のまま、`grep -n -A6 '^  fand_trace_silent:' config/rules.yaml` の
   `enabled` が `true` であることを見る（`false` なら C2 のとおり `true` に直す）。root で venv を同期する（`docs/ubuntu-deploy.md` 2 節の形の `uv sync --no-dev`）。
   `/opt/coldaisle/.venv/bin/python -I -c "import PIL; print(PIL.__version__)"` が `12.3.0` を出すこと。導入先に置いた unit に差分は無いので置き直さない（`daemon-reload` も要らない）。
   C3: `/etc/coldaisle/internal-telemetry.yaml` に `excluded_metrics` を足す（§2.3）。ほかに repo の yaml との差分があれば所有者が扱いを決める（今の運用のまま）。
   `systemctl start coldaisle-rollup.service` で migration を当て、終わりを待つ。`SELECT MAX(version) FROM schema_version` が 17、`hint_shadow_records` に
   `worker_instance_id` の列があることを見る
5. 止めた書き手とタイマーを起動し直す（`coldaisle-daemon` / `coldaisle-api` / `coldaisle-telemetry` / `coldaisle-slackbot`、止めていれば `coldaisle-eventd`、続けて
   `coldaisle-rollup.timer` / `coldaisle-report.timer`）。`air.*`・内部の telemetry・`sys.gpu_state` の新しい sample が DB に届き、hotspot / mem の行が増えないことを確かめてから 6 へ進む（0169 §2.4 の 3 と同じ）
6. §2.1 (b) の T3 の短い回
7. R1・R2（R1 は所有者が現地。0161 §2.4）
8. R3（2 時間。trace の `schema_version` 17）と、その最初の 30 分に I1 と §2.3 の確かめ。基準との照合は、R3 が CPU の負荷を始める 20 分より前のアイドルで 2 分ごとに 10 組とる
9. すべて合格したら fand を `enable --now` して C1（`schema_version` 17）・C4 を確かめる。C4 に足す: trace の `workload_hint` が `schema_version` 2・`stage` `off`・
   `certificate` と `shadow_counters` が null、fand の起動ログに「workload hint の証明書を照合した」の行が無い（`stage: off` では照合しない）、`hint_shadow_records` が 0 行のまま。
   許可したチャンネルでボットにメンションし、答えが返り太字が崩れないこと（#383）を見る
10. **#47 の 24 時間の RSS の測定を始める**（`docs/ubuntu-deploy.md` 4.4・0173）。採取のスクリプトは更新した `/opt/coldaisle/deploy/soak/rss-sample.sh` を作業用のディレクトリへ写して使う。
    6 の T3 と 9 までで `coldaisle-daemon` を止めているので、**9 が済んでから**始める（期間中に取り込みの MainPID が変わると不合格になる。0173 §2.4）。
    **期間の 24 時間は `/opt` の更新・`coldaisle-daemon` の再起動をしない**（0173 §2.6）。fand・telemetry・api・slackbot・timer は普段どおり動かしたままにする
11. 測定の期間中の翌朝、`coldaisle-report.timer` の送信が Slack で親の題（`📄 {month}月{day}日の振り返り`）・要点の返信・表の PNG の返信になることを見る（0171。§5 の 6）。
    手で送り直さない
12. 結果を #74 にコメントで残す（0167 §5 の 3。前後の commit・区分・各確認の結果。実機のホスト名・path は書かない）。RSS の結果は #47 に残す（0173 §2.8）

**戻すとき**（6〜9 のどれかが条件を満たさないとき）:

1. 書き手とタイマー（5 で起動し直したもの）と fand を止める
2. `/opt/coldaisle` を `git reset --keep 5cd9820` で戻す（ローカルの `config/rules.yaml` の変更を残したまま戻す。`--hard` は使わない）。venv を同期し直す（Pillow は外れる）
3. `/etc/coldaisle/internal-telemetry.yaml` から `excluded_metrics` の行を消す（§2.3）
4. **DB を手順 4 の backup で置き換える**。`5cd9820` のコードは schema 17 の DB を開かない（`store/migrations.py` の `_pending` が `MigrationError` を上げる）ので、
   `/opt` だけを戻しても書き手も fand も起動しない（0167 §2.3）。backup の後に書かれた sample・trace・アラートの行は失われる（確認の間の分、最長で約 4 時間）。
   失った区間を後で見たいときは、置き換える前の DB を別に写して残す（読むのは `750f05a` のコードで）
5. **fand を `enable --now` して**、止めた書き手とタイマーを戻した venv から起動し直し、新しい sample を確かめ、`fand_trace_silent.enabled` が `true` であることを見て、
   常駐の状態に戻してから C1（`schema_version` 16）からやり直す（0169 §2.4 の最後と同じ理由で、動いている書き手の下で `/opt` を書き換えない）

## 3. Consequences

### 良くなること

- S の更新を、差分の確認と T3・R1〜R3・I1 で入れられる。T1（24 h）や全部の T をやり直さない
- migration を含む更新でも、backup と戻す手順が先に決まっている
- 0176 の設定が導入先の `/etc` の写しにも入り、状態の画面の hotspot / mem の欠測が消える
- 更新の後にそのまま #47 の RSS の 24 時間に入れる（0173 の「`/opt` を更新しない期間」の前に更新を済ませる）

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| T2（書き込みの失敗）・T5（watchdog）・T6（tach stall）は実機でやり直さない | その経路のコード（`safety/`・`hardware/`・`safety_handoff.py`・unit）は変わっていない。R1 が `kill -STOP` / `kill -KILL` と引き継ぎを通す |
| B1 の経路（`_open_hint_shadow`・受け口・書き手）は導入先では動かず、実機で試されない | `stage: off` では開かないことを (a) で確かめた。B1 を始めるときは 0133 §2.6 / 0162 の前提と、読み手と書き手のスレッドを含めた R3 を改めて行う（0161 §5 の 5 と同じ） |
| 戻すと backup の後の sample と trace を失う | 戻すのは確認の間だけで、置き換える前の DB を別に残せる。fand は戻した venv で常駐に戻るので冷却は失わない |
| backup の分のディスクと時間が要る | 手順 2 で空きを見る。backup は確認が済むまで残し、その後は所有者が消す（§5 の 2） |
| 約 4.5 時間（更新と backup 約 30 分・T3 15 分・R1〜R2 約 1 時間・R3 2 時間・常駐に戻す確認 15 分）と所有者の立ち会いが要る。その後に RSS の 24 時間 | R3 は常駐の運転とつなげる。I1 は R3 と重ねる。RSS の 24 時間は人の作業が始めと終わりだけ（各 15 分ほど） |
| `/etc` の写しの `excluded_metrics` は repo と別に持つので、戻すときに消し忘れると telemetry と API が起動しない | §2.3 と戻す手順の 3 に書いた。C3 で今後も差分として見える |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| R1〜R3 だけで済ませ、T3 をやり直さない | 0167 は安全の配線の変更を R1〜R3 では見つけられないとして S にした。配線は変わっていないが、trace の版と検証が変わったので、fault の tick を 1 回実際に通す |
| T2・T3・T5・T6 をすべてやり直す | 各 fault の検出と出力の経路のコードは変わっていない。T3 で loop を通ることを確かめれば足りる |
| migration の backup を取らず、戻すときに 0017 を手で逆に当てる（表を 0016 の形に作り直し `schema_version` の 17 の行を消す） | 0167 §2.3 が backup を求めている。適用済みの migration を手で巻き戻すのは 0002 §2.11 の「追記のみ」の外で、`schema_version` の表を人が書き換えることになる |
| `/etc` の `internal-telemetry.yaml` を変えず、0176 を導入先で効かせない | `/opt` の `server-health.yaml` から `missing_tolerated` が外れるので、状態の画面の `signal` が恒常的に下がる。0176 §2.5 は次の一括の更新で入れるとした |
| RSS の 24 時間を R3 の 2 時間と重ねて始める | 0173 §2.6 は負荷の試験と重ねないとし、T3 で `coldaisle-daemon` を止めると MainPID が変わる。確認がすべて済んでから始める |
| `git pull` で更新する | その時点の main が `750f05a` より進んでいると、本記録で区分していない差分まで入る |

## 5. 判断点（決定済み。推奨を先頭に）

**所有者の決定（2026-10-29）**: 1〜6 のすべてを推奨の案で決めた。

| # | 内容 | 選択肢（推奨を先に） |
|---|---|---|
| 1 | S の部分の実機の確認 | **(推奨) 差分の確認 ＋ T3 の短い回（各 1 回）** / 差分の確認だけ（R1〜R3 に任せる）/ T2・T3・T5・T6 をすべて |
| 2 | DB の backup の取り方と残す期間（0167 §2.3 で取ることは決まっている） | **(推奨) 書き手を止めた後に `Connection.backup()` で全体を写し、`integrity_check` を見て、RSS の 24 時間の判定が済むまで残す** / 同じ取り方で、9 の常駐に戻した時点で消す / `/opt` を更新する前に DB ファイルを `cp` で写す（書き手を止めていれば同じだが、`-wal` の扱いを人が誤りうる） |
| 3 | I1 の時間 | **(推奨) R3 の最初の 30 分と重ねる** / R3 とは別に 30 分 |
| 4 | `/etc/coldaisle/internal-telemetry.yaml` の `excluded_metrics` | **(推奨) 更新と同時に足す（C3。0176 §2.5）** / 足さずに更新し、別の日に足す（その間は状態の画面の `signal` が下がる）/ 足さない（0176 を導入先で効かせない） |
| 5 | #47 の RSS の 24 時間を始める時 | **(推奨) 9 で常駐に戻し C1・C4 を確かめた直後** / 翌朝の日次レポートのスレッドを確かめてから（その分だけ遅れる。レポートの timer は期間中も普段どおり動いてよい） |
| 6 | 日次レポートの新しい形（0171）の確認 | **(推奨) 翌朝の timer の送信で見る（手で送らない）** / 9 の後に `coldaisle-report --date <前日>` を手で送る（同じ日のレポートが 2 通になる） |
