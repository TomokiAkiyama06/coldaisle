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
`source_monotonic_ms`をそのままdecision traceに残す。鮮度は受信時刻より厳しい元snapshot時刻から数える
（受信が遅れた古い提案を新鮮と扱わない）。

次は失敗として記録する: 元snapshotから`supervisor.valid_ms`を超えたoutput、未来の受信時刻、
現在より未来の元tick、snapshot schema・artifact versionの不一致、configの許可範囲外の
strategy / weight / target band。同じtickのoutputはregimeとconfidenceが現在と一致すること、
過去tickのoutputはregimeが現在と一致すること（その後にregimeが変わった古い戦略はRuleへ戻す）。
壁時計`computed_at_ms`やworkerの時計は期限判定に使わない（0028 §2.3）。

active RLが利用不能ならinlineの決定論的RulePolicyへfallbackする。RulePolicyも失敗した場合は
Supervisor contextを返さず、#79 Fallback Controllerがsnapshotだけで運転を継続できる境界を保つ。
ControlTick v3はこのdecisionをoptionalに保存し、保存済みv1/v2 traceはそのまま読み書きできる。
