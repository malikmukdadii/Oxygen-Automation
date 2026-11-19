# Coordinated regenerative demo: ADC2 / Braking Control extension

## Scope

The two-output supply/drive sequencing milestone remains implemented by
`run-experiment`; see `efficiency-experiments.md`. That path still does not
command ADC2/Braking Control.

Schema-v3 now optionally hosts a third logical role `vesc_brake` mapped to the
exact OXYGEN channel `Braking Control`, plus coordinated drive/brake timelines
on the existing dispatcher. Live physical braking stays fail-closed:
`live_control.enable_physical_brake` must remain false, and physical
authorization of `vesc_brake` is rejected until a separate commissioning review
enables it.

Preserve schema-3 `dispatcher.json`, existing per-output profiles where they
remain single-role, and the working open-loop engine. Do not add a competing
dispatcher/backend or repurpose drive neutral as a brake command.

| Physical OXYGEN name | Terminal | Logical role | Current state |
|---|---|---|---|
| Drive Control | AO2/1 | `vesc_throttle` | Active; keep exact name/role |
| Braking Control | AO2/2 | `vesc_brake` | Offline/memory coordination supported; live writes disabled |
| Takasago Voltage Control | AO2/3 | `rzxa_voltage_demand` | Active; keep exact name/role |

## Implemented extension within the existing engine

1. `vesc_brake` / `motor_brake` is a first-class schema role with independent
   `brake_release_aout_v`, `brake_min/max_aout_v`, `brake_release_verified`,
   discovery/readback, and memory fixtures named exactly `Braking Control`.
2. Profiles may declare `outputs: ["vesc_throttle", "vesc_brake"]` while keeping
   a single primary `output` / assignment owner. Existing single-output V3
   profiles remain valid.
3. One global ownership lock still serializes SCPI/fixture writes. Coordinated
   compile builds a shared monotonic schedule with per-role segments; partial
   update failures are emitted and abort the cycle.
4. Interlocks: `drive_brake_mutual_exclusion`, `zero_drive_before_brake`,
   `release_brake_before_drive`. Runtime refuses overlapping drive and brake
   demand. Drive-only motor profiles verify brake release when the role is
   configured.
5. Cleanup attempts motor neutral then brake release; supply demand is still
   preserved.
6. CES eight-button names are unchanged. Behind the buttons:
   - Motor Gentle / Run Efficiency Scan: drive-only + brake-release verify
   - Run Regen Test (`motor_coast_compare`): coordinated accel → coast → brake
   - Run Driving Cycle (`motor_stop_go`): accel/coast/brake bursts

Offline memory waveforms use provisional brake voltages for rehearsal only.
They are not commissioned live limits.

## Still required before live braking

- Verify the exact ADC1/ADC2 app configuration, transfer curves, deadbands,
  neutral/release and fault levels on this controller/firmware.
- Confirm mutual exclusion and fault behavior when both ADC inputs are
  non-neutral, an input is disconnected, CAN is lost, or the SCPI host fails.
- Verify phase/battery regenerative limits and that the RZ-XA installation can
  absorb energy independently of AOUT demand.
- Verify independent e-stop/inhibit and decide release-vs-hold brake policy per
  fault. Enable `enable_physical_brake` only after that review.
- Measure signed DC power at the relevant energy boundary; speed reduction alone
  is not proof of regeneration.

## Staged live experiment (future, after commissioning)

Use one explicitly coordinated run, not a new DOUT:

- Establish and verify supply with drive neutral and brake released.
- Ramp drive, hold a measured condition, return drive to neutral and verify.
- Apply a bounded commissioned brake ramp/hold; measure returned DC energy.
- Release braking and verify completion states.
- Only then consider small bus-demand disturbances during a permitted phase.
