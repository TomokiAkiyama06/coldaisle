# 決定記録 0062: Model Registry の運用入口（CLI）と起動時検証、lifecycle 監査の追跡

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-09-21
- **Supersedes**: なし
- **関連**: [`docs/model-registry.md`](../model-registry.md)、
  [`0030-control-decision-trace-storage.md`](0030-control-decision-trace-storage.md) §2、
  [`0037-model-registry-rollback-target.md`](0037-model-registry-rollback-target.md) §2 / §5、
  [`0048-thermal-model-artifact-and-inference.md`](0048-thermal-model-artifact-and-inference.md) §2.4、
  [`0052-learned-mpc-optimizer-and-hard-constraints.md`](0052-learned-mpc-optimizer-and-hard-constraints.md) §2.1、
  [`0057-authority-rollout-stage-changes.md`](0057-authority-rollout-stage-changes.md) §2.2、
  GitHub #79 / #82 / #92 / #93 / #104
- **対象 Issue**: #104

**所有者の承認が必要。** 制御モデルの昇格・rollback という安全側の運用手順を決めるため、
実装担当モデルに関係なく人間のレビューを必須とする（AGENTS.md「実装の担当」）。

## 1. Context

`ModelRegistry`（PR #123 / #151 / #157）は lifecycle・承認の束縛・原子的な pointer 切り替えを
持っている。一方で、**人がそれをどう動かすのか**が決まっていなかった。

- Python API しか無く、昇格・rollback は対話セッションから直接呼ぶしかない。
  `docs/authority-rollout.md`「まだ無いもの」にも、管理操作の入口が無いと書いてある
- `load_production()` は**1 kind ずつ**しか検証しない。起動時に
  「いまの registry でどの kind が Fallback になるのか」「戻り先は残っているのか」を
  まとめて確かめる経路が無い。0037 §5 は、rollback target が `None` になったことに
  運用者が気付けない点を未決事項として残している
- #104 の受入基準は「promotion / rollback が decision trace へ残る」と言うが、
  0030 の decision trace は **tick 単位**の保存で、registry の lifecycle event は tick に
  対応しない。どこへ載せるかが決まっていない

## 2. Decision

### 2.1 管理操作の入口は CLI（`coldaisle-registry`）にする

`src/coldaisle/registry.py` を合成の起点として置き、`status` / `audit` / `verify` /
`register` / `validate` / `promote` / `rollback` / `retire` を持たせる。

- **読み取りが既定。** `status` / `audit` / `verify` は registry を作らず、書き換えない
- registry を変える操作（`validate` / `promote` / `rollback` / `retire`）は
  `--expected-revision` を**必須**にする。既定値を持たせない。いまの revision を読まずに
  書けると、他者の判断を後勝ちで潰せる
- 読み取り API（#23）にも AI ツール（#22 / #23）にもこの操作を出さない。
  **LLM は registry を書き換えられない**（AGENTS.md ルール1）
- CLI が読むファイル（artifact 本体・metadata・承認・contract）は、**読む前に上限で切る**。
  artifact 本体は `max_artifact_bytes`、それ以外は `max_snapshot_bytes` を上限とし、
  上限＋1 byte だけを読んで超えていれば拒否する。読んでから大きさを判断しない。運用者が
  間違えて巨大なファイルを指したときに、管理 process を MemoryError で落とさない
- 壊れた YAML / JSON、読み取り失敗は traceback ではなく、構造化ログと終了コード 1 で返す。
  **握りつぶさない**（AGENTS.md コード規約）が、運用者が読める形にする
- **parser の再帰も設定の誤りとして扱う。** 深く入れ子にした YAML は byte 上限に収まって
  いても PyYAML の再帰を尽くし、`RecursionError`（`ValueError` でも `yaml.YAMLError` でも
  ない）を投げる。深さを先に測って弾く方式は採らない。YAML は flow・block・alias で入れ子を
  作れるので、片方だけを数える走査は、持っていない上限を持っていると主張することになる。
  parser に測らせ、その結果を設定の誤りへ寄せる（PR #162 codex review 4057748805）

