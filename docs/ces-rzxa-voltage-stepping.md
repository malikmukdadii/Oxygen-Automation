# CES RZ-XA manual demand stepping (V3)

## Review status and scope

Offline implementation only; physical operation is **not commissioned by these tests**.
The local `dispatcher.json` and all existing six-button examples remain untouched. No new
transport, dispatcher, controller, recording automation, energy analysis or Matrix Sampler
calculation is introduced. OXYGEN remains responsible for measurement, integration,
calculations, visualization and manually controlled recording.

Use `examples/ces-eight-button.json` for offline review. It is memory-only, all physical
actuation flags are false, and `supply_demand_grid_commissioned` is false. Do not copy fixture
identities into a physical configuration. The physical supply role must select the existing
operator-confirmed `Takasago Voltage Control` AOUT by exact discovered name; never select a
channel by assumed terminal, numeric ID, or name similarity. The motor remains limited to
0.40 V in this example. RZ-XA SCPI and output-enable toggling remain disabled.

## Eight logical assignments

The trigger names below are logical operator-example labels, **not claims about available
chassis DOUT identities**. Replace each `trigger` with the exact OXYGEN DOUT channel name
supplied and verified by the operator. Names need not match the logical function label.
Do not rename or assume existing OXYGEN channels to fit this table.

| Logical function | Assignment | Profile |
|---|---|---|
| Ramp Enable | `rzxa_to_40_hold` | `rzxa_ramp_hold.json` |
| Ramp Disable | `rzxa_down` | `rzxa_ramp_down.json` |
| Motor Gentle | `motor_gentle` | `motor_gentle.json` |
| Run Regen Test | `regen_test` | `motor_coast_compare.json` |
| Run Driving Cycle | `driving_cycle` | `motor_stop_go.json` |
| Run Efficiency Scan | `efficiency_scan` | `motor_three_level.json` |
| RZXA Voltage Down | `rzxa_voltage_down` | `rzxa_voltage_down.json` |
| RZXA Voltage Up | `rzxa_voltage_up` | `rzxa_voltage_up.json` |

The regen-test label does not establish active regenerative braking or recovered energy.
The efficiency-scan label selects demand plateaus; Python calculates no efficiency.

## Demand grid and execution

Verified mapping: **1 V AOUT corresponds to 80 V nominal bus demand**, not measured bus voltage.
One press requests one adjacent nominal 0.5 V change, i.e. **0.00625 V AOUT**.

| Nominal bus demand (V) | AOUT demand (V) |
|---:|---:|
| 36.0 | 0.45000 |
| 36.5 | 0.45625 |
| 37.0 | 0.46250 |
| 37.5 | 0.46875 |
| 38.0 | 0.47500 |
| 38.5 | 0.48125 |
| 39.0 | 0.48750 |
| 39.5 | 0.49375 |
| 40.0 | 0.50000 |

The dedicated `relative_aout_step` profile contains exactly one step, a supply `channel`,
and `direction: up` or `down`. It requires `motor_neutral: true`, open-loop mode, gain 80,
the unchanged global 0.0..0.5 V supply range, and `hold_final` with
`value: runtime_target` and `operator_action_required_to_stop: true`. It cannot be combined
with other steps or applied to a motor. Existing static profiles retain static completion.

At every press the dispatcher checks its selected output context and current property
readback against the last known demand, verifies motor-neutral demand, and requires a grid
starting point. Grid membership uses the existing 1 microvolt AOUT readback tolerance
(0.00008 V nominal bus equivalent); targets are canonical grid values, not accumulated
floating-point additions. Off-grid, invalid, missing, changed or unknown starts fail closed.
There is no automatic snap-to-grid or entry from zero. The unchanged Ramp Enable utility
can establish 0.50000 V demand only under a separately reviewed safe entry procedure.

The dispatcher owns the one active profile globally. Before a transition it rechecks the
supply and motor; it issues exactly one `command()` through the existing constant-property
writer, error queue and readback verification. Completion independently verifies the runtime
target and retains `hold_final` ownership. No high-frequency ramp is used for relative steps.
At 36 V/down or 40 V/up, `relative_step_boundary_noop` is logged with `hardware_write: false`;
readback verification continues but the profile makes no output write.

`relative_step_target`, `output_command_failed`, `profile_interrupted_state` and
`profile_completion_state` retain demand and ownership evidence. An interrupted cycle or
unknown output blocks further profiles in that dispatcher, even if a caller catches the
exception. A failed write is never retried. Normal `run()` exits to the existing cleanup:
motor-neutral demand is attempted, supply demand is preserved/read back, and failure is
reported. Cleanup is not a continuation or a retry of the supply write. An explicit restart
after independent investigation adopts actual startup readbacks; it does not restore a saved
setpoint or establish physical safety.

## Motor authorization and independent protection

