# 決定記録 0167: fand を `enable` した後に `/opt/coldaisle` を更新するとき、差分の区分でやり直す確認を決める

- **種別**: Decision Record
- **Status**: FINAL（2026-10-23、リポジトリ所有者の承認。§5 の 1〜4 はすべて推奨の案）
- **Date**: 2026-10-23
- **Supersedes**: なし（[`0161`](0161-fand-enable-prerequisites.md) §5 の 8 が「最初の更新の前に別の記録で決める」とした一般の規定を決める。
  0161 §2.4 の R1〜R3 の中身と合格の条件は変えずに使う）
- **関連**: [`0121`](0121-hardware-test-procedure.md) §2.3 / §2.10 / [`0128`](0128-gpu-fan-curve-service.md) §2.7 /
  [`0148`](0148-fand-state-alert.md) / [`0161`](0161-fand-enable-prerequisites.md) §1.3 / §2.4 / §2.5 / §5 の 8 /
  [`0165`](0165-r3-split-cpu-load-run.md) / `docs/ubuntu-deploy.md` 4「コードを更新するとき」/ 6.5 / 6.9 /
  `docs/fan-header-mapping.md`「HEAD での確認」
- **対象 Issue**: #74

---

## 1. Context

fand は 2026-10-23 に `/opt/coldaisle` = `5ff6f96` で `enable` した（0161 §2.5 の手順 8）。0161 §2.4 は、`d3c5404` → `5ff6f96` の更新に限って
R1〜R3（約 3.5 時間）をやり直すと決め、`enable` の後の更新で何をやり直すかは「最初の更新の前に別の記録で決める」とした（0161 §5 の 8）。

main はすでに `5ff6f96` より進んでいる（#369 の `sys.gpu_state`、`src/coldaisle/internal_telemetry/`・`telemetry_daemon.py`・`config/gpu_state.yaml`）。
更新のたびに R1〜R3 をやり直すと毎回 3.5 時間かかり、所有者が現地にいる必要がある（R1）。一方で、何もやり直さずに入れると、
fand の経路の回帰に気づけない。

0161 §2.4 は、R1〜R3 で足りるかの判断をやり直す path の一覧を、すでに持っている。本記録はそれを一般の規定に広げ、
差分の区分ごとにやり直す確認を決める。

## 2. Decision

### 2.1 区分

更新の前に、導入済みの commit（`<deployed>`）と入れる commit（`<target>`）の差分を
`git diff --name-only <deployed> <target> -- src deploy config uv.lock pyproject.toml firmware` で取り、各 path を次の区分に当てる。
**差分の全体の区分は、当たった区分のうち最も上のもの**とする。

| 区分 | 当たる path・変化 | やり直す確認 |
|---|---|---|
| **S**（安全の経路） | `src/coldaisle/control/safety/`・Critical Safety を組み立てて呼び、結果を出力の前に当てる配線（`src/coldaisle/control/loop.py`・`src/coldaisle/control_daemon.py`。R1〜R3 は過熱や安全の fault を注入しないので、配線の欠け・順序の誤りを見つけられない）・`control/reactive/`・`control/fallback/`・`control/hardware/`・`src/coldaisle/safety_handoff.py`・`src/coldaisle/control/config.py`（`SafetyConfig` を定義して読み、Critical Safety がそのまま使う。読み方・検証・単位・既定値の変化は値の変化と同じ）・較正の値と当てる経路（`config/calibration.json`・`src/coldaisle/calibration_offsets.py`・`src/coldaisle/ingest/calibration.py`・`src/coldaisle/ingest/normalize.py`・**`src/coldaisle/daemon.py`**（較正を読み `Normalizer` へ渡す配線。`_calibration_for` と `build` の組み立て。I1 は較正した値を基準と照らさないので、この配線の変化は I では見つけられない。daemon.py の差分は丸ごと S に当てる）。取り込みが保存の前に offset を当て、Critical Safety はその値を絶対温度の上限とそのまま比べるので、誤った offset は過熱を隠しうる）・`deploy/systemd/coldaisle-fand.service`・`deploy/udev/`・`safety.yaml` の版か値の変化を求める版・`src/coldaisle/gpu_fan_curve.py` と `deploy/systemd/coldaisle-gpu-fan.service` | **本記録の規定の外。** 更新の前に、やり直す 0121 の試験（T のどれか）と 0128 §2.7 の確認を新しい決定記録で決め、所有者が承認する。それまで入れない |
| **F**（fand のほかのコード） | S に当たらない `src/coldaisle/control/`・fand の import の閉包（§2.2）の module（2026-10-23 の main では `store/`（migration を含む）・`ingest/`（S に当たる較正の部分を除く）・`control_admin/`・`learned_channel/`・`workload_hint_reader/`・`event_entry/`・`channels.py`・`metrics.py`・`measurement.py`・`startup_calibration.py`・`hint_certificat*.py`・`local_socket.py`・`logs.py`・`clock.py`・`csv_export_manifest.py` ほか）・`uv.lock`・`pyproject.toml`（依存・build・同梱物・Python の版）・`ControlTick` の版・Control Config の版 | 0161 §2.4 の **R1〜R3** を、そのままの手順と合格の条件で行う。ただし R3 の trace の `schema_version` は 16 の固定ではなく、`<target>` の `src/coldaisle/control/schema.py` の `SCHEMA_VERSION` と一致することとする（`stage: off`。R3 の CPU と GPU の負荷の回は 0165 §2.3 の直したスクリプトで）。合格するまで `enable` の状態に戻さない（§2.3） |
| **I**（fand の入力を作るもの。閉包の外） | `src/coldaisle/telemetry_daemon.py`・`src/coldaisle/internal_telemetry/`・`src/coldaisle/cpu_power_publisher.py`・それらの unit（`coldaisle-daemon` / `coldaisle-telemetry` / `coldaisle-cpu-power`）・`config/internal-telemetry.yaml`・`config/gpu_state.yaml`・`config/quality.yaml`・`config/metrics.yaml`・`firmware/`。取り込みと共有する module（`ingest/`・`metrics.py` など）は fand の閉包に入るので F | **I1**（§2.4）。`firmware/` の本番のスケッチが変わったときは、I1 の前に所有者がコンパイルと書き込みを行い（0022 §2.9。`firmware/README.md` の確認の一覧）、起動バナーの版が `<target>` の版であることを確かめる |
| **O**（その他） | 上のどれにも当たらないもの（`api/`・`web/`・`ai/`・`notify/`・`rules/`・`report.py` ほか、`docs/`・`tests/`、ほかの `config/`） | 共通の確認（§2.3）だけ |

