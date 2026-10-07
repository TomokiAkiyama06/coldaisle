# 決定記録 0096: 反実仮想 Thermal Model artifact v2 の較正の digest（`calibration_binding.sha256`）の計算方法

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が §5 の9点をすべて推奨案で承認。§6）
- **Date**: 2026-10-07
- **Supersedes**: [0079](0079-model-artifact-formats.md) §2.3 の「較正の束縛」の項のうち、「digest は dataset を作る側が
  明示して渡す」の部分のみ（§2.6。呼び出し側は**較正の値**を明示して渡し、digest は trainer と loader が同じ関数で計算する。
  既定値を置かないことは変えない。§5 #1）。0079 の他の節は有効。旧記録側への `Superseded by` の追記は本 PR で行った。
  ほかは 0079 §2.3 が計算方法を決めていなかった点への追加
- **Superseded by**: [0099](0099-calibration-change-log.md)（§5 #4 の推奨のうち「(ii) `--apply` は … そこへ追記し」の書き手の部分のみ。書き手は取り込み（`coldaisle-daemon`）が起動時に実効の写像の変化を検知して記録し、`--apply` は書かない。§5 #4 の他の部分と他の節は有効）／ [0101](0101-calibration-via-learned-frame.md)（§2.7 の3つ目の項と §5 #5 の推奨の列のうち、0090 に較正ファイルを足さない理由「L9 → artifact を読み込めない → 0089 で authority が Baseline に落ちる」の部分のみ。L9 は MPC worker で働くので fand の authority は下がらず、Learned の提案が届かず Fallback になる。結論（足さない）と他の節は有効）
- **関連**: [0079](0079-model-artifact-formats.md) §1 / §2.3 / §2.4（L9）/ §5 #3 / §6 の質問 4・5 /
  [0084](0084-model-artifact-anchor-support.md) §2.3（L8 の `metric_binding` のキー集合）/
  [0087](0087-dataset-v2-action-grid.md) §2.6（較正の変更の検査の置き場所）/
  [0024](0024-calibration.md)（較正の手順と記録）/ [0010](0010-csv-replay.md) §2.9（再生では較正を当てない）/
  [0056](0056-model-drift-detection-and-retraining-triggers.md) §2.5（宣言された変更）/
  [0089](0089-authority-bound-to-loaded-artifact.md) / [0090](0090-authority-bound-to-loaded-config.md) /
  [0002](0002-metric-naming.md) §2.2（派生値は保存しない）/
  `config/calibration.json` / `config/metrics.yaml` / `src/coldaisle/channels.py` /
  `src/coldaisle/ingest/calibration.py` / `src/coldaisle/ingest/normalize.py` /
  `src/coldaisle/control/model/counterfactual.py` / `src/coldaisle/control/model/counterfactual_training.py`
- **対象 Issue**: #84（PR #226 で `calibration_binding` を入れた）/ #86（0079 段 4。この前に決める）

## 1. Context

PR #226（#84、0079 段 2）は artifact v2 の manifest に `calibration_binding: {sha256 | null}` を入れた。
しかし digest の**計算方法**はどの決定記録にも無い。いまのコードは次のとおりで、値の意味を誰も決めていない。

- trainer（`CounterfactualTrainingSpec.calibration_sha256`）は呼び出し側から digest を受け取り、そのまま manifest に書く
- loader（`RegistryCounterfactualThermalModel.from_verified_artifact(calibration_sha256=)`）も digest を受け取り、
  L9 で manifest の値と**完全一致**を見るだけ（不一致は拒否して Fallback。0079 §6 の質問 4）

このままだと、学習の側と runtime の側が別の規則で digest を作っても気づけない。規則が食い違えば、
較正が同じでも L9 で拒否され続けるか（安全側だが artifact が使えない）、較正が違っても通る（危険側）。
段 4（#86。MPC を段 2 の型へ束縛する）より前に決める必要がある。

