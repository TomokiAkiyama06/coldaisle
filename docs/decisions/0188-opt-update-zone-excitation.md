# 決定記録 0188: `/opt/coldaisle` を `f706c6c` から揺らぎ（0185 / 0186）の入った main へ更新するときにやり直す確認と、apply の始め方（区分 S）

- **種別**: Decision Record
- **Status**: FINAL（2026-11-23、リポジトリ所有者の承認。§5 の 1〜4 はすべて推奨の案）
- **Date**: 2026-11-23
- **Supersedes**: なし（[`0167`](0167-opt-update-recheck-scope.md) §2.1 の区分 S が「更新の前に新しい決定記録で決める」とした、この 1 回の更新の確認を決める）
- **関連**: [`0121`](0121-hardware-test-procedure.md) §2.5 / [`0161`](0161-fand-enable-prerequisites.md) §2.4 / [`0165`](0165-r3-split-cpu-load-run.md) /
  [`0167`](0167-opt-update-recheck-scope.md) / [`0178`](0178-opt-update-5cd9820-to-750f05a.md) / [`0185`](0185-routine-zone-excitation.md) /
  [`0186`](0186-zone-excitation-after-gate.md) / [`0187`](0187-ridge-shared-gram.md) / `docs/control-config.md`「Control Config v18 と `fan-policy.yaml` v14」
- **対象 Issue**: #74 / #84

---

## 1. Context

導入先の `/opt/coldaisle` は `f706c6c`（0179 で更新。fand・slackbot が常駐）。その後の main（`<target>`。§2.4 の手順 1 で hash を決める）には、
揺らぎ（0185 / 0186。PR #429 / #431）と、学習・Dataset・評価の CLI の変更（#417〜#426・#433）、trace の削除の境界（#411）が入った。
揺らぎを 1 週間 apply する（0185 §5 の 6）には、この版を入れる必要がある。

`git diff --name-only f706c6c <target> -- src deploy config uv.lock pyproject.toml firmware` を 0167 §2.1 に当てた結果:

| 変わったもの | PR | 区分 | 中身 |
|---|---|---|---|
| `src/coldaisle/control/loop.py` | #431 | **S**（安全の配線） | `_requested` の後・`compose` の前に揺らぎの 1 段を足した（§2.1 (a)）。trace に `zone_excitation` を載せる |
| `src/coldaisle/control/config.py` | #429 | **S**（`SafetyConfig` を定義するファイル） | Control Config v17 → **v18**、`fan-policy.yaml` v13 → **v14**（`zone_excitation` の塊が必須）。`SafetyConfig` と `safety.yaml` の版（5）は変わらない |
| `src/coldaisle/control/schema.py`（`ControlTick` 17 → **18**、`AuthorityRecord` 1 → **2**）・`control/zone_excitation.py`（新しい）・`control/authority.py`・`control/__init__.py` | #429・#431 | F | 揺らぎの記録の型・予定の部品・authority の trace の記録に照合の上限 2 欄 |
| `src/coldaisle/store/db.py`・`store/export_binding.py` | #411・#416〜#423 | F（`store/`） | trace の削除の境界を索引で求める（#408 の lock の原因）・trace を 1 行ずつ写す読み出し。**migration は無い** |
| `src/coldaisle/control/model/`（`training.py`・`counterfactual_training.py`・`counterfactual_confidence.py`・`dataset.py`） | #417〜#426・#433 | F（`control/` は丸ごと F 以上。fand の閉包の外） | 学習と Dataset の変更。人が手で実行する CLI からだけ使う |
| `src/coldaisle/dataset.py`・`training_entry.py`・`thermal_evaluate.py`・`thermal_evaluation_report.py` | #417〜#426 | O（閉包の外の CLI） | |
| `src/coldaisle/web/airflow-trace.js` | #431 | O | v18 を知っている版にした（画面は変わらない） |
| `deploy/soak/rss-sample.sh` | #410 | O | 常駐に影響しない |

- `src/coldaisle/control/safety/`・`reactive/`・`fallback/`・`hardware/`・`safety_handoff.py`・`control_daemon.py`・`daemon.py`・`ingest/`・
  `config/calibration.json`・`calibration_offsets.py`・`gpu_fan_curve.py`・`deploy/systemd/`・`deploy/udev/`・`firmware/`・`uv.lock`・`pyproject.toml`・
  `config/` に差分は無い（`<target>` で手順 1 のときに確かめ直す）