- 区分の当てはめは、更新を準備する側（エージェントでもよい）が行い、差分の path と区分の一覧を所有者に示す。**所有者が確かめてから更新する**
- 一覧に無い新しい path（新しい module・新しい unit）は、fand の import の閉包に入るなら F、fand の入力を作るなら I とする。
  どちらか判断できなければ上の区分に寄せる
- 当てはめの結果とやり直した確認の結果は、#74 にコメントで残す（`/opt` の前後の commit と区分。実機のホスト名・path は書かない）

### 2.2 fand の import の閉包

F の「fand の import の閉包」は、**`<target>` のコードを入れた環境**で次の一行が出す path とする。導入先の `/opt/coldaisle/.venv` は
更新の前は `<deployed>` のコードなので使わない（`<target>` で足した module が閉包に出ず、O に見誤る）。`/opt` とは別の場所に `<target>` を
checkout し、その中で `uv sync --no-dev` してできた venv で実行する（`-I` は作業ディレクトリを読まないので、venv に入った `<target>` の
コードだけが見える）。

```bash
git clone --quiet <リポジトリ> /tmp/coldaisle-target && git -C /tmp/coldaisle-target checkout --quiet <target>
(cd /tmp/coldaisle-target && uv sync --no-dev --quiet)
/tmp/coldaisle-target/.venv/bin/python -I -c "import sys, coldaisle.control_daemon; print('\n'.join(sorted(m.__file__ for n, m in sys.modules.items() if n.startswith('coldaisle') and getattr(m, '__file__', None))))"
```

出た path がすべて `/tmp/coldaisle-target/src/` の下であることを確かめる（別の場所の `coldaisle` を読んでいない）。

関数の中で import する module（`control/supervisor/artifact.py` などの遅延の import）はここに出ないことがあるが、`src/coldaisle/control/` は
丸ごと F 以上に当てるので、閉包の取りこぼしは `control/` の外の module に限られる。閉包に出なくても fand から使う module を見つけたら、
次の更新から F に足す（本記録の §2.1 の表は path の例であり、閉包と合わせて読む）。

### 2.3 すべての区分で行う手順と共通の確認

更新の手順は `docs/ubuntu-deploy.md` 4「コードを更新するとき」のとおりで、**最初に fand を止める**（DB の書き手なので。`systemctl stop` で
3 zone は BIOS の制御へ戻る）。止めている間は BIOS の制御で、0148 の通知を出す `coldaisle-daemon` も止まっている。

