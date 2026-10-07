# 決定記録 0098: 0077 段階 2（`coldaisle-fand` の registry の束縛。PR #232）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が推奨案で承認）
- **Date**: 2026-10-07
- **Supersedes**: なし（0077 が決めていなかった点への追加と、0077 の文面の読み方の明記。0077 / 0062 / 0089 の
  決定は置き換えない）
- **関連**: [0077](0077-learned-proposal-handoff.md) §2.3 / §2.6 / §2.10 /
  [0062](0062-model-registry-operations.md) §2.4 / §5 /
  [0089](0089-authority-bound-to-loaded-artifact.md) §2.1 /
  [0061](0061-rl-supervisor-policy-artifact-and-binding.md) /
  [0095](0095-learned-channel-stage1-settled-points.md) /
  `src/coldaisle/control/registry_binding.py` / `src/coldaisle/learned_channel/registry_watch.py` /
  `src/coldaisle/learned_channel/server.py` / `src/coldaisle/control_daemon.py` / `docs/model-registry.md`
- **対象 Issue**: #104（実装 PR #232）/ #86

## 1. Context

0077 §2.10 の段階 2（`coldaisle-fand --registry-root`・起動時に1回読む snapshot からの provenance・Gate の期待値・
`expected_rl_identity`・frame の `expected_artifacts`・`registry_superseded`）を PR #232 で実装した。その実装で、
0077 に書かれていない点、または文面だけでは読み方が分かれうる点が6点あり、PR #232 の「人間レビューが必要な点」で
推奨案とともに示した。2026-10-07、所有者が6点すべてを推奨案で承認した（PR #232 はマージ済み）。
決定記録は追記のみなので、0077 の本文には手を入れず、ここに残す。記録だけを読んだ人が、実装と違う挙動を
前提にしないためである。

## 2. Decision

### 2.1 fand は 0062 §2.4 の `verify()` を呼ばない

**決着（2026-10-07 所有者の決定、推奨案）。** 0077 §2.6 は「0062 §2.4 の起動時検証の報告を使う」と書く一方で、
「`coldaisle-fand` は artifact の bytes を読まない（bytes の検証は worker の仕事）」と「1つの `RegistrySnapshot` から
作る」とも書いている。`ModelRegistry.verify()` は artifact の bytes を読み、snapshot を返さない（使うと別の読み込みに
なる）ので、両立しない。

`coldaisle-fand` は `ModelRegistry.inspect()` で snapshot を1回だけ読み、そこからすべての期待値を作る。
`verify()` は呼ばない。production の artifact の bytes が壊れていれば、worker の読み込みが失敗して
`model_load_failure` を返し、Gate が Fallback に倒す。

### 2.2 閉じた役割の接続は残し、届いたメッセージは検証の前に捨てる

**決着（2026-10-07 所有者の決定、推奨案）。** `registry_superseded` で閉じた役割は、接続を切らずに残す。
その役割に届いたメッセージは、0077 §2.6 の「以後に届いた結果は検証の前に捨てる」の文言どおり**検証の前に**捨て、
`<role>.registry_superseded` として数える。heartbeat も検証しないので生存として数えず、worker は
`worker_idle_timeout_ms` ごとに idle で切られ、再接続を繰り返しうる（そのたびに接続のログが出る）。
閉鎖は接続・切断の知らせでは解けず、`coldaisle-fand` の再起動まで続く。

### 2.3 registry の root が無いときは「記録の無い registry」（revision 0）として読む

**決着（2026-10-07 所有者の決定、推奨案）。** `--registry-root` が存在しない場所を指すときは、registry 自身の規則
（`inspect()` が空の snapshot を返し、`verify()` が `no_production` を返す）に従い、revision 0・production 無しとして
読む。trace の `registry` は `revision: 0` になり、「読まなかった・読めなかった」（`unbound()`）と区別できる。
production が無いので Learned は使わない。root の打ち間違いは、起動ログの `registry_present: false` で見る。
走行中に `registry.json` が現れて production ができたときは、固定（無し）と違うので、その役割を閉じる。

### 2.4 frame の `expected_artifacts` は `thermal_model` と `supervisor_policy` だけ

**決着（2026-10-07 所有者の決定、推奨案）。** frame の `expected_artifacts`（`LearnedFrame` v2）と `registry_superseded`
の監視が扱う kind は、役割に対応する2つ（`thermal_model` → `mpc`、`supervisor_policy` → `supervisor`）だけにする。
`confidence_model` / `feature_transform` はどの役割にも対応せず、監視もしないので載せない。複数 kind を同時に
入れ替える手順は 0062 §5 のまま開いている。

### 2.5 RL の識別を作れない版は RL を採らない

**決着（2026-10-07 所有者の決定、推奨案）。** `supervisor_policy` の production の版が `SupervisorPolicyIdentity` の
書式に収まらない（semver の build metadata `+` を含むなど）ときは、`expected_rl_identity` を `None` にし、error を
残す。Coordinator はどの RL 出力も採らず RulePolicy のままになる。安全側にしか効かない。frame の
`expected_artifacts` には registry の事実（その版）をそのまま載せる。

### 2.6 production の監視は Learned の経路が開いているときだけ

**決着（2026-10-07 所有者の決定、推奨案）。** 0077 §2.6 は監視を受付スレッドに置いている。`--registry-root` を
与えても `--learned-channel-config` を省いた（または経路を開けなかった）構成には受付スレッドが無いので、監視しない。
その構成では提案が届く経路も無いので、Gate と Coordinator が古い artifact の結果を採ることはない。
registry を起動時に読めなかったときも監視しない（固定が無く、Gate と Coordinator はすべてを拒む）。

## 3. Consequences

### 良くなること

- 記録だけを読んだ人が、実装と違う挙動（fand が起動時に artifact の bytes を検証する・閉じた役割の接続を切る・
  root が無い起動を `unbound()` とする、など）を前提にしない
- fand が artifact の bytes を読まないこと（ML を制御プロセスへ入れない）と、期待値の出どころが1つであることが
  両方保たれる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| production の artifact の破損を fand の起動ログでは知れない（§2.1） | worker の `model_load_failure` と trace の Fallback の理由で見える。運用では `coldaisle-registry verify` を使う |
| 閉じた役割の worker が idle で切られて再接続を繰り返し、ログが周期的に出る（§2.2） | 0077 §2.6 のとおり worker は自主停止して再起動の frame を待つ。問題になれば「接続を切って新しい接続を拒む」を別の記録で検討する |
| root の打ち間違いが trace では「記録の無い registry」に見える（§2.3） | 起動ログの `registry_present: false` で区別する |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| fand の起動時に `verify()` も呼ぶ | artifact の bytes を制御プロセスで読み、snapshot の読み込みが2回になる（0077 §2.6 の「1つの snapshot から」と食い違う） |
| 閉じた役割の接続を切り、その役割の新しい接続を拒む | 0077 §2.6 の「以後に届いた結果は検証の前に捨てる」は接続が残る前提の文言。安全上の差は無い |
| root が無い起動を読めなかった（`unbound()`）として扱う | registry 自身の規則（`no_production`）と食い違い、trace で「読んだが空」と「読まなかった」を混ぜる |
| `expected_artifacts` に全 kind を載せる | 役割に対応せず監視もしない kind を載せると、worker が固定として扱える根拠の無い値を運ぶ |

## 5. 未決事項

なし（複数 kind の同時の入れ替えは 0062 §5、worker 側の照合は 0077 段階 3 / 4 のまま）。
