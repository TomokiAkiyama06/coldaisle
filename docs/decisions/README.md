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
| [0031](0031-thermal-dataset-contract.md) | Thermal Dataset v1 の時刻対応と再生成契約 | FINAL |
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
| [0050](0050-model-confidence-ood-and-authority.md) | Model Confidence / OOD の判定方式と confidence に応じた Authority 制限 | FINAL（§2.1 のうち Profile v2 の束縛の対象を model payload の SHA-256 にする部分は [0079](0079-model-artifact-formats.md)） |
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
| [0077](0077-learned-proposal-handoff.md) | Learned MPC / RL Supervisor の worker から制御ループへの提案の受け渡し（役割ごとの別 unit・`coldaisle-fand` が持つ役割ごとの `SOCK_SEQPACKET` ソケットと役割ごとのグループ・受付スレッド・worker の heartbeat・worker の入力は毎 tick の frame だけ・`run_id` と元 snapshot への束縛・起動時に1回読む registry から provenance と Gate の期待値を作る・worker の異常はすべて Fallback で Max にしない・authority の昇格で worker は束縛を作り直す） | FINAL（2026-09-30、所有者が推奨案で承認） |
| [0078](0078-air-balance-fallback-coordination.md) | Air Balance の `coordinate()` を Fallback / Baseline の requested に掛ける（Fallback の直後・Gate の前・合成の前、`fan-policy.yaml` の `mode: off / shadow / apply` で人が開く・上げるだけで zone ごとの `max_raise` と下げる前の保持・`uncalibrated` とは組めない・CPU Telemetry の stale による Top の `forced_max` は 1.0 と見積もり Top の見積もりは合成の下限（`ramp_down` を含む）込み・Fan fault と確定前の tach 無応答（`tach_unconfirmed_zones`。Top を含む）の tick は協調しない・`shadow` も保持を模擬して `counterfactual_output` を記録・`apply` の失敗は 0028 §2.7 どおり `fallback_exception`（`shadow` は記録のみ）・昇格の証拠を `fan-policy.yaml` の trace に束縛・trace に `candidate` / `proposed` / `output` と適用した `max_raise`・最初の apply の Top は `max_raise` 0・値は #75 の後） | FINAL（§2.2 / §2.6 の `apply` の失敗の tick も Gate を通す点は [0085](0085-coordination-failure-bypasses-gate.md) で置き換え） |
| [0079](0079-model-artifact-formats.md) | Confidence Profile を反実仮想 Thermal Model artifact v2 に同梱して組で昇格・rollback する・artifact v2 の形式（action 列の格子・学習データの時間窓・metric と単位と較正の束縛・4層の digest）・Registry の検証経路だけが作る封をした型と読み込み時検査 L1〜L10・学習した action 列の外（margin なし）は探索しない・失敗は Fallback（暗黙の降格なし）・anchor から最初の step への遷移も照合・Registry の健全性の通知は開いたまま（0050 §2.1 / 0052 §2.1 の Profile の束縛の一部を置き換える。0050 §2.2 の推論ごとの照合は残す） | FINAL |
| [0080](0080-fand-systemd-unit.md) | `coldaisle-fand` の systemd unit（`Type=notify` と `NotifyAccess=main`・`WatchdogSec` は `safety.yaml` を写し長ければ起動しない・`Restart=always` と諦めない再起動・終了コード 3 / 4 だけ再起動しない・`ExecStopPost` は root の引き継ぎ実行部をソースから直接・正常停止は記録の削除で伝える・専用の非 root ユーザーと udev で hwmon の属性だけ書ける・`ProtectKernelTunables` は使わない・`/run/coldaisle` は fand だけが持つ・取り込みへは `After=` + `Wants=` だけ・`/var/lib/coldaisle` を 2770 と `UMask=0007` でグループ共有・fand は管理ソケットのグループにも入る。0060 §2.7 と 0028 §2.7 の一部を置き換え） | FINAL（§2.4 の運転中の `WATCHDOG=1` の送信の失敗は [0083](0083-fand-exit-settled-points.md) で読み替え） |
| [0081](0081-drain-authority-on-receiver-death.md) | 受付スレッドの死を検知したら authority の枠だけを取り出せるまで非ブロッキングで試し、受理済みの降格を journal へ書き残す（モードの枠は読まない・`MAX` は再起動まで保つ。0072 §2.2 の一部を置き換え） | FINAL |
| [0082](0082-authority-wiring-settled-points.md) | #92 段階 2（authority の配線と管理ソケットでの降格）の実装で決着した点（journal は v3 へ常に書き直す・`--authority-root` と lock の待ち上限は `tick_deadline_ms`・起動時の壊れた journal は終了コード 5・`superseded_by` の CHECK を `<>` に緩める（0076 §2.2 (a) の一部を置き換え）・`lower_authority` は `full` を拒否・書けなかった降格は予約として heartbeat の後に書き直す・trace の `authority` は Gate が読んだ時点・履歴を延長しない journal は `journal_unreadable`・人の event が残らないときは監査の表だけ） | FINAL |
| [0083](0083-fand-exit-settled-points.md) | 0080 段階 2 の実装で決着した点（終了コード 7 では受け渡し口に残った降格を lock を待たずに1回 journal へ書く・運転中の `WATCHDOG=1` の送信の失敗は捕まえて運転を続け止めるのは deadman に任せる（0080 §2.4 の一部を置き換え）・`CONTROL_CONFIG_VERSION` 12・書き込みの失敗の期間の数え方・DB の確認は `-journal` も対象・`hardware_write_fail_exit_ms` は暫定） | FINAL |
| [0085](0085-coordination-failure-bypasses-gate.md) | `mode: apply` で Air Balance の協調が失敗した tick は Gate の失敗と同じく Controller Gate を迂回して raw baseline を選ぶ（Learned MPC はその tick に選ばれない・`fallback_exception` と次 tick の `EMERGENCY` は 0078 のまま・`shadow` の失敗は迂回しない・`fallback_reason` は `air_balance_coordination_failed`・不具合が続けば `fault_clear_hold_ms` ごとの解除と再試行を繰り返す（再起動まで latch しない。0028 §2.5 のまま）・trace の不変条件と試験は 0078 段 3 で。0078 §2.2 / §2.6 の一部を置き換え） | FINAL |
