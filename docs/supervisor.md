# Supervisor Interface

Supervisor はFan Demandを決めず、Learned MPCが任意contextとして読める運転戦略を返す。
出力schemaは `strategy`、多目的最適化の `weights`、CPU/GPU temperature `target_band`、
観測済みWorkload Regime、policy artifact version、計算壁時計だけを持つ。Demand、PWM、hwmon、
Reactive GuardやCritical Safetyの上下限を表す欄はない。

初期運用は `RulePolicy active + RLPolicy shadow` とする。両policyは同じimmutable
`SupervisorInput`（現在snapshot、recent history、Workload Regime、recent control performance）を使い、
active / shadowの成功または構造化された失敗を1つの`SupervisorDecision`へ記録する。
shadow出力は`selected_output`にならない。

RLPolicyは制御loop外のworkerで動かす（決定記録 0028 §2.2）。制御loopはworkerを呼び出さず、
受信済みoutputと、制御loop自身の単調時計で記録した元snapshotの時刻（`source_monotonic_ms`）・
受信時刻（`received_monotonic_ms`）だけを`SupervisorCoordinator`へ渡す。推論は非同期なので、
outputは通常、現在より前のtickのsnapshotから作られる。元tickが現在以前で、元snapshotから
`supervisor.valid_ms`以内なら採用し、outputの`tick_id` / `ts_ms`（元tickの識別子）と
`source_monotonic_ms`・`received_monotonic_ms`をそのままdecision traceに残す。鮮度は受信時刻より
厳しい元snapshot時刻から数える（受信が遅れた古い提案を新鮮と扱わない。決定記録 0041）。

次は失敗として記録する: 元snapshotから`supervisor.valid_ms`を超えたoutput、未来の受信時刻、
現在より未来の元tick、snapshot schema・artifact versionの不一致、configの許可範囲外の
strategy / weight / target band。同じtickのoutputはregimeとconfidenceが現在と一致すること。
壁時計`computed_at_ms`やworkerの時計は期限判定に使わない（0028 §2.6）。

過去tickのRL提案は、valid_ms 以内かつ提案時の Workload Regime が現在の Regime と一致する場合のみ
利用する。異なる場合は RulePolicy へ fallback する。Supervisor proposal の有効性判定であり、
Safety State は変えない。比較は提案時と現在の2点だけで、confidenceの一致は求めない。
途中でRegimeが変わって戻った場合（A→B→A）は、`valid_ms` が鮮度を制限しているため設計上そのまま
利用する（決定記録 0041）。

active RLが利用不能ならinlineの決定論的RulePolicyへfallbackする。RulePolicyも失敗した場合は
Supervisor contextを返さず、#79 Fallback Controllerがsnapshotだけで運転を継続できる境界を保つ。
ControlTick v3はこのdecisionをoptionalに保存し、保存済みv1/v2 traceはそのまま読み書きできる。

## Shadow 集計（`coldaisle-supervisor-shadow`。#89 / 決定記録 0074 §2.1）

Rule（active）と RL（shadow）の提案の突き合わせは、**制御プロセスの外で、運転の後に**行う。
`coldaisle-fand` は `ControlTick.supervisor` を decision trace へ保存するだけで、台帳
（`SupervisorShadowLedger`）を持たない。台帳の例外・上限・集計の遅さは control tick にも
heartbeat にも届かない。

```bash
uv run coldaisle-supervisor-shadow --evidence var/supervisor-shadow-runs.yaml \
  --registry-root var/model-registry --out var/supervisor-shadow.json
```

`--evidence` の manifest は期間（`[start_ms, end_ms)`。相対指定は置かない）と、比べた RL artifact の
**model ID と版だけ**を名指す。bytes hash は受け取らず、Registry を `load_version()` で**読んで**
（書かない）得た attestation から識別を作る。

```yaml
schema_version: 1
period: {start_ms: 1700000000000, end_ms: 1700086400000}
rl_artifact: {model_id: rl-supervisor, version: 0.1.0}
```

- 証拠の DB は `coldaisle-evaluate` と同じ `EvidenceDatabase`（`immutable=1`）で開く。
  `-wal` / `-journal` に中身があれば開かない
- shadow の下限は検証済みの `config/rl-policy.yaml`（`--rl-policy`）の `shadow` から取る。
  **引数や manifest で上書きする口は無い**
