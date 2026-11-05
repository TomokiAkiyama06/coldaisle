# 決定記録 0180: #74 を閉じる前に残っている判断（CALIBRATION・終了コード 1・D 状態の hang・config_invalid の再主張・Guard の値の status）

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-11-05
- **Supersedes**: なし（[`0072`](0072-control-admin-entry.md) §2.10 の段階 4・[`0080`](0080-fand-systemd-unit.md) §5 の 4 / 6 / 7・
  [`0028`](0028-fan-control-contracts.md) §5 の 6・[`0060`](0060-control-loop-runtime.md) §5 の 1・
  [`0118`](0118-hwmon-backend-label-less-headers.md) §5 の 3 / 4 に答える。どの記録の決定も置き換えない。新しい安全の値は決めない）
- **関連**: [`0119`](0119-safety-values-first-host.md) §2.14 / [`0153`](0153-airflow-measurement-method.md) /
  [`0161`](0161-fand-enable-prerequisites.md) §2.1 / [`0174`](0174-no-airflow-characterization.md) /
  `docs/reactive-guard.md` / `docs/fan-header-mapping.md`
- **対象 Issue**: #74

## 1. Context

fand は 0161 §2.5 の順序を終えて導入先で常駐している（2026-10-23 に `enable`。0169・0178 の更新の後も R1〜R3・I1 に合格）。
#74 の受入基準は、試験（`tests/test_control_loop.py` の invariant 群・`tests/test_fand_exit_codes.py`・
`tests/test_safety_handoff.py`）と実機の T1〜T9・R1〜R3 でほぼ確かめ済みだが、#74 を「後で決める場所」として指した
未決が、決定記録の上で次の 5 つ残っている。どれも実装の不具合ではなく、決めないまま残っていることが問題である
（次に読む人が、未決のまま運用していると誤解する）。

| # | 残っているもの | 出どころ | いまの実態 |
|---|---|---|---|
| A | `CALIBRATION` モードの受理 | 0072 §2.10 の段階 4（前提は「#75 の測定計画の形」） | 管理ソケットは `calibration` を予約として拒否する（`control_admin/messages.py` の `RESERVED_MODES`）。0153 で「CALIBRATION モードは使わない。MANUAL で足りる」と決め、0174 で #75 の測定自体を行わないことにした。loop 側の `OperatingMode.CALIBRATION` と、その間も floor を外さない試験（invariant 7）は残っている |
| B | 終了コード 1 が `config_invalid` と未捕捉の例外で重なっている | 0080 §5 の 7・0083 §2.2（「#74 で引き続き扱う」） | どちらも 1 で終わり、どちらも再起動する（`RestartPreventExitStatus=3 4` に 1 は無い）。journal では `config_invalid で停止` の行の有無と traceback で見分けられる |
| C | `ExecStopPost` も動かないとき、チップが PWM をどう保つか | 0028 §5 の 6・0080 §5 の 4・0118 §5 の 3（「#74 の実機確認」） | 0161 §2.1 で、D 状態の hang の残るリスクを所有者が受け入れた（2026-10-21）。この機械で D 状態を起こして確かめたことは無く、確かめるには OS を hang させる必要がある |
| D | `config_invalid` の Max の間に、外から `pwmN_enable` を戻されたときに Max を書き直すか | 0060 §5 の 1・0080 §5 の 6・0118 §5 の 4 | `run_config_invalid_max()` は Max を 1 回書いて終了コード 1 で終わる。引き継ぎ記録が残るので `ExecStopPost` が Max を書き、`Restart=always`・`RestartSec=2`・`StartLimitIntervalSec=0` で起動し直し、起動の `STARTUP` の Max と `config_invalid` の Max をまた書く。**再起動の輪が数秒ごとに Max を書き直している**（0080 §5 の 6 の「結果として書き直される」）が、決定としては未決のまま |
| E | Reactive Guard の値の `status` | #74 の「#80 Reactive Guard = active（実測閾値確定後）」・`docs/reactive-guard.md`「#50 / #75 の実測が終わるまでは `confirmed` にしない」 | Guard の値は 0119 §2.14 で、2500 ms ごとの実測の上昇の速さの p99 付近から決め、所有者が 2026-10-11 に承認した。導入先の `fan-policy.yaml` の各値の `status` は `provisional` のままで、起動ログの `provisional_values` に 59 件（すべて `safety.yaml` の外）出る。`fan-policy.yaml` の `status` は起動ログに出るだけで、Guard の動作は変わらない。#75 は 0174 で測らないことになったので、文書の「#75 の実測まで」は来ない |

あわせて、#74 をいつ閉じるかを決める（§2.6）。

## 2. Decision

（§5 の推奨の案で書く。所有者の判断で変わったら、ここを判断に合わせて直してからマージする）

### 2.1 A: `CALIBRATION` の受理は取り下げる

