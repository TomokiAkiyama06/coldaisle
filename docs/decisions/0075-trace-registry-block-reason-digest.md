# 決定記録 0075: decision trace の registry の塊では、自由記述の `reason` を digest にする

- **種別**: Decision Record
- **Status**: FINAL（2026-09-29、リポジトリ所有者が承認）
- **Date**: 2026-09-29
- **Supersedes**: [`0071-control-trace-read-api.md`](0071-control-trace-read-api.md) §2.5 のうち、
  pointer を成立させた変更の `RegistryAuditEvent.trace_metadata()` を**そのまま**（自由記述の
  `reason` の全文を含めて）毎 tick 載せる部分のみ。0071 の他の節と、§2.5 の残り（`revision` と
  kind ごとの production pointer を毎 tick 載せる・registry event 用の表を足さない・正本は audit）は有効
- **関連**: [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md) /
  [`0062-model-registry-operations.md`](0062-model-registry-operations.md) §2.5 /
  [`0071-control-trace-read-api.md`](0071-control-trace-read-api.md) §2.5 / §5 #7 /
  `config/retention.yaml`（`control_trace_days`）/ [`docs/model-registry.md`](../model-registry.md)
- **対象 Issue**: #104

## 1. Context

0071 §2.5 は、`ControlTick` v10 の `registry` の塊に、kind ごとの production pointer と、
それを成立させた最後の promotion / rollback の `RegistryAuditEvent.trace_metadata()` を
**毎 tick** 載せると決めた。`trace_metadata()` には人が書いた自由記述の `reason` がそのまま入る。

`reason` は registry の audit 側で最大1000字まで許される（`RegistryAuditEvent.reason` /
`HumanApproval.reason`）。毎 tick の塊に全文を写すと、保存量は次のように見積もられる。

| 前提 | 値 |
|---|---|
| production pointer を持ちうる kind | 4（`thermal_model` / `confidence_model` / `supervisor_policy` / `feature_transform`） |
| 1 kind あたりの `reason` | 最大1000字。trace は `ensure_ascii` を使わない JSON なので、日本語なら UTF-8 で最大約3000 bytes |
| tick の周期 | 1秒を想定（86,400 tick / 日） |
| 保持期間 | `control_trace_days: 30`（`config/retention.yaml`）→ 約2,592,000 tick |

最悪の場合、1 tick あたり `reason` だけで 4 × 1000字 ≒ 4 KB（ASCII）〜 12 KB（日本語）になり、
30日分で**約10 GB〜約31 GB**になる。これは判断の記録ではなく、起動中はずっと同じ文字列の繰り返しである。
理由の全文は registry の audit が正本であり（0062 §2.5 / 0071 §2.5）、trace に写しを積み上げる必要は無い。

リポジトリ所有者は、保存量を抑えるために、各 tick の registry の塊が持つものを減らすことを選んだ。

## 2. Decision

### 2.1 毎 tick 載せるもの

各 tick の `registry` の塊（`RegistryProvenance`）は、引き続き次を持つ。

- registry の `revision`
- kind ごとの production pointer（artifact の identity。`artifact_sha256`）
- その pointer を成立させた pointer 変更の identity（`RegistryPointerChange`：
  `registry_revision`・`event`・artifact の kind / id / version・actor・承認者・承認時刻・
  承認した artifact の checksum など）

ただし `RegistryPointerChange` は、自由記述の `reason` の全文を持たない。代わりに
**`reason_sha256`（`reason` を UTF-8 で符号化した bytes の SHA-256、小文字16進64字）**を持つ。

- 形は `RegistryAuditEvent.tick_trace_metadata()` が作る。`trace_metadata()` と同じ欄から `reason` を除き、
  `reason_sha256` を足したもの。`trace_metadata()` 自体は変えない（`coldaisle-registry audit` の出力は全文のまま）