- I の path（`telemetry_daemon.py`・`internal_telemetry/`・`cpu_power_publisher.py`・それらの unit と設定）に差分は無いので、I1 は当たらない

全体の区分は **S** になる（0167 §2.1）。migration は無いので、0167 §2.3 の DB の backup の規定は当たらない。

もう 1 つ、**導入先の `/etc/coldaisle/control-config/fan-policy.yaml` を v14 に書き換える必要がある**。新しいコードは v13 を拒否し、
`config_invalid` の全 zone Max で止まる。古いコードは v14（未知の鍵 `zone_excitation`）を拒否する。コードと設定は同じ止めている間に、組で変える。

## 2. Decision

### 2.1 S の部分の確認

**(a) 配線の変わり方を差分で確かめた**（本記録の作業。2026-11-23）

- `control/loop.py` の `f706c6c..<target>` の差分は、揺らぎの部品の生成（`__init__`）・1 段の呼び出し・trace の欄・2 つの静的関数（`_excitation_blocked` /
  `_excitation_temperatures`）だけ。`_run_tick` の順は、`self._safety.evaluate(...)` → `_coordinate` → `_select`（Gate）→ `_requested` →
  **揺らぎ（新しい）** → `self._composer.compose(..., guard=..., safety=safety_decision, ...)` → `_apply` → `_beat` のまま。揺らぎは compose の
  前の requested だけを変え、Guard の floor / ceiling と Critical Safety の floor・`forced_max` は compose で今までどおり当たる
- **`mode: "off"` では requested を変えない**: `ZoneExcitation.step` は最初の行で `mode is OFF` なら、受け取った requested をそのまま返す
  （同じ object）。CI の試験 `tests/test_zone_excitation_loop.py::test_off_records_off_every_tick_and_does_not_change_requested` が固定している
- `_excitation_blocked` / `_excitation_temperatures` は off でも毎 tick 呼ばれる（例外を出しうる処理ではないが、R3 の 2 時間で実際の tick を通す）
- `ControlTick` v18 の検証（`_check_zone_excitation`・AuthorityRecord v2）が tick ごとに増える。trace の検証の失敗は今までどおり trace の失敗として扱う
- `control_daemon.py`・`safety/`・`reactive/`・`fallback/`・`hardware/` は変わっていない
- 取り込みの rules（0148 の `fand_*`）は trace の `state.safety_state` と `faults` だけを読み、版を見ない（`rules/fand_trace.py`）。v18 でも同じに読める

**(b) 実機で安全の配線を 1 回通す: 0121 §2.5 の T3 を短く行う**（0178 §2.1 (b) と同じ手順と合格の条件。約 15 分）。
fault の tick も v18 の trace として書けることと、Top が Max・Front / Rear が `fault_demand` 1.0 になることを実際の fault で見る。

**(c) 較正**: 較正の値と経路に差分は無いので、確認しない。

### 2.2 F の部分の確認: 0161 §2.4 の R1〜R3

0167 §2.1 の F のとおり R1〜R3 を行う（`zone_excitation.mode: "off"`）。**R3 の trace の `schema_version` は 18**、`authority.schema_version` は 2、
`zone_excitation` は毎 tick `{"schema_version": 1, "mode": "off", "state": "off"}`。0178 の判定のスクリプトは版を固定で見るので、18 と 2 に直してから使う。
R3 の 2 時間の soak は常駐の運転とつなげてよい（0167 §2.3）。

`store/db.py` の削除の境界（#411）は `coldaisle-rollup` のタイマーでだけ動く。手順 4 で `coldaisle-rollup.service` を 1 回手で走らせ、`status=0/SUCCESS` で
終わることを見る。

### 2.3 `fan-policy.yaml` の書き換え

- 書き手と fand を止めた後（手順 3）、`/etc/coldaisle/control-config/fan-policy.yaml` を root だけが読める同じ場所の `fan-policy.yaml.pre0188` へ写してから、
  `docs/control-config.md` の v14 の塊を **`mode: "off"`** で足し、`schema_version: 14` に上げる。ほかの行は変えない