- 0072 §2.10 の段階 4（`CALIBRATION` の受理）は**行わない**。前提だった #75 の測定計画は、0153 で MANUAL で測ると決まり、
  0174 で測定そのものを行わないことになった
- 管理ソケットは今のまま `calibration` を予約として拒否する（`RESERVED_MODES` は変えない）。新しい入口を足さない
- loop 側の `OperatingMode.CALIBRATION` と、その間も Critical Safety を外さない試験は**残す**。消すと `ControlTick` の
  語彙と trace の版に触れることになり、得るものが無い。人の入口が無いので、運用でこのモードになることは無い
- `RESERVED_MODES` の docstring（「#75 の測定計画の参照形が決まるまで」）は、次に `control_admin/` を変える PR で
  「0180 §2.1 で受理を取り下げた」に直す。docstring だけのために `/opt` の区分 F の更新を起こさない

### 2.2 B: 終了コード 1 は分けない

- `config_invalid` も未捕捉の例外も終了コード 1 のままにする。どちらも「再起動する」で systemd の扱いが同じで、分けても
  `RestartPreventExitStatus=` は変わらない（0080 §5 の 7 の注のとおり）
- 見分けは journal で行う。`config_invalid` は `config_invalid で停止`（error）の行が、未捕捉の例外は traceback が出る。
  この見分け方を `docs/ubuntu-deploy.md` 6.5 に書き足す（文書だけ。終了コードの表の正本は `control_daemon.py` の docstring で、
  次に `control_daemon.py` を変える PR でそちらにも書く）
- 終了コードで見分ける必要が出たら（通知・監視が終了コードを読むようになったとき）、空いている番号を足す記録を別に作る

### 2.3 C: D 状態の hang は「既知の限界」として閉じる。実機で確かめない

- 0028 §5 の 6・0080 §5 の 4・0118 §5 の 3 は、0161 §2.1 の受け入れをもって**閉じる**。D 状態を意図して起こす確認は行わない
  （OS の hang を起こす試験は、この機械の上の他のサービスと GPU の作業を巻き込み、得られるのも driver の 1 つの振る舞いだけ）
- 残るリスクは 0161 §2.1 の「受け入れても残ること」のまま（Max までの時間に上限が無い・引き継ぎ実行部も同じ driver で止まりうる・
  `fand_trace_silent` は知らせるだけで夜間は届かない）。緩和もそのまま（チップの保護、fand と別プロセスの GPU の Fan）
- 見直す契機: journal に hung task（`INFO: task ... blocked for more than`）か、説明の付かない watchdog の終了・trace の欠けが出たとき。
  そのときは 0161 §5 の 1 の残りの選択肢（ハードウェアの watchdog・EC の fail-safe など）を別の記録で検討する

### 2.4 D: 周期的な書き直しのコードは足さない。再起動の輪を再主張の仕組みとして明記する

- `config_invalid` の間の再主張は、§1 の表の D の再起動の輪（`ExecStopPost` の Max → `RestartSec` の後の起動 → `STARTUP` の Max →
  `config_invalid` の Max）が担う。間隔はおよそ `RestartSec` + 起動の時間（R2 の READY=1 まで最長 346 ms）で、数秒ごとに書き直される
- これを決定とする。fand の中に周期的な書き直しのループを足さない（`config_invalid` の経路は「不正な閾値を読まない・確認済みの
  hardware mapping だけを使う」最小の経路で、tick の仕組みを持ち込まない。0060 §2.9）
- この輪が成り立つ条件は unit の 3 つの設定（`Restart=always`・`RestartPreventExitStatus=` に 1 を入れない・`StartLimitIntervalSec=0`）。
  これらを変える PR は、この節を見直す。**`StartLimitIntervalSec=0` を外すと、既定の上限（10 秒に 5 回）で輪が止まり、
  再主張がなくなる**
- 外から `pwmN_enable` を BIOS の自動制御へ戻された場合、次に書き直されるまでの数秒は BIOS の曲線で回る。BIOS の曲線は fand を
  入れる前の運用で、0119 §2.14 の実測の元でもあるので、この数秒を危険とはみなさない
- 試験: `config_invalid` で引き継ぎ記録を書いてから Max を書き、記録を残したまま終わることは `tests/test_hwmon_fan_backend.py` が
  既に見ている。unit の 3 つの設定が変わっていないことを見る試験を `tests/` に足す（unit のテンプレートを読むだけ。実機不要）

### 2.5 E: Guard の値は 0119 §2.14 の承認で「実測から確定済み」とし、`status` の書き換えは Authority を上げる前にまとめる

- #74 の「#80 Reactive Guard = active（実測閾値確定後）」は、0119 §2.14（実測の p99 付近から決め、所有者が承認）で**満たした**とする
- `docs/reactive-guard.md` の「#50 / #75 の実測が終わるまでは `confirmed` にしない」は、0119 §2.14 の承認と 0174（#75 は測らない）に
  合わせて書き直す（文書だけ）。値の確定・緩和に所有者の承認が要ることは変えない