### 2.2 promotion / rollback の human approval はファイルで渡す。CLI は合成しない

`HumanApproval`（承認者・理由・対象 artifact・その checksum・対象 revision）は**人が書いた
JSON** をそのまま読み、CLI は中身を作らない・直さない。

- `--approver` / `--reason` のような、承認を引数から組み立てる flag を**作らない**
- `promote` の対象 artifact は**承認が名指しした artifact** を使う。別に指定できると、
  承認とずれた対象を渡す経路ができる（registry 側も拒むが、経路自体を残さない）
- 束縛の検証は従来どおり registry が行う。CLI は通り道であって、判断の主体ではない

### 2.3 CLI は artifact を deserialize も実行もしない

Registry と同じ安全境界をそのまま引き継ぐ。`subprocess` / `eval` / 任意 SQL を呼ばず、
Fan Demand・PWM・hwmon・Authority Stage へ届く経路を持たない（AGENTS.md ルール1・2）。

### 2.4 起動時検証は読み取り専用の報告にする。例外で起動を止めない

`ModelRegistry.verify(compatibility)` を足し、`RegistryHealthReport` を返す。

| `RegistryHealth` | 意味 | CLI 終了コード |
|---|---|---|
| `ok` | production も、検証済みの戻り先も揃っている | 0 |
| `degraded` | production は使えるが、known-good な戻り先が無い / 壊れている | 3 |
| `unusable` | production pointer はあるが load できない。その kind は Fallback | 4 |
| `no_production` | production pointer がひとつも無い。**候補があっても昇格扱いにしない** | 4 |
| `invalid` | snapshot を検証できない（schema version 違いを含む） | 4 |

- **書き込まない。** registry root が無ければ作らない
- 戻り先の検証は **checksum と format だけ**にする。互換性は rollback 実行時のその時点の
  runtime contract で見る（0037 §2）。schema を更新しただけで健全な戻り先を失わせない
- `ArtifactHealth.compatibility_checked` を**既定値の無い必須項目**にする。contract を
  渡さずに得た `loaded` を「互換性も確かめた」と読み違えさせない
- 総合判定は個別の検証結果から導く。`RegistryHealthReport` は、個別の結果と食い違う
  総合判定（失敗があるのに `ok`、何も検証していないのに `ok`）を受け付けない
- `verify` が使う runtime contract は kind ごとの YAML（`schema_version: 1`）で渡す。
  未知の kind / stage は黙って落とさず拒否する。**検査したつもりの kind を検査しない**
  状態を作らない
- **`--contract` を渡したなら、いま production の kind を全部覆う。** 覆っていない kind が
  あれば、CLI は判定を出さずに失敗する（終了コード 1）。欠けた kind は checksum と format
  だけで `loaded` になり、schema や authority が合っていなくても総合判定が `ok` になる。
  `RegistryHealthReport.unchecked_kinds()` がその kind を返すので、CLI 以外の呼び出し側
  （#74 の起動経路など）も同じ確認ができる。contract を渡さない整合性だけの検証は、
  それと分かる形（全 kind が `compatibility_checked: false`）のまま残す

### 2.5 lifecycle 監査の正本は `registry.json` の audit にする

decision trace への載せ方は、次の2点に限って決める。

- `RegistryAuditEvent.trace_metadata()` を用意する。path を含まず、**欄は常に揃える**
  （値が無ければ `None`）。欄ごと消すと、読む側が「記録されていない」と「起きていない」を
  区別できない
- `RegistrySnapshot.pointer_changes` で、production pointer を動かした判断（promotion /
  rollback）だけを取り出せるようにする

