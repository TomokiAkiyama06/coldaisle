# 決定記録 0152: Workload Hint の単調性ゲートの証明書 — 置き場所・書き手・fand と worker の照合

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-16
- **Supersedes**: なし（[0066](0066-workload-hint-stage-b-conditions.md) §2.1 (b) / (c) と
  [0133](0133-workload-hint-stage-b-prior-rollout.md) §2.5 / §2.5a / §2.6 / §2.9 を置き換えず、具体の形を補う。
  0066 §5 の未決 #3（ハッシュの置き場所）と #5（Thermal Model の昇格との結び付け）、0133 §5 の未決 #3 に答える）
- **関連**: [0061](0061-rl-supervisor-policy-artifact-and-binding.md) §2.1 /
  [0062](0062-model-registry-operations.md) §2.1〜§2.4 /
  [0064](0064-workload-hint-entry-and-supervisor-prior.md) §2.2 / §2.10 /
  [0066](0066-workload-hint-stage-b-conditions.md) §2.1 (b)〜(d) /
  [0071](0071-control-trace-read-api.md) §2.5 /
  [0079](0079-model-artifact-formats.md) §2.4（L9） / [0096](0096-calibration-binding-digest.md) /
  [0101](0101-calibration-via-learned-frame.md) /
  [0077](0077-learned-proposal-handoff.md) §2.6 / [0090](0090-authority-bound-to-loaded-config.md) /
  [0098](0098-registry-binding-settled-points.md) §2.1 / §2.4 / §2.6 /
  [0104](0104-authority-raise-least-privilege.md) §2.1 / §2.3 / [0105](0105-registry-shared-root-settled-points.md) /
  [0133](0133-workload-hint-stage-b-prior-rollout.md) / [0149](0149-hint-skip-reason-rule-policy-not-active.md) /
  AGENTS.md「絶対に守るルール」1・2・3・4・7・9
- **対象 Issue**: #288（B1 `shadow_mpc` の前提）

---

## 1. Context

0133 は Workload Hint の Stage B を B0 `observe` → B1 `shadow_mpc` → B2 `active` の段階で解放すると決め、
**B1 / B2 を設定していても、認証のハッシュが無い・合わないときは実効の段階を B0 に留める**（0133 §2.6）とした。
B0 は PR #332（#319）でマージ済みで、いまの実装は「照合する仕組みがまだ無い」ことを理由に、B1 / B2 の設定を
常に B0 に留めて `skipped: certification_mismatch` を残している（`control/supervisor/workload_hint.py` の
`IMPLEMENTED_STAGE` / `effective_stage()`、`control_daemon.py` の `_open_workload_hint()`）。

ハッシュの中身（何を束ねるか）は 0066 §2.1 (c) が決めている。決まっていないのは次の4つで、B1 の実装 Issue を
切る前に要る（0133 §5 の未決 3 は「B2 の前に」としていたが、B1 にも同じハッシュが要る（0133 §2.6 の表）ので
ここで閉じる）。

1. **何が・いつ** 単調性ゲート（0066 §2.1 (b) の網羅試験）を実行するか
2. 結果（証明書とハッシュ）を**どこに置き、誰が書くか**。所有者は 2026-10-14 に「Model Registry の lifecycle
   （0062）に、Thermal Model の production と組で置く」方向を承認している（0133 §5 の 3）
3. **fand がどう読み、どう照合するか**。合わないときに B0 に留まること
4. **MPC worker 側の照合**。0066 §2.1 (c) が束ねる「探索のコストに入る他のモデルと条件」
   （`LearnedMpcController.conditions()`）と Optimizer の設定は worker のプロセスの中にあり、fand は持っていない。
   worker は自分の `--config-dir` を読む（0077）ので、fand の設定と同じとは限らない。fand だけが照合しても、
   worker が認証と違う設定で解く穴が残る

---

## 2. Decision

### 2.1 単調性ゲートは `src/coldaisle/` の認証ランナーが、本番のホストで、人の手で走らせる

- **実行するもの**: 0066 §2.1 (c) のとおり、`src/coldaisle/` 配下の production module（認証ランナー）。
  格子の生成・二重解法・全 offset の比較・合否・証明書の生成をすべてここで行い、`tests/` やリリーススクリプトは
  それを呼ぶだけにする。入口は読み取り専用の CLI（仮の名前 `coldaisle-hint-certify`）とする
