# Air Balance Model

`coldaisle.control.air_balance` evaluates candidate requested demands before Reactive Guard and
Critical Safety. It does not actuate fans and does not produce effective demand or PWM.

## Flow scale and calibration

Each zone has a replaceable curve:

```text
demand → Airflow Index → Effective Flow Unit (EFU)
```

Airflow Index is meaningful only within its own zone. EFU is the common internal scale used for
`q_front`, `q_rear`, and `q_top`; it is not CFM. Curves must cover demand and Airflow Index from
0.0 through 1.0, so evaluation never silently extrapolates an uncovered candidate range.

The committed example lives under `tests/fixtures/` rather than `config/`. It is explicitly
`uncalibrated`, and constructing a model from it requires
`allow_uncalibrated_for_testing=True`. The default path rejects it. This lets Mock and Replay
exercise the interface without making provisional values usable by a production controller. The
file hash and calibration status travel with every estimate.

The configuration also owns the target balance ratio and accepted band. The implementation does
not assume that `balance_ratio == 1.0` is correct.

## Evaluation

```text
estimated_intake  = q_front
estimated_exhaust = q_rear + q_top
balance_ratio     = estimated_exhaust / estimated_intake
```

The ratio state is combined with GPU intake, case delta, CPU package, and GPU temperature. Crossing
any configured non-safety thermal limit produces `THERMALLY_LIMITED`. A zero Front estimate cannot
produce a ratio and is `UNKNOWN`; fan stall and stale telemetry remain responsibilities of Critical
Safety and the telemetry input contract. If Rear or Top exhaust is active in that state, coordination
still raises Front make-up air rather than leaving Front at zero.

## Coordination and the Top boundary

- Exhaust-heavy candidates raise Front make-up air toward the configured target ratio.
- Intake-heavy candidates raise Rear only when a thermal limit is active. Rear is exhausted first;
  Top receives only the remaining case-auxiliary request. This order is not a policy introduced by
  this model: decision record 0026 (FINAL) states that normal case ventilation uses Front + Rear
  and that Top `case_aux_exhaust_demand` rises only when Front + Rear are insufficient, and #81
  repeats it. Selecting the exhaust zone from the #75 response matrix
  (`docs/airflow-model.md` "Thermal effectiveness") would change that approved order and therefore
  requires a new decision record that supersedes 0026 first.
- A coordination proposal never lowers any candidate demand.
- `requested.top` is explicitly tagged `case_aux_exhaust`. It is not CPU cooling demand and it does
  not contain a safety floor. Critical Safety still computes
  `max(case_aux_exhaust, cpu_cooling_floor)` later in the fixed pipeline, so this model has no path
  that can lower the CPU floor.
- Every zone actually runs at `max(requested_z, lower bounds of the composition)`, so
  `coordinate()` accepts optional per-zone `projected_floors` (decision record 0078 §2.2): for
  zone `z`, `1.0` when Critical Safety forces it to Max, otherwise
  `max(Guard floor, Safety floor, ramp_down floor)` (`0.0` when none exists). Airflow for `before`
  and `projected`, and the raise targets (Front make-up air, Rear → Top shortfall), are estimated
  at `max(requested_z, projected_floors.z)`. A floor that makes the case exhaust-heavy (for
  example a CPU cooling floor or a ramp-down on Top, or a high Rear floor) still triggers Front
  make-up air (`docs/airflow-model.md` "Top → Front make-up air"), and a high Front floor can call
  for Rear exhaust when a thermal limit is active. A zone whose floor already delivers the target
  airflow is not raised. The floors are never copied into `requested`; their ownership stays with
  Critical Safety and the composition, and omitting them keeps the previous behaviour.

The model is pure and uses no hardware I/O, which lets Mock and Replay exercise all state changes
without a sensor module.

## Control Config and the runtime (#81 / decision record 0073)

`air-balance.yaml` (schema version 2) is the fourth Control Config (decision record 0033 / 0073).
`ControlConfig.from_directory()` validates all four files and returns only when every file is
valid (bundled `CONTROL_CONFIG_VERSION` 11). Version 1 files are rejected and never filled in.