- 値は `docs/control-config.md` の例のまま（0185 §5・0186 §2.2 で決めた値・`provisional`）。`seed` は 1
- 書き換えた後、`<target>` の venv で 4 ファイルを読み込み（fand の起動の前に）、`config_invalid` にならないことを見る
- 戻すときは `.pre0188` で置き換える（古いコードは v14 を拒否するので、`/opt` を戻すときは必ず組で戻す）
- `fan-policy.yaml` の hash が変わるので、authority の照合（0090）では承認のときの設定と違うことになる。導入先の authority は `shadow` なので、
  Baseline より上が無効になる影響は無い

### 2.4 順序

1. 本記録の承認。`<target>` を決める（その時点の main の HEAD。手順 2 の前に、上の表の区分を `<target>` で確かめ直す。表の外の path があれば止める）
2. **止める前に読むだけの確認**（root で。書き換えない）: `fan-policy.yaml` が v13 で `zone_excitation` が無い／Workload Hint の `stage` が `off`／
   `/opt/coldaisle` の `git status --short` が ` M config/rules.yaml` と `?? var` だけ／GPU のジョブと docker・CPU 電力（試験の前の確認）
3. 0167 §2.3 の手順で書き手と fand を止める。**確認の間は fand を `disabled` にしておく**。止まっている間は BIOS の制御
4. `/opt/coldaisle` を `<target>` へ更新する: `git fetch` の後 **`git merge --ff-only <target>`**（`git pull`・`git stash`・`git checkout -- .`・`git reset --hard` は使わない）。
   `config/rules.yaml` のローカルの変更（`fand_trace_silent.enabled: true`）が残ることを見る。root で venv を同期する（`uv sync --no-dev`）。
   §2.3 の書き換えと読み込みの確認。`coldaisle-rollup.service` を 1 回手で走らせる
5. 止めた書き手とタイマーを起動し直し、新しい sample が DB に届くことを確かめる
6. §2.1 (b) の T3 の短い回
7. R1・R2（R1 は所有者が現地。R2 は OS の再起動）
8. R3（2 時間。§2.2 の版）
9. すべて合格したら fand を `enable --now` して C1（`schema_version` 18）・C2・C4 を確かめる。C4 に足す: `zone_excitation` が毎 tick `mode: off`・`state: off`
10. **apply を始める**（§2.5）
11. 結果を #74 と #84 にコメントで残す（前後の commit・区分・各確認の結果。実機のホスト名・path は書かない）

**戻すとき**（6〜9 のどれかが条件を満たさないとき）: 書き手とタイマーと fand を止める → `/opt/coldaisle` を `git reset --keep f706c6c` で戻し venv を同期し直す →
`fan-policy.yaml` を `.pre0188` で置き換える → fand を `enable --now` して書き手とタイマーを起動し直し、C1（`schema_version` 17）からやり直す。
migration が無いので DB は戻さない。v18 の trace の行は DB に残るが、古いコードの取り込みの rules は版を見ずに読み（§2.1 (a)）、画面は「未対応の版」と出すだけで、
fand が新しい v17 の行を書けば最新の行は v17 に戻る。

### 2.5 apply の始め方と、1 週間の見方

1. `fan-policy.yaml` の `mode` だけを `"apply"` に変え（ほかの値は §2.3 のまま）、読み込みを確かめてから `systemctl restart coldaisle-fand`（C1 をもう一度）
2. **最初の 1 時間を見る**（off の区間は 10〜20 分なので、区間が 2〜4 回入る）:

| 確認 | 合格 |
|---|---|
| 区間が入る | `state: on` の tick があり、1 つの区間は 1 つの zone だけ、`after - before` は 0.10（1.0 で頭打ちのときは 1.0 まで） |
| 上げるだけ | `on` の tick の zone の `effective_demand` が `after` 以上（Guard / Safety が上げれば更に高い）。ほかの 2 zone の requested は揺らぎの前と同じ |
| 条件で打ち切る | `skipped` の tick の理由が、その tick の状態と合う（Guard が掛かった・温度・manual など）。打ち切りの後は `idle` に戻る |
| 安全の経路 | `safety_state` は `normal` のまま（startup を除く）、`faults` は空。0148 の `fand_*` のアラートが出ない |