- **入力**: Control Config の4ファイル（`--config-dir`）・`config/safety.yaml` の `workload_hint.max_eps` /
  `max_eps_c`・Model Registry（`--registry-root`。thermal_model ほか MPC が束縛する production を**読むだけ**）・
  ロックファイル（§2.4）・**実行時に効く較正**（下）。Thermal Model artifact は Registry の production を使い、パスで
  別のファイルを渡す入口を作らない（「Stage B で使う Thermal Model artifact」以外で証明書を作れないようにする）
- **較正も入力にする。** thermal_model の artifact の特徴量が較正に縛られている（`calibration_binding` を持つ。
  0096）場合、実行時の読み込みは L9（0079 §2.4。較正の digest が runtime の較正と一致しなければ拒否）を行い、
  worker はその較正を fand の frame から受け取る（0101）。ランナーには frame も fand の入力も届かないので、
  そのままでは較正つきの artifact をすべて不合格にするか、L9 を迂回して実行時と別の経路を認証することになる。そこで:
  - ランナーは、fand が起動時に読むのと**同じ較正ファイル**を `--calibration` で受け取り、fand と**同じ読み取りの関数**
    （いまは `control_daemon.py` の `read_runtime_calibration`。実装で横断の場所へ移し、両方がそれを呼ぶ）で
    `RuntimeCalibration`（`available` / `unavailable`）にする。既定の path を置かない（0101 §2.1 / 0096 §5 #8 と同じ）
  - その `RuntimeCalibration` を、worker と同じ loader の L1〜L12（**L9 を含む**）に渡して束縛を作る。L9 を飛ばす・
    較正を 0 で埋める・別の較正を当てる経路をランナーに作らない（0096 §2.6 / 0101 §2.3）。L9 に外れれば束縛を
    作れないので不合格（証明書を出さない）
  - 使った較正の digest を証明書の部品 `calibration`（§2.3）に入れ、fand・worker の照合の対象にする（§2.4 / §2.6）
- **出力**: 合格したときだけ、証明書の JSON（§2.3）を `--out` のパスへ書く。不合格なら証明書を書かず、
  不合格の点の数と最初の点の軸（値は載せない）を構造化ログに出して終了コードを 0 以外にする。
  **Registry にも設定にも書かない**（`coldaisle-calibrate` の既定と同じく、書くのは人の次の操作）
- **いつ**: 次のどれかが変わったとき、人が走らせる。どれが変わっても証明書のハッシュは合わなくなる（§2.4）ので、
  走らせ忘れは B0 に倒れるだけで、危険側にはならない
  - Thermal Model の昇格・rollback、`src/coldaisle/` のコード・依存（ロックファイル・入った wheel）・Python の処理系
  - `context_overrides`・基準の context・格子・`eps` / `eps_c`・Optimizer と目的関数の設定・`conditions()` に入るもの
- **どこで**: 0066 §2.1 (c) のとおり実行環境の指紋が合わないと使えないので、**本番のホスト**（`/opt/coldaisle/.venv`）で
  走らせる。CI は小さな格子と Mock の Thermal Model artifact でランナー自体を試すだけで、本番の証明書は作らない
- **優先度**: ランナーは起動の直後に自分のスケジューリングを `SCHED_IDLE` に下げ、`sched_getscheduler` で確かめてから
  格子を回す。下げられなければ何もせずに終了する（0133 §2.5 の Shadow solver と同じ扱い。本番のホストで fand・
  Learned worker と CPU を取り合わないため。§5 の 5）
- **LLM は実行できない・書けない。** ランナーと Registry の CLI は AI 層のツール・読み取り API・eventd・管理ソケットの
  どれからも呼べない（import しない）。証明書の内容も API に出さない（0133 §2.5a の「公開しない」と同じ）

### 2.2 置き場所は Model Registry。新しい kind `hint_certificate` として、thermal_model と組で照合する

- 証明書は Model Registry の **新しい `ArtifactKind`: `hint_certificate`** の artifact とする（形式は `json`）。
  thermal_model と同じ lifecycle（`register` → `validate` → `promote`、`rollback` / `retire`）を通り、
  **`promote` / `rollback` は人が書いた承認ファイル（`HumanApproval`。0062 §2.2）を要する**