前提として、いまの較正の仕組みは次のとおりである。

- 較正は**取り込み**（`coldaisle-daemon` の `Normalizer`）で掛かり、store には較正後の値だけが入る。
  どの較正で作った値かを store は記録しない（0079 §1 / §6 の質問 5）
- `config/calibration.json` は `Calibration` 型で読む。値は `offsets_c: {チャネル名: ℃}` で、
  ほかに `note` / `calibrated_at` / `reference` / `samples` を持つ。**ファイル自体に版の欄は無い**
- `offsets_c` に無いチャネルは 0.0 として扱う（`Calibration.offset_for`）。非有限値は読み込み時に拒否する
- **湿度には当てない**（`Normalizer` が metric 名の末尾 `_humidity` で除く）
- チャネル名と metric 名の対応は `CHANNEL_TO_METRIC`（`air.*` の7つ）。`gpu.*` / `cpu.*` / `fan.*` などの
  telemetry には較正が無い
- 派生値（`d.*`）は保存せず、`config/metrics.yaml` の `minuend - subtrahend` で毎回計算する（0002 §2.2）。
  `minuend` / `subtrahend` に `d.*` は書けない（`validate_metric` が拒否する）ので、展開は1段で終わる
- `--source replay` は較正を当てない（0010 §2.9。CSV の値は元の取り込み時に較正済み）

2026-10-07、所有者は PR #226 のエージェントの推奨案（派生値は展開し、`CHANNEL_TO_METRIC` で対応する metric の
較正 offset を `{metric: offset}` の canonical JSON にして SHA-256。対応する metric が無ければ `null`）で
決定記録を書くと決めた。本記録はその推奨案を実装が参照できる具体性まで詰めたものである。
推奨案の中で曖昧だった点は §2 に推奨の解釈を書き、§5 で確認を求めた（2026-10-07、すべて推奨案で決着。§6）。

## 2. Decision

規則の名前を **`calibration-digest-v1`** とする（本記録の中での呼び名。bytes には含めない。§2.5）。

### 2.1 入力

digest は次の2つだけから決まる。

1. **metric の集合**: artifact の `metric_binding.entries` の `metric` の集合（0084 §2.3 の L8 により、
   feature と target の metric の和集合と一致する）。派生値の定義は各 entry の `derived`（`minuend` / `subtrahend`）を使う
2. **較正の値**: `config/calibration.json` を `Calibration` 型で読んだ後の `offsets_c` と、
   `Calibration.offset_for` の「無いチャネルは 0.0」の規則

`note` / `calibrated_at` / `reference` / `samples` は**入れない**。どれも store の値を変えないので、
これらが変わっただけで artifact を失効させない（`metric_binding` が表示名 label を入れないのと同じ向き。0079 §2.3）。
特に `calibrated_at` を入れると、値を変えない再較正（残差がほぼ 0）でも artifact が使えなくなる。

### 2.2 展開と対応づけ

1. metric の集合の各 entry について、`derived` が `null` ならその metric を、そうでなければ `minuend` と `subtrahend`
   の2つを取り出す。派生値そのものの名前（`d.*`）は残さない。取り出した名前の集合を重複なしで持つ
   （同じ `air.room` を複数の派生値が使っても1つ）
2. 取り出した名前のうち、`METRIC_TO_CHANNEL`（`CHANNEL_TO_METRIC` の逆写像）にあるものだけを残す
3. そのうち `Normalizer` が較正を当てない metric（いまは湿度。末尾 `_humidity`）を除く。判定は `Normalizer` と
   **同じ述語**を共有し、2か所に書かない（§5 #3）
4. 残った各 metric に、対応するチャネルの**実効の offset** `Calibration.offset_for(channel)` を対応させる
   （`offsets_c` に無ければ 0.0。§5 #2）
5. 残った metric が1つも無ければ digest は **`null`**

展開した先が `d.*` であることは `validate_metric` が既に禁じているが、digest の関数でも検査し、
来たら digest を作らず例外にする（黙って1段だけ展開して進めない）。

