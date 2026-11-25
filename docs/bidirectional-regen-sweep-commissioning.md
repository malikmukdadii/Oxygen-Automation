# Bidirectional regenerative sweep commissioning

Offline-first guide for the autonomous bidirectional DC bus voltage sweeps with a
repeatable Fast Regen accel+brake pattern at every voltage point.

**Not commissioned by software alone.** Live physical ADC2 remains fail-closed until
the operator enables it after commissioning. No live OXYGEN timing validation is
performed by agents — validate physical SCPI timing manually on the chassis.

## Modes (DOUT-assignable)

| Logical button | Assignment | Mode |
|---|---|---|
| Fast Regen Test | `fast_regen_test` | Standalone ~4.35 s pattern at current supply demand |
| Open Loop Sweep | `motor_sweep_open_loop` | Fixed settle after AOUT ack; no measurement gate |
| Voltage Closed Loop Sweep | `motor_sweep_voltage_closed_loop` | Supervisory measured-bus qualification |
| Full Closed Loop Sweep | `motor_sweep_full_closed_loop` | Voltage qualify + RPM-governed holds |
| Stop Sweep | `stop_sweep` | Cooperative software cancel (not a physical E-stop) |

Existing CES / RZ-XA eight-button assignments remain available. New buttons are
**additive**. Example: `examples/ces-bidirectional-sweep.json` (13 assignments).
Do **not** overwrite the gitignored local `dispatcher.json` — copy/merge manually.

## Voltage grid

Defaults: **36.0–40.0 V**, step **0.25 V**, bidirectional, initial **descending**
from 40.0 V. One Fast Regen drive event per visit.

| Quantity | Value |
|---|---|
| Distinct points | 17 |
| Visits / round trip | 33 (endpoints not duplicated on reverse) |
| Visits / 2 round trips | 65 |

Finite `round_trips` is required unless `continuous: true` is set explicitly.
Omitted/invalid `round_trips` is **not** infinite. Continuous example:
`examples/ces-bidirectional-sweep-continuous.json`.

Verified RZ-XA mapping: **DC = AOUT × 80** (36 V → 0.45 V AOUT … 40 V → 0.5 V).
Distinguish commanded DC / verified AOUT / measured DC. Ack’d AOUT is never
“verified physical bus voltage.” Manual relative 0.5 V RZ-XA steps remain on the
existing commissioned grid.

## Fast Regen nominal (~4.35 s)

Accel ramp 0.25 → hold 1.5 → throttle release 0.15 → transition 0.10 → brake ramp
0.20 → brake hold 1.5 → brake release 0.15 → recovery 0.50. Defaults: throttle
**1.6 V**, brake from assignment `brake_target_v` / `fast_regen.brake_target_v`.
Throttle and brake are mutually exclusive. RZ-XA demand is unchanged during the
drive event. Zero-duration ramps jump (no divide-by-zero). Phase boundaries are
never skipped under lateness.

## Feedback policy

All three sweep modes coexist under global `control.mode: open_loop`. Per-assignment
`motor_sweep.mode` selects feedback. Voltage qualification is **supervisory only** —
it does not call `require_controller()` / `controller.correct()`. Qualification
timeout fails the sweep (no silent open-loop fallback). Full closed-loop needs
operator RPM targets before physical use.

## SCPI timing defaults

Independently configurable; schema floor **10 ms**:

| Path | Default |
|---|---|
| DOUT poll (`timing.poll_ms`) | 20 ms |
| AOUT update (`timing.interval_ms`) | 20 ms |
| Voltage feedback poll | 20 ms |
| RPM feedback poll | 20 ms |

Reuse the persistent SCPI session. Unchanged setpoints are not rewritten. Timeline
JSONL already logs `lateness_ms`, `interval_ms`, `skipped_update_slots`, and
`maximum_lateness_ms`. No separate benchmark utility.

## Progressive commissioning

1. Memory validate / open-loop sweep dry run
2. Physical open-loop sweep with conservative amplitudes
3. Enable measured voltage qualification after bus channel mapping is verified
4. Commission RPM/CAN binding and set accel/brake RPM targets
5. Operator enables physical brake only after ADC2 commissioning

## Migration notes

- Assignment limit raised **8 → 16** so CES buttons can coexist with sweeps.
- Optional root `motor_sweep_defaults` plus per-assignment overrides.
- Optional interlock `supply_demand_step_v` (default 0.5) allows listing a 0.25 V
  motor-prerequisite grid without changing relative 0.5 V step profiles.
- Assignment `kind`: `profile` (default) | `motor_sweep` | `fast_regen` | `stop_sweep`.

## Launch commands

```powershell
# Validate sweep example (no actuation)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings examples/ces-bidirectional-sweep.json

# Memory scenario rehearsal (operator supplies scenario JSON)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings examples/ces-bidirectional-sweep.json --execute --confirm-simulated-output --scenario scenarios/<your-sweep-scenario>.json

# Dry-run voltage sequence (no outputs)
.\.venv-scpi\Scripts\python.exe -c "from oxygen_engine.voltage_grid import dry_run_sequence, assert_grid_examples; assert_grid_examples(); import json; print(json.dumps(dry_run_sequence(round_trips=1)[:5], indent=2))"
```