3. 合格したら 1 週間 apply のまま運転する（0185 §5 の 6）。毎朝の日次レポートと Server Health で温度・アラートを見る
4. **止める条件**（どれか 1 つで `mode` を `"off"` に戻して restart する。所有者はいつでも戻せる）: 揺らぎの `on` の tick で `safety_state` が `normal` を外れる／
   0148 の `fand_emergency` / `fand_degraded` が鳴る／所有者が騒音か温度を不適と判断する。戻したら結果を #84 に書き、本記録か新しい記録で扱いを決める
5. 1 週間の後、`mode` を `"off"` に戻すか続けるかを所有者が決める。その週を含む Dataset → 学習 → offline 評価（0172）を行う

## 3. Consequences

### 良くなること

- S の更新を、差分の確認と T3・R1〜R3 で入れられる。T1（24 h）や全部の T をやり直さない
- 揺らぎを off で入れて R1〜R3 を通してから apply にするので、更新の確認と揺らぎの効きを分けて見られる
- migration が無いので、戻すのは `/opt` と `fan-policy.yaml` の組だけで、DB の行を失わない

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| T2（書き込みの失敗）・T5（watchdog）・T6（tach stall）は実機でやり直さない | その経路のコード（`safety/`・`hardware/`・`safety_handoff.py`・`control_daemon.py`・unit）は変わっていない。R1 が `kill -STOP` / `kill -KILL` と引き継ぎを通す |
| R1〜R3 は `mode: "off"` で行うので、apply の経路は R3 の負荷の下で試されない | apply は上げるだけで、Guard と Safety の前に掛かる（0186 §2.1）。§2.5 の最初の 1 時間で `on` と `skipped` を実際の tick で見る。温度の条件（CPU 80・GPU 77 ℃）で高負荷では掛からない |
| 揺らぎの区間は Fan が最大 0.10 上がり、音が変わる | 5 分の区間を 10〜20 分おき。所有者がいつでも off に戻せる（§2.5 の 4） |
| `/etc` の `fan-policy.yaml` を書き換えるので、戻すときにコードと組で戻し忘れると全 zone Max で止まる | 戻す手順に組で書いた。`.pre0188` を残す |
| 約 4 時間（更新 20 分・T3 15 分・R1〜R2 約 1 時間・R3 2 時間・常駐に戻す確認 15 分）と所有者の立ち会い（R1）が要る | R3 は常駐の運転とつなげる。apply の最初の 1 時間は人の作業が始めと終わりだけ |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| R1〜R3 だけで済ませ、T3 をやり直さない | 0178 §4 と同じ。安全の配線（loop.py）が変わり、trace の版と検証も変わったので、fault の tick を 1 回実際に通す |
| 最初から `mode: "apply"` で R1〜R3 を行う | 更新の確認の不合格と揺らぎの影響を分けられない。R3 の負荷の回で揺らぎが温度の条件に当たり、判定が読みにくくなる |
| DB の backup を取る | migration が無く、古いコードも v18 の行を読める（§2.4 の戻すとき）。backup の時間とディスクに見合う利点が無い |
| `<target>` を #431 のマージ（`fa300bb`）に固定する | #433（学習の上限）は閉包の外の学習の CLI だけで、1 週間の後の学習に要る。同じ更新で入れる |

## 5. 判断点（推奨を先頭に）

| # | 判断 | 選択肢 | いつまでに | 状態 |
|---|---|---|---|---|
| 1 | やり直す確認 | **(推奨) T3 の短い回・R1〜R3（§2.1・§2.2）。I1・DB の backup は無し** / T2・T5・T6 もやり直す / R1〜R3 だけ | 更新の前 | 決定済み（所有者、2026-11-23）: 推奨の案 |
| 2 | 揺らぎを入れる順 | **(推奨) off で入れて R1〜R3 を通し、その後 apply（§2.5）** / 最初から apply | 更新の前 | 決定済み（所有者、2026-11-23）: 推奨の案 |
| 3 | apply の最初の確認と止める条件 | **(推奨) §2.5 の 1 時間の確認と止める条件** / 確認なしで 1 週間 | apply の前 | 決定済み（所有者、2026-11-23）: 推奨の案 |
| 4 | `<target>` | **(推奨) 承認のときの main の HEAD（#433 を含む）** / `fa300bb`（揺らぎまで） | 更新の前 | 決定済み（所有者、2026-11-23）: 推奨の案 |