| `air-balance.yaml` | `coldaisle-fand` |
|---|---|
| missing | invalid Control Config: all zones Max (`config_invalid`) until restarted with a valid file |
| malformed (schema, v1, non-monotonic curve, unverifiable `thermal_inputs`) | same as missing |
| `source.status: uncalibrated` | validated; Air Balance is **disabled**. No `ConfiguredAirBalanceModel` is built and the trace records `disabled: uncalibrated` |
| `source.status: calibrated` | validated; Air Balance is enabled and every tick records the estimate |

Disabling Air Balance leaves Critical Safety, the CPU cooling floor and Reactive Guard unchanged.
The enable/disable decision comes from `source.status` only; there is no separate flag.

v2 adds `thermal_inputs`, which binds each thermal indicator to a snapshot metric (or `null` to
leave it unused). Each metric must exist in the Metric Catalog with unit `C`; derived values
(`d.*`) are expanded to their operands. Bound metrics join the control input contract as
`ADVISORY` inputs (existing inputs keep their importance), whether Air Balance is enabled or not.
Missing or stale values reach the model as `None`, never 0. The concrete bindings are decided
after the #75 / #81 measurements (0073 §5); the committed template binds none.

### Decision trace (`ControlTick` v11)

- `ZoneRecord.applied_demand` is the demand the Hardware Backend mapped to PWM, after the profile's
  `minimum_stable_demand` lift and start-up kick. It is `None` when there was no write result or
  when `write_ok` / `readback_ok` is false. `FanHardwareResult.applied_demand` follows the same rule.
- `ZoneRecord.estimated_flow` is that applied demand mapped through the zone's EFU curve. It is
  `None` while Air Balance is disabled, for zones without a confirmed write, for zones with a
  backend fault on that tick (including a `TACH_STALL` before the stall window elapses), and for
  zones whose stall is an active Critical Safety fault.
- `air_balance` holds `status` (`enabled` / `disabled`), the model id, source status and file hash,
  `q_front` / `q_rear` / `q_top`, intake / exhaust / ratio, `state` and the thermal reasons. The
  zone flows must match the `q_*` values zone by zone. Thermal reasons are recorded even when a
  `q_*` is missing (`state: unknown`).
- `runtime` is schema version 2 and carries the bundled config version plus the schema version and
  SHA-256 of all four files.

Decision record 0073 names these "v10"; `ControlTick` v10 was already taken by #104, so the
implementation uses v11 (0073 §5 allows the next free number).

### Learned MPC

