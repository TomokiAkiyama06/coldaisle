# 決定記録 0160: 証明書の部品 `conditions` の digest に入れる値を、解の結果に効くものに限る

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-20
- **Supersedes**（いずれも限定的な置き換え。旧記録は FINAL のため本文を書き換えず、`Superseded by` の追記だけを行う）:
  - [0066](0066-workload-hint-stage-b-conditions.md) §2.1 (c) の「探索のコストに入る他のモデルと条件」の項のうち、
    **「`LearnedMpcController.conditions()` が返す値の全体」「列挙を別に持たない」**の部分だけ。ハッシュに
    `conditions()` を束ねること・`conditions()` に項目が増えたらそのまま束ねること・Acoustic / Air Balance /
    `BalanceBand` の計算のモジュールを（ソースツリーで）束ねることは変えない
  - [0152](0152-hint-monotonic-gate-certificate.md) §2.3 の部品の表の `conditions` の行の
    **「`LearnedMpcController.conditions()` の全体（列挙を別に持たない。0066 §2.1 (c)）」**の部分だけ。
    照合する側（worker）・他の部品・`certificate_sha256` の符号化は変えない
- **関連**: [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.1 / §2.5 /
  [0057](0057-authority-rollout-stage-changes.md) §2.2 /
  [0062](0062-model-registry-operations.md) §2.1 / §2.5 /
  [0066](0066-workload-hint-stage-b-conditions.md) §2.1 (b)〜(d) /
  [0077](0077-learned-proposal-handoff.md) §2.6 /
  [0133](0133-workload-hint-stage-b-prior-rollout.md) §2.5 / §2.6 /
  [0152](0152-hint-monotonic-gate-certificate.md) §2.1〜§2.6 / §5 の「実装 Issue の切り方」2 /
  [0156](0156-hint-gate-spec-schema.md) §2.3〜§2.5 /
  [0157](0157-hint-runtime-launch-form.md) §2.1 /
  `src/coldaisle/control/mpc/controller.py`（`LearnedMpcController.conditions()`）/
  `src/coldaisle/control/mpc/counterfactual.py`（`MpcModelBinding.from_verified_artifact`）/
  `src/coldaisle/control/model_registry.py`（`ArtifactAttestation.trace_metadata()`）/
  `src/coldaisle/hint_certificate_digest.py`（`conditions_sha256`）/
  AGENTS.md「絶対に守るルール」2・3・4・7・9
- **対象 Issue**: #288（0152 の段 2「認証ランナー」の前提）

---

## 1. Context

0152 の段 2（認証ランナー `coldaisle-hint-certify`）の実装に入る前に main（b920917）を読んで、次の矛盾が見つかった。

0066 §2.1 (c) と 0152 §2.3 は、部品 `conditions` を「`LearnedMpcController.conditions()` の**全体**（列挙を別に持たない）」の
digest と決めている。ところが `conditions()` の値には、**解の結果に効かないのに、証明書を作った後に必ず変わる値**が入っている。

| `conditions()` の位置 | 中身 | 変わる時 |
|---|---|---|
| `binding.registry_revision` | `ArtifactAttestation.trace_metadata()` の欄。artifact を読んだ時点の Registry の snapshot の `revision`（`model_registry.py` の `_load_record`） | Registry への**どの書き込みでも**1つ進む |
| `authority_stage` | `MpcModelBinding.authority_stage`。worker が束縛を作ったときの実効の stage（frame の `authority_stage`。0077 §2.3） | Authority の昇格・降格・rollback（0057）のたび |

このため、次のことが**原理的に**起きる。

1. ランナーは Registry を revision R で読み、`conditions` の digest に `registry_revision = R` を入れた証明書を作る
2. 人がその証明書を `register` → `validate` → `promote` する（0152 §2.2）。Registry は少なくとも R+3 に進む
3. worker が束縛を作るときの `registry_revision` は R+3 以上で、`conditions` の digest は証明書と**決して一致しない**

つまり**証明書を昇格すること自体が、証明書を合わなくする**。supervisor_policy の昇格のような別の kind の操作でも同じことが起きる。
`authority_stage` も、ランナーがどの stage で束縛するかを記録が決めておらず、Authority が動けば合わなくなる。

危険側にはならない。0152 §2.6 のとおり、worker の Shadow solver は `certification_mismatch` で解かず、B0 に留まる。
ただし認証ランナーは作っても役に立たない（B1 に一度も進めない）。直すには、`conditions` の「全体」を
「解の結果に効くもの」に限る必要があり、0066 / 0152 に無い判断になる。所有者は 2026-10-19 にこの方向（(a)）を選んだ。
この記録はその形を決める。

---

## 2. Decision

### 2.1 除く値は閉じた一覧で持つ。それ以外は今までどおりすべて束ねる

部品 `conditions` の digest の入力は、**`LearnedMpcController.conditions()` の全体から、次の閉じた一覧の位置だけを除いた値**とする。

| 除く位置 | 結果に効かない根拠 | 除かないと起きること |
|---|---|---|
| `binding.registry_revision` | `LearnedMpcOptimizer.solve` も `MpcModelBinding.predict` / `predict_plan` も読まない。Registry の lifecycle の通し番号で、artifact の bytes（`binding.artifact_sha256`）とは独立に進む | 証明書の昇格・別の kind の操作で必ず不一致（§1） |
| `binding.production_active` | 予測・探索のどちらも読まない。しかも `MpcModelBinding.from_verified_artifact` は `production_active` が偽の artifact を拒む（`counterfactual.py`）ので、束縛ができた時点で値は常に真。情報を落とさない | 実害は無いが、Registry の状態を表す値を結果の条件と混ぜる |
| `binding.artifact_status` | 同上。束縛ができた時点で常に `production`（`ArtifactAttestation` は production pointer でないものを production として発行しない。`model_registry.py`） | 同上 |
| `authority_stage` | 予測・探索のどちらも読まない。`LearnedMpcController` は束縛の stage が実効の stage を覆うかを確かめる（`_check_binding_covers_authority`）だけで、覆わなければ解を作らず失敗にする。どの stage でも、解けたときの解は同じ | Authority の昇格・降格のたびに不一致。ランナーの stage の選び方に証明書が縛られる |

- **一覧に無い位置はすべて束ねる。** `binding` の残りの欄（`artifact_kind` / `artifact_capability` / `model_id` / `model_version` /
  `artifact_sha256`）・`confidence_profile_sha256`・`model_confidence`・`acoustic`・`air_balance`・`balance_band`・
  `balance_minimum_stable_demand` は今までどおり入れる。Confidence の2つは解そのものには効かないが、artifact と設定が
  決める**変わらない値**なので、入れても照合は壊れない。「効かない」と示せても、変わらない値は除かない（除く理由が無い）
- **`conditions()` に鍵が増えたら、そのまま束ねる**（0066 §2.1 (c) の「項目が増えたときはそれもそのまま束ねる」は変えない）。
  除く一覧は増やす向きにだけ決定記録を要する（黙って除かれる値を作らない）
- `conditions()` そのものは**変えない**。offline 評価（#91 / #105。`control/rl/environment.py` の条件）・decision trace に
  載る値は今のまま。変えるのは証明書の digest の入力だけ

### 2.2 除く形は `conditions_sha256` の手前の1か所で定義する

- 除く位置の一覧は `src/coldaisle/hint_certificate_digest.py` に**定数1つ**（位置の組の列。例: `("binding", "registry_revision")`）で持ち、
  `conditions_sha256` が digest を計算する直前にだけ当てる。ランナー・worker（段 2・段 4）は `conditions()` の値を
  そのまま `conditions_sha256` に渡し、自分で鍵を除かない（0152 §2.3「部品の digest を作る関数は1つだけ」）。
  `tests/` 側で除く・上書きしない
- **位置が無ければ例外にする（fail closed）。** 除く位置の親が mapping でない・鍵が無いときは `ValueError` を返し、
  digest を作らない。`conditions()` の形が変わって除く位置が別の場所へ移ったのに、古い位置を黙って探さない、を防ぐ。
  ランナーは証明書を出さず、worker の Shadow solver は `certification_mismatch` で解かない（どちらも B0）
- 入力の mapping は書き換えない（写しから除く）。除いた後は今までどおり canonical JSON（0156 §2.7 と同じ規約）
- 部品のタグを `coldaisle-hint-conditions-v1` から **`coldaisle-hint-conditions-v2`** に上げる。v1 で作った証明書は
  （本番ではまだ1つも無いが）不一致で B0 に倒れる
- 試験（ルール 7）: 除く一覧が上の表の4つと等しい・除く位置の値だけを変えても digest が変わらない・それ以外の位置
  （`conditions()` の各鍵と `binding` の残りの各欄）を1つずつ変えると digest が変わる・鍵を足すと変わる・除く位置が
  無い入力は例外・入力の mapping は書き換えられない。本物の `LearnedMpcController` で、同じ artifact を別の revision・
  別の authority stage で束縛した2つの `conditions()` の digest が一致すること

### 2.3 除き漏れ・除き過ぎで危険側に倒れない

- **除き漏れ**（結果に効かないのに変わる値が残る）: 照合が合わなくなるだけで、worker の Shadow solver は解かず B0
  （0152 §2.6）。ヒントは `requested_demand` に効かない。見つけたら、この記録と同じ手順で一覧に足す
- **除き過ぎ**（結果に効く値を除く）を防ぐ形:
  - 除くのは上の4つだけで、それぞれ「予測・探索が読まない」ことをコードで示せる値に限る。結果に効く値（artifact の
    bytes・Optimizer の設定・目的関数の任意依存）は `conditions` に残るか、別の部品（`thermal_model`・`optimizer`・
    `source_tree`）で束ねている
  - 除いた値が守っていた性質は別の照合が受け持つ: production であることは fand（0152 §2.4 の手順 2・5。同じ snapshot の
    production の thermal_model と証明書の `thermal_model` の一致）と worker（0077 §2.6 の production の監視）が、
    authority は Gate と Authority の段階（0057。B2 の実効は Learned MPC の authority に従う。0133 §2.6）が持つ。
    証明書は authority を上げる道具ではない
  - 除いた値を変えても、証明書の合否以外は何も変わらない。Critical Safety・Reactive Guard・Fallback・Gate の経路と
    値は変えない（ルール 2・3）
- B0 に倒れても `requested_demand` は変わらない（0152 §2.5）

### 2.4 段 2（認証ランナー）の実装に残る判断

§5 の 1〜6 は、0152・0156・0157 に書かれておらず、段 2 の実装に要る判断である。この記録の承認で一緒に決める。
決まった案は段 2 の PR がそのまま実装する。

---

## 3. Consequences

### 良くなること

- 証明書を昇格しても、別の kind を操作しても、Authority を動かしても、`conditions` の照合が壊れない。段 2 の
  認証ランナーが作る証明書で、B1 に進めるようになる
- 除く値が閉じた一覧で1か所にあり、試験で固定されるので、「何を束ねていないか」がコードと記録で読める
- `conditions()` と offline 評価の条件は変わらない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 0066 の「列挙を別に持たない」が「除く位置だけを列挙する」に変わる | 列挙するのは**除く側だけ**で、束ねる側は今までどおり全体（増えた鍵も自動で入る）。除く側を増やすには決定記録を要る |
| `conditions()` の形を変えると、除く位置が見つからず証明書を作れなくなる | fail closed（B0）。形を変える PR で一覧と試験を一緒に直す |
| production であること・authority の段階が、`conditions` の digest に表れなくなる | fand・worker・Gate がそれぞれの照合で持つ（§2.3）。証明書の `thermal_model` は artifact の bytes で結ぶ |

---

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| **`conditions()` そのものからこれらの値を外す** | offline 評価（#91 / #105）の条件の hash と decision trace の意味が変わる。評価の記録は Registry の revision まで区別して残すことに意味がある。証明書の都合で他の使い手を変えない |
| **今のまま受け入れる**（証明書は常に合わず B0） | 安全側だが、認証ランナーを作る意味が無くなり、B1 に一度も進めない |
| **束ねる値を許可の一覧で列挙する**（除く側ではなく入れる側を列挙） | `conditions()` に鍵が増えたとき、一覧を直し忘れると黙って束ねられない値ができる（除き過ぎの向き）。除く側を列挙すれば、直し忘れは不一致（B0）にしか倒れない |
| **ランナーが昇格後の revision を予測して入れる** | 昇格の手順・他の kind の操作の順に依り、決まらない。人の手順に脆い |
| **worker が証明書の revision に合わせて読み直す** | 照合の対象を証明書の値に合わせることになり、0152 §2.3 の「証明書の欄は値の出どころにしない」と合わない |
| **除く位置が無ければ黙って飛ばす** | `conditions()` の形が変わって値が別の位置へ移ったとき、変わる値が残り続けても気づけない。例外にして B0 に倒す |

---

## 5. 判断点（所有者の判断を求める点。推奨を先頭に）

§2.1〜§2.3（除く値と形）は所有者が 2026-10-19 に選んだ方向（(a)）を具体にしたもので、この記録の承認で確定する。
次の 1〜6 は段 2（認証ランナー）の実装に要る、記録に無い判断である。どの案も、合わなければ B0 に倒れる向きにしてある。

| # | 判断点 | 選択肢（推奨を先頭） |
|---|---|---|
| 1 | ランナーが thermal_model を束縛する authority stage（§2.1 で digest からは外れるので、効くのは Registry の互換性の検査 `authority_compatibility` だけ） | **(a 推奨) `shadow` に固定する**（最も低い制御権。実行時の stage での互換性は worker が束縛のときに確かめる。ランナーは制御権を持たない）/ (b) 必須の引数 `--authority-stage` で人が選ぶ（選び間違いは証明書の合否に効かず、互換性の検査だけが変わる）/ (c) Authority の journal を読む（ランナーが journal の場所と読み方を持つことになる） |
| 2 | 格子の点で solve が `ok` にならないとき（timeout・`plan_out_of_learned_range`・`cost_unusable` など。0156 §2.5 は `HardConstraintSet.build` の実行不能だけを決めている） | **(a 推奨) 不合格にして証明書を出さない。** ヒント有り・無しのどちらでも同じ。点を黙って除かない（0156 §2.5 と同じ理由。`grid_points` は試して比べた点の数）。ログには解けなかった点の数と最初の点の軸（値は載せない）と理由の code を出す / (b) 両方が解けなかった点は「実行時も基準を使う点」として数えずに続ける（試していない点を試したと記録する） |
| 3 | 二重解法の予算の分け方 | **(a 推奨) 2回の solve に、それぞれ `mpc.budget_ms` と `mpc.optimizer.max_evaluations` を丸ごと渡す。** optimizer は打ち切ると解を返さない（部分解を出さない。`optimizer.py`）ので、実行時に予算を分け合って解けた解は、ランナーの解と同じになる。時計は実時計（`SCHED_IDLE` の下で timeout すれば 2 のとおり不合格）/ (b) 実行時と同じく分け合う（基準が使った残りをヒント有りに渡す。実行時なら基準を使うだけの点で不合格になる）/ (c) 進まない時計を渡し、評価の回数だけで打ち切る（ホストの負荷に依らず決まるが、実行時と違う時計の経路を認証する） |
| 4 | 目的関数の温度の metric（`mpc.optimizer.cost_metrics.cpu_temperature` / `gpu_temperature`）が、artifact の window の feature に無いとき（0156 §2.4 は書いていない） | **(a 推奨) 不合格にして証明書を出さない**（温度の軸が window に効かず、格子が 0066 の4区分を試したことにならない）/ (b) その温度の軸を window に当てずに続ける（軸の `points` の数だけ同じ点を重ねて数える） |
| 5 | 平らな window（0156 §2.4）の時刻の原点 | **(a 推奨) `action_ts_ms` を artifact の `window_ms` にし、格子の時刻を 0 から `sample_period_ms` 刻みで作る。各 cell の `source_ts_ms` はその格子の時刻**（stale にしない）。値は設定に置かない（閾値ではなく、時刻の並びの起点）/ (b) 起点を設定に置く（設定が1つ増え、`hint_gate` に入れるかの判断が要る）/ (c) 実行時の壁時計（証明書の結果が走らせた時刻に依りうる） |
| 6 | ランナーが solve を呼ぶ入口（`LearnedMpcController` の optimizer は外から呼べない） | **(a 推奨) `LearnedMpcController` に公開の入口を1つ足す**（観測 window・safety floor・context の `weights` / `target_band`・baseline を受け取り、anchor 推論とその照合・`HardConstraintSet.build`・`solve` を `propose()` と同じ関数で行い、`OptimizerOutcome` を返す。`propose()` の振る舞いは変えない。段 4 の worker の二重解法とも共有する）/ (b) ランナーが `LearnedMpcOptimizer` と `MpcCostModel` を自分で組み立てる（controller の「目的関数を外から受け取らない」に反し、組み立てが2か所になる）/ (c) `propose()` を呼ぶ（snapshot・`SupervisorOutput`・Fallback の提案を合成する必要があり、Confidence の判定が解の取り出しに混ざる） |