Legacy `motor_supply_demand_v: 0.5` configurations still work. The optional mutually exclusive
`motor_supply_demands_v` is a nonempty, unique, explicitly listed subset of the nine values
above, not a continuous range. The eight-button example lists all nine for memory rehearsal.
Motor profiles still require `require_supply_demand_before_motor: true`; property readback
must match both the tracked demand and one listed value. Merely adding values to a list is
not evidence of physical commissioning. Physical use of the list or either relative profile
additionally requires `interlocks.supply_demand_grid_commissioned: true`, only after separate
review, on top of all existing wiring, mapping, neutral, external protection, live and exact
output-confirmation gates. This acknowledgement is not live feedback.

The discrete extension is open-loop only. It does not broaden a closed-loop measured-bus
interlock or infer readiness at lower voltages. All original V3 closed-loop gates remain.
Stepping always verifies motor-neutral demand even if the optional legacy supply-motion
neutral guard is disabled. Active motor playback excludes supply operations, and vice versa.

**0 V ADC1 / motor AOUT means neutral demand only.** It does not prove that the wheel is
stationary, the drive is inhibited, regeneration has stopped, or the source/sink can safely
accept energy. Physical supply-voltage commissioning requires independent motor inhibition,
verified source/sink safety and externally enforced bus/regen protections. This dispatcher
has no independent live bus/readiness feedback in open-loop and cannot enforce those
physical protections. Supply demand, Ramp Disable, Ctrl+C and software cleanup are **not
emergency stops**. A communication failure may prevent even a neutral-demand write.

## Trigger timing and offline latency results

LOW then HIGH, startup-HIGH baseline rejection, no queue, config-order simultaneous priority,
stale-edge detection and tracker reset after profiles are unchanged. Polling pauses during
profiles; pulses entirely while busy or between observations can be missed without evidence.
Use independent physical stop/inhibit, not a DOUT cancellation expectation.

The example retains `poll_ms: 50` and `max_trigger_gap_ms: 250`. Deterministic memory tests
model **complete DOUT-read latency**, not just wire transmit time:

- Eight reads at 15 ms each cost 120 ms; with the 50 ms poll wait, the normal sweep is
  170 ms. Periodic verification of two output properties, also modeled at 15 ms each,
  can extend the inter-observation gap to 200 ms. Each of the eight buttons successfully
  triggers once per LOW/HIGH sequence, not repeatedly while held HIGH; rearming is tested.
- Eight reads at 30 ms cost 240 ms; the poll wait alone brings the sweep to 290 ms, beyond
  the 250 ms maximum. Tracking resets and no rising edge is inferred across the gap.
- A single 300 ms read is discarded. Slow preconditions also age out an otherwise valid edge.

No safety timeout was relaxed. These models do **not** prove the real eight-channel scan
fits. Actual SCPI response latency, jitter, error checks, logging, optional telemetry,
preconditions and periodic verification must all be included. A single-channel `watch-do`
run cannot establish eight-channel timing. If real operation cannot reliably fit the unchanged
budget, do not commission eight-button operation; investigate OXYGEN/network latency and a
separately reviewed observation strategy rather than hiding missed edges with larger limits.

## Read-only commissioning review (no dispatcher execution)

1. Keep the independent motor inhibit engaged, supply output isolated/disabled, and stored
   energy managed under the site's electrical safety procedure. Verify actual stopped motion
   independently. Record physical wiring, source/sink capability and protection settings.
2. Do not edit the active `dispatcher.json` yet. Review the memory example and profiles offline:

   `./.venv-scpi/Scripts/python.exe -m oxygen_engine dispatch-patterns --bindings examples/ces-eight-button.json`

   Omit `--execute`; this preview does not connect or write.
3. With the existing reviewed OXYGEN bindings and endpoint, run the existing read-only
   `discover-aout --bindings dispatcher.json`. Verify `Takasago Voltage Control`, the motor
   channel, their full string IDs, voltage units, mode, selected constant source/property,
   ranges and actual property readbacks. Do not toggle source/mode or use a write diagnostic.
4. Obtain the operator's eight exact DOUT names from OXYGEN. Use the existing read-only
   `watch-do --channel "<exact operator name>" --duration 30` one channel at a time with the
   correct endpoint arguments to verify baseline LOW, HIGH, return LOW, and polarity. Keep
   the dispatcher stopped; the watcher does not perform output control. This is mapping
   verification only, not eight-channel latency acceptance.
5. Capture timestamped read-only SCPI observations of all eight configured DOUT properties in
   sequence, plus the same two periodic AOUT property reads, using approved read-only site
   diagnostics. Include repeated scans, startup, normal acquisition load, slow replies and
   network jitter; retain raw replies and query timings. Verify each channel's worst observed
   inter-observation gap and the end-to-end trigger/precondition age fit 250 ms with margin.
   Do not infer this from an average or a single-channel test. No such real capture was run
   as part of this offline change.
6. Review the historical `-200` failure and logs, particularly 0.46875 V AOUT (37.5 V nominal
   demand). Do not conclude it was fixed by software changes. Any unavailable property,
   unexpected readback or error means stop and investigate before writes are authorized.

