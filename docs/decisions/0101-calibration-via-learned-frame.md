# 決定記録 0101: `coldaisle-fand` が起動時に読んだ較正を LearnedFrame で MPC worker へ運び、worker が L9 に渡す（あわせて PR #239 で決着した2点）

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が推奨案で承認。§6）
- **Date**: 2026-10-07
- **Supersedes**: [0077](0077-learned-proposal-handoff.md) §2.3 の最後の項のうち「frame に含めてよいのは Telemetry の値と
  制御の状態だけ」の部分のみ（起動時に読んだ較正の値と、読めなかったことを表す理由の code も含めてよい。path・個体識別子を
  入れない点と LLM のプロンプトへ渡さない点は変えない。§2.2）／
  [0096](0096-calibration-binding-digest.md) §2.7 の3つ目の項と §5 #5 の推奨の列のうち、0090 に較正ファイルを足さない
  **理由**として書いた「L9 → artifact を読み込めない → 0089 の『artifact を持たない構成は Baseline より上を有効にしない』で
  既に authority が Baseline に落ちる」の部分のみ（L9 は worker で働くので、fand の authority は下がらない。Learned の提案が
  届かず Fallback になる。§2.4）。結論（0090 の束縛に較正ファイルを足さない）は変えない。
  0077 / 0096 の他の節は有効。旧記録側への `Superseded by` の追記は本 PR で行った。
  ほかは 0077 §2.3 / §2.10 段階 3 と 0079 §2.4 / §2.5 が決めていなかった点への追加