### 2.3 canonical JSON

ハッシュする bytes は `{metric 名: offset}` の JSON object 1つとし、既存の `metric_binding_sha256` /
`canonical_json_bytes`（`control/model/thermal.py`）と同じ規約で直列化する。

```python
(
    json.dumps(
        obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    + b"\n"
)
```

- **キー**: metric 名（`air.front_intake` など）。チャネル名ではない。`sort_keys=True` による Python の文字列順
  （metric 名は ASCII なので code point 順と同じ）
- **値**: `Calibration` 型で読んだ後の `float`（IEEE 754 binary64）。直列化は `json.dumps` の既定、すなわち
  Python の `float.__repr__`（往復で同じ値に戻る最短の10進表記。例 `0.191`、`0.0`、`-0.31`）
  - ファイルに `0` / `0.0` / `0.00` のどれで書いても `Calibration` が `0.0` にするので、digest は同じ
  - **`-0.0` は `0.0` に正規化する**（加算の結果が同じなので、同じ較正として扱う。`coldaisle-calibrate` は
    `round(...) or 0.0` で既に正規化しているが、手で書いたファイルにも備える）
  - **丸めない。** `Normalizer` は丸めずに足すので、丸めると store の値が違う2つの較正が同じ digest になる
  - 非有限値は `Calibration` が読み込み時に拒否する。`allow_nan=False` は二重の守り
- **末尾に改行 `\n` を1つ付ける**（既存の canonical JSON と同じ）
- digest は bytes の SHA-256 の小文字16進64文字（manifest の `Sha256` 型）

例（値は説明用）: metric の集合が `{d.intake_rise, gpu.0.core}` で、`offsets_c` が `{"front_intake": 0.191}` だけのとき、
展開で `{air.front_intake, air.room, gpu.0.core}`、対応づけで `{air.front_intake, air.room}`、bytes は
`{"air.front_intake":0.191,"air.room":0.0}\n` になる。

### 2.4 学習時と読み込み時

計算する関数は1つ（純粋関数。仮に `calibration_digest(entries, offsets) -> str | None`）とし、`control/model` に置く。
入力は `metric_binding` の entry の列とチャネル名 → offset の `Mapping` だけで、`ingest` を import しない
（`channels.py` はレイヤ横断なので import してよい）。湿度の述語の置き場所は §5 #3。

- **学習時**: 合成の起点が、学習データの期間に効いていた較正ファイルを**明示して**読み（既定の path を置かない。
  0079 §2.3 の「既定値を置かない」のまま）、trainer へ**較正の値**を渡す。trainer は自分が作った
  `metric_binding` の entry とその値から digest を計算して manifest に書く。呼び出し側が digest を手で作る経路は
  残さない（§2.6）
- **読み込み時（runtime）**: `coldaisle-fand` は起動時に1回だけ較正ファイルを読む（0079 §2.4。path は設定で受け取る）。
  loader は L8 が通った後の L9 で、**artifact の `metric_binding`** と runtime の較正の値から digest を計算し、
  manifest の `calibration_binding.sha256` と完全一致を見る。外れたら拒否して Fallback（0079 §6 の質問 4 のまま）
  - metric の集合を artifact から取るので、runtime は artifact を読む前に digest を1つに決められない。
    これが loader に digest ではなく較正の値を渡す理由である
  - L8 が runtime の `MetricCatalog` と派生値の定義の一致を保証した後に計算するので、artifact の `derived` と
    runtime の定義は同じである
  - **`null` は manifest の申告だけで信じない。** L9 はまず artifact の `metric_binding` から §2.2 の1〜3で較正の掛かる
    metric の集合を導く。集合が空なら期待値は `null` で、manifest も `null` なら runtime の較正の値に依らず通る。
    集合が空でないのに manifest が `null`、または集合が空なのに manifest が `null` でなければ拒否する
