# 決定記録 0103: Thermal Dataset の読み込みで、同じ anchor の複製と同じ観測の食い違いを拒否する（v1 / v2 共通）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-07
- **Supersedes**: なし（0031 / 0087 / 0094 の読み込み時の検査への追加。どの記録の決定も置き換えない。0094 §5 #2 をここで決める）
- **関連**: [0031](0031-thermal-dataset-contract.md) §2.1 / §2.2 /
  [0079](0079-model-artifact-formats.md) §2.9 /
  [0087](0087-dataset-v2-action-grid.md) §2.1 / §2.8 /
  [0094](0094-dataset-v2-settled-points.md) §2.6 / §2.7 / §5 #2 /
  [0097](0097-artifact-profile-v2-settled-points.md) /
  migration `0001_initial.sql`（`readings` の主キー `(metric, ts_ms)`）/
  migration `0002_control_traces.sql`・`0007_control_trace_seq.sql`（`control_traces` の主キー `(ts_ms, tick_id)`）/
  `src/coldaisle/control/model/dataset.py` / `src/coldaisle/dataset.py` /
  PR #221 の Codex の指摘（[r4203402682](https://github.com/TomokiAkiyama06/coldaisle/pull/221#discussion_r4203402682)・
  [r4203402686](https://github.com/TomokiAkiyama06/coldaisle/pull/221#discussion_r4203402686)）
- **対象 Issue**: #224

## 1. Context

PR #221（Thermal Dataset v2）のレビューで、Codex が読み込み時の検証の欠落を2件指摘した（どちらも P2）。
同じ欠落が v1 の loader（0031）にもあるため、2026-10-07 に所有者が「v1 / v2 共通の loader の強化として
別 Issue（#224）で扱い、実装の前に決定記録を作る」と判断した（0094 §2.7）。

1. **同じ anchor の複製を拒否していない。** example の一意性は `example_id` だけで検査している
   （`ThermalDataset` / `ThermalDatasetV2` の「example_idが重複している」）。example を丸ごと複製して別の `example_id` を付け、
   `example_count` と `examples_sha256` を計算し直せば読み込みを通る。v2 の元の tick の照合（0094 §2.6 #2）も、
   複製は tick が一致するので通る。同じ物理的な anchor が split と学習で複数回数えられ、学習データの分布が歪む
2. **同じ観測の食い違いを拒否していない。** 1つの as-of の観測を複数の frame が使うとき、`(metric, source_ts_ms)` は
   保存された1つの読み取りを指す（`readings` の主キーが `(metric, ts_ms)`）。しかし現在の検査は metric ごとの
   `source_ts_ms` が逆行しないことしか見ない（v1 / v2 共通の `_check_example_against_run_and_spec`）。同じ `source_ts_ms` の
   frame に違う値・quality・missing を持たせても、hash を計算し直せば読み込みを通る。1つの観測から変化する特徴量の
   軌跡を作れてしまう。重なる example の間（ある example の window と別の example の window / target）でも同じことが起きる

どちらも改ざん・破損した artifact への防御で、builder（`coldaisle/dataset.py`）が DB から作る dataset では起きない
（§2.4 で確かめた）。artifact v2・trainer（#84）が v2 の dataset を学習に使う前に、読み込みの契約として塞いでおく。
loader の契約（何を不正として拒むか）が v1 / v2 の両方で変わるため、実装の前に記録する。

## 2. Decision

### 2.1 同じ anchor は1つの example にしかしない（v1 / v2 共通）

`ThermalDataset` / `ThermalDatasetV2` の検証で、**`(source_run_id, action_ts_ms, control_tick_id)` を anchor のキーとし、
dataset 全体で一意であることを求める。** 2つ以上の example が同じキーを持てば、`example_id` や中身が違っても
（中身が完全に同じでも）読み込みで拒否する。

- キーは `control_traces` の主キー `(ts_ms, tick_id)` に run を足したもの。1つの source run の DB で1つの ControlTick を
  一意に指す（0031 §2.3 で DB は1 run 専用）
- **v1 で `control_tick_id` をキーから外さない。** v1 の builder は `control_traces` の行をそのまま anchor にし、
  主キーは `(ts_ms, tick_id)` なので、同じ `ts_ms` で `tick_id` の違う2つの tick（同じミリ秒の中の再起動など）を
  正当に作りうる。`(source_run_id, action_ts_ms)` だけをキーにすると、builder の正当な出力を拒みうる
- **v1 で `action_ts_ms` もキーから外さない。** `tick_id` は `coldaisle-fand` の再起動で振り直されるので、v1 では
  同じ `tick_id` が別の時刻に現れうる（v2 は再起動を含む anchor を作らない。0087 §2.2）
- v2 では、0094 §2.6 #2 の照合が「同じ `(run, 時刻)` の tick は1つの `tick_id` しか持たない」ことを既に求めているので、
  このキーの一意性は `(source_run_id, action_ts_ms)` の一意性と同じになる。v2 だけ別のキーにはしない（v1 / v2 で同じ検査を共有する）
- `example_id` の重複の検査は今のまま残す（`example_id` の一意性と anchor の一意性は別の性質）
- **`example_id` の値と anchor の欄の対応は検査しない**（builder は `"{run}:{ts}:{tick_id}"` を付けるが、0031 はこの形を契約にしていない）。
  代替案は §4 の B、確認は §5 #1

### 2.2 同じ観測は、どの frame で使っても同じ値・quality・missing を持つ（v1 / v2 共通）

`ThermalDataset` / `ThermalDatasetV2` の検証で、**`(source_run_id, metric, source_ts_ms)` をキーとし、dataset 全体
（全 example の window と target）を通して、そのキーを持つ観測時刻ありの cell の `(value, quality, missing_mask)` が
一致することを求める。** 一致しなければ読み込みで拒否する。

- 範囲は**1つの example の中に限らない**。重なる example の window 同士、ある example の target と後の example の window、
  horizon の違う target 同士（v1 の最近傍や v2 の期待時刻以前の採り方で、同じ観測が複数の horizon に採られうる）を含む。
  Codex の指摘のとおり、重なる example の間でも1つの観測から矛盾する軌跡を作れるため
- **target を含める。** 1つの観測を、ある example では教師値、別の example では入力として使う。教師値の側だけ書き換えられると、
  入力と矛盾するラベルを学習に入れてしまう（含めない案は §4 の D、確認は §5 #2）
- feature と target の両方に同じ metric があれば、同じキーとして照合する（どちらも同じ `readings` の系列から採る）
- **照合に `stale_mask` を含めない。** `stale_mask` は frame の時刻と観測時刻の差で決まり（0031 §2.2）、同じ観測でも frame ごとに
  正当に変わる。frame ごとの `stale_mask` は今の検査（spec の `stale_after_ms` との照合）がそのまま見る
- 観測時刻の無い cell（`source_ts_ms = null`）は照合しない（観測を指さない）
- 値の比較は Python の `==`（`FiniteValue` なので NaN は無い）。`-0.0` と `0.0` は同じ値として扱う（§5 #5）
- `missing_mask` は観測時刻のある cell では `value is None` と同値（0031 §2.1）なので値の比較に含まれるが、
  拒否の理由を読めるように欄として明示して照合する（`missing_mask` だけが違う組は cell の既存の検査が先に拒むので、
  この照合で独立には起きない）
- run をキーに含めるので、別の run の同じ `(metric, source_ts_ms)` は照合しない（run は別の DB で、時刻が重なっても別の観測）

### 2.3 版を上げない・builder を変えない

- **`schema_version` を上げない（v1 は 1、v2 は 2 のまま）。** 0031 §2.1 の「schema の意味を変える場合は版を上げる」には当たらない。
  example は1つの ControlTick を基点にし（0031 §2.1）、`(metric, source_ts_ms)` は1つの保存済み観測を指す、という
  既存の意味から導かれる性質を、読み込みで確かめるだけである。0094 §2.6 も同じ理由で、版を上げずに v2 の読み込み時の照合を足した
- **builder と artifact の形を変えない**（0087 §2.8 の「v1 の builder と artifact は変えない」を保つ）。変えるのは loader の検証だけ
- 検証の置き場所は `control/model/dataset.py` の `ThermalDataset` / `ThermalDatasetV2` の model validator とし、
  v1 / v2 に共通の関数を1つ置いて両方から呼ぶ（今の `_check_example_against_run_and_spec` と同じ構成）。
  そのため、dataset を読み直す入口はすべて同じ検査を通る。v1: `write_dataset` の公開前の再検証、`coldaisle-drift` の
  `load_inputs`（v1 だけを読む）、Confidence Profile v1 の再検証。v2: `write_dataset` の再検証、反実仮想の trainer
  （`counterfactual_training`）の `ThermalDatasetV2` の再検証
- 計算量は example の cell の総数に線形、追加のメモリは run ごとの異なる観測の数と anchor の数に比例する
  （0094 §2.6 #2 の tick の写像と同じ程度）

### 2.4 builder の出力がこの検査を満たすこと（根拠と確認）

**根拠（構造）**

- anchor: v1 の builder は `control_traces` の各行から example を1つ作り、v2 の builder は seq 順の tick の添字ごとに高々1つ作る。
  `control_traces` の主キーが `(ts_ms, tick_id)` なので、1つの run の DB から同じキーの example は2つできない
- 観測: v1 / v2 の builder は window（`_window_frame`）と target（`_target_frame` / `_target_frame_at_or_before`）の cell を、
  1回の read snapshot で読んだ metric ごとの系列（`store.series`。`readings` の主キー `(metric, ts_ms)`）の同じ
  `SeriesPoint` から `value` / `quality` をそのまま写し、`missing_mask` を `value is None` から決める。同じ `(metric, ts_ms)` は
  1つの `SeriesPoint` しか持たないので、どの frame・どの example で使っても同じ組になる。`stale_mask` だけが frame の時刻で変わる
- `example_id` の形（`"{run}:{ts}:{tick_id}"`）・`readings` と `control_traces` の主キーは、v1 の builder が入った最初の commit
  （cf85996）から変わっていない。過去に builder で作った v1 の artifact もこの性質を満たす

**確認（2026-10-07、main の 2d5db52）**

本記録の2つの検査を「拒否せず、違反を記録するだけ」の形で `ThermalDataset` / `ThermalDatasetV2` の検証に差し込み、
`uv run pytest -m "not hardware"` を全件走らせた（4064 passed。記録用の一時的な plugin で、リポジトリには入れていない）。

| 対象 | 同じ anchor の複製 | 同じ観測の食い違い |
|---|---|---|
| builder が作った dataset（`tests/test_thermal_dataset.py` / `tests/test_thermal_dataset_v2.py`） | 0 | 0 |
| テストが手で組み立てた dataset（全ファイル） | 0 | window に関わるもの 0 |
| 同上のうち `tests/test_thermal_model_v2.py`（57 回の組み立て）/ `tests/test_confidence_profile_v2.py`（105 回） | 0 | **target 同士の食い違いあり**（下記） |

この2ファイルの fixture は、隣り合う anchor の target の値を anchor から計算している（例: `target_values(anchor_tick, horizon_ms)`）。
anchor `a` の horizon 2 s と anchor `a + 1 s` の horizon 1 s は同じ `source_ts_ms` を指すのに値が違い、§2.2 の検査で拒まれる。
**物理的に矛盾した合成データであり、正当な dataset ではない。** 実装の PR で、target の値を `(metric, source_ts_ms)` の関数にするよう
fixture を直す（学習の係数・残差を確かめる試験の期待値も合わせて見直す）。§2.2 から target を外せばこの書き換えは要らないが、
§4 の D の理由で推奨しない（§5 #2）。

### 2.5 既存の公開済み dataset / artifact への影響と移行

- **リポジトリに dataset artifact は無い**（`git ls-files` に `manifest.json` / `examples.jsonl` が無い）。実機の dataset もまだ無い（0079 §1）
- 所有者の手元にある、builder（`coldaisle-dataset`・`ThermalDatasetV2Builder`）で作った artifact は、§2.4 の根拠により通る。移行の作業は要らない
- 新しい検査で拒まれる artifact は、builder を通らずに作った・書き換えた・壊れたものである。**直さずに作り直す。**
  SQLite と ControlTick が正本で artifact は再生成物（0031 §2.1）であり、封印された dataset 用 DB から同じ bytes を作り直せる。
  拒まれた artifact の中身を検査が通るように編集して使うことはしない（編集した artifact は provenance を失う）
- 学習済みの Thermal Model artifact（v1 / v2）は dataset を読み直さないので、本記録で読み込みが変わることはない。
  拒まれる dataset から学習した artifact が手元にあれば、作り直した dataset から学習し直し、Production にあるなら SHADOW から
  上げ直す（0089 の束縛）。現時点でそのような artifact は無い想定（§5 #4）
- 拒否は読み込みの時点で起きる（`ValueError` から pydantic の検証エラー）。黙って example を落として読み進めることはしない
  （部分的に読むと `examples_sha256` と件数の照合の意味が無くなる）

### 2.6 試験（すべて `-m "not hardware"`）

v1 / v2 のそれぞれで、次を確かめる。

1. **複製の拒否**: builder の出力から1つの example を複製して別の `example_id` を付け、`example_count` と `examples_sha256` を
   計算し直した dataset を、読み込みで拒否する。中身の一部を変えた複製（anchor のキーは同じ）も拒否する
2. **正当な近い anchor は通る（v1）**: 同じ `action_ts_ms` で `control_tick_id` の違う2つの example、同じ `control_tick_id` で
   `action_ts_ms` の違う2つの example（再起動の前後）は通る
3. **window の中の食い違いの拒否**: 1つの example の2つの frame が同じ `(metric, source_ts_ms)` を使い、`value` / `quality` /
   `quality` は同じで `value` だけが違う dataset、`value` は同じで `quality` だけが違う dataset、片方が値のある suspect で
   もう片方が `value = null` の suspect（`missing_mask` も違う）の dataset を、それぞれ新しい検査で拒否する。
   `missing_mask` だけが違う組は、cell の既存の検査（`missing_mask` と `value is None` の同値。0031 §2.1）が先に拒むので
   作れない。新しい検査の試験に数えない（PR #246 の Codex の指摘）
4. **`stale_mask` の違いは通る**: 同じ観測を使う frame の `stale_mask` だけが、spec の鮮度どおりに違う dataset は通る
5. **example の間の食い違いの拒否**: 重なる2つの example の window 同士、ある example の target と別の example の window、
   1つの example の horizon の違う2つの target、別の example の target 同士で、同じ観測の値が違う dataset を拒否する
6. **別の run は照合しない**: 2つの source run の同じ `(metric, source_ts_ms)` に違う値がある dataset は通る
7. **builder の出力は通る**: 既存の builder の試験（`tests/test_thermal_dataset.py` / `tests/test_thermal_dataset_v2.py`）がすべて通る。
   加えて、window が重なり（`sample_period_ms` < anchor の間隔 < `window_ms`）、同じ観測が複数の frame と target に採られる
   合成の run から作った dataset が通ることを確かめる
8. **入口が同じ検査を通る**: v1 は `coldaisle-drift` の `load_inputs` と `write_dataset` の再検証で、v2 は `write_dataset` の再検証と
   反実仮想の trainer（`counterfactual_training`）の再検証で、複製・食い違いの dataset が拒まれる（`load_inputs` は v1 だけを読むので
   v2 の試験には使わない。PR #246 の Codex の指摘）
9. **fixture の直し**: §2.4 の2ファイルの fixture を `(metric, source_ts_ms)` の関数に直した後、既存の学習・Profile の試験が通る

## 3. Consequences

### 良くなること

- 同じ物理的な anchor を複数回数えた dataset、1つの観測から矛盾する軌跡・ラベルを作った dataset を、学習・評価・drift の
  入口で一律に拒める
- v1 / v2 で同じ規則・同じ関数を使い、0094 §2.7 で懸念した「v2 だけ直して v1 と検査が食い違う」状態を作らない
- 版を上げず builder も変えないので、builder で作った既存の artifact はそのまま読める

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| v1 / v2 の loader の契約が厳しくなり、手で組み立てた dataset が拒まれうる | 拒まれるのは物理的に矛盾した dataset だけ（§2.4）。テストの fixture 2ファイルは実装の PR で直す |
| 読み込みの時間とメモリが増える | cell の総数に線形・異なる観測の数に比例（§2.3）。0094 §2.6 #2 と同じ程度 |
| 版を上げないので、この検査の前に作った「通らない artifact」を版で見分けられない | builder の出力は通る（§2.4）。通らないものは作り直す（§2.5） |
| 存在しない anchor（DB に無い時刻の tick）をでっち上げた example は、loader だけでは見抜けない | 本記録の範囲外。loader は DB を読まない。provenance は manifest の `control_trace_sha256` / `telemetry_sha256` と DB の封印（0031 §2.3）で追う |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| A. anchor のキーを `(source_run_id, action_ts_ms)` にする | v1 の `control_traces` の主キーは `(ts_ms, tick_id)` で、同じ時刻の2つの tick を builder が正当に作りうる。v2 では §2.1 のキーと同じ意味になるので、分ける利点が無い |
| B. `example_id` を `"{source_run_id}:{action_ts_ms}:{control_tick_id}"` と一致させることを求める（それで一意性を兼ねる） | 0031 は `example_id` の形を契約にしていない。キーの一意性で目的は果たせる。手で組み立てるテストの v2 の fixture（`"{RUN_ID}:{anchor_ms}"` など）が多く外れる（§5 #1 で採否を確認） |
| C. 食い違いの照合を1つの example の中だけにする | 重なる example の間で、1つの観測に違う値を持たせられる（Codex の指摘のとおり）。dataset 全体の照合の費用は小さい |
| D. 食い違いの照合から target を外す | 教師値の側だけを書き換えれば、別の example の入力と矛盾するラベルを学習に入れられる。外す利点はテストの fixture を直さずに済むことだけ |
| E. `stale_mask` も照合に含める | 同じ観測でも frame の時刻で正当に変わる（0031 §2.2）。builder の出力を拒んでしまう |
| F. `schema_version` を上げる（v1 → 3 等） | 意味は変えておらず、既存の意味から導かれる性質の検査を足すだけ。0094 §2.6 も版を上げずに照合を足した。版を上げると、builder で作った既存の v1 artifact まで読めなくなる |
| G. 拒まれた example を落として残りを読む | `example_count` / `examples_sha256` の照合の意味が無くなり、どの example を落としたかが学習の結果に黙って効く |
| H. v2 だけを直す | v1 / v2 で検査が食い違う（0094 §2.7・§4 の D と同じ理由） |

## 5. 未決事項（所有者に確認したい点。推奨案を先頭に）

| # | 内容 | 推奨案 | 代替案 |
|---|---|---|---|
| 1 | anchor の一意性を何で確かめるか | **`(source_run_id, action_ts_ms, control_tick_id)` の一意性だけ**（§2.1） | `example_id` をそこから導いた値と一致させることも求める（§4 の B。テストの fixture の `example_id` を直す必要がある） |
| 2 | 同じ観測の照合の範囲 | **dataset 全体（run ごと）・window と target の両方**（§2.2）。テストの fixture 2ファイルを直す | window だけ（target を外す。§4 の D）／1つの example の中だけ（§4 の C） |
| 3 | 版を上げるか | **上げない**（§2.3。0094 §2.6 と同じ扱い） | v1 / v2 を上げる（§4 の F） |
| 4 | 新しい検査で拒まれた既存の artifact の扱い | **直さず、正本の DB から作り直す**（§2.5）。拒まれた dataset から学習した model artifact があれば学習し直し、SHADOW から上げ直す | 拒まれた artifact を一度だけ救う移行の道具を作る（provenance を失うので推奨しない） |
| 5 | `-0.0` と `0.0` を同じ値とみなすか | **同じとみなす**（Python の `==`。builder は同じ行から写すので区別が要らない） | JSON の表現（bytes）で比べて区別する |
| 6 | 実装を1つの PR にするか | **v1 / v2 の検査・fixture の直し・§2.6 の試験を1つの PR（#224）にする** | v1 と v2 を分ける（間で v1 / v2 の検査が食い違う期間ができる） |