- **関連**: [0077](0077-learned-proposal-handoff.md) §2.3 / §2.5 / §2.6 / §2.10 /
  [0079](0079-model-artifact-formats.md) §2.4（L9）/ §2.5 / §2.6 /
  [0096](0096-calibration-binding-digest.md) §2.4 / §2.6 / §2.7 / §5 #5・#8・#9 /
  [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.3 /
  [0089](0089-authority-bound-to-loaded-artifact.md) / [0090](0090-authority-bound-to-loaded-config.md) /
  [0092](0092-learned-frame-applied-from-hardware-result.md) / [0095](0095-learned-channel-stage1-settled-points.md) /
  [0098](0098-registry-binding-settled-points.md) / [0099](0099-calibration-change-log.md) /
  `docs/calibration.md` §4 / `src/coldaisle/control/learned_handoff.py` /
  `src/coldaisle/control/model/calibration_digest.py` / `src/coldaisle/control_daemon.py`（`--calibration`）/
  `src/coldaisle/control/mpc/optimizer.py`
- **対象 Issue**: #86（実装 PR #239。0079 段 4）

## 1. Context

PR #239（#86、0079 §2.9 の段 4）は、Learned MPC の束縛を反実仮想 Thermal Model artifact v2 の封をした型へ
切り替え、`coldaisle-fand --calibration <path>` で較正ファイルを**起動時に1回だけ**読んで `RuntimeCalibration`
（読めたら `available(offsets_c)`、読めなければ `unavailable(reason)`。0096 §2.6）を作るところまでを入れた。
loader（`MpcModelBinding.from_verified_artifact` / `LearnedMpcRuntime.load`）は `RuntimeCalibration` を既定値なしの
必須の引数として受け取り、L9（0079 §2.4）で照合する。

ところが、**fand が読んだ `RuntimeCalibration` を L9 へ届ける経路**について、記録どうしが食い違って読める。

| 記録 | 書いてあること | 読める帰結 |
|---|---|---|
| 0079 §2.4（FINAL） | L9 の「runtime の較正」は `coldaisle-fand` が起動時に1回だけ読む較正ファイル | 較正を読むのは fand |
| 0096 §2.4 / §2.7 / §5 #8（FINAL） | fand が起動時に読み、loader が L9 で照合する。L9 に外れれば 0089 で authority が Baseline に落ちる | fand が L9（artifact の読み込み）まで行うようにも読める |
| 0077 §2.6（FINAL） | `coldaisle-fand` は artifact の bytes を読まず、deserialize もしない。bytes の検証は worker の仕事 | L9 を行うのは MPC worker |
| 0077 §2.3（FINAL） | worker の入力は fand が毎 tick 送る frame だけ。frame に含めてよいのは Telemetry の値と制御の状態だけ | worker は較正を得る経路を持たない |

PR #239 は fand で読むところと loader の入口までを入れ、fand → worker の受け渡しは入れなかった（MPC worker は
0077 段階 3 で、まだ無い）。PR #239 の「人間レビューが必要な点」1 で推奨案と代替案を示し、2026-10-07 に所有者が
推奨案で承認した。あわせて同 PR の判断点 2・3（記録の読み方）も推奨案で承認された。決定記録は追記のみなので、
0077 / 0079 / 0096 の本文は書き換えず、ここに残す。

## 2. Decision

### 2.1 較正は fand が読み、frame で worker へ運び、worker が loader（L9）に渡す

役割の分担を次のとおりにする。どの記録の原則も崩さない。

| 仕事 | 担当 | 根拠 |
|---|---|---|
| 較正ファイルを読む（起動時に1回だけ。path は `--calibration` で受け取り、既定の path を置かない） | `coldaisle-fand` | 0079 §2.4 / 0096 §5 #8 |
| 読んだ `RuntimeCalibration`（`available` の offsets か `unavailable`）を worker へ運ぶ | `coldaisle-fand` → `LearnedFrame`（§2.2） | 本記録 |
| artifact の bytes を読み、L1〜L12 を検査して封をした型を作る（L9 にはこの frame の較正を渡す） | MPC worker | 0077 §2.6 / 0079 §2.4 |
| 較正ファイルを読む | **worker はしない** | 0079 §2.4（runtime の較正は fand が読んだもの） |
| artifact の bytes を読む・deserialize する | **fand はしない** | 0077 §2.6 |

- worker は較正ファイルの path を受け取らない（worker の CLI・設定に較正の path の欄を足さない）。frame 以外から
  較正の値を得る経路を持たない
- fand は `ControlDaemon.runtime_calibration`（PR #239）を frame へ写すだけで、artifact の `metric_binding` も
  digest も計算しない。digest の計算は 0096 §2.4 のとおり loader が L9 で行う

### 2.2 `LearnedFrame` に `calibration` の欄を足し、frame の版を v3 にする

- `LearnedFrame` に必須の欄 `calibration` を足す（既定値を持たない）。形は `RuntimeCalibration` の2状態をそのまま
  写す判別共用体とする
  - 読めた: `status = "available"` と `offsets_c`（チャネル名 → ℃。`RuntimeCalibration.available` と同じく有限値だけ）
  - 読めなかった: `status = "unavailable"` と理由。**空の `offsets_c` で「読めなかった」を表さない**（0096 §2.6 と同じ理由。
    空の値は全チャネル 0.0 と同じ digest になる）
- **毎 tick 同じ値**を載せる（fand は較正を起動時にしか読まない。`expected_artifacts` と同じ扱い。0077 §2.6）。
  別のメッセージ（`hello` など）に載せない。worker が窓に使う frame と、束縛に使う較正の出どころを1つにする
- `LEARNED_FRAME_SCHEMA_VERSION` を 2 から **3** へ上げる（v2 は 0077 段階 2 で `expected_artifacts` を足した版）。
  封筒（`OutboundEnvelope`）の版は変えない
- frame に path・個体識別子を入れない（0077 §2.3、AGENTS.md ルール10）。**`unavailable` の理由に、PR #239 の
  `read_runtime_calibration` が作る例外の文字列（`f"{type(error).__name__}: {error}"`）をそのまま載せない。**
  `FileNotFoundError` などの文字列は較正ファイルの path を含むためである。理由の表し方は §5 #1
- 較正の値はチャネル名と offset だけで、Telemetry ではないが制御の状態でもない。0077 §2.3 の「frame に含めて
  よいのは Telemetry の値と制御の状態だけ」に、**起動時に読んだ較正の値と、読めなかったことを表す理由**を加える
  （ヘッダの Supersedes）。frame を LLM のプロンプトへ渡さない点（ルール8）は変えない

### 2.3 worker の扱い

- **束縛を作るたびに、使う frame の `calibration` を `RuntimeCalibration` に戻して loader に渡す。** worker が束縛
  （`MpcModelBinding` / `LearnedMpcRuntime`）を作る時点は次のとおりで、どれも L1〜L12 をやり直す
  - その `run_id` の最初の検証を通った frame を受け取ったとき
  - **`run_id` が変わったとき（fand の再起動）。** `expected_artifacts` が前と同じでも、手元の束縛を捨てて新しい
    frame の較正と `expected_artifacts` から作り直す。fand の再起動で較正が変わりうるためで、これが無いと
    `docs/calibration.md` の再起動の順番（§2.5）が効かない。0077 §2.6 の「止まった worker は別の `run_id` の frame で
    再開する」は止まっていない worker にも同じく掛かる
  - frame の `authority_stage` が上がったとき（0077 §2.3 の作り直し）
- frame の `unavailable` は `RuntimeCalibration.unavailable` として loader に渡す。L9 は 0096 §2.6 のとおり、較正の
  掛かる metric を使う artifact を digest を比べずに拒否し、使わない `null` の artifact だけを通す
- **frame に較正が無い・壊れている・版が古いとき**は、その frame を使わない。`LearnedFrame` は
  `extra="forbid"`・`strict=True` で `schema_version` を `Literal` で持つので、v2 の frame（`calibration` の欄が無い）や
  `calibration` の検証に通らない frame は frame 全体の検証で落ちる。worker はそれを壊れた入力として捨て、
  **窓にも束縛にも使わない**。どの欄も信用できない frame から較正だけを拾わない
  - worker は、欠けた較正を `available({})`・全チャネル 0.0・較正ファイルの読み込みで**埋めない**（0096 §2.6）
  - 検証を通った v3 の frame がまだ無い `run_id` では束縛を作らず、提案を出さない。Gate はこれまでどおり
    `learned_proposal_unavailable` で Fallback にする（0077 §2.5）
  - 帰結として、版が食い違う fand と worker の組では、較正の掛かる artifact に限らず Learned は一切使われない。
    `unavailable`（`null` の artifact は通す）より保守側であり、安全側＝Fallback の向きに一致する
- 同じ `run_id` の中で frame の `calibration` が変わったとき（fand は起動時にしか読まないので正常では起きない）の
  扱いは §5 #2

### 2.4 L9 に外れたときの帰結と authority

- L9 に外れると loader は `MpcModelUnusableError` を投げ、worker の `LearnedMpcRuntime` は起動を止めずに以後の
  `propose()` を毎回 `MODEL_LOAD_FAILURE`（`failure_reason` に L9）にする（PR #239、0079 §2.6）。Gate は Fallback にする
- **fand の authority は L9 では下がらない。** fand は artifact を読まない（0077 §2.6）ので、L9 の結果を知らない。
  0089 が照らす「loaded artifact」は Gate の `expected_artifact_sha256` と同じ出どころ（起動時の registry の
  production）のままで、journal の証拠と一致すれば authority の上限は Baseline に落ちない。Learned の提案が
  `MODEL_LOAD_FAILURE` になるので、制御の結果は Fallback と同じである
- 0096 §2.7 の3つ目の項と §5 #5 が 0090 の束縛に較正ファイルを足さない**理由**として書いた「L9 → artifact を
  読み込めない → 0089 で authority が Baseline に落ちる」は、この経路では成り立たない（ヘッダの Supersedes）。
  **結論（足さない）は変えない。** 較正が食い違う runtime で Learned の提案が Demand の選択に効かないことは、
  worker の L9 と Gate の Fallback で保たれる。trace の実効 stage が journal のまま見えることの扱いは §5 #3

### 2.5 再起動の順番（`docs/calibration.md` §4）が保たれる

`docs/calibration.md` は「**取り込みより先に** `coldaisle-fand` を再起動する」と書く（0096 §5 #9）。本記録の経路でも
この手順のまま効く。

1. fand を再起動する → 新しい較正ファイルを読み、新しい `run_id` の frame に新しい `calibration` を載せる
2. worker は `run_id` の変化で束縛を捨て、新しい較正で L9 をやり直す（§2.3）。変わった offset のチャネルを使う
   artifact は拒否され Fallback になる
3. そのあと取り込みを再起動する → 新しい較正の値が store に入る

worker の再起動は手順に要らない。fand の再起動の後、worker が新しい `run_id` の frame を受け取るまでの間は、
fand は再起動の `STARTUP` の Max を通り（0077 §2.6）、別の `run_id` の結果は受付スレッドが捨てる（0077 §2.4 の1）ので、
古い較正で照合した束縛の提案は使われない。`docs/calibration.md` の「照合を行う MPC worker はまだありません」の
注記は、本記録の実装 PR（§2.7）で外す。

### 2.6 PR #239 の実装で決着した点

#### (a) Fallback の requested の support の照合は、制約へ収めた後の値で行う（0079 §2.5 の読み方）

0079 §2.5 の「Fallback の requested（incumbent の出発点。0052 §2.3）自体が範囲外なら、optimizer は解を返さない」は、
**0052 §2.3 の出発点＝Fallback の requested を制約（Safety floor・変化幅・探索範囲）へ収めた値**を Profile v2 の
support（zone ごとの範囲・step ごとの cell・anchor からの遷移・step 間の遷移。0084 §2.2）で照らす、と読む。
生の（制約へ収める前の）Fallback の requested は照らさない。

- optimizer が評価し incumbent にするのは制約へ収めた値なので、外挿かどうかを問うべき plan はそれである。
  生の値は optimizer がどの経路でも評価しない
- 外れたら `OptimizerOutcome` の `status = error`、`reason.code = plan_out_of_learned_range`（detail に step・zone・cell）。
  学習範囲の内へ**丸めて探索を続けない**（0079 §2.5 のまま。制約へ収めることと、学習範囲へ丸めることは別である）
- PR #239 の `MpcOptimizer` はこの読み方で実装されている（`baseline_requested = self._clamped(...)` を
  `plan_support_violation` に渡す）

#### (b) `coldaisle-fand --calibration` を省いた起動は `unavailable`（warning）

- `--calibration` を与えない起動は、較正ファイルを読めなかったときと同じく `RuntimeCalibration.unavailable` にする。
  構造化ログは warning で、理由の code は `calibration_path_not_given`（読めない・壊れているときは error で
  `calibration_unreadable`）。起動は止めない（0096 §5 #8）。既定の path は置かない（0079 §2.4）
- 0079 §2.4 の「既定の path を制御側に置かない」と 0096 §5 #8 の「読めなくても起動は止めない」からの帰結である。
  較正の掛かる metric を使う artifact は L9 で使われず、Fallback で運転を続ける
- **systemd テンプレート（`deploy/`）への `--calibration` の追加は 0077 §2.10 の段階 6 で行う。** それまでテンプレート
  どおりに配備した fand は `unavailable` で動き、較正の掛かる artifact を使わない（安全側）

### 2.7 実装の時期と試験すべき性質

- §2.1〜§2.3 は **MPC worker（0077 §2.10 の段階 3）と同じ PR** で入れる。frame の版を上げる側（fand）と読む側
  （worker）を別の PR に分けると、間の main で版の食い違う組が生まれる。段階 3 の PR は `LearnedFrame` v3・
  fand での `calibration` の写し・worker での `RuntimeCalibration` への復元と loader への受け渡し・`docs/calibration.md`
  の注記の更新を含める
- 試験（hardware なし。0077 §2.10 と同じく試験の偽 fand / 偽 worker で確かめる）
  - fand: `available` の較正を読んだ起動では毎 tick の frame に同じ `offsets_c` が載る。`--calibration` 無し・読めない
    ・壊れているときは `unavailable` が載り、理由に path を含まない（§5 #1 の形）
  - 往復: frame の `calibration` から復元した `RuntimeCalibration` で、fand が読んだ値と同じ digest になり L9 が通る・
    変えた offset で拒否される・`unavailable` で較正の掛かる artifact が拒否され `null` の artifact が通る
  - worker: v2 の frame・`calibration` の欠けた / 型の違う frame・空の `offsets_c` を `available` と偽る形は使われず、
    束縛が作られない（`available({})` や 0.0 で埋めない）
  - worker: `run_id` が変わると、`expected_artifacts` が同じでも新しい較正で束縛を作り直し、変えた offset で L9 に外れる
  - worker は較正ファイルを開かない（較正ファイルを置いた状態で frame に `unavailable` を載せると、較正の掛かる
    artifact は拒否される）
  - 変異で落ちること: worker が frame の較正を無視して固定値を渡す・`run_id` の変化で作り直さない・壊れた frame の
    較正を拾う

## 3. Consequences

### 良くなること

- 0077 §2.6（fand は artifact を読まない）・0079 §2.4（runtime の較正は fand が読む）・0096 §5 #8（読めなくても
  起動は止めない）の3つが同時に成り立つ。どれも置き換えずに済む
- 較正を読む主体が1つ（fand）なので、fand と worker が別の較正を見ることが無い。L9 の「runtime の較正」が
  frame の `run_id` と組で定まる
- `docs/calibration.md` の再起動の順番がそのまま効き、worker の再起動を手順に足さない

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| frame が毎 tick 較正の値を運び、大きさが少し増える | チャネルは数個（いまは6）で、`max_message_bytes`（0077 §2.2）の範囲に収まる。段階 3 で frame の大きさを測る |
| L9 に外れても fand の authority は下がらず、trace の実効 stage が journal のまま見える（§2.4） | 制御の結果は Fallback と同じ。理由は worker の `MODEL_LOAD_FAILURE` の `failure_reason` と Gate の `fallback_reason` が示す。見え方を変えるかは §5 #3 |
| fand と worker の版が食い違うと、`null` の artifact も含めて Learned が使われない（§2.3） | 両者は同じパッケージから配備する。安全側（Fallback）にしか倒れない |
| `--calibration` を付けない配備では較正の掛かる artifact が使われない（§2.6 (b)） | 起動時の warning の構造化ログで見える。テンプレートへの追加は 0077 段階 6 |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| (b) fand 自身が起動時に固定された artifact を L1〜L12 で検査し、外れたら thermal を unbound 扱いにする（0089 で authority も Baseline に落ちる） | 0077 §2.6（FINAL）の「fand は artifact の bytes を読まず deserialize もしない（ML を制御プロセスへ入れない）」を覆す。制御デーモンに artifact の大きさの上限・展開の検査・Profile の組み立てが入る |
| (c) worker が自分で較正ファイルを読む（worker に `--calibration` を渡す） | 0079 §2.4 の「runtime の較正は fand が起動時に読む較正ファイル」を覆す。fand と worker が別々に読むと、較正の書き換えと再起動の間で別の値を見うる。`docs/calibration.md` の「fand を先に再起動」の手順に worker の再起動が要る |
| 較正を `hello` など frame 以外のメッセージで1回だけ送る | worker が窓に使う frame と束縛に使う較正の出どころが2つになる。`expected_artifacts` を毎 tick 載せる 0077 §2.6 の形にそろえる |
| frame に較正の digest だけを載せる | digest は artifact の `metric_binding` に依るので、fand は artifact を読まずに digest を1つに決められない（0096 §2.4「runtime は artifact を読む前に digest を1つに決められない」） |
| 古い版の frame や較正の欠けた frame を受け取ったら、worker が `unavailable` として扱い `null` の artifact だけは使う | どの欄も信用できない frame の他の欄（snapshot・`authority_stage`・`expected_artifacts`）を窓と束縛に使うことになる。frame ごと捨てるほうが保守側（§2.3） |

## 5. 未決事項

### 所有者が決めた点（2026-10-07 オーナーが推奨案で承認）

| # | 判断点 | 決着（2026-10-07 所有者の決定、推奨案） | 代替（判断前の記録） |
|---|---|---|---|
| A | fand が読んだ `RuntimeCalibration` を L9 へ届ける経路（PR #239 の判断点 1） | fand が起動時に読んだ較正を `LearnedFrame`（v3）で worker へ運び、worker がそれを loader に渡す。worker は較正ファイルを読まず、fand は artifact を読まない。欠け・壊れ・古い版の frame は安全側（§2.1〜§2.3） | (b) fand が artifact を検査する／(c) worker が較正ファイルを読む（§4） |
| B | Fallback の requested の support の照合の対象（PR #239 の判断点 2） | 制約へ収めた後の値（incumbent の出発点）で照合する（§2.6 (a)） | 制約へ収める前の生の Fallback の requested で照合する |
| C | `--calibration` を省いた起動（PR #239 の判断点 3） | `unavailable`（warning）として扱い、起動は止めない。systemd テンプレートへの追加は 0077 段階 6（§2.6 (b)） | 省いた起動を拒否する（既定の path を置かない以上、較正を使わない構成も起動できなくなる） |

### 本記録を書く中で見つかった点（実装 PR（0077 段階 3）の前に所有者が決める）

| # | 論点 | 推奨 | 代替 |
|---|---|---|---|
| 1 | frame の `unavailable` の理由の表し方。PR #239 の `RuntimeCalibration.unavailable(reason)` は例外の文字列（path を含みうる）を持つので、そのまま frame に載せると 0077 §2.3 / AGENTS.md ルール10 に反する（§2.2） | **閉じた code にする。** 構造化ログの理由の code と同じ `calibration_path_not_given` / `calibration_unreadable` の2値だけを frame に載せ、詳細（例外の文字列・path）は fand の構造化ログにだけ残す。worker は code から `RuntimeCalibration.unavailable(<code>)` を作る | 自由記述のまま載せ、fand が path を伏せ字にしてから送る（伏せ字の漏れを試験で網羅しにくい） |
| 2 | 同じ `run_id` の中で frame の `calibration` が前の frame と変わったとき（fand は起動時にしか読まないので正常では起きない） | **異常として、0077 §2.6 の `artifact_not_production` と同じ形で止まる。** その周期の結果を作らず、失敗（`model_load_failure`、理由 `calibration_changed_within_run`）を1回返し、別の `run_id` の frame まで再開しない。束縛を黙って作り直さない | 変わった frame を壊れた入力として捨て、束縛を作ったときの較正のまま続ける（fand の不具合を隠す） |
| 3 | L9 に外れても fand の authority の上限が下がらず、trace の実効 stage が journal のまま見えること（§2.4。0096 §5 #5 の理由が成り立たなくなった点） | **いまは変えない。** 0096 §5 #5 の結論（0090 の束縛に較正ファイルを足さない）を保ち、制御の結果が Fallback であることは worker の `failure_reason` と Gate の `fallback_reason` で示す。見え方が運用で問題になれば、0089 が予定する `AuthorityRecord` v2 の版上げと一緒に新しい記録で扱う | worker が L9 の失敗を fand へ知らせ、fand が 0089 と同じく authority の上限を Baseline に落とす（worker → fand の経路に結果以外の型が増え、0077 §2.4 の6「worker の経路は提案の型しか運ばない」に触れる）。または 0090 の束縛に較正ファイルの hash を足す（0096 §5 #5 の代替） |

## 6. 承認記録

**2026-10-07、リポジトリ所有者が §5 の A・B・C を推奨案で承認し、本記録を FINAL にした。**

| §5 の判断点 | 決定 | 本記録 |
|---|---|---|
| A | fand が読んだ較正を `LearnedFrame` v3 で worker へ運び、worker が loader（L9）に渡す。実装は MPC worker（0077 段階 3）と同じ PR | §2.1〜§2.5 / §2.7 |
| B | Fallback の requested は制約へ収めた後の値で support と照合する | §2.6 (a) |
| C | `--calibration` を省いた起動は `unavailable`（warning）。テンプレートへの追加は 0077 段階 6 | §2.6 (b) |

§5 の 1〜3 は本記録を書く中で見つかった点で、開いたままである。0077 段階 3 の実装 PR の前に所有者が決める。