- 照合は artifact を読み込むときだけで、tick ごとには行わない（0052 §2.6 と同じ。較正は起動時にしか読まない）

### 2.5 規則の版

artifact v2（`schema_version` 2）の `calibration_binding.sha256` の意味を **`calibration-digest-v1` と定義する**。
いまは規則が1つしか無いので、規則の名前を bytes にも manifest にも入れない。manifest の形（`{sha256 | null}`）は
変えないので、本記録で artifact の `schema_version` は上げない。

**規則を変えるときは 0079 §2.3 の「形式の意味を変えるときは `schema_version` を上げる」に従い、artifact の
`schema_version` を上げる**（または manifest に規則の欄を足す。どちらも新しい決定記録で決める）。
「規則が違えば digest は必ず一致しないので古い artifact は L9 で拒否される」とは言えない。投影が変わらない入力
（特に `null`）や、新旧で同じ bytes になる入力では、新旧どちらの規則でも同じ値になり、版が無いと runtime は
どちらの規則で作った digest かを見分けられないためである（PR #229 の Codex の指摘）。

### 2.6 trainer と loader の引数を替える

PR #226 の引数（`calibration_sha256: Sha256 | None`）を次へ替える。どちらも**既定値を持たない**。

- `CounterfactualTrainingSpec`: `calibration_sha256` を廃し、チャネル名 → offset の値を受け取る
- `from_verified_artifact` / `_load_checked`: `calibration_sha256` を廃し、runtime の較正を受け取る。型は
  「読めた較正の値（チャネル名 → offset）」と「**読めなかった**」を区別する2状態とする（仮に
  `RuntimeCalibration.available(offsets)` / `RuntimeCalibration.unavailable(reason)`）。空の `Mapping` で「読めなかった」を
  表さない。空の値は全チャネル 0.0 と同じ digest になり、0.0 で学習した `null` でない artifact が L9 を通ってしまうため
  （PR #229 の Codex の指摘）
  - L9 は、どちらの状態でも先に `metric_binding` から較正の掛かる metric の集合を導き、manifest の `null` /
    非 `null` がそれと合うかを見る（§2.4）。`unavailable` のときは、集合が空（かつ manifest が `null`）の artifact
    だけを通し、集合が空でない artifact は**digest を比べずに**拒否する（§5 #8）。manifest の `null` の申告だけで
    通さない。本規則より前に任意の値で作られた、`air.*` を使うのに `null` の artifact を通さないため（PR #229 の
    Codex の指摘）。`available` なら §2.4 のとおり計算して比べる
- `check_artifact_contents` の `check_calibration=False`（作成時に L9 を飛ばす）はそのまま

実装は #84 の後続の PR とし、#86（段 4）の束縛の切り替えより前にマージする（§5 #6）。

### 2.7 較正が変わったとき

- 較正を変えると、**変わった offset のチャネルが §2.2 の展開と対応づけで残る** artifact は L9 で拒否され、Learned MPC は
  使われず Fallback になる（0079 §3 の「較正を変えるたびに artifact が使えなくなる」のまま）。artifact が使わない
  チャネルだけの変更では digest が変わらず（§2.9 の感度）、その artifact は L9 を通る。`null` の artifact も影響を受けない
- 使える artifact を得るには、**変更後の較正で取り込んだデータ**で Dataset v2 を作り直し（変更をまたぐ窓は
  0087 §2.6 で拒否される）、学習・登録・昇格をやり直す。新しい artifact は SHA-256 が変わるので、0089 により
  authority は新しい artifact について SHADOW から上げ直しになる
- **0090 の config 束縛には較正ファイルを足さない。** 0090 が束縛するのは Control Config の4ファイル
  （`ControlConfig.sources`）で、較正ファイルはそれに含まれない。較正の変化は L9 → artifact を読み込めない
  → 0089 の「artifact を持たない構成は Baseline より上を有効にしない」で既に authority が Baseline に落ちる（§5 #5）