**0030 の decision trace（tick 単位の保存）へ registry event 用の保存先を足すかは、ここでは
決めない。** tick に対応しない記録へ tick_id を合成するのは、束縛していない識別子を作ることに
なる。#82 側の決定に委ねる（§5）。

### 2.6 rollback target が無い状態を可視化する

0037 §5 の未決事項のうち、**気付けるようにする**部分だけをここで閉じる。戻り先が無い、または
壊れている registry は `degraded` として報告し、CLI は終了コード 3 で返す。
Rule Engine / Notification（#18 / #20）への接続は引き続き別 Issue とする。

## 3. Consequences

- 昇格・rollback が1コマンドで再現でき、誰が・なぜ・いつ・どの checksum に対して承認したかが
  audit に残る。対話セッションの履歴に依存しない
- 起動時に「この registry では何が Fallback になるか」を、制御を起動せずに確認できる
- 終了コードが 0 / 3 / 4 に分かれるため、`verify` を systemd の `ExecStartPre` などに置くと
  **production が無いだけで起動を止めてしまう**。止めたいのは本当に危険なときだけなので、
  緩和策として、起動を止める用途に使うなら 4 のうち `invalid` だけを見るように
  報告本文（`health`）で分岐する
- 承認をファイルにしたため、`artifact_sha256` と `expected_revision` を人が写す手間が増える。
  緩和策: `status` が両方を出す。写し間違いは registry が拒否する（黙って通らない）
- `--contract` を渡す運用では、production の kind が増えるたびに contract の更新が要る。
  緩和策: 更新を忘れると `verify` が失敗し、名前を挙げて知らせる（黙って `ok` にしない）
- registry の lifecycle が SQLite の decision trace に入らない期間が続く。緩和策:
  `audit --pointer-changes` が同じ内容を JSON で出せる

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| CLI に `--approver` / `--reason` を持たせ、承認をその場で組み立てる | 承認が「実行した人の自己申告」になる。checksum と revision への束縛が、実行時の引数と同じ強さしか持たなくなる |
| 読み取り API（#23）へ昇格・rollback を足す | 書き込み経路を HTTP へ出すことになる。0045 は書き込み入口を読み取り API と分けると決めている |
| `verify` で壊れた registry を自動修復する（戻り先の付け替え・retire） | 人の承認なしに production pointer を動かすことになる。#104 の原則に反する |
| 起動時検証で例外を投げ、制御の起動を止める | production が無い・壊れている場合こそ #79 Fallback で運転を続けなければならない（AGENTS.md ルール4） |
| registry event に tick_id を合成して 0030 の decision trace へ書く | tick に対応しない記録へ、束縛していない識別子を与えることになる（#159 のレビューで繰り返し指摘された失敗の型） |
| 戻り先も runtime contract で検証する | schema 更新の直後に、健全な戻り先を `degraded` 扱いで失う。0037 §2 の決定と食い違う |
| `--contract` が覆っていない kind を、整合性だけ検証して `ok` に含める | 互換性まで確かめるつもりで渡した contract の取りこぼしが、`ok` と区別できなくなる（PR #162 codex review 4057584854） |
| 読んでからファイルの大きさを判断する | 上限を超える入力で、判断する前に管理 process が落ちる（同 4057584857） |
| YAML の入れ子の深さを自前の走査で先に測る | flow・block・alias のどれか1つしか数えられず、持っていない上限を持っていると主張することになる（同 4057748805） |

## 5. 未決事項

- registry の lifecycle event を 0030 の decision trace（SQLite）へ載せるか、載せるなら
  どの表・どの束縛で載せるか。**#82 側で決める**
- `degraded`（戻り先なし）を Rule Engine / Notification へ流すか（0037 §5 の残り）
- 複数 kind（confidence model / supervisor policy / feature transform）を**同時に**入れ替える
  必要が出たときの手順。いまは kind ごとに独立した promotion しかない
- retired artifact の bytes をいつ消すか（保持期間）。いまは消さない