- 導入先の `fan-policy.yaml` の `status` を `confirmed` へ書き換える作業は、0119 §5 の 3（`fan-policy.yaml` の §2.14 以外の値。
  Authority を上げる前・0028 の承認点 5）と**まとめて**行う。それまで起動ログの `provisional_values` が出続けるが、Guard の動作には影響しない。
  書き換えだけのために導入先の設定を変えて fand を再起動しない

### 2.6 #74 を閉じる条件

次がそろったら #74 を閉じる。

1. 本記録のマージ
2. 文書の追従の PR（`docs/ubuntu-deploy.md` の enable の記述。PR #412）と、§2.2・§2.5 の文書の書き足し、§2.4 の unit の設定の試験
3. Replay からの E2E の再現の試験（受入基準「同じ Replay + Clock + Config で tick の順序を再現できる」の補い。別の PR）

Learned（#84 / #85 / #86 / #89 / #92 / #104）の統合と、Learned を配備するときの soak のやり直し（0161 §2.2 の注）は
それぞれの Issue で扱い、#74 を開けたままにする理由にしない。

## 3. Consequences

### 良くなること

- #74 を指していた未決がすべて閉じ、決定記録の上で「何が決まっていて、何を受け入れたか」が 1 か所で読める
- D の再主張が unit の設定に依存していることが明記され、その設定を変える PR で気付ける（§2.4 の試験）

### 悪くなること・その緩和

| 悪くなること | 緩和 |
|---|---|
| `CALIBRATION` の入口が無いまま残る | 測定は MANUAL と lease で足りる（0153）。要るようになったら、新しい記録で受理を決める |
| 終了コード 1 だけでは原因が分からない | journal の行で見分けられる（§2.2）。終了コードを機械で読むようになったら番号を足す |
| D 状態の hang を実機で確かめないまま運用する | 0161 §2.1 で受け入れ済み。見直す契機を §2.3 に置く |
| `config_invalid` の間、外から戻されると数秒は BIOS の曲線 | BIOS の曲線は fand 以前の運用で、Max へ数秒で戻る（§2.4） |
| 起動ログの `provisional_values` が当面出続ける | Guard の動作には効かない。Authority を上げる前にまとめて書き換える（§2.5） |

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| `CALIBRATION` の受理を実装する（段階 4 を行う） | 使う測定が無い（0153 / 0174）。人の入口を増やすと、管理ソケットの試験と監査の対象が増える |
| loop から `OperatingMode.CALIBRATION` も消す | `ControlTick` の語彙と trace の版に触れる。人の入口が無いので残しても運用で入らない |
| 終了コード 1 を分け、`config_invalid` に新しい番号を付ける | systemd の扱いが同じで、fand の import の閉包を変えて区分 F の更新（R1〜R3）を起こすだけの得が無い |
| D 状態の hang を実機で起こして確かめる | OS ごと hang させる。0161 §2.1 で受け入れ済み |
| `config_invalid` の経路に周期的な書き直しのループを足す | 最小の経路に tick の仕組みを持ち込む。再起動の輪が数秒ごとに書き直している |
| 導入先の `fan-policy.yaml` の §2.14 の値の `status` を今すぐ `confirmed` にする | 導入先の設定の変更と fand の再起動が要るが、動作は変わらない。Authority を上げる前の書き換えとまとめる |

## 5. 判断点（推奨を先頭に）

| # | 判断 | 選択肢 | いつまでに | 状態 |
|---|---|---|---|---|
| 1 | A: `CALIBRATION` | **(推奨) 受理を取り下げる。予約の拒否と loop の語彙は残す** / 受理を実装する / loop の語彙も消す | #74 を閉じる前 | 未決 |
| 2 | B: 終了コード 1 | **(推奨) 分けない。journal の見分け方を文書に書く** / `config_invalid` に新しい番号を付ける | #74 を閉じる前 | 未決 |
| 3 | C: D 状態の hang | **(推奨) 0161 §2.1 の受け入れで閉じ、実機で確かめない。見直す契機を置く** / 実機で確かめるまで開けておく | #74 を閉じる前 | 未決 |
| 4 | D: `config_invalid` の再主張 | **(推奨) 再起動の輪を再主張の仕組みとして決め、unit の 3 つの設定の試験を足す** / fand の中に周期的な書き直しを足す / 決めずに開けておく | #74 を閉じる前 | 未決 |
| 5 | E: Guard の値の `status` | **(推奨) 0119 §2.14 で満たしたとし、文書を直す。`status` の書き換えは Authority を上げる前にまとめる** / 導入先の `status` を今すぐ `confirmed` にする / #74 の要件を満たしていないとして開けておく | #74 を閉じる前 | 未決 |
| 6 | #74 を閉じる条件 | **(推奨) §2.6 の 3 つがそろったら閉じる** / Learned の統合まで開けておく | — | 未決 |