- **「組」は内容で結ぶ。** 証明書は自分が認証した thermal_model の `ArtifactRef` と `artifact_sha256` を持つ（§2.3）。
  fand は**同じ1つの snapshot**（0077 §2.6）から thermal_model と hint_certificate の production を読み、
  証明書が名指す thermal_model が production の thermal_model と一致するときだけ使う。
  Thermal Model を昇格・rollback すれば、証明書を作り直して昇格するまで自動的に B0 に倒れる（0066 §5 #5 への答え）
- 独立の production slot なので、証明書の rollback で1つ前の証明書へ戻せる（それが production の thermal_model と
  合わなければやはり B0）
- 閉じた列挙に値を足すので、0061 §2.1 と同じく **Registry の snapshot の schema version を上げる**
  （古い読み手が `INVALID_REGISTRY` で全体を読めなくなるのを、版で明示する）。`ArtifactCapability` にも
  `hint_monotonicity_certificate` を足す。この能力の artifact は MPC の内部モデル・RL の policy として束縛されない
  （0061 §2.1 の `supervisor_strategy` と同じ、型で取り違えを止める形）
- **frame の `expected_artifacts` には入れない**（0098 §2.4 のまま thermal_model と supervisor_policy だけ）。
  worker は証明書の artifact を読まない。照合に要る値だけを frame で受け取る（§2.6）
- 書き手と権限は 0104 §2.1 / §2.3 の Registry の表のまま。`register` / `validate` / `promote` は
  `coldaisle-registry` の人、fand は `--registry-root` を渡したときだけ読む（書かない）。新しい uid・グループ・
  ディレクトリを作らない
- Registry の CLI は artifact を deserialize も実行もしない（0062 §2.3 のまま）。証明書の中身の検査は fand と
  worker が行う（§2.4 / §2.6）。承認者は、昇格の前に 0133 §2.6 の B1 の他の前提（RulePolicy の本番値・
  本番のホストでの実時間の試験の合格）が揃っていることを確かめ、承認ファイルの `reason` に書く（§5 の 3）

### 2.3 証明書の形（契約。型と版の細部は実装 Issue）

| 欄 | 内容 |
|---|---|
| `schema_version` | 証明書の形の版 |
| `result` | `passed` だけ（不合格では証明書を作らないので、それ以外の値を持たない。読むときも `passed` 以外を拒む） |
| `thermal_model` | 認証した thermal_model の `ArtifactRef` と `artifact_sha256` |
| `components` | 0066 §2.1 (c) が束ねるものの、**部品ごとの SHA-256**（下の表）。どれが変わったかを照合で名指すため |
| `certified_domain` | 格子の各軸の範囲（0066 §2.1 (d) の認証領域）と刻み |
| `eps` / `eps_c` | 試験で使った許容誤差 |
| `grid_points` | 試した点の数（0 なら不正） |
| `certificate_sha256` | `components`・`certified_domain`・`eps` / `eps_c`・`thermal_model` を、長さ付きの正準な符号化（0066 §2.1 (c) のソースツリーと同じ規律）で連結した列の SHA-256。**これが 0066 / 0133 の言う「認証のハッシュ」** |

`components` の部品と、照合する側:

| 部品 | 中身（0066 §2.1 (c)） | 照合する側 |
|---|---|---|
| `source_tree` | 実際に import された `coldaisle` パッケージのディレクトリの全ファイル（`__pycache__` と `*.pyc` を除く）を、相対パスの昇順に長さ付きで符号化した列の SHA-256 | fand と worker |
| `lockfile` | ロックファイルの SHA-256 | fand と worker |
| `environment` | 処理系の実装と版・`sysconfig` のプラットフォーム・`platform.machine()`・入った配布物の名前・版・`RECORD` のハッシュから作る指紋 | fand と worker |
| `hint_policy` | 基準の context の全体と `context_overrides`（`fan-policy.yaml` の `supervisor` の該当部分を正準化したもの） | fand |
| `hint_gate` | 格子の定義・`eps` / `eps_c`、および `safety.yaml` の `max_eps` / `max_eps_c` | fand |
| `optimizer` | Optimizer の設定（探索格子・`mpc.budget_ms`・`mpc.optimizer.max_evaluations` など `solve` の結果に効くもの）と目的関数の設定 | worker |
| `conditions` | `LearnedMpcController.conditions()` の全体（列挙を別に持たない。0066 §2.1 (c)） | worker |
| `calibration` | ランナーが使った `RuntimeCalibration` の digest。`available` なら較正を当てる全 metric の実効の offset（`calibration_offsets.effective_metric_offsets`）の `offsets_sha256`、`unavailable` ならその閉じた code（0101 §2.2 の2値）を区別できる形で符号化したものの SHA-256。2状態を同じ digest にしない（空の `offsets_c` の `available` と `unavailable` を混ぜない。0096 §2.6） | fand（起動時に読んだ較正）と worker（frame の `calibration`） |