- digest の計算は `coldaisle.control.schema.registry_reason_sha256()` の1か所に置く
- `RegistryPointerChange` は `reason` の欄を受け取らない（`extra="forbid"` で拒否する）
- 理由の全文は registry の audit（`coldaisle-registry audit --pointer-changes`）だけで引く。
  trace の `registry_revision` で audit の該当 event を特定し、その `reason` の digest が
  `reason_sha256` と一致することで、trace と audit が同じ変更を指していることを確かめられる

### 2.2 毎 tick の塊にある他の欄の点検

同じ規則（毎 tick 載る欄に、上限の無い自由記述を置かない）で、`RegistryPointerChange` の他の欄を点検した。

| 欄 | 形 | 扱い |
|---|---|---|
| `reason` | 自由記述（最大1000字） | **digest に置き換える**（§2.1） |
| `previous_artifact` / `rollback_target` | `ArtifactRef.key`（`<kind>/<model_id>/<version>`）。各部は上限付きの識別子だが、trace 側の schema には長さの上限が無かった | 自由記述ではないので値は残す。**trace 側の schema で長さの上限（240字）を閉じる** |
| `artifact_kind` / `model_id` / `actor` / `approver` | 識別子の pattern・最大120字 | そのまま |
| `model_version` | semver・最大80字 | そのまま |
| `event` | 閉じた語彙（`promoted` / `rolled_back`） | そのまま |
| `approval_artifact_sha256` / `artifact_sha256` | 固定長の16進 | そのまま |
| `registry_revision` / `occurred_at_ms` / `approved_at_ms` | 整数 | そのまま |

上限の無い自由記述は `reason` だけだった。`production` の鍵（kind）も識別子の pattern と上限で閉じている。

### 2.3 版

`ControlTick` v10 と `RegistryProvenance` の `schema_version: 1` はまだ `main` に入っていない（#104 の PR 内）。
このため版は上げず、v10 の定義そのものを本記録の形にする。`reason` の全文を持つ v10 の記録は保存されていない。

## 3. Consequences

### 良くなること

- 毎 tick の registry の塊の大きさが `reason` の長さに依存しなくなる。1 kind あたり最大約3000 bytes が
  64字の digest に置き換わり、30日保持の最悪値から約10〜31 GB が消える
- trace と audit の対応を、`registry_revision` と `reason_sha256` の2点で機械的に照合できる
- 自由記述が毎 tick の記録に紛れないので、trace を読む経路（API・画面）へ人が書いた文章が流れ込む量が減る

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| trace だけを読んでも「なぜ昇格したか」が分からない | 正本は audit であり（0062 §2.5 / 0071 §2.5）、`registry_revision` で該当 event を引ける。`docs/model-registry.md` に引き方を書く |
| registry の audit を失うと、理由の全文は trace から復元できない | audit の保全は registry の責務（0062）。trace は元々写しであり、正本の代わりにはしない |
| 短い定型文の `reason` は digest から推測できる | `reason` は秘匿情報ではない（audit にも全文がある）。digest は照合のためで、隠すためのものではない |

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| すべて載せ続ける（0071 §2.5 のまま、`reason` の全文を毎 tick 写す） | §1 の見積もりのとおり、最悪で30日に数十 GB が同じ文字列の繰り返しに使われる。全文は audit が正本で、trace に写す利点が保存量に見合わない |
| 変化したときだけ載せる（起動した tick や pointer が変わった tick にだけ registry の塊を載せる） | 0071 §2.5 が退けた形と同じ問題がある。保持期間（30日）より長く動いたデーモンでは、その tick が消えて「どの昇格の下で動いているか」が trace から言えなくなる。また「前回載せたか」を判断するために、制御 loop が過去の trace や追加の状態を持つことになり、0071 §5 #7 の「制御デーモンは起動時に過去の trace を読まない」と衝突する |
| `reason` を先頭 N 字に切り詰めて載せる | 切った文章は全文と区別がつかず、誤読を招く。長さの上限は下がるが、digest と違って audit との照合にも使えない |

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | `coldaisle-registry audit` に `registry_revision` や `reason_sha256` で絞り込むオプションを足すか | 必要が出てから別 Issue。いまは全件を出して読む側で絞る（0071 §2.5） |