- 較正ファイルを書き換えてから取り込みを再起動するまでの隙は、0079 §2.4 のとおり本記録でも閉じない
- **L9 は `coldaisle-fand` の起動時にしか働かない。** 較正を変えて取り込みだけを再起動すると、fand は古い較正の値と
  それで照合済みの artifact のまま、新しい較正の値を読み続け、L9 による拒否と Fallback は起きない。0079 §2.4 は
  「取り込みと `coldaisle-fand` を両方再起動する手順で運用する」と決めたが、いまの `docs/calibration.md` の手順と
  `coldaisle-calibrate --apply` の出力は取り込みの再起動しか言わない（PR #229 の Codex の指摘）。この食い違いを
  手順で閉じる（§5 #9 で決着）。実装 PR で `docs/calibration.md` と `--apply` の出力に「取り込みより先に fand を再起動する」を書く

### 2.8 移行

- PR #226 の時点で、実データから作った v2 artifact は無い（実機 dataset が無い。0079 §1）。Registry に
  登録済みの v2 artifact も無い前提で、移行の手順は置かない
- 試験の fixture が使っている任意の digest（`tests/test_thermal_model_v2.py` の `CALIBRATION_SHA`）は、
  §2.6 の実装 PR で本規則の計算値に置き換える
- 本規則より前の手順（呼び出し側が任意の digest を渡す）で作った v2 artifact が仮にあっても、L9 で拒否されるとは
  限らない（渡した値が本規則の値と偶然一致する、特に `null` の artifact は通る。§2.5 と同じ理由）。L9 に頼らず、
  そうした artifact は Registry に production として登録・昇格しない。あれば本規則の trainer で作り直し、古いものは
  retire する（PR #229 の Codex の指摘）

### 2.9 試験すべき性質

実装 PR（§2.6）で次を確かめる。

- **決定性**: 同じ入力から同じ digest。`calibration.json` のキーの順、`note` / `calibrated_at` / `reference` /
  `samples` の変更で digest が変わらない
- **実効値**: `offsets_c` にチャネルが無いことと `0.0` を明示することが同じ digest。`0` / `0.0` / `-0.0` も同じ
- **感度**: 使う metric のチャネルの offset を最小の差（`math.nextafter`）だけ変えると digest が変わる。
  使わない metric のチャネルの offset を変えても変わらない
- **展開**: `d.intake_rise` だけを使う artifact の digest が `air.front_intake` と `air.room` の offset に依存し、
  派生値の名前に依存しない。複数の派生値が同じ metric を使っても1つのキーになる
- **湿度**: `air.room_humidity` を使い、`offsets_c` に `room_humidity` があっても digest に入らない（§5 #3 の結論に従う）
- **null**: `gpu.*` / `cpu.*` だけを使う artifact は `null`。その artifact は runtime の較正の値に依らず L9 を通る
- **golden vector**: §2.3 の例の入力に対する bytes と digest の16進を試験に固定する（規則の意図しない変更を捕まえる）
- **学習と読み込みの一致**: trainer が作った artifact を同じ較正の値で読み込むと L9 を通り、1つの offset を変えた値で
  読み込むと `ArtifactCheck.CALIBRATION` で拒否される
- `d.*` を展開した先がまた `d.*` である entry を与えると、digest を作らず例外になる
- **読めなかった較正**: `unavailable` を渡すと、全 offset が 0.0 で学習した `null` でない artifact も L9 で拒否され、
  較正の掛かる metric を使わない `null` の artifact は通る。`available({})`（空）は `unavailable` と別に扱われる
- **`null` の申告の検査**: §2.2 の1〜3で残る（較正を当てる）metric、すなわち湿度を除く `air.*` を使うのに manifest が
  `null` の artifact は、`available` でも `unavailable` でも L9 で拒否される。`gpu.*` だけ、または `air.room_humidity` と
  `gpu.*` だけを使うのに manifest が `null` でない artifact も拒否される。湿度だけの artifact は `null` で、`unavailable` でも通る