- `calibration` の部品は artifact の `metric_binding` に依らない**較正の全体**の digest とする。L9 の digest
  （0096 §2.4。artifact が使う metric に関わる値だけ）は artifact を読まないと1つに決まらず、fand は artifact を
  読まない（0077 §2.6 / 0101 §4）ため。全体で束ねるので、artifact が使わないチャネルの較正を変えても再認証が要る
  （安全側。L9 より厳しい向きにしか働かない）。実効の offset の写像は `Normalizer` と L9 が共有する
  `calibration_offsets.py` の関数をそのまま使い、規則を3か所目に書かない（0096 §5 #3）
- **較正が変わったら証明書は合わなくなり、B0 に留まる。** 較正の変更は fand の再起動で効く（0101 §2.5 /
  `docs/calibration.md` の再起動の順番）ので、再起動した fand の照合で `calibration_mismatch` になり、新しい較正で
  ランナーを走らせて証明書を昇格し直すまで B0 のまま。`--calibration` を省いて起動した fand（`unavailable`。0101 §2.6 (b)）も、
  `available` で作った証明書とは合わない

- **部品の digest を作る関数は1つだけ**にし、ランナー・fand・worker が同じ関数を呼ぶ（置き場所は横断の module。
  `calibration_offsets.py` と同じ扱い）。`tests/` 側で digest を計算・上書きしない（0066 §2.1 (c)）
- 証明書の大きさは Registry の `max_artifact_bytes` で抑え、読む側も上限＋1 byte だけを読む（0062 §2.1 の規律）

### 2.4 fand の起動時の照合と実効の段階

fand は起動時に1回だけ、tick を始める前に照合する（合成の起点 `control_daemon.py`。DB は開かない）。
**どの失敗でも起動は止めない**（0066 §2.1 (c) / 0133 §2.6）。

- **実効の段階** = `min(設定の stage, 実装が動かせる最も高い段階, 照合の結果)`。照合が通らなければ B0。
  設定より上がることは無い
- 照合の順（最初に落ちたところで止め、その理由を残す）:
  1. `--registry-root` が渡され、snapshot を読めた（`registry_unbound`）
  2. hint_certificate の production があり、checksum・形式が合う（`certificate_missing` / `certificate_unreadable`）
  3. 証明書の `schema_version` を知っている・`result` が `passed`・`grid_points > 0`（`certificate_invalid`）
  4. 証明書の `thermal_model` が、同じ snapshot の thermal_model の production と一致する（`thermal_model_mismatch`）
  5. `eps` / `eps_c` と `max_eps` / `max_eps_c` が 0066 §2.1 (b) の制約を満たす（`eps_bounds_invalid`）
  6. fand 側の部品（`source_tree` / `lockfile` / `environment` / `hint_policy` / `hint_gate` / `calibration`。`calibration` は
     起動時に `--calibration` から読んだ `RuntimeCalibration`、つまり frame で worker へ運ぶのと同じ値から計算する）を自分で計算し、
     それぞれ一致する（`<部品>_mismatch`）
  7. 証明書の `certificate_sha256` を、証明書の欄から計算し直して一致する（`certificate_hash_mismatch`）
- ロックファイルは起動時の引数（仮の名前 `--hint-lockfile`。root の所有のパス）で渡す。渡されない・読めないときは
  `lockfile_mismatch` で B0（§5 の 4）
- 照合の結果は構造化ログ（JSON Lines）に1行、閉じた語彙で残す（`configured_stage` / `effective_stage` /
  `certification`（上の理由。通れば `certified`）/ `certificate_sha256`（あれば））。パス・ホスト名は載せない
- **稼働中に hint_certificate または thermal_model の production が変わったら**（0098 §2.6 の production の監視）、
  実効の段階をその tick から B0 に下げ、**再起動まで上げない**（0066 §2.1 (c) の「稼働中に artifact が変わった
  場合も Stage B を止める」。下げるだけで上げる経路を作らない。§5 の 6）。ヒントの読み手と B0 の記録は続ける。
  production の監視は Learned の経路が開いているときだけ（0098 §2.6）で、経路が開いていなければ B1 は
  そもそも働かない
