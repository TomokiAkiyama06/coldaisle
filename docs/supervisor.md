# Supervisor Interface

Supervisor はFan Demandを決めず、Learned MPCが任意contextとして読める運転戦略を返す。
出力schemaは `strategy`、多目的最適化の `weights`、CPU/GPU temperature `target_band`、
観測済みWorkload Regime、policy artifact version、計算壁時計だけを持つ。Demand、PWM、hwmon、
Reactive GuardやCritical Safetyの上下限を表す欄はない。

初期運用は `RulePolicy active + RLPolicy shadow` とする。両policyは同じimmutable
`SupervisorInput`（現在snapshot、recent history、Workload Regime、recent control performance）を使い、
active / shadowの成功または構造化された失敗を1つの`SupervisorDecision`へ記録する。
shadow出力は`selected_output`にならない。

RLPolicyは制御loop外のworkerで動かす。制御loopはworkerを呼び出さず、受信済みoutputと受信時の
ローカル単調時刻だけを`SupervisorCoordinator`へ渡す。受信から`supervisor.valid_ms`を超えたoutput、
未来の受信時刻、tick・snapshot schema・regime・artifact versionの不一致、configの許可範囲外の
strategy / weight / target bandは失敗として記録する。壁時計`computed_at_ms`を期限判定には使わない。

active RLが利用不能ならinlineの決定論的RulePolicyへfallbackする。RulePolicyも失敗した場合は
Supervisor contextを返さず、#79 Fallback Controllerがsnapshotだけで運転を継続できる境界を保つ。
ControlTick v3はこのdecisionをoptionalに保存し、保存済みv1/v2 traceはそのまま読み書きできる。