## Separately authorized isolated low-energy test

This phase includes writes and is **not** part of the read-only review. Do not proceed until
site-authorized personnel approve the isolation, limits, source/sink state and independent
protection. Never commission supply motion using motor-neutral demand alone.

1. Work on a reviewed configuration copy. Preserve the existing physical authorization gates,
   exact AOUT names, discovery, explicit output confirmations, 0.0..0.5 V supply range,
   0.40 V motor ceiling, `require_supply_demand_before_motor: true`, independent emergency
   stop and external enable/protection requirements. Keep both RZ-XA SCPI/toggle flags false.
   Map all eight operator-supplied DOUT names; disable motor assignments for supply-only
   isolated tests but retain the motor role and neutral verification. Keep optional Python
   telemetry off; use OXYGEN for all actual measurements and recording.
2. Obtain separate approval for `supply_demand_grid_commissioned: true` and exactly which
   discrete motor supply prerequisites are commissioned. The flag is an operator attestation,
   not protection or evidence of acceptance. No physical flags are enabled by the shipped
   CES memory example. Use the existing endpoint/output-ownership and explicit live-confirm
   workflow; do not run another output controller, write diagnostic or experiment engine.
3. With independent motor inhibition maintained, establish a reviewed grid entry point using
   the existing Ramp Enable setup or an independently approved entry procedure. Relative
   buttons must refuse an initial zero/off-grid demand. Never bypass refusal by editing
   tracked state or by assuming the last session's output was restored.
4. At 40 V nominal demand, press Up: expect a boundary no-op and no supply write. Press Down
   once: expect exactly one 0.49375 V demand write, error check, readback and completion.
   Release to LOW and allow observation before another press. Test held-HIGH non-retrigger.
5. Under the approved low-energy conditions, test one adjacent demand at a time down through
   all grid points and back up. Stop for inspection of each JSONL transaction, actual AOUT
   and independently measured bus response; especially inspect the 0.46875 V demand point.
   At 36 V/down verify the no-write boundary. Never automate a hardware sweep or continue
   after an unknown state. Confirm actual response/settling and protection independently;
   these are not measured bus steps guaranteed by the dispatcher.
6. Review live eight-channel gap/stale diagnostics and measured timing under acquisition load.
   Busy presses are not queued. Do not send supply buttons during motor playback expecting
   them to be accepted or to stop motion. Verify ownership and return-neutral behavior first
   with the motor independently inhibited, before any separately authorized motor tests.
7. On `-200`, `NONE`, timeout, changed context or unexpected readback: stop the test, maintain
   physical inhibition/protection, preserve complete JSONL/raw SCPI records and independently
   establish the output/bus state. No reconnect, mode/source toggling, retries or next-point
   continuation. Restart only after investigation and explicit authorization. A software
   demand is not power isolation or an emergency stop.
8. Enable motor profiles only after separate low-voltage motor/source/sink commissioning for
   every listed supply prerequisite. Neutral demand, readback, CAN speed and measured bus
   voltage have different meanings; none alone establishes regenerative safety. Let OXYGEN
   perform measurement, energy integration, Matrix Sampler calculations and visualization.

## Remaining real-system acceptance items

- Exact eight DOUT identities, polarity, pulse widths and worst-case scan/precondition latency.
- Constant-property update reliability at every grid point, especially the unresolved -200 /
  unavailable response near 0.46875 V, and stability of selected source/property during load.
- Physical 80 V/V mapping accuracy, DAC resolution, tolerance, actual bus settling and overshoot.
- Independent motor inhibition, neutral behavior, stopped-motion verification, source/sink
  readiness and protection during regenerative energy flow at each commissioned demand.
- Low-energy validation and separate sign-off before changing the local operator config.

Offline success cannot close any of these real-system acceptance items.

## Offline validation report

Final workspace validation (no live OXYGEN / RZ-XA operation):

| Check | Result |
|---|---|
| `./.venv-scpi/Scripts/python.exe -m compileall -q oxygen_engine tests` | Passed, exit 0 |
| `./.venv-scpi/Scripts/python.exe -m pytest -q` | **581 passed in 27.39 s; no failures or skips** |
| `dispatch-patterns --bindings examples/ces-eight-button.json` without `--execute` | Eight assignments validated; `connected: false`, `writes_attempted: 0` |
| `git diff --check` | Passed, no whitespace errors |
| Visual Studio solution build | Unavailable: no solution loaded / `Build.BuildSolution` unavailable |
| Physical commissioning / real eight-channel timing | Not performed; remains required |

The initial full run found six failures in the existing all-V3-library test because it assumed
static completion and unrestricted supply starting points for every profile. That test now
checks relative runtime completion, off-grid rejection and boundary no-writes while retaining
the original legacy-profile checks. The final full suite above includes those cases, all nine
grid points in both directions, discrete motor prerequisites, ownership, latency models and
the scripted real-writer -200/unavailable-property cases at 0.46875 V.