- trace の `skipped` は既存の `certification_mismatch` のまま（0133 §2.8 / 0149 の優先の順のまま）。
  理由の細目は trace に載せず、ログに残す（§2.7）

### 2.5 B0 に留めるのは「使わない」であって、失効ではない

0133 §2.4 のとおり、照合の失敗はヒントの状態を変えない。B0 の記録（`would_override` など）は続き、
`requested_demand` には何も起きない。MAX にもしない（ヒントは安全の入力ではない）。

### 2.6 MPC worker 側の照合（frame で受け取る）

- B1 の frame の版上げ（0133 §2.5 の `supervisor_hint` を足すもの）と**同じ版上げ**で、frame に欄
  `hint_certification` を足す: `certificate_sha256` と、worker 側の部品の期待値
  （`source_tree` / `lockfile` / `environment` / `optimizer` / `conditions` / `calibration` の digest）。
  worker の `calibration` は、束縛に使った frame の `calibration`（0101 §2.2。L9 に渡したのと同じ値）から同じ関数で
  計算する。較正ファイルを読まない（0101 §2.1）
  `supervisor_hint` を入れる tick だけに入れ、それ以外は `None`（`supervisor_hint` と片方だけの frame は作らない）
- **Shadow solver の子プロセス**は、artifact の digest の照合（0133 §2.5）に加えて、自分の部品を同じ関数で計算し、
  frame の期待値とすべて一致するときだけ解く。一致しなければ解かずに `skipped`・理由 `certification_mismatch` の
  記録を返す（`hint_shadow_records` の `reason` の閉じた語彙に足す。0133 §2.5a は「など」で開けている）。
  計算は子の起動時に1回だけ行い、tick ごとには比べるだけにする（基準の周期に何も足さない）
- 子の返す記録は、自分が照合に使った `certificate_sha256` と `conditions` の digest を載せる。fand は記録の検証
  （0133 §2.5 の「新しさ・`run_id`・artifact・対の基準」）に、これらが起動時に照合した証明書と同じであることを加え、
  違えばその記録だけを捨てる（`hint_record_rejected`）。**基準の `MpcProposal` には何も足さず、扱いも変えない**
  （0133 §2.5 の「失敗の影響を片方向にする」）
- worker の不一致では fand の実効の段階を変えない（記録が捨てられるだけ。B1 は `requested_demand` に効かない）。
  B2 で worker の申告する digest をどこまで信じるかは、B2 の記録で決める（§5 の 7）

### 2.7 trace への記録

- B1 の `ControlTick` の版上げ（frame と同じ実装 PR）で、ヒントの塊に `certificate_sha256`（その run で照合を通った
  証明書。通っていなければ `null`）を足す。毎 tick 同じ値で、どの証明書の下で B1 / B2 が動いたかを後から引くため。
  理由の細目は載せない（起動時のログに1回残す）
- `hint_shadow_records` の `record_json` は 0133 §2.5a のとおり認証のハッシュを載せる。値はこの `certificate_sha256`
- Registry の provenance（trace の `registry`。0071 §2.5）に hint_certificate の production の識別が載る
  （`RegistrySnapshot.trace_provenance()` の kind の列挙に従う）
- 証明書の中身・照合の結果を読み取り API と AI ツールに出さない（ルール 8・0133 §2.5a）

### 2.8 試験の方針（すべて実機不要。ルール 7）

- **較正**: 較正つきの Mock の thermal artifact で、ランナーが `--calibration` の較正を L9 に通して合格する・
  別の較正では L9 で束縛を作れず不合格・`--calibration` を省けば較正つきの artifact は不合格（較正の無い artifact は
  通る）。証明書を作った後に較正の offset を1つ変えた fand が `calibration_mismatch` で B0・`unavailable` の fand が
  `available` の証明書で B0・空の `offsets_c` の `available` と `unavailable` の digest が違う。worker が frame の
  較正から計算した digest が fand のものと一致する（往復）
- **digest の関数**: 同じ入力で同じ値・パスと内容の境界をずらした2つのツリーが違う値になる・
  `__pycache__` / `*.pyc` を含めない・部品ごとに1か所変えると、その部品とそれだけが変わる
