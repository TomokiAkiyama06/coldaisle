# 決定記録（Decision Records）

設計・運用上の決定はすべてここに集約する。
**ADR と「プロジェクト決定」を分けない。** 1人で開発する規模では、
探す場所が2箇所になる不利益のほうが大きい。

## 命名

```text
docs/decisions/NNNN-<slug>.md      例: 0002-metric-naming.md
```

- **連番。日付ではない。** 同日に複数出ても衝突せず、「0002 で決めたとおり」と参照できる
- **番号は再利用しない。** 取り下げた決定も欠番にせず `Status: Rejected` で残す

## 追記のみ

**既存の決定記録を書き換えない。**
変更が必要なら新しい記録を作り、`Supersedes` で旧記録を指す。
旧記録側への `Superseded by` の追記だけが例外。

「なぜ当時そう決めたか」が消えると、同じ議論を半年後に繰り返す。

決定の内容ではない付随情報（Status の遷移、改名に伴うリンクの追従）の更新は
書き換えに当たらない。

## Status

| 値 | 意味 |
|---|---|
| `Proposed` | レビュー中。PR のマージをもって `FINAL` になる |
| `FINAL` | 有効な決定 |
| `Superseded` | 後続の記録に置き換えられた。`Superseded by` を付ける |
| `Rejected` | 取り下げ。番号は欠番にせず残す |

## テンプレート

```markdown
# 決定記録 NNNN: <題名>

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: YYYY-MM-DD
- **Supersedes**: なし
- **関連**: <参照する要件・レビュー・他の決定記録>
- **対象 Issue**: #N

## 1. Context

何が問題で、なぜ今決める必要があるのか。

## 2. Decision

決めた内容。実装が参照できる具体性で書く。

## 3. Consequences

良くなること / 悪くなること。悪くなることには緩和策を添える。

## 4. 却下した代替案

案と、却下した理由。**ここが最も後から効く。**

## 5. 未決事項

先送りした論点と、どこで決めるか。
```

## 一覧

