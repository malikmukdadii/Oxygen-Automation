# Coordinated regenerative demo: ADC2 / Braking Control extension

## Scope

The two-output supply/drive sequencing milestone remains implemented by
`run-experiment`; see `efficiency-experiments.md`. That path still does not
command ADC2/Braking Control.

Schema-v3 optionally hosts a third logical role `vesc_brake` mapped to the
exact OXYGEN channel `Braking Control`, plus coordinated drive/brake timelines
on the existing dispatcher. Live physical braking stays fail-closed:
`live_control.enable_physical_brake` must remain false, and physical
authorization of `vesc_brake` is rejected until a separate commissioning review
enables it.

Preserve schema-3 `dispatcher.json` (local/operator file; do not overwrite via
automation), existing single-role profiles where applicable, and the working
open-loop engine. Do not add a competing dispatcher/backend or repurpose drive
neutral as a brake command.

| Physical OXYGEN name | Terminal | Logical role | Current state |
|---|---|---|---|
| Drive Control | AO2/1 | `vesc_throttle` | Active; CES offline peak authorized to **1.60 V** after manual ADC1 test |
| Braking Control | AO2/2 | `vesc_brake` | Offline/memory coordination; release **0 V**; live writes disabled |
| Takasago Voltage Control | AO2/3 | `rzxa_voltage_demand` | Active; keep exact name/role |

Operator-confirmed physical motor notes (docs only; do not change VESC limits):
ADC1 0 V = zero positive drive; ADC2 0 V = brake released; ADC1 > 0 = positive
current demand (not regulated speed); Motor/Battery ±10 A, Abs max 20 A.

## CES offline waveforms (software)

Exact eight-button labels: Ramp Enable, Ramp Disable, Motor Gentle, Run Regen
Test, Run Driving Cycle, Run Efficiency Scan, **RZ-XA Step Down**, **RZ-XA Step Up**.

| Button | Profile | Peaks / notes |
|---|---|---|
| Motor Gentle | `profiles/ces/motor_gentle.json` | 0.60 V, ~10 s, drive-only |
| Run Regen Test | Trial B default `motor_regen_trial_b.json` | 1.60 V then prompt ADC2 brake |
| Run Regen Test (alt) | Trial A `motor_coast_compare.json` | 1.60 V then coast; swap via assignment profile |
| Run Driving Cycle | `profiles/ces/motor_stop_go.json` | 0.60 / 1.00 / 1.60 V + coast/brake intervals |
| Run Efficiency Scan | `profiles/ces/motor_three_level.json` | 0.40 / 0.80 / 1.20 / 1.60 V, ~4 s each |

Coast vs regen: same DOUT; select Trial A or Trial B by changing the assignment
`profile` path. Open-loop timing does **not** claim matched start RPM; RPM-triggered
brake remains disabled without sync measurement.

Brake demand levels are config `brake_max_aout_v` (provisional 0.15/0.20 V in
profiles). ADC1 testing does **not** authorize ADC2 braking capability.

## Still required before live ADC2

- Calibrate ADC2 release/range/fault response independently of ADC1
- Confirm mutual exclusion and host/CAN/disconnect faults
- Verify RZ-XA regenerative sinking and DC-link protection
- Enable `enable_physical_brake` only after that review
- Measure signed DC power; speed reduction alone is not regen proof