- **fand の照合**（`tmp_path` の Registry・Mock の artifact）: §2.4 の各理由ごとに1つずつ壊した入力で、
  実効の段階が B0・ログの理由がそれ・起動は止まらない・`requested_demand` は `stage: off` と同じ。
  すべて揃えたときだけ B1。設定が `observe` なら証明書があっても B0（設定より上げない）
- **稼働中の production の変化**: 証明書・thermal_model の production を入れ替えると次の tick から B0、
  元に戻しても再起動まで B0 のまま
- **worker の照合**: 部品を1つずつ変えた worker で、子が解かずに `certification_mismatch` を返す・基準の
  `MpcProposal` の内容と送信時刻が照合の無い worker と同じ（0133 §2.5 の決定論の試験に加える）。
  `certificate_sha256` を偽った記録を fand が `hint_record_rejected` で捨てる
- **ランナー**: 小さな格子と Mock の Thermal Model で、合格なら証明書・不合格なら証明書なし。証明書の digest は
  ランナーが計算した値で、試験から値を渡す経路が無い。格子の軸や offset を減らした設定の証明書は、元の設定の
  fand と照合が通らない。`SCHED_IDLE` に下げられない環境では何もせずに終了する
- **境界**: AI 層・API・eventd・control_admin がランナーと証明書の module を import しない（0064 §2.2 の import 走査に
  加える）。`coldaisle.control.safety` / `reactive` / `fallback` / `hardware` も import しない（0133 §2.7）
- **Registry**: 新しい kind の `promote` が承認ファイル無しでは通らない。古い版の snapshot を新しい読み手が読める・
  新しい版の snapshot を古い読み手が `INVALID_REGISTRY` として拒む

---

## 3. Consequences

### 良くなること

- 認証のハッシュの置き場所・書き手・承認・照合が既存の Registry の lifecycle と権限（0062 / 0104）に乗り、
  新しい書き込みの経路・uid・ディレクトリを作らない。LLM が触れる経路も増えない
- Thermal Model の昇格・コードや設定の変更・worker の設定の食い違いのどれでも、照合が落ちて B0 に倒れる。
  走らせ忘れ・置き忘れは安全側にしか効かない
- 照合の理由が部品ごとに出るので、「何を変えたから再認証が要るのか」が分かる
- fand が持たない条件（`conditions()`・Optimizer の設定）も、worker の Shadow solver が解く前に確かめる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| `src/coldaisle/` のどのコードを変えても再認証が要る（0066 §2.1 (c) の帰結） | 変えなければ B0 に倒れるだけ。ランナーは CLI 1つで、走らせて登録・昇格するだけ |
| Registry の snapshot の版が上がり、古い読み手が読めなくなる | 0061 §2.1 と同じ。版を上げる PR で読み手をまとめて更新する |
| fand の起動にソースツリーと配布物のハッシュの計算が加わる | 起動時に1回だけ。tick の外。失敗しても B0 で起動する |
| worker 側の照合は worker の自己申告を fand が比べる形で、worker の内側を fand は見ない | B1 は `requested_demand` に効かないので記録が汚れるだけ。B2 でどこまで信じるかは B2 の記録で決める（§5 の 7） |
| 承認者が B1 の他の前提を確かめるのは人の手順で、fand は確かめない | 承認ファイルの `reason` に書かせ、Registry の audit に残す（§5 の 3） |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 証明書を `config/` の隣のファイルに置く（0066 §5 #3 の一案） | 所有者が承認した方向（Registry の lifecycle。0133 §5 の 3）と合わない。承認・audit・rollback の仕組みを別に作ることになる |
| thermal_model の `ArtifactMetadata` に証明書を埋める | metadata は登録時に固定される。証明書はコード・設定の変更でも作り直すので、そのたびに同じ Thermal Model を別の版で登録し直すことになる |
| Registry の `registry.json` に thermal_model の key に結んだ別の節を作る | 新しい lifecycle の操作・承認・audit の種類を作ることになる。kind を足せば既存の操作がそのまま使える |
| fand の起動時に網羅試験を走らせる | 0066 §4 のとおり遅く、起動の可否が揺れる |
| 照合が合わないとき起動を拒む | 0066 §2.1 (c) / 0133 §2.6 のとおり、ヒント無しの運転が安全側。起動は止めない |
| 照合が合わないとき B1 に下げて測り続ける | 0133 §4 で却下済み（未認証の組み合わせで worker の挙動を変えない） |
| 証明書の照合を fand だけで行う | worker は自分の `--config-dir` を読むので、`conditions()`・Optimizer の設定が認証と違っても見逃す |
| 証明書の照合を worker だけで行い、fand は worker の結果を信じる | 最終の判定を ML 側のプロセスに置かない（0133 §2.5）。fand が持つ部品は fand が自分で計算する |
| 稼働中に新しい証明書が昇格したら再照合して B1 へ上げる | 稼働中に段階を上げる経路ができる。制御権に関わる段階は下げるだけにし、上げるのは再起動の時だけにする（0077 §2.6 の production の監視と同じ規律） |
| ランナーが較正を 0 で埋める・L9 を飛ばして束縛を作る | 実行時（worker が frame の較正を L9 に通す。0101）と別の経路を認証することになる。較正つきの artifact では、認証した予測と実行時の予測が別物になりうる |
| 証明書の `calibration` に L9 の digest（artifact の使う metric だけ）を入れる | fand は artifact を読まないので照合できない（0101 §4）。較正の全体で束ねる |
| ハッシュ1つだけを持ち、部品ごとの digest を持たない | 合わないとき何を変えたのかが分からず、再認証の要否の判断を人が推測することになる |