差分に `deploy/systemd/` の unit が含まれるときは、**変わった unit をすべて** `/etc/systemd/system/` へ置き直して `daemon-reload` してから書き手を起動する
（`docs/ubuntu-deploy.md` 4 の例は `coldaisle-telemetry.service` だけを置くが、それに限らない。置き直さないと古い unit のまま確認が通ってしまう）。
導入先で書き換えている行（0161 §2.5 の手順 5）があれば、置き直した後に同じ書き換えをやり直す。

**差分に DB の migration（`src/coldaisle/store/migrations.py` の新しい版）が含まれるときは、migration を当てる前に DB の backup を取る。**
書き手をすべて止めた後に root で SQLite の backup を取り（venv の `python -I` から `sqlite3` の `Connection.backup()`）、確認が済むまで残す。古いコードは
新しい版の DB を開かない（`store/migrations.py`）ので、`/opt` だけを戻しても起動できない。戻すときは書き手を止め、`/opt` を `<deployed>` へ戻し、
DB を backup で置き換えてから起動する。backup の後に書かれた sample と trace はそのとき失われる（確認の間の分だけ）。

共通の確認（どの区分でも行う）:

| # | 確認 | 合格 |
|---|---|---|
| C1 | 書き手を起動し直した後、`coldaisle-fand` を `start` する（`enable` は残ったまま） | `active`・`NRestarts` 0、起動ログの `safety_config_is_provisional` が false、trace が届き、`schema_version` が期待の版 |
| C2 | `/opt/coldaisle/config/rules.yaml` の `fand_trace_silent.enabled` | 出荷の `false` に戻っていたら `true` に直して `coldaisle-daemon` を再起動する（0161 §2.5 の手順 10） |
| C3 | `/etc/coldaisle/internal-telemetry.yaml` の写しと repo の yaml の差分 | 差分があれば所有者が扱いを決める（今の運用のまま） |
| C4 | 更新の後 10 分の trace | `safety_state` が `normal` に戻り（起動の直後の `startup` を除く）、`faults` が空 |

F では、R1〜R3 の間は 0161 §2.4 の手順どおり `systemctl start` / `stop` で動かし、R3 に合格するまで C1 で常駐の状態に戻さない
（R3 の 2 時間の soak をそのまま常駐の運転とつなげてよい）。合格しなければ `/opt` を `<deployed>` へ戻し（migration を当てたなら上の backup で DB も戻す）、C1 からやり直す。

### 2.4 I1: fand の入力を作るものが変わったときの確認

| 確認 | 範囲 | 合格 |
|---|---|---|
| fand の trace の `faults` と理由 | 更新の後 30 分（アイドルでよい） | `cpu_telemetry_stale`・`gpu_telemetry_stale`・`air_telemetry_stale`・`t_sensor_stale` の `faults` と、Top の理由の detail の `cpu_power_unavailable` が 0 tick |
| 入力の新しさ | 同じ 30 分 | `/api/v1/health` の `stale` が false、`data_age_seconds` が 5 s 未満。`coldaisle-telemetry`・`coldaisle-daemon`・`coldaisle-cpu-power` が `active` で `NRestarts` 0（`coldaisle-cpu-power` は `RestartSec=2` で戻るので、5 s の古さの上限に届かない再起動は trace に出ない） |
| 変わった metric | 差分が足した・変えた metric | DB に新しい sample が届いている（例: #369 の `sys.gpu_state`） |
| **基準との照合**（値を作る部分が変わったとき。`internal_telemetry/`・`telemetry_daemon.py`・`cpu_power_publisher.py`・`firmware/`） | 同じ 30 分に、アイドルで 10 組以上 | Critical Safety が読む metric（導入先の `safety.yaml` の絶対温度の上限と CPU cooling floor が読むもの。この機械では CPU 温度・GPU コア温度・CPU Package 電力）の DB の値を、coldaisle を通さない読み取りと比べる。GPU は `nvidia-smi --query-gpu=temperature.gpu,power.draw`、CPU 温度は hwmon の `tempN_input` を直接（÷1000）、CPU 電力は root で RAPL の `energy_uj` の 1 s の差。時刻の差 2 s 以内の組で、温度は ±1 °C 以内・電力は ±5 % か ±3 W の大きいほう以内。空気の温度（`firmware/`）は基板の値と別の温度計の差が今までの差から 1 °C を超えて動かないこと。1 組でも外れれば不合格 |

基準との照合は、値がそのまま届いていても変換の誤り（単位・桁・チャネルの取り違え）で低く出る退行を見つけるためのもので、所有者の判断で足した（2026-10-23。PR #372 の Codex の指摘）。

I1 で合格しなければ `/opt` を `<deployed>` へ戻す（migration を当てたなら DB も §2.3 の backup で戻す）。fand は入力が古くなれば `*_telemetry_stale` で Max・fault にする（安全側。0060 §2.2）ので、
I1 の間に不合格になっても冷却は失わない。