- Rule 表は `--control-config` の `fan-policy.yaml` から作る。trace の `runtime.config` の
  `fan-policy.yaml` hash が違う行が1つでもあれば **run 全体を拒否する**（期間を分けて渡す）
- 索引と本文の食い違い・v8 未満（`runtime` が無い）の trace・台帳の拒否
  （Rule 表との不一致・同じ `(ts_ms, tick_id)` の食い違い）も run 全体を拒否し、何も書かない
- 台帳へ渡さない行は理由別に数え、`filtered` に**3つの鍵を常に全部**出す
  （`no_supervisor_decision` / `no_shadow_slot` / `active_not_rule`）
- 台帳は tick を `(ts_ms, tick_id)` で識別する。`tick_id` は再起動で 0 に戻るため

出力は `SupervisorShadowRunReport`（schema v1）の canonical JSON で、`summary`
（`SupervisorShadowSummary` v2 そのもの）・`summary_sha256`・`filtered`・`period`・
`fan_policy_sha256` を持つ。生成時刻を持たず、同じ入力からは同じ bytes になる。

終了コードと出力ファイルの対応:

- **0**: 包みを一時ファイルへ書いてから `--out` へ置き換えた。`--out` が変わるのはこのときだけ
- **1**: run を拒否した。`--out` には**触れない**。以前の run の包みが残っていても、それは
  **今回の結果ではない**（ログの `stale_out_exists` で分かる）。昇格に渡す前に、包みの
  `period` と `fan_policy_sha256` が意図した run と一致することを確かめる

**昇格に使うのは `summary` だけである。** `read_run_report()` で読み戻した `.summary` を
`promote_supervisor_policy()` へ渡す。昇格の入口は、昇格の時点で読んだ検証済み
`rl-policy.yaml` の `shadow`（`shadow_config`）を必須にとり、集計の `minimum_ticks` /
`minimum_paired_fraction` が**値で等しくなければ拒否する**（緩いほうも厳しいほうも）。
包みの digest は昇格の証拠にしない。

RL worker と `expected_rl_identity` の配線が無い間は、RL の提案がすべて欠落か識別の不一致になり、
集計は `usable=False` になる（0074 §3）。それが正しい振る舞いである。

## Validated 化（`validate_supervisor_policy()`。#105 / 決定記録 0074 §2.3）

supervisor policy を #104 の validated にする入口は `validate_supervisor_policy()`
（`control/supervisor/artifact.py`）だけにする。入力は `PolicyEpisodeReport`・**元の
`PolicyComparison`**・検証済みの `rl-training.yaml` / `fan-policy.yaml` / `safety.yaml` /
`rl-policy.yaml`・**比較のすべての RL arm** の certify 済み artifact の対応・その中で validated に
する arm（`target`）・Baseline の Rule policy・Registry の `ref` と `expected_revision`。

次を1つでも満たさなければ拒み、**Registry へ何も書かない**。

- 渡した比較を**JSON として読み戻して検証し直す**（`revalidated_comparison()`。`model_copy` などで
  validator を迂回した比較をここで拒む）。以降の照合はすべて読み戻した比較で行う
- 比較の canonical digest が report の `comparison_sha256` と一致する
- その比較・`rl-policy.yaml`・Rule policy・全 RL arm の artifact から作り直した report の bytes が、
  渡した report の bytes と一致する（Baseline・表と action の束縛・1対1 の照合も再び通る）
- report の設定の digest が、渡した検証済み設定の digest と一致する
- `target` の gate が `pass` である
- `ref` が supervisor policy で `certified_identity()` の model ID・版を名指し、`expected_revision`
  の snapshot の記録の checksum と metadata が artifact と一致する（`promote_supervisor_policy()`
  と同じ照合）

通れば `ModelRegistry.mark_validated()` を1度だけ呼び、`offline_evaluation_ref` に
`supervisor-episode:<digest>` を記録する。**いまは反実仮想 artifact が無いので gate が必ず
`blocked` になり、この入口は必ず拒む**（0074 §3）。#104 の `mark_validated()` を直接呼ぶ経路
（0062 の CLI）は残余として残る。supervisor policy の validated 化はこの入口でだけ行う。