## 3. Consequences

良くなること。

- 学習と runtime が同じ関数で digest を作るので、規則の食い違いで L9 が誤って通る・誤って拒否する状態が作れない
- 値を変えない較正の記録の更新（`note` や `calibrated_at`）や、使わないチャネルの再較正では artifact が失効しない
- `gpu.*` / `cpu.*` だけを使う model は較正に縛られない

悪くなること（と緩和策）。

| 悪くなること | 緩和策 |
|---|---|
| 学習時に渡す較正の値が、学習データの期間に実際に効いていたものだと機械的には保証できない（store が記録しない。0079 §6 の質問 5） | 0087 §2.6 の検査で期間内の変更は拒否される。期間の後に較正を変えてから学習する場合の穴は §5 #4 で塞ぐ案を出す |
| 再生（`--source replay`）で取り込んだデータは、そのとき `config/calibration.json` に何が書いてあっても較正が当たっていない（0010 §2.9） | 合成の起点は「元の取り込み時に効いていた較正」を渡す必要がある。v2 の dataset は ControlTick を要するので、実運用の dataset は実機の取り込みからだけ作られる見込み。再生由来で学習するときは人が明示する |
| 未較正（全 0.0、`calibrated_at = null`）と「0.0 で較正済み」が同じ digest になる | store の値が同じなので model の前提も同じ。未較正の警告は取り込みの起動時のログが担う（0024 §2.8） |
| PR #226 の引数を替える（§2.6） | 実データの artifact はまだ無く、呼び出し側は試験だけ |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| 較正ファイル全体（bytes）の SHA-256 | `note` や `calibrated_at` の更新、使わないチャネルの再較正、キーの並び替えで artifact が失効する。0079 §2.3 の「使った metric に関わるもの」に反する |
| `offsets_c` 全体（全チャネル）の canonical JSON | 使わないチャネルの再較正で失効する |
| キーをチャネル名にする | artifact の他の束縛（`metric_binding`）が metric 名で書かれており、照合や説明で2つの名前空間を行き来することになる |
| 派生値を展開せず、派生値の名前のまま入れる | 派生値の値は minuend / subtrahend の較正に依存する。名前だけでは較正の変化を捉えられない |
| offset を小数3桁などに丸めてから入れる | `Normalizer` は丸めずに足す。丸めると store の値が違う2つの較正が同じ digest になる |
| `offsets_c` に明示されたチャネルだけを入れる（無いチャネルは入れない） | 「無い」と「0.0」は store の値が同じなのに digest が違う。§5 #2 で決着 |
| 規則の版を今から bytes や manifest に含める | 規則が1つのうちは区別する相手が無い。規則を変えるときに `schema_version` を上げれば（§2.5）、版の無い v2 を新しい規則で読まない。§5 #7 |
| 呼び出し側が digest を計算して渡す（PR #226 のまま） | runtime は artifact の metric の集合を知る前に digest を決められず、学習側と別の実装になりやすい（§2.4） |

## 5. 未決事項（所有者に確認した点）

2026-10-07、所有者が9点すべてを推奨案で決めた（§6）。番号は本文からの参照を保つため提案時のまま残し、各行の論点の先頭に「決着」と書いた。「代替」の列は判断前の記録である。