### 2.5 最初の適用（`5ff6f96` → `4e80252`）

2026-10-23 の main（`4e80252`）を §2.1 に当てた結果（`git diff --name-only 5ff6f96 4e80252 -- src deploy config uv.lock pyproject.toml firmware`）:

| path | 区分 |
|---|---|
| `config/gpu_state.yaml`（新しい） | I（`coldaisle-telemetry` が読む。fand は読まない） |
| `src/coldaisle/internal_telemetry/__init__.py`・`gpu_state.py`（新しい）・`models.py`・`nvml.py` | I |
| `src/coldaisle/telemetry_daemon.py` | I |

`coldaisle.control_daemon` の import の閉包（§2.2。開発の checkout の `4e80252` で実行）には `coldaisle.internal_telemetry`・`coldaisle.telemetry_daemon`・`coldaisle.daemon`・`coldaisle.cpu_power_publisher` が無いことを確かめた。
`deploy/`・`uv.lock`・`pyproject.toml`・`store/`・`firmware/` に差分は無い。したがって区分は **I** で、共通の確認と I1 を行う。`nvml.py` が変わったので、I1 の基準との照合（GPU の温度と電力）も行う。本記録の承認の後、所有者の判断で更新する。

その後の main（`d4f3087`。#373 の PR-A）は `control/loop.py`・`control_daemon.py` を変えたので、そこまで入れると区分は **S** になる（§2.1）。その更新の前に、やり直す試験を新しい決定記録で決める。

## 3. Consequences

### 良くなること

- 更新のたびに 3.5 時間の確認をせずに済む。fand に関係しない更新（文書・通知・AI など）は、共通の確認だけで入れられる
- 安全の経路が変わる更新は、規定の外として必ず新しい記録と承認を通る。毎回同じ R1〜R3 で済ませて、変わった安全の振る舞いを試さないまま入れることが無い
- 区分の当てはめを path で機械的に行えるので、エージェントが準備して所有者が確かめる形にできる

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| 区分の表に無い経路で fand に効く変更（遅延の import・共有の設定）を O と見誤りうる | `control/` は丸ごと F 以上。判断できなければ上の区分に寄せる。0148 の通知・C4 で常駐の後も見る。見つけたら表に足す |
| 更新の間（fand を止めてから C1 まで）は BIOS の制御になる | 今までの更新と同じ。BIOS の曲線とチップの保護は効いている。0161 §2.4 の R1〜R3 の間も同じ |
| I では負荷の下の tick の時間を見ない | fand のコードは変わっていない。入力の欠けは stale で Max になる（安全側） |
| F で R3 の 2 時間の間は所有者の負担が残る | R1 だけ現地が要る。R3 は常駐の運転とつなげられる |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 毎回 R1〜R3 をやり直す（0161 §5 の 8 の 2 つ目の案） | fand に関係しない更新でも 3.5 時間かかり、更新が溜まる。溜まると 1 回の差分が大きくなり、かえって切り分けにくい |
| 区分 S も R1〜R3 で済ませる | R1〜R3 は T1 の後に安全の経路が変わっていないことを前提にした短い確認で（0161 §2.4）、安全の振る舞いそのものを試さない |
| 区分を path ではなく PR ごとの申告（ラベル）で決める | 申告の漏れを防げない。path は差分から機械的に取れる |
| 更新の間も fand を動かしたまま入れる | fand は DB の書き手で、migration の手順（`docs/ubuntu-deploy.md` 4）と合わない。止めても BIOS の制御で冷える |

## 5. 判断点（決定済み。推奨を先頭に）

**所有者の決定（2026-10-23）**: 1〜4 のすべてを推奨の案で決めた。

| # | 内容 | 選択肢（推奨を先に） | 時期 |
|---|---|---|---|
| 1 | 区分の数と境界 | **(推奨) S / F / I / O の 4 つ（§2.1）** / F と I を 1 つにまとめ、どちらも R1〜R3 / S と F を 1 つにまとめ、どちらも R1〜R3 | 承認のとき |
| 2 | I1 の観察の長さ | **(推奨) 30 分（アイドルでよい）** / 2 時間（R3 と同じ） / 共通の確認の 10 分だけ | 承認のとき |
| 3 | 当てはめの結果を残す場所 | **(推奨) #74 のコメント** / `docs/fan-header-mapping.md` に節を足す / 残さない | 承認のとき |
| 4 | `5ff6f96` → `4e80252` の更新（§2.5） | **(推奨) 本記録の承認の後、区分 I として共通の確認と I1 で入れる** / B1 などの次の F の変更と一緒に入れる | 承認の後 |
