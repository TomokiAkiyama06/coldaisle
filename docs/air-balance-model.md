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
- Because Top actually runs at `max(case_aux_exhaust, safety_floor)`, `coordinate()` accepts an
  optional `projected_top_floor`: the Top floor the caller expects Critical Safety to apply,
  derived by reading `safety.yaml` as a constraint (decision record 0028 §2.4 / §2.8). It is used
  only to estimate `q_top` for `before` and `projected`, so a floor that makes the case
  exhaust-heavy still triggers Front make-up air (`docs/airflow-model.md` "Top → Front make-up
  air"), and floor airflow counts toward any Top case-auxiliary shortfall. It is never copied
  into `requested.top`; ownership of the floor stays with Critical Safety, and omitting it keeps
  the previous behaviour.

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

Applying `coordinate()` to the Fallback controller's requested demand is **not** done: decision
record 0073 leaves it open because it changes Baseline behaviour (0073 §5).