| # | 論点 | 推奨 | 代替 |
|---|---|---|---|
| 1 | **決着**（2026-10-07 所有者の決定、推奨案）。§2.6 で trainer / loader の引数を digest から較正の値へ替えることを、0079 §2.3 の「digest は dataset を作る側が明示して渡す」の部分的な置き換えとして扱うか | **扱う。** FINAL にするときヘッダの Supersedes に書き、0079 へ `Superseded by` を追記する（README「追記のみ」の例外） | 「明示して渡す」の対象が値に替わるだけで趣旨は同じとみなし、置き換えにしない |
| 2 | **決着**（2026-10-07 所有者の決定、推奨案）。`offsets_c` に無いチャネルの扱い（推奨案の「対応する metric の較正 offset」の読み方） | **実効値 0.0 として入れる**（§2.2 の4）。store の値は同じなので同じ digest にする | 明示されたチャネルだけを入れる。「較正の記録に載っているか」を区別したいならこちら |
| 3 | **決着**（2026-10-07 所有者の決定、推奨案）。較正を当てない湿度（`air.room_humidity`）を digest から除くか | **除く**（§2.2 の3）。`Normalizer` の述語を `channels.py` など横断の場所へ移して共有する | 推奨案どおり `CHANNEL_TO_METRIC` にあるものはすべて入れる（湿度の offset を書き換えると、store の値は変わらないのに artifact が失効する） |
| 4 | **決着**（2026-10-07 所有者の決定、推奨案）。学習データの期間の**後**に較正を変えてから学習したとき、合成の起点がいまの較正ファイルを渡すと、古い較正のデータで学習した artifact が新しい較正の runtime の L9 を通ってしまう | **学習の合成の起点は、期間の先頭から学習の時点までの宣言された変更（0056 §2.5）を受け取り、期間の後に `calibration_changed` があれば学習を拒否する**。古い較正の値を正しく渡して作った artifact も、新しい較正の runtime では L9 で拒否されるので、作る利益が無い。学習の CLI を作る PR（0094 §2.4（PR #225）の CLI の v2 対応の後）で入れる。**ただしこの検査は宣言が残っていることに頼る。** いまの `coldaisle-calibrate --apply` は較正ファイルを書き換えるだけで `calibration_changed` の宣言を残さず、0087 §2.6 は空の宣言も許すので、宣言が無ければ穴は閉じない（PR #229 の Codex の指摘）。そこで (i) `calibration_changed` の**追記のみの永続的な記録先**を作り、(ii) `--apply` は**実効の offset の写像（§2.2 の4 と §2.3 の正規化の後）が変わったときだけ**そこへ追記し（`calibrated_at` や `note` だけの更新では追記しない。§2.1 と同じ向き）、(iii) dataset と学習の入口はその記録先を**必ず読み**、呼び出し側が空の宣言を渡して迂回できないようにする。0056 §2.5 の `DeclaredChange` はいま呼び出し側が渡すだけ（`DriftEvidenceManifest.changes` は証拠の YAML ごと）で、この記録先はまだ無い。記録先の形と 0056 / 0087 §2.6 との関係は**別の決定記録（予定 0099。所有者の指示で別の Issue と並行して作成中）**で決める。その記録では少なくとも、変化の比較を artifact に依らず `METRIC_TO_CHANNEL` の較正を当てる温度 metric 全体（無いチャネルは 0.0、湿度は除く）で行うこと、変更の時刻をファイルを書いた時刻ではなく**取り込みが新しい較正を使い始めた時点**（取り込みの再起動）で記録するか、書いた時刻から再起動までを変更の区間として保守的に扱うこと、`--apply` 以外の書き手（手で `calibration.json` を編集する経路。`coldaisle-calibrate` と現行のファイルの `note` が想定している）による変更も漏らさないこと（たとえば取り込みが起動時に実効の写像の変化を検知して記録する、または手編集の経路を禁じる）を決める（PR #229 の Codex の指摘）。それまでこの穴は閉じない（#4 の検査は穴を閉じないものとして扱う）（PR #229 の Codex の指摘） | (a) 呼び出し側の責任とし、手順書に書くだけにする。(b) 宣言に頼らず、dataset の manifest に取り込み時点の較正（`offsets_c` と `calibrated_at`）の写しを束縛する（store が較正を記録しないので、写しを取る時点の正しさは結局人の手順に依る） |
| 5 | **決着**（2026-10-07 所有者の決定、推奨案）。0090 の config 束縛に較正ファイルを足すか | **足さない**（§2.7）。L9 → artifact 無し → 0089 で Baseline に落ちる経路で足りる | 足す（証拠に較正ファイルの hash を入れ、較正を変えたら authority の上限を直接 Baseline にする） |
| 6 | **決着**（2026-10-07 所有者の決定、推奨案）。§2.6 の実装をどの PR で行うか | **#84 の後続 PR**（`calibration_digest` と引数の置き換え・試験・golden vector）。#86 の段 4 より前 | #86 の段 4 の PR に含める |
| 7 | **決着**（2026-10-07 所有者の決定、推奨案）。規則の版（`calibration-digest-v1`）を bytes や manifest に入れるか | **いまは入れない。** v2 の digest の意味を `calibration-digest-v1` と定義し、規則を変えるときは artifact の `schema_version` を上げる（§2.5。0079 §2.3 のまま） | いまのうちに `calibration_binding` に `rule` の欄を足す（manifest の形が変わる。実データの artifact が無い今なら移行の費用は小さい） |
| 8 | **決着**（2026-10-07 所有者の決定、推奨案）。runtime で較正ファイルを読めなかったとき | **`fand` の起動は止めず、loader へ `unavailable` を渡し、`null` でない artifact を L9 で拒否して Fallback**（較正の掛かる metric を使わない `null` の artifact だけ通す。`null` の申告だけでは通さない。§2.4 / §2.6）。読めないことは構造化ログに出す | 起動を止める（較正ファイルが無いと Baseline でも動かない） |
| 9 | **決着**（2026-10-07 所有者の決定、推奨案）。較正を変えたのに fand を再起動しない運用で、L9 が働かないこと（§2.7） | **手順で閉じる。** `docs/calibration.md` と `coldaisle-calibrate --apply` の出力に「**取り込みより先に** `coldaisle-fand` を再起動する（新しい較正の値で L9 をやり直してから、新しい較正の値を store に入れる）」を書く。本記録の実装 PR（§5 #6）と同じ PR で行う | fand が較正ファイルの変化を周期的に検知して L9 をやり直す（制御側が較正ファイルを監視する経路が増える）、または取り込みが使った較正の digest を store に記録して fand が照合する（0079 §6 の質問 5 を覆すので新しい記録が要る） |