When the Learned MPC worker is wired (#86 / #104), `LearnedMpcController` receives the
`AirBalanceModel` and `BalanceBand` from `ControlConfig.air_balance` (or `None` while disabled)
together with `ControlConfig.fan_hardware`. The balance cost term evaluates candidate demands after
`FanHardwareConfig.stable_demands()` — the same pure function the simulated backend uses — so it
scores the ratio the hardware would actually produce. Passing Air Balance without the hardware
profile is rejected at construction (`MpcCostUnusableError`). The mapping never changes the plan's
requested demand.

## Coordinating the Baseline (#81 / decision records 0078 / 0085)

Decision record 0078 applies `coordinate()` to the Fallback (Baseline) requested demand, never to
the Learned MPC proposal (the MPC already carries the balance term). `fan-policy.yaml`
`air_balance_coordination.mode` selects `off` (default) / `shadow` / `apply`, changed only by a
person and a restart. The loop order is:

```text
Fallback.propose()            → raw baseline
Critical Safety.evaluate()    → unchanged (does not read requested)
AirBalanceCoordinator.apply() → coordinated baseline (differs from raw only in mode: apply)
ControllerGate.select(fallback = coordinated baseline, learned = …)
composition (0028 §2.4)       → Guard ceiling / floors, Safety floor, ramp_down, forced_max
```

- Coordination runs only when every condition of 0078 §2.3 holds: Air Balance enabled, `AUTO`,
  snapshot available, a Fallback proposal, Safety `NORMAL` / `DEGRADED`, no zone Fan fault and no
  unconfirmed tach (`CriticalSafetyDecision.tach_unconfirmed_zones`: a backend `TACH_STALL` or a
  running stall timer, from the first report). Otherwise the tick is `skipped`, the raw baseline
  is used and the release hold is cleared.
- The applied value only raises, by at most `max_raise` per zone, and keeps the largest recent
  raise for `release_hold_ms`. `shadow` records the same held value as `counterfactual_output`
  and hands the raw baseline to the Gate, so the fans see exactly what `off` would give them.
- In LIMITED / EXPANDED the authority band is centred on the coordinated baseline. With FULL
  authority the selected Learned MPC request is used as is. The Gate's
  `fallback_transition_floor` is applied after coordination.
- Failures: in `apply`, an exception from the coordinator (including a lowering value) is recorded
  as `failed`, translated into a `fallback_exception` fault (the next tick's Safety goes
  `EMERGENCY`), and that tick **bypasses the Gate** and uses the raw baseline (0085), with
  `fallback_reason: air_balance_coordination_failed` when ML could otherwise have run. A persisting
  failure alternates between `EMERGENCY` and one bypassed tick every `fault_clear_hold_ms`
  (0085 §2.6). In `shadow`, a failure is only recorded. The `try` covers the coordinator call only;
  Critical Safety and composition exceptions still end the process.
- `ControlTick` v14 records the `air_balance_coordination` block (`mode`, `status`,
  `skip_reason`, `candidate` / `proposed` / `output` / `counterfactual_output`, the applied
  `max_raise`, `bounded_by_max_raise`, `held`, `projected_floors` and their basis, the before /
  projected state and ratio, `reasons`, `failure`) and `tach_unconfirmed_zones` copied from the
  Safety decision. `output` is what the coordination stage produced as the Baseline value; on a
  bypassed tick it is the raw baseline (`candidate`).
- Promotion evidence is bound to the `fan-policy.yaml` trace (0078 §2.5, stage 4): evaluation
  report v4 counts each consumed tick's `runtime.config.policy_sha256` against the evaluated
  `fan-policy.yaml` (`fan_policy_trace_binding`), and `_check_evidence()` refuses a report with any
  mismatched or missing tick (`MIN_EVIDENCE_REPORT_SCHEMA_VERSION` 4). Changing only the
  coordination `mode` therefore stops promotion until traces recorded under the new file exist.
  The Baseline arm of the offline evaluation is not recomputed: the evaluator reads the values the
  loop recorded (the coordinated baseline on both the applied side and the 0053 shadow Fallback
  value), and the arm key does not include the coordination `mode` (decision record 0093 §2.2).

### Shadow summary (`coldaisle-air-balance-shadow`, decision record 0093)

Before moving from `shadow` to `apply` (0078 §2.8), the owner reads a summary of the stored
decision traces. The tool only reads the evidence database (`EvidenceDatabase`, `immutable=1`),
writes one output file and **gives no verdict**: the shadow → apply criteria are decided with the
owner after seeing real shadow data (0078 §5).

```bash
# var/air-balance-shadow-evidence.yaml
#   schema_version: 1
#   period: {start_ms: <start>, end_ms: <end>}   # [start_ms, end_ms)
uv run coldaisle-air-balance-shadow --evidence var/air-balance-shadow-evidence.yaml \
  --control-config var/control-config --out var/air-balance-shadow.json
uv run coldaisle-air-balance-shadow --evidence var/air-balance-shadow-evidence.yaml \
  --control-config var/control-config --format markdown --out var/air-balance-shadow.md
```

- Output: tick counts per `status` and per `skip_reason` (every key, zeros included); per zone,
  the distribution of `counterfactual_output - candidate` over the ticks that recorded a
  counterfactual (all of them, and the raised ones only), the number of raised ticks
  (`counterfactual_output > candidate`) and how often raised / not raised switched between
  consecutive evaluated ticks (skipped / failed ticks are left out of that sequence); the same for
  "any zone raised"; and per zone the `skipped` ticks caused by a Fan fault of that zone.
  A Top Fan fault is an unconditional `EMERGENCY`, so its tick reads `skip_reason: safety_state`;
  the tool counts it from the same tick's `faults` (0088 §3).
- Percentiles come from `config/air-balance-shadow.yaml` (no defaults in code).
- The whole run is refused (nothing written, exit code 1) on a row outside the period, an index
  that disagrees with the body, a trace older than `ControlTick` v14, or a tick whose
  `fan-policy.yaml` / `air-balance.yaml` / `fan-hardware.yaml` hash differs from the given Control
  Config. Pass separate periods instead.
- The output is for a person. It is not promotion evidence, and nothing on the control side
  imports the tool.