| 番号 | 内容 | Status |
|---|---|---|
| [0001](0001-initial-project-decisions.md) | プロジェクト初期の決定（D-01〜D-19、V-01） | FINAL |
| [0002](0002-metric-naming.md) | メトリクス命名規約とDBスキーマ（ロング形式） | FINAL |
| [0003](0003-device-json-schema.md) | デバイス出力 JSON スキーマ v1 | FINAL |
| [0004](0004-storage-read-contract.md) | ストレージ層の読み出し契約 | FINAL |
| [0005](0005-model-selection.md) | ローカルモデルは Qwen3.8-27B 単体構成 | FINAL |
| [0006](0006-gpu-mode-and-mixed-state.md) | GPU Mode を2段階にし `mixed` を異常として扱う | FINAL |
| [0007](0007-ingest-pipeline.md) | 取り込みパイプラインの規約 | FINAL |
| [0008](0008-rollup-and-retention.md) | ロールアップ・保持期間・日次CSVの規約 | FINAL |
| [0009](0009-read-api.md) | 読み取り API の契約 | FINAL |
| [0010](0010-csv-replay.md) | CSV 再生の規約 | FINAL |
| [0011](0011-dashboard.md) | 開発用ダッシュボードの方針 | FINAL |
| [0012](0012-rule-engine.md) | ルールエンジンの規約 | FINAL |
| [0013](0013-notifications.md) | 通知の規約 | FINAL |
| [0014](0014-llm-provider.md) | LLM Provider の規約 | FINAL |
| [0015](0015-llm-tools.md) | 読み取り専用ツールの規約 | FINAL |
| [0016](0016-evidence-alerts.md) | アラート説明の Evidence 形式 | FINAL |
| [0017](0017-daily-report.md) | 日次レポートの規約 | FINAL |
| [0018](0018-tool-exposure.md) | AI 向けツールの公開方法 | FINAL |
| [0019](0019-claude-escalation.md) | Claude へのエスカレーション | FINAL |
| [0020](0020-decision-memory.md) | 運用メモリへの記録 | FINAL |
| [0021](0021-public-repo-hygiene.md) | public リポジトリの衛生 | FINAL |
| [0022](0022-firmware-v1.md) | 本番ファームウェア v1 | FINAL |
| [0023](0023-serial-source.md) | シリアル取り込み | FINAL |
| [0024](0024-calibration.md) | 較正の手順と記録 | FINAL |
| [0025](0025-probe-identity.md) | プローブの同定 | FINAL |
| [0026](0026-three-zone-fan-control.md) | Front / Rear / Top を独立Fan zoneとして制御する | FINAL |
| [0027](0027-fan-control-architecture.md) | Fan 制御アーキテクチャ（Supervisor + Learned MPC + Reactive Guard + Critical Safety） | FINAL |
| [0028](0028-fan-control-contracts.md) | Fan 制御の層間契約（入出力・優先順位・状態遷移・周期・故障時の扱い・設定・承認点） | FINAL（§2.7 の正常停止の見分け方は [0080](0080-fand-systemd-unit.md) で置き換え） |
| [0029](0029-telemetry-loss-classes.md) | 制御入力の欠測の分類（Critical / Degraded / Advisory） | FINAL |
| [0030](0030-control-decision-trace-storage.md) | Control decision trace の保存先 | FINAL |
| [0031](0031-thermal-dataset-contract.md) | Thermal Dataset v1 の時刻対応と再生成契約 | FINAL（§2.2 の target に期待時刻より後ろの観測を採りうる部分は、Dataset v2 についてだけ [0087](0087-dataset-v2-action-grid.md)） |
| [0032](0032-internal-telemetry-metric-names.md) | Internal Telemetry のメトリクス名 | FINAL |
| [0033](0033-air-balance-config-boundary.md) | Air Balance characterization の設定境界 | FINAL（§2 の「`uncalibrated` を runtime controller は起動時に拒否する」の一文は [0073](0073-air-balance-control-config-integration.md)） |
| [0034](0034-unavailable-fan-tach-safety.md) | 制御対象 Fan の tach 読み取り不能を Safety fault にする | FINAL |
| [0036](0036-transient-cpu-gpu-regime.md) | Workload Regime に TRANSIENT_CPU_GPU を加える | FINAL |
| [0037](0037-model-registry-rollback-target.md) | Model Registry の promotion 時の rollback target | FINAL |
| [0038](0038-internal-telemetry-outage-counting.md) | Internal Telemetry の欠測の数え方（有効な間の停止は欠測、無効期間は数えない） | FINAL |
| [0039](0039-dashboard-labels-and-catalog.md) | ダッシュボードの表示名と `GET /api/v1/metrics` | FINAL |
| [0040](0040-server-health-api.md) | Server Health API の契約（`/server-health` への一本化、signal 規則、機種が公開しない metric の扱い） | FINAL |
| [0041](0041-supervisor-proposal-freshness.md) | 非同期 RL Supervisor 提案の有効性（期限の起点と Regime の一致） | FINAL |
| [0042](0042-server-health-signal-rules.md) | Server Health の signal 判定規則の詳細（パネルは表示専用、監視対象と quality・source 状態の導出、アラート全件、1スナップショット） | FINAL |
| [0043](0043-cpu-die-and-gpu-throttle-metrics.md) | CPU die 温度（k10temp）と GPU の T.Limit margin・throttle reason・fan speed のメトリクス名 | FINAL |
| [0044](0044-node-in-ci-for-dashboard-tests.md) | ダッシュボードの JS テストのために CI へ Node を入れる（テスト専用。ビルドには使わない） | FINAL |
| [0045](0045-local-socket-write-entry.md) | 書き込み専用のローカル Unix ソケット入口（GPU Mode イベント / Workload Hint） | FINAL |
| [0046](0046-airflow-ui.md) | エアフロー / ファン制御の可視化画面（置き場所・模擬データの分離・色分けの設定） | FINAL |
| [0047](0047-cpu-utilization-metric.md) | CPU 使用率のメトリクス名（`cpu.utilization`） | FINAL |
| [0048](0048-thermal-model-artifact-and-inference.md) | Thermal Model v1 artifactと読み取り専用推論境界 | FINAL |
| [0049](0049-internal-telemetry-source-kind.md) | 内部テレメトリの出どころの種類（`sys.telemetry_kind`: hardware / mock）を記録し、いまの値にだけ「実測」と書く | FINAL |
| [0050](0050-model-confidence-ood-and-authority.md) | Model Confidence / OOD の判定方式と confidence に応じた Authority 制限 | FINAL（§2.1 のうち Profile v2 の束縛の対象を model payload の SHA-256 にする部分は [0079](0079-model-artifact-formats.md)。Profile v2 の §2.1 の residual の基準の母集団と §2.2 の `support` の OOD の条件は [0084](0084-model-artifact-anchor-support.md)） |
| [0051](0051-airflow-cpu-utilization-display.md) | エアフロー画面の CPU 使用率の表示（`cpu.utilization` と「未計測」の判断） | FINAL |
| [0052](0052-learned-mpc-optimizer-and-hard-constraints.md) | Learned MPC optimizer の内部モデル要件（反実仮想 capability）と Hard Constraints の扱い | FINAL（§2.1 の `artifact_sha256` の生成時照合を artifact v2 で封をした型の保証へ替える部分は [0079](0079-model-artifact-formats.md)） |
| [0053](0053-control-shadow-mode-and-counterfactual-logging.md) | Control Shadow Mode の記録内容（counterfactual の置き場所・予測と実測の突き合わせ・export） | FINAL |
| [0054](0054-offline-evaluation-attribution-and-gates.md) | Offline Evaluation の帰属規則（適用と counterfactual を分ける）・coverage の扱い・rollout gate | FINAL |
| [0055](0055-shadow-duplicate-observation-rule.md) | Shadow の照合は同じ metric・同じ時刻の食い違う観測を受け取らない（同じ値の重複は1つに畳む） | FINAL |
| [0056](0056-model-drift-detection-and-retraining-triggers.md) | Thermal Model の drift 検知の置き場所（runtime は 0050 のまま）・証拠の規則・再学習の条件（0053 §2.3 の記録内容を1点だけ拡張） | FINAL |
| [0057](0057-authority-rollout-stage-changes.md) | Authority Rollout の stage 変更（人の承認・証拠の束縛・自動降格・設定は上限） | FINAL（§3 の帰結1項と §5 の未決1項は [0059](0059-decision-trace-model-artifact.md)） |
| [0058](0058-rl-supervisor-training-environment.md) | RL Supervisor 学習環境の責務（action は戦略まで・dynamics の出どころ・Safety 違反は terminal） | FINAL |
| [0059](0059-decision-trace-model-artifact.md) | decision trace が tick ごとに model artifact を記録する（適用側の証拠を artifact へ束縛し、LIMITED 以降の昇格を通す） | FINAL（§2.1 の照合対象と §2.2 の入れ子の版は [0065](0065-assessment-identity-and-nested-gate-version.md)） |
| [0060](0060-control-loop-runtime.md) | Control Loop の実行時契約（`tick_ms` は safety.yaml・Telemetry はストア経由・モードは読み取り port・deadman の配線・実行の記録を trace へ） | FINAL（§2.7 の一部は [0080](0080-fand-systemd-unit.md) で置き換え） |
| [0061](0061-rl-supervisor-policy-artifact-and-binding.md) | RL Supervisor policy の artifact 形式（全 regime の表・Demand を表現できない）・束縛（active は開かない門・shadow 用の提案は active slot を通らない）・shadow 集計の規律 | FINAL |
| [0062](0062-model-registry-operations.md) | Model Registry の運用入口（CLI）と起動時検証、lifecycle 監査の追跡 | FINAL |
| [0063](0063-compute-mode-advisory.md) | Compute Mode 切替時の環境条件アドバイザリ（助言のみ・実測フルロードとの比較・欠けた材料の明示） | FINAL |
| [0064](0064-workload-hint-entry-and-supervisor-prior.md) | Workload Hint の入口（0045 のソケットを再利用）・形と版・期限の数え方・矛盾時は Telemetry 優先・trace への出し方・実装の段階 | FINAL |
| [0065](0065-assessment-identity-and-nested-gate-version.md) | assessment の identity に model version を含め、入れ子の `ModelGateDecision` に版を持たせる（0059 §2.1 / §2.2 の締め直し） | FINAL |
| [0066](0066-workload-hint-stage-b-conditions.md) | Workload Hint の Stage B 条件の精緻化（冷却の単調性は構成制限＋網羅試験＋実行時の二重解法で保証・試験の成果物はソースと実行環境に束縛・ヒントの期限は boot id と CLOCK_BOOTTIME で数え、行は追記順に処理・終端状態と取り込み境界を制御デーモンが永続化） | FINAL |
| [0067](0067-acoustic-measurement-study.md) | 騒音は「うるさいか」の回答で測る（騒音計を使わない・UI は表示専用のまま回答は 0045 の入口へ・回答は Fan を変えない・effective の Demand と結びつける・曲線は `approximate` のまま） | FINAL |
| [0068](0068-dashboard-status-strip-and-fault-cards.md) | ダッシュボードの状態の帯（`server-health` の signal を描く）と、異常なカードの見た目（stale の取り消し線・欠測の斜線＋赤枠） | FINAL |
| [0069](0069-ubuntu-deploy-templates.md) | Ubuntu 常駐化のテンプレート（固定名 `/dev/server-sensors` を名前の順より先に選ぶ・systemd / udev は `deploy/` に仮の値で置く・`coldaisle-fand` の unit は含めない） | FINAL |
| [0070](0070-soak-acceptance-interpretation.md) | 連続運転テスト（soak）の受入基準の読み方（欠測率は最も悪いチャネルで判定・母数は期間÷送信周期で周期不明なら判定不能・再起動はすべて意図しないものとみなす・DB を読み取り専用で開く） | FINAL |
| [0071](0071-control-trace-read-api.md) | decision trace の読み取り API（`/api/v1/control/latest` と `/control/traces`・記録した順の `seq` によるページングと保持期間の境界の明示・本文は保存した JSON のまま版の解釈は読む側・registry の版を毎 tick 載せる・AI ツールに足さない。0030 §2 / §5 の1項目めの承認が前提） | FINAL（§2.5 のうち `reason` の全文を毎 tick 載せる部分は [0075](0075-trace-registry-block-reason-digest.md)） |
| [0072](0072-control-admin-entry.md) | 制御デーモンの管理操作の入口（`coldaisle-fand` の専用ソケットでモード設定と authority の降格だけを受ける・昇格は CLI・同じ uid と root を暗黙に認めない・次の tick で反映・journal は毎 tick の stat で読み直す・監査は追記専用） | FINAL（§2.3 の受理の応答の形は [0076](0076-implementation-settled-points.md) §2.4 で `superseded_by` を追加。§2.2 の死後の authority の枠は [0081](0081-drain-authority-on-receiver-death.md)） |
| [0073](0073-air-balance-control-config-integration.md) | `air-balance.yaml` を4つ目の Control Config として一括検証に統合する（束ねた版 11・不在は `config_invalid`・未校正は Air Balance を無効にして起動・`ControlTick` v10 に applied demand 基準の `estimated_flow` と Air Balance の記録・trace に4ファイルの版と hash・昇格の証拠を `air-balance.yaml` に束縛） | FINAL（実装では `ControlTick` v11。版番号の読み替えは [0076](0076-implementation-settled-points.md) §2.1） |
| [0074](0074-supervisor-shadow-wiring-and-episode-evaluation.md) | 運転中の Supervisor decision を Shadow 台帳へ流す配線（制御プロセスの外の CLI が保存済み trace から集計・鍵は `(ts_ms, tick_id)`）と、RL episode 結果を別の report 型 `PolicyEpisodeReport` の `episode:` arm として出す接続（`for_active` の条件は範囲外） | FINAL |
| [0075](0075-trace-registry-block-reason-digest.md) | decision trace の registry の塊では自由記述の `reason` を全文でなく `reason_sha256`（UTF-8 の SHA-256）にする（毎 tick の保存量を抑える・全文は registry の audit が正本・`previous_artifact` / `rollback_target` の長さも閉じる。0071 §2.5 の一部を置き換え） | FINAL |
| [0076](0076-implementation-settled-points.md) | 0071 / 0072 / 0073 の実装で決着した点（Air Balance の記録は `ControlTick` v11 で入った＝0073 の「v10」の読み替え・監査の表 `control_admin_audit` の DDL と起動ごとの `run_id`・`ControlTick` v12 の `mode_command` と `admin_receiver_dead`・`control-admin.yaml` の暫定値・応答の `superseded_by`（0072 §2.3 の一部を置き換え）・受付スレッドの死は Safety では `manual_max` のまま・出荷設定は同じ uid を認めない・`accept()` の失敗は待ち受けだけを上限付きで休み、`escalate_after_ms` 続いたら受付スレッドの死として `MAX`・`control-admin.yaml` は版 2（v1 は拒否）・画面の古さの倍数 3.0 を確定） | FINAL（§2.2 (a) の `superseded_by` の条件は [0082](0082-authority-wiring-settled-points.md) で緩和） |
| [0077](0077-learned-proposal-handoff.md) | Learned MPC / RL Supervisor の worker から制御ループへの提案の受け渡し（役割ごとの別 unit・`coldaisle-fand` が持つ役割ごとの `SOCK_SEQPACKET` ソケットと役割ごとのグループ・受付スレッド・worker の heartbeat・worker の入力は毎 tick の frame だけ・`run_id` と元 snapshot への束縛・起動時に1回読む registry から provenance と Gate の期待値を作る・worker の異常はすべて Fallback で Max にしない・authority の昇格で worker は束縛を作り直す） | FINAL（2026-09-30、所有者が推奨案で承認。§2.3 の `applied` の出どころは [0092](0092-learned-frame-applied-from-hardware-result.md)） |
| [0078](0078-air-balance-fallback-coordination.md) | Air Balance の `coordinate()` を Fallback / Baseline の requested に掛ける（Fallback の直後・Gate の前・合成の前、`fan-policy.yaml` の `mode: off / shadow / apply` で人が開く・上げるだけで zone ごとの `max_raise` と下げる前の保持・`uncalibrated` とは組めない・CPU Telemetry の stale による Top の `forced_max` は 1.0 と見積もり Top の見積もりは合成の下限（`ramp_down` を含む）込み・Fan fault と確定前の tach 無応答（`tach_unconfirmed_zones`。Top を含む）の tick は協調しない・`shadow` も保持を模擬して `counterfactual_output` を記録・`apply` の失敗は 0028 §2.7 どおり `fallback_exception`（`shadow` は記録のみ）・昇格の証拠を `fan-policy.yaml` の trace に束縛・trace に `candidate` / `proposed` / `output` と適用した `max_raise`・最初の apply の Top は `max_raise` 0・値は #75 の後） | FINAL（§2.2 / §2.6 の `apply` の失敗の tick も Gate を通す点と §2.7 の `output` の欄の定義は [0085](0085-coordination-failure-bypasses-gate.md) で置き換え） |
| [0079](0079-model-artifact-formats.md) | Confidence Profile を反実仮想 Thermal Model artifact v2 に同梱して組で昇格・rollback する・artifact v2 の形式（action 列の格子・学習データの時間窓・metric と単位と較正の束縛・4層の digest）・Registry の検証経路だけが作る封をした型と読み込み時検査 L1〜L10・学習した action 列の外（margin なし）は探索しない・失敗は Fallback（暗黙の降格なし）・anchor から最初の step への遷移も照合・Registry の健全性の通知は開いたまま（0050 §2.1 / 0052 §2.1 の Profile の束縛の一部を置き換える。0050 §2.2 の推論ごとの照合は残す） | FINAL（§2.1 / §2.5 の全 step でまとめた support と §2.4 の L8 は [0084](0084-model-artifact-anchor-support.md)。§2.9 の段 1 の action 列の範囲は [0087](0087-dataset-v2-action-grid.md)。§2.3 の較正の digest を渡す主体は [0096](0096-calibration-binding-digest.md)） |
| [0080](0080-fand-systemd-unit.md) | `coldaisle-fand` の systemd unit（`Type=notify` と `NotifyAccess=main`・`WatchdogSec` は `safety.yaml` を写し長ければ起動しない・`Restart=always` と諦めない再起動・終了コード 3 / 4 だけ再起動しない・`ExecStopPost` は root の引き継ぎ実行部をソースから直接・正常停止は記録の削除で伝える・専用の非 root ユーザーと udev で hwmon の属性だけ書ける・`ProtectKernelTunables` は使わない・`/run/coldaisle` は fand だけが持つ・取り込みへは `After=` + `Wants=` だけ・`/var/lib/coldaisle` を 2770 と `UMask=0007` でグループ共有・fand は管理ソケットのグループにも入る。0060 §2.7 と 0028 §2.7 の一部を置き換え） | FINAL（§2.4 の運転中の `WATCHDOG=1` の送信の失敗は [0083](0083-fand-exit-settled-points.md) で読み替え。§2.1 / §2.2 の `authority.json` の置き場所と unit の3つの値は [0086](0086-authority-approver-uid.md) で置き換え） |
| [0081](0081-drain-authority-on-receiver-death.md) | 受付スレッドの死を検知したら authority の枠だけを取り出せるまで非ブロッキングで試し、受理済みの降格を journal へ書き残す（モードの枠は読まない・`MAX` は再起動まで保つ。0072 §2.2 の一部を置き換え） | FINAL |
| [0082](0082-authority-wiring-settled-points.md) | #92 段階 2（authority の配線と管理ソケットでの降格）の実装で決着した点（journal は v3 へ常に書き直す・`--authority-root` と lock の待ち上限は `tick_deadline_ms`・起動時の壊れた journal は終了コード 5・`superseded_by` の CHECK を `<>` に緩める（0076 §2.2 (a) の一部を置き換え）・`lower_authority` は `full` を拒否・書けなかった降格は予約として heartbeat の後に書き直す・trace の `authority` は Gate が読んだ時点・履歴を延長しない journal は `journal_unreadable`・人の event が残らないときは監査の表だけ） | FINAL |
| [0083](0083-fand-exit-settled-points.md) | 0080 段階 2 の実装で決着した点（終了コード 7 では受け渡し口に残った降格を lock を待たずに1回 journal へ書く・運転中の `WATCHDOG=1` の送信の失敗は捕まえて運転を続け止めるのは deadman に任せる（0080 §2.4 の一部を置き換え）・`CONTROL_CONFIG_VERSION` 12・書き込みの失敗の期間の数え方・DB の確認は `-journal` も対象・`hardware_write_fail_exit_ms` は暫定） | FINAL |
| [0084](0084-model-artifact-anchor-support.md) | 反実仮想 Thermal Model artifact v2 の 0079 の後に残った3点（anchor 推論の計画 action の列は現在の effective demand を horizon の間保つ `hold_effective` で、Profile の residual の基準と評価も同じ規則・held の列も step ごとの support で照合し外れれば `support` の OOD で Fallback・residual の基準からも support の外の validation example を除き、残りが無い出力があれば Profile を作らない・L11 で manifest と Profile の規則の一致を検査・support は step の番号ごと、遷移は対応する step の組ごとに持ち外れた候補は評価しない（margin なし）・L12 で step の欄の数を格子と照合・L8 で `metric_binding` のキー集合が feature と target の metric の和集合と一致することを検査。0079 §2.1 / §2.5 / §2.4 L8 と、Profile v2 について 0050 §2.1 / §2.2 の一部を置き換え） | FINAL |
| [0085](0085-coordination-failure-bypasses-gate.md) | `mode: apply` で Air Balance の協調が失敗した tick は Gate の失敗と同じく Controller Gate を迂回して raw baseline を選ぶ（Learned MPC はその tick に選ばれない・`fallback_exception` と次 tick の `EMERGENCY` は 0078 のまま・`shadow` の失敗は迂回しない・`fallback_reason` は `air_balance_coordination_failed`・不具合が続けば `fault_clear_hold_ms` ごとの解除と再試行を繰り返す（再起動まで latch しない。0028 §2.5 のまま）・trace の不変条件と試験は 0078 段 3 で。迂回した tick の trace の `output` は `candidate`・0078 §2.2 / §2.6 / §2.7 の一部を置き換え） | FINAL |
| [0086](0086-authority-approver-uid.md) | Authority の昇格の承認者を CLI を実行した人の uid（`uid.<数値>`）に結びつける（`os.getuid()` で `geteuid` と違えば拒み `SUDO_UID` を読まない・承認者のグループと authority 専用の 2770 のディレクトリへ移す・root と fand の実行ユーザーは承認できない・store は `0660` で作り CLI はディレクトリを作らない・承認ファイルに承認者を書かせない・rollback は uid を記録し誰も拒まない・journal v4 の `approver_binding`・名前は表示のときだけ。0080 §2.1 / §2.2 の一部を置き換える。PR #209 の unit テンプレートを合わせる） | FINAL |
| [0087](0087-dataset-v2-action-grid.md) | Thermal Dataset v2 の action 列を格子へ写す規則（step `k` は `anchor + k × step_ms` 以前で直近の ControlTick の effective を as-of で取り元の tick を記録・既定値の無い `action_stale_after_ms` を超える step があれば example を作らない（値は実データの後）・horizon は `step_ms` の整数倍で `steps × step_ms` は最大の horizon・step `k` は区間 `[k, k + 1)` で `offset_ms` はその終端・較正の変更の検査は合成の起点で行い `control/model` には時刻の列だけ・v2 の anchor action は anchor の tick より厳密に前の直近の ControlTick の effective（`prior_action`。鮮度も検査）・鮮度で除いた件数を manifest に記録・`ControlTick` の版は上げない・`seq` の順で `ts_ms` が単調でない run と移行前の行を含む run からは生成しない（v1 は開いたまま）・`prior_action` の tick から最大の horizon まで tick の連続を求める・step の区間の中で値が変わった example は作らず除外件数を理由ごとに記録・v2 の target は期待時刻以前の観測だけ（0031 §2.2 を v2 について置き換え）・`prior_action` の tick から最大の horizon まで `tick_id` がちょうど 1 ずつ増えることを求め、減少・同値は「再起動」、欠番は「欠番」として除外件数に記録・移行前の行は `seq ≤ legacy_through_seq` で見分ける（`control_trace_prune` に足す migration は段 1 の PR に同梱。v2 について `ts_ms ≤ legacy_until_ms` の読み方を置き換え）・この検査は最大の horizon より後の最初の tick（`tick_id` が `n + 1`。値は使わない）まで延ばし、それが無い run の末尾の example は「連続」で除く。0079 §2.9 の段 1 の範囲の文言と、v2 について 0031 §2.2 の一部を置き換え） | FINAL |
| [0088](0088-air-balance-wiring-settled-points.md) | 0078 段 3（Air Balance の協調の loop への配線。PR #214）の実装で決着した点（Controller Gate 自身の例外の tick の requested は Gate へ渡すはずだった Baseline で、`apply` で上げた tick なら coordinated baseline（0085 の迂回は raw baseline のまま）・`skipped` の tick の trace は `projected_floors` / `proposed` 等を `null`・`held` は `null` を許さず3 zone とも偽・`max_raise` は記録し、`failed` は渡した `projected_floors` を記録・0078 §2.3 の表は上から順に判定し、上の行（`air_balance_disabled`〜`baseline_unavailable`）がすべて満たされた tick では、Top の Fan fault は無条件の `EMERGENCY` なので `skip_reason` は `safety_state`（確定前の tach 無応答は Top でも `tach_unconfirmed`）。0078 / 0085 は置き換えない） | FINAL |
| [0089](0089-authority-bound-to-loaded-artifact.md) | `coldaisle-fand` は、いま使っている artifact（Gate の `expected_artifact_sha256` と同じ出どころ）に対して承認された分だけ authority を有効にする（journal が最後に Baseline にいた後の昇格すべての `evidence.artifact_sha256` が loaded artifact と一致しなければ実効 stage の上限を Baseline に・artifact を持たない構成は Baseline より上を有効にしない・journal は書かない・照合は journal を読み直すたび・構造化ログと `trace_metadata()` に出し `AuthorityRecord` は版を上げない（理由の欄は trace の次の版上げで `AuthorityRecord` v2 に足す）・部分一致も Baseline・Production の入れ替え後は新しい artifact について SHADOW から上げ直す。0057 / 0072 / 0086 は置き換えない） | FINAL |
| [0090](0090-authority-bound-to-loaded-config.md) | `coldaisle-fand` は、起動時に読んだ Control Config の4ファイル（`ControlConfig.sources` のファイルごとの SHA-256）に対して承認された分だけ authority を有効にする（journal が最後に Baseline にいた後の昇格すべての証拠の4つの config hash が一致しなければ実効 stage の上限を Baseline に・証拠に hash の無い v1 の昇格も Baseline・部分一致も Baseline・journal は書かない・照合は 0089 と同じ時点・構造化ログ（`authority_config_mismatch` / `_matched`）と `trace_metadata()` の `authority_config_binding_ceiling` に出し `AuthorityRecord` は版を上げない・設定を差し替えたら新しい設定について SHADOW から上げ直す。0089 は置き換えない） | FINAL |
| [0091](0091-authority-audit-sink-failure-exit-code.md) | `coldaisle-authority` は監査ログ（stderr の JSONL）を書けなかったことを握りつぶさない（`StreamHandler` を継承して `handleError` / `flush` の失敗を記録する handler を `coldaisle-authority` だけが使う・`ensure_ascii` は保つ・journal は確定したが監査の記録に失敗したら終了コード 6・stdout の結果に `audit_logged` を常に出す・fsync の失敗（5）と重なったら 5 を優先・行われなかった失敗（1/3/4）は従来のまま・raise / rollback（no-op を含む）を分けない・6 では操作をやり直さず journal で確かめる。0086 は置き換えない） | FINAL |
| [0092](0092-learned-frame-applied-from-hardware-result.md) | worker へ渡す frame の `applied` は各 zone の `FanHardwareResult.applied_demand` から作り、結果が無い・確かめられない（起動時・最低安定の写像・書き込みや readback の失敗）ときは欠測のまま渡す（推測で埋めない。0077 §2.3 の表の `applied` の行を置き換え） | FINAL |
| [0093](0093-air-balance-shadow-summary-and-baseline-arm.md) | 0078 §5 の2点の決着（Air Balance の協調の shadow 集計は新しい CLI `coldaisle-air-balance-shadow` で保存済み trace を読むだけ・`status` / `skip_reason` ごとの件数・zone ごとの引き上げ幅 `counterfactual_output - candidate` の分布・引き上げの有無の切り替わりの回数だけを出し合否は出さない・Top の Fan fault は `safety_state` と同じ tick の `faults` を合わせて数える（0088 §3）・#91 の offline 評価には足さない／offline 評価の Baseline の arm は評価器で再計算せず trace の値を読むだけ・arm の鍵に `mode` を足さない（PR #220 の束縛のため）。0078 は置き換えない） | FINAL |
| [0094](0094-dataset-v2-settled-points.md) | 0079 段 1 / 0087（Thermal Dataset v2。PR #221）の実装で決着した点（anchor 候補が action の規則ですべて除外されたら拒否せず `example_count = 0` と除外件数を返す（候補自体が無ければ v1 と同じく拒否）・window / target が run に収まらない端の tick は除外件数に数えない・`legacy_through_seq` を下げさせない trigger は入れない・`coldaisle-dataset` の v2 対応は別 PR・migration は追記のみで既存の行と `legacy_until_ms` と読み取り API の不変と冪等を試験・読み込み時に同じ元の tick の `tick_id` と値を dataset 全体で照合し `prior_action` の `tick_id` は anchor − 1 で step の元の `tick_id` は時刻の順に進む・同じ anchor の複製と window 内の同一観測の食い違いは v1 / v2 共通の loader の強化として #224。0079 / 0087 は置き換えない） | FINAL |
| [0095](0095-learned-channel-stage1-settled-points.md) | 0077 段階 1（`coldaisle-fand` 側の Learned worker の経路。PR #222）の実装で決着した点（frame の `baseline` は raw baseline・`ECONNABORTED` 以外の `accept()` の失敗は受付スレッドの死・「接続数の上限」は `listen_backlog` で同時接続は役割ごとに1つ・`hello` に `run_id` も載せる・段階 1 の Supervisor のソケットは接続と heartbeat と frame の受信まで・`--learned-channel-config` を省くと経路を配線しない・ソケットの親ディレクトリは役割ごとに分ける。0077 / 0092 は置き換えない） | FINAL |
| [0096](0096-calibration-binding-digest.md) | 反実仮想 Thermal Model artifact v2 の較正の digest の計算方法（`metric_binding` の metric を派生値は minuend / subtrahend へ展開し、`METRIC_TO_CHANNEL` で対応し較正を当てる metric だけに実効の offset（無いチャネルは 0.0・`-0.0` は 0.0・丸めない）を対応させ `{metric: offset}` を既存の canonical JSON（キー順・区切り・末尾改行）で SHA-256・対応する metric が無ければ `null`・`note` / `calibrated_at` 等は入れない・trainer と loader は digest ではなく較正の値を受け取り同じ純粋関数で計算し L9 で照合・較正を変えたら作り直しと SHADOW からの昇格のやり直し・0090 の束縛には足さない・`unavailable` の較正と `null` の申告の照合・規則を変えるときは `schema_version` を上げる・較正の変更の記録先は別の記録（予定 0099）。0079 §2.3 の一部を置き換え） | FINAL（§5 #4 の記録の書き手（`--apply`）は [0099](0099-calibration-change-log.md) で置き換え） |
| [0097](0097-artifact-profile-v2-settled-points.md) | 0079 段 2 / 段 3（artifact v2・Confidence Profile v2。PR #226 / #230）の実装で決着した点（Profile v2 の形と読み込み時の検査は段 2・生成と判定器と step ごとの support の照合関数は段 3・Profile の欄は `StepActionSupport` / `StepTransitionSupport` / `AnchorTransitionSupport` にまとめる・`action_variation_summary` は zone ごとに train の「`prior_action` → step 0」と「step k → k+1」の組の数と変化した組の数・L11 は版の照合（L4 の前半）の後・schema 全体の読み込みの前に生の JSON で見分ける・loader は authority stage を受け取らない（MPC 側が attestation で照合。L2 の入れ子と token 数は Registry の `VerifiedArtifact` 発行前の検査に依る）・`MAX_ACTION_STEPS` は `MAX_FEATURE_COLUMNS // 3` から導く・較正 digest の計算方法は 0096 に委ねる・support の OOD の detail は最初に外れた1点だけ（0084 §2.1 の列挙順）・`residual_validation_example_count` は除外後に基準へ使った validation example の数・0084 §5 #2 の合計の上限は数値を足さず実機 dataset で測ってから（現状は 8 MiB 上限で登録時に拒否）・v2 の fan range と v1 の support cell の anchor は `prior_action`・drift の `coverage()` に held の support を含めるかは未決。0079 / 0084 / 0087 は置き換えない） | FINAL |
| [0098](0098-registry-binding-settled-points.md) | 0077 段階 2（`coldaisle-fand` の registry の束縛。PR #232）の実装で決着した点（fand は 0062 §2.4 の `verify()` を呼ばず `inspect()` の1つの snapshot から期待値を作る・閉じた役割の接続は残し届いたメッセージは検証の前に捨てる（idle で切られうる）・root が無いときは revision 0 の空の registry として読む・frame の `expected_artifacts` と監視は `thermal_model` / `supervisor_policy` だけ・RL の識別を作れない版は RL を採らない・production の監視は Learned の経路が開いているときだけ。0077 / 0062 / 0089 は置き換えない） | FINAL |
| [0099](0099-calibration-change-log.md) | 較正の変更の記録先（取り込みが書く SQLite DB の追記のみの表 `calibration_activations`・書き手は `coldaisle-daemon`（serial / mock）で起動時・ソースを読む前に、全温度 metric の実効の offset の写像（0096 の canonical JSON）が変わったときだけ1行・`--apply` は書かない・時刻は保存済みの最後の値より厳密に後・`row_sha256` の鎖と trigger で追記のみ・Dataset v2 と学習の入口は本番の DB を読み取り専用で必ず読み、被覆が無い・壊れている・期間の中や後に変更がある・較正ファイルと最後の行が食い違うときは拒否・CSV の秒の切り捨ては区間で扱う・運転中に時計が戻ったら最後の記録より前の sample は捨てて数える・DB ごとに取り込みを1つに限る lock を取ってから記録・再生の timezone と入力の DB の束縛は #237・過去の履歴は埋めない） | FINAL |
| [0102](0102-calibration-change-log-settled-points.md) | 0099（較正の変更の記録先。PR #240）の実装で決着した点（書き手の排他は DB の実体の path の隣の `<db>.ingest.lock` への `flock`・hard link の別名がある DB では起動しない・DB ファイルだけの bind mount の別名は検出できないので配置しない・運転中に捨てた sample の件数はログにだけ残す・学習の入口と v2 の CLI の配線は `coldaisle-dataset` の v2 対応の PR・時刻の下限は sample だけ（hello には掛けない）・起動を拒否したら終了コード 1。0099 は置き換えない） | FINAL |