---

## 5. 未決事項（所有者の判断を求める点。推奨を先頭に）

| # | 判断点 | 選択肢（推奨を先頭） |
|---|---|---|
| 1 | 証明書の置き場所 | **(A 推奨) Registry の新しい kind `hint_certificate`**（§2.2。既存の lifecycle・承認・audit・rollback をそのまま使い、内容で thermal_model と結ぶ）/ (B) thermal_model の metadata に埋める（登録し直しが要る）/ (C) `registry.json` に thermal_model に結んだ別の節（新しい操作が要る）/ (D) `config/` の隣のファイル（承認済みの方向と合わない） |
| 2 | 証明書の metadata の形（`ArtifactMetadata` の必須欄 `training_dataset_version` / `source_runs` / `feature_schema_version` などは証明書に合わない） | **(a 推奨) 既存の欄を kind ごとに意味を固定して使う**（例: `training_dataset_version` に認証した thermal_model の key、`source_runs` に証明書の `certificate_sha256`、`model_family` に `hint-monotonicity-certificate`。snapshot の版を上げる同じ PR で検証を足す）/ (b) kind ごとの別の metadata の型を足す（読み手の分岐が増える） |
| 3 | B1 の他の前提（RulePolicy の本番値・本番のホストでの実時間の試験の合格。0133 §2.6）をどう確かめるか | **(a 推奨) 証明書の昇格の承認ファイルの `reason` に人が書き、audit に残す。fand は確かめない** / (b) 実時間の試験の報告の digest を証明書に入れる（ランナーが試験を呼ぶことになり、`subprocess` を使わずに作るのが重い） |
| 4 | ロックファイルの場所を fand・worker へどう渡すか | **(a 推奨) 起動時の引数で root の所有のパスを渡す。無ければ B0** / (b) ロックファイルを束ねず、実行環境の指紋（入った配布物の `RECORD`）だけで代える（0066 §2.1 (c) の置き換えが要る） |
| 5 | ランナーの優先度 | **(a 推奨) `SCHED_IDLE` 必須。下げられなければ実行しない**（§2.1）/ (b) `nice` で足りるとする / (c) 制約しない（本番のホストで fand・worker と CPU を取り合う） |
| 6 | 稼働中に証明書・thermal_model の production が変わったとき | **(a 推奨) B0 に下げ、再起動まで上げない**（§2.4）/ (b) 再照合して合えばその場で B1 へ上げる |
| 7 | worker の照合の不一致の扱い | **(a 推奨) B1 ではその記録を `certification_mismatch` で捨てるだけ。B2 でどこまで信じるかは B2 の記録で決める**（§2.6）/ (b) 1件でも不一致なら fand がその run の残りを B0 に下げる |

### 実装 Issue の切り方（参考）

1. digest の関数と証明書の型（横断の module）・Registry の kind と snapshot の版上げ
2. 認証ランナー（`coldaisle-hint-certify`）と CI 用の小さな格子
3. fand の起動時の照合・実効の段階・production の変化での B0・起動時のログ
4. B1 の実装（0133 §5 の実装 Issue の 4）に、frame の `hint_certification`・子の照合・記録の検証・trace の
   `certificate_sha256` を含める
