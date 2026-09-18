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

The model is pure and uses no hardware I/O, which lets Mock and Replay exercise all state changes
without a sensor module.

The model is not wired into `ControlConfig` or the runtime daemon. Decision record 0033 documents a
proposed four-file atomic configuration boundary; decision record 0028's approved three-file
boundary remains authoritative until that proposal is accepted.