## 6. 承認記録

**2026-10-07、リポジトリ所有者が §5 の9点をすべて推奨案で承認し、本記録を FINAL にした。**

| §5 の判断点 | 決定 | 本記録 |
|---|---|---|
| 1 | trainer / loader の引数を digest から較正の値へ替えることを 0079 §2.3 の部分的な置き換えとして扱い、0079 へ `Superseded by`（部分）を追記する | ヘッダ / §2.6 |
| 2 | `offsets_c` に無いチャネルは実効値 0.0 として入れる | §2.2 の4 |
| 3 | 較正を当てない湿度は除き、`Normalizer` と述語を共有する | §2.2 の3 |
| 4 | 学習の入口で期間の後の `calibration_changed` を拒否する。前提の追記のみの記録先は別の記録（予定 0099）で決め、それまでこの穴は閉じない | §5 #4 |
| 5 | 0090 の config 束縛に較正ファイルを足さない | §2.7 |
| 6 | 実装は #84 の後続 PR で、#86 の段 4 より前 | §2.6 |
| 7 | 規則の版はいまは bytes にも manifest にも入れず、規則を変えるときに artifact の `schema_version` を上げる | §2.5 |
| 8 | runtime で較正ファイルを読めなければ起動は止めず `unavailable` を渡し、較正の掛かる metric を使う artifact を L9 で拒否して Fallback | §2.4 / §2.6 |
| 9 | fand を再起動しない運用の穴は手順で閉じる（実装 PR で `docs/calibration.md` と `--apply` の出力に「取り込みより先に fand を再起動する」を書く） | §2.7 |

§5 #4 の記録先（予定 0099）だけが本記録の外で開いている。
