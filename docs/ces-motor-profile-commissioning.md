# CES motor profiles: current-demand acceleration and normal stopping

## Status: memory rehearsal only, not a physical authorization

This revision corrects the interpretation introduced in commit `5b317d4`. `Drive Control`
(AO2/1, VESC ADC1) commands a controller in **current-control mode**, not regulated-speed
mode. Updated physical observations establish 0 V as the intended **normal stop demand**.
Approximately 0.3 V is low positive current/torque demand: the wheel can accelerate slowly
to nearly maximum RPM. Approximately 0.6..1.0 V produces greater demand and faster
acceleration; 1.6 V accelerated much faster but reached approximately the same eventual RPM.
These observations do not calibrate voltage to amperes, torque or RPM, or verify the entire
electrical operating range. A constant positive demand can continue accelerating the wheel;
it is not a constant-speed command or evidence of a stable operating point.

**0.3 V is not coast, and 0.1..0.2 V is not a demonstrated regenerative-braking region.**
Substantial deceleration at 0 V does not prove regeneration or establish a safe fault,
communication-loss or emergency-stop response. No VESC ADC-configuration export or verified
numeric motor-current, battery-current or regenerative-current limits were found in the
repository. Experiment abort thresholds and synthetic feedback are not controller settings.

All four redesigned replacements are staged under `profiles/ces/`, using the actual schema-3
`set_aout`, `ramp_aout`, `hold`, `checkpoint`, and `return_neutral` steps. They start and
complete at 0 V, with no positive-voltage coast/brake plateaus. The obsolete memory-only
nonzero-neutral exception is removed. The existing JSON identifiers `neutral_aout_v`,
`motor_neutral` and `return_neutral` are retained for compatibility; here they mean a
zero-demand command/readback, not verified stopped motion, coast or electrical safety.
Historical root motor waveforms remain unchanged for compatibility, with corrected warnings.
No RZ-XA profile, 80 V/V supply mapping or 0..0.5 V supply range is modified.

The local, gitignored `dispatcher.json` is **not modified or overwritten by this revision**.
At review it retained all eight trigger strings, assignment names and profile paths, a 0.40 V
ceiling and 0 V normal-stop value; its four motor assignments were disabled and
`neutral_verified` was false following the prior review. Those settings remain unchanged.
Although the reason for treating 0 V as an incorrect normal stop is withdrawn, the new
observations do not authorize a live restart or a safe fault response. Existing validation
continues to block physical dispatch, including supply dispatch with this motor role present.
Do not bypass that hold by changing acknowledgements, removing the motor role or disabling
interlocks. Independently enforced inhibition/protection and explicit commissioning review
are still required. No local flag is cleared or enabled automatically.

## Offline configuration and unchanged button identities

`examples/ces-motor-commissioning.json` is a new memory-only rehearsal config, not the live
operator file. It uses fake in-process AOUT fixtures, an initial supply demand of 0.50 V and
an initial motor demand of 0 V normal stop. All physical actuation flags are false; both mapping
and neutral verification are false. Explicit output-level 1.00 V commissioning fields are
**fake rehearsal authorization only**, not a claim that 1.00 V is physically permissible.

The example preserves the inspected operator's eight names and logical functions:

| Exact current DOUT string | Assignment | Staged profile |
|---|---|---|
| `Ramp Enable` | `rzxa_to_40_hold` | Existing `rzxa_ramp_hold.json`, unchanged |
| `Ramp Disable` | `rzxa_down` | Existing `rzxa_ramp_down.json`, unchanged |
| `Motor Gentle` | `motor_gentle` | `profiles/ces/motor_gentle.json` |
| `Run Regen Test` | `regen_test` | `profiles/ces/motor_coast_compare.json` |
| `Run Driving Cycle` | `driving_cycle` | `profiles/ces/motor_stop_go.json` |
| `Run Efficiency Scan` | `efficiency_scan` | `profiles/ces/motor_three_level.json` |
| `RZ-XA Step Down` | `rzxa_voltage_down` | Existing `rzxa_voltage_down.json`, unchanged |
| `RZ-XA Step Up` | `rzxa_voltage_up` | Existing `rzxa_voltage_up.json`, unchanged |

These are operator-supplied channel strings, not inferred chassis identities. The earlier
`ces-eight-button.json` remains a historical zero-neutral memory fixture; it is not the revised
motor mapping. Do not copy its `RZXA Voltage Down/Up` example labels over the operator names.
The staged four-point scan retains the historical `motor_three_level.json` basename for
traceability; the staged JSON name and waveform identify **four** levels.

## Waveform commands and timing

Every voltage below is **VESC AOUT current demand in volts**, not a calibrated torque,
current in amperes, bus voltage or regulated speed. Never apply the RZ-XA 80 V/V mapping
to this output. All sequences begin and end at 0 V normal stop demand. Holds maintain
current demand, not measured wheel speed; the wheel may continue accelerating during them.
Durations are nominal playback time, excluding startup, verification and I/O delays.

### Motor Gentle — 10 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..3 | Smooth ramp 0 -> 0.60 V, modest acceleration demand |
| 3..7 | Hold 0.60 V for sustained drive demand |
| 7..10 | Smooth ramp 0.60 -> 0 V; normal completion |

The final ramp is a normal-stop request, not a separate low-positive-voltage brake command.
Actual RPM, stopping time and electrical power must be measured; memory does not model them.

### Run Regen Test — 18 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..3 | Ramp 0 -> 1.00 V; acceleration-demand measurement window |
| 3..7 | Hold 1.00 V positive current demand; RPM need not be constant |
| 7..8 | Ramp 1.00 -> 0 V; begin deceleration measurement window |
| 8..18 | Hold 0 V; zero-demand deceleration/power observation, normal completion |

Checkpoints explicitly delimit `candidate_acceleration_window_begin/end` at 0/3 s and
`candidate_deceleration_window_begin/end` at 7/18 s; zero-demand observation starts at 8 s.
The intervening 3..7 s hold has its own current-demand checkpoint. These are command-time
windows, not confirmation of the wheel's response. The button/filename is retained for
compatibility; the deceleration window is **not labeled proven regeneration**. A 10 s
observation window does not guarantee a stopped wheel or readiness for another run.

### Run Driving Cycle — 24 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..2 / 2..4 | Ramp 0 -> 0.40 V / hold low positive current demand |
| 4..5 / 5..7 | Ramp to 0 V / zero-demand deceleration observation |
| 7..9 / 9..11 | Ramp 0 -> 0.60 V / hold medium positive current demand |
| 11..12 / 12..14 | Ramp to 0 V / zero-demand deceleration observation |
| 14..16 / 16..19 | Ramp 0 -> 1.00 V / hold higher positive current demand |
| 19..20 / 20..24 | Ramp to 0 V / zero-demand observation; normal completion |

Three progressively higher current demands illustrate different acceleration rates, not
three regulated RPM setpoints. Short zero-demand intervals separate the events. Residual
rotation can remain between bursts; equal initial speed and a complete stop are not assumed.

### Run Efficiency Scan — 22 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..1 / 1..5 | Ramp 0 -> 0.40 V / 4 s hold |
| 5..6 / 6..10 | Ramp to 0.60 V / 4 s hold |
| 10..11 / 11..15 | Ramp to 0.80 V / 4 s hold |
| 15..16 / 16..20 | Ramp to 1.00 V / 4 s hold |
| 20..22 | Ramp to 0 V; normal completion |

The 0.40/0.60/0.80/1.00 V candidates are retained as equally spaced demand commands, with
consistent 4 s observation windows, not predetermined speed points. There is no evidence
in the repository establishing a quantitatively better set. Compare measured acceleration,
current, power, RPM, offsets, saturation and repeatability before selecting alternative levels;
do not infer improvement from voltage alone. Future 1.20/1.40/1.60 V candidates require separate
staged authorization and revalidated timing, not an automatic profile change.

A wider-spaced 0.40/0.80/1.20/1.60 V set is a possible future comparison, not an enabled
waveform or a recommendation to raise a limit. It might separate acceleration rates more
clearly, but if the wheel reaches RPM/current limits early, later demands may add no useful
contrast and increase electrical risk. Comparable initial speed/load/temperature, a suitable
load and independently measured current/torque are needed to judge that tradeoff. An
unloaded sequential scan cannot establish equal initial conditions or mechanical efficiency.

A positive-current hold need not settle to constant RPM. Compare time-aligned transient
measurements, or use independently controlled load/feedback if steady-state points are needed.
Mechanical efficiency requires appropriate torque and speed measurements plus synchronized
electrical input; demand and RPM alone are insufficient. OXYGEN performs all energy integration,
Matrix Sampler calculations and visualization; no such Python analysis is added.

## Limits and staged physical commissioning

| Setting | Existing local physical config | New memory rehearsal |
|---|---|---|
| Motor AOUT max | 0.40 V, unchanged | 1.00 V, explicitly authorized **fake** fixture |
| Legacy `neutral_aout_v` | 0 V normal-stop value; existing false acknowledgement unchanged | 0 V normal stop, `neutral_verified: false` |
| Normal completion | Existing `return_neutral`, no live reauthorization | Verified 0 V demand readback, not verified stopped motion |
| Cancel/fault/shutdown | Existing policies unchanged, not safety-qualified by this prompt | Attempt 0 V demand when backend is available; unknown on failure |
| Supply range / gain | 0..0.50 V / 80 V/V, unchanged | Same, supply role only |
| Physical higher levels | None newly enabled | No physical authorization |
| 1.20 / 1.40 / 1.60 V | Proposed future levels only | Not requested by these profiles |

Previously, output-level higher-limit fields were mandatory for simulations but could be
absent for a physical clamp above 0.40 V. This gap is closed: above 0.40 V, **both** transports
must explicitly provide a sufficient `commissioned_drive_limit_v` and
`higher_drive_limit_authorized: true`. They cannot silently clamp or bypass per-profile bounds.
A matching number/Boolean is an operator attestation, not feedback or proof of commissioning.
Existing experiment-level authorizations still apply to experiment runners. Authorization at
one layer does not bypass the other.

### Normal stopping versus safety-critical termination

`Dispatcher.prepare()` verifies a zero-demand startup readback; it does not verify stationary
motion. Normal profile completion ramps to 0 V and verifies that property readback.
`Dispatcher.run()` calls the same `cleanup()` path after cancellation, faults and shutdown:
the existing policy attempts motor zero demand, then preserves/verifies supply demand. No
supply shutdown/ramp is inserted into a motor profile. On communication loss that command
may be impossible; state is reported unknown, not stopped. There is no automatic reconnect
or retry of a failed profile command. Startup before authorization performs no guessed cleanup.

**Limitation:** completion, cancellation, fault and shutdown currently share the one legacy
`neutral_aout_v`/`return_neutral` command. The software does not independently inhibit the
drive and cannot represent a separate, safety-qualified hardware fault response with these
fields. A successful zero-demand readback establishes neither stopped motion nor safe energy
flow. Process death cannot run cleanup at all. Independent emergency stop/inhibition,
watchdog/failsafe behavior and source/sink protection must cover those failures. If the VESC
review requires a different fault response, separately specify and validate that response
before changing the engine or enabling live profiles. This revision makes no such assumption.

Before any physical profile can be enabled:

1. Keep the motor independently inhibited and supply/source/sink protection verified. Obtain
   the actual VESC Tool configuration/export: ADC application/control mode, ADC voltage range,
   center/deadband, hysteresis, polarity, filtering, startup protection, signal loss and timeout
   behavior. Confirm the observed current-control mode and voltage-to-current request mapping,
   including the complete intended 0..1.6 V span; do not treat 0.3 V as coast or infer a
   low-positive-voltage braking region.
2. Record motor phase-current min/max, battery current min/max (including charge/regen),
   power/voltage/ERPM limits, low/high bus-voltage cutoffs, thermal limits and maximum acceptable
   regenerated energy. Verify supply sink capability and readiness independently over all
   approved bus demands. No numeric values are invented here.
3. Independently validate the 0 V normal-stop response and positive-current demand span using
   an approved isolated low-energy procedure. Determine actual current, acceleration, maximum
   RPM, stopping time, bus transients and energy flow. Current demand must remain bounded by
   verified controller/electrical limits even as the wheel approaches maximum RPM. Neither
   the observed 1.6 V acceleration nor a normal stop qualifies an electrical fault state.
4. Determine the physical safe response for cancellation, process death, communication loss,
   startup and shutdown. It may require an independently enforced inhibit/contactor/watchdog
   rather than another ADC value. Process death/network loss cannot reliably issue any AOUT
   command. Do not assume 0.30 V, 0 V or a successful property readback establishes safety.
5. Separately review normal zero-demand completion and the approved physical fault response,
   including the shared-command limitation above. All hardware authorizations and the local
   `neutral_verified`/assignment hold remain unchanged. Do not re-enable them merely because
   the prior 0.3 V coast interpretation has been corrected.
6. Commission the complete usable ADC span, then raise limits in reviewed stages (e.g. 0.40,
   0.60, 0.80, 1.00 V, then potentially 1.20/1.40/1.60 V). Select only profiles entirely within
   the authorized span: Gentle requires 0.60 V; regen, driving cycle and scan require 1.00 V.
   Future 1.20/1.40/1.60 V commands remain planning-only. Existing validation already supports
   explicit higher-limit stages, including 1.60 V; no limit bypass or relaxed bounds is added.
   Revalidate current/voltage/thermal and regenerative protections at each stage.
7. Use OXYGEN-native measurements/recording and independent motor inhibition throughout initial
   commissioning. Validate actual acceleration, settling, repeatability and deceleration; then
   review the staged profiles' live eligibility and exact channel mappings separately.

The one-active-profile lease, supply-demand readback prerequisite, motor-neutral verification
before supply motion, trigger observation limits, no queue, no retry and error diagnostics
are preserved. Supply demand is never modified inside these motor sequences. Buttons are not
polled during synchronous playback; a busy press can be missed and is not a stop command.
Existing SCPI -200/readback fault behavior remains fail-closed; do not reconnect, toggle
mode/source, retry, or continue to the next point after an unknown output.

## Distinguishing current demand, normal deceleration and returned energy in OXYGEN

The active dispatcher's `measurements` object is empty. Optional Python dispatcher logging is
not a new DC-power monitor or regenerative interlock. Existing illustrative experiment
feedback/synthetic data is not evidence from this rig. No telemetry subsystem is added.

Use the existing independently verified OXYGEN DC-voltage/current/power and decoded VESC
speed/current channels where available; verify channel identities, scaling, sample alignment
and polarity at the actual sensor locations. CAN current/speed alone does not establish
power returned to the RZ-XA. Record AOUT demand and align phase checkpoints with acquisition
while allowing for PC/SCPI timing uncertainty; checkpoints are local JSONL labels, not native
OXYGEN acquisition markers.

Choose and verify a sign convention, for example positive DC power flowing into the VESC:

- **Motoring:** positive measured DC power during a drive request; demand is not power.
- **Positive current demand:** 0.3 V is not coast; even low positive demand can eventually
  reach high RPM. Higher demand primarily increases acceleration, within controller limits.
- **Normal deceleration:** measured speed reduction after the demand ramps to 0 V. Friction, dissipation or
  capacitor charging can brake without returning useful energy to the source/sink.
- **Regeneration:** sustained negative measured DC power at a verified measurement point,
  above uncertainty/offsets and time-aligned with braking. To claim energy returned to the
  RZ-XA, additionally establish the energy path and supply-side sink acceptance; motor-side
  negative power alone may only charge a DC-link capacitor or feed another load.

OXYGEN performs all energy integration, Matrix Sampler calculations and visualization. Do not
infer regeneration from decreasing speed, a low ADC voltage, the button name, or simulation.
Without independent feedback the open-loop dispatcher cannot enforce physical bus/regen safety.

## Offline validation commands

From the repository root:

- `./.venv-scpi/Scripts/python.exe -m oxygen_engine dispatch-patterns --bindings examples/ces-motor-commissioning.json`
  validates the staged eight-button config without connecting or writing; do not add `--live`.
- For an approximately 78-second in-process rehearsal, use the same bindings with
  `--execute --confirm-simulated-output --scenario scenarios/ces_motor_candidates.json`.
  This remains a memory fixture, not OXYGEN simulated hardware or a real controller.
- `./.venv-scpi/Scripts/python.exe -m pytest -q tests/test_ces_motor_candidates.py tests/test_experiments.py tests/test_regen_deceleration.py`
- `./.venv-scpi/Scripts/python.exe -m pytest -q`

Tests use a virtual clock and the production timeline/writer with a memory backend for the
new waveforms. Coverage includes all four exact durations, bounds, contiguous ramps, explicit
zero-demand deceleration phases, repeated completion, exact measurement-window checkpoints,
startup no-jump, supply-demand interlocks, global ownership, cancellation during drive and
deceleration observation, zero-demand cleanup in memory, unknown state on disconnect without
reconnect, nonzero-normal-stop rejection for all transports and staged
higher-limit validation through 1.60 V. Real hardware acceptance remains outstanding.

### Validation for this revision

| Check | Result |
|---|---|
| Focused candidate tests | **48 passed in 1.69 s** |
| Full `python -m pytest -q` | **630 passed in 40.41 s; no failures or skips** |
| `python -m compileall -q oxygen_engine tests` | Passed |
| Eight-button preview | Memory transport, `connected: false`, `writes_attempted: 0`; durations 10/18/24/22 s |
| Local operator file | SHA256 matches the pre-edit value; no content changes |
| Supply profiles / limits | No RZ-XA profile changes; supply range/mapping and physical motor clamp unchanged |
| `git diff --check` | Passed; normal LF-to-CRLF conversion warning on historical scan file only |
| Visual Studio solution build | Unavailable: no solution loaded / `Build.BuildSolution` unavailable |
| Physical tests | None performed |

Commit `5b317d4` reported 626 passing offline tests for its prior interpretation. That report
is historical, not evidence for the corrected waveforms. The tests now explicitly require
zero-demand normal completion and reject 0.3 V as a startup normal-stop condition. Existing
physical acknowledgements remain required; none is restored to make a test pass.

The additional stage tests exercise a hypothetical 1.60 V waveform only in an explicitly
authorized memory fixture, and show that a higher physical ceiling does not replace the
required stop acknowledgement. Injected SCPI -200 failure retains interruption diagnostics,
does not retry the failed current-demand command, and uses the existing separate zero-demand
cleanup attempt without modifying supply AOUT. None of these tests validates fault safety.

### Changed files for this revision

Source:
- `oxygen_engine/schema.py`

Revised staged profile JSON:
- `profiles/ces/motor_gentle.json`
- `profiles/ces/motor_coast_compare.json`
- `profiles/ces/motor_stop_go.json`
- `profiles/ces/motor_three_level.json`

Historical motor JSON (descriptions/warnings only; old waveforms retained):
- `profiles/motor_gentle.json`
- `profiles/motor_coast_compare.json`
- `profiles/motor_stop_go.json`
- `profiles/motor_three_level.json`

Revised memory configuration/scenario:
- `examples/ces-motor-commissioning.json`
- `scenarios/ces_motor_candidates.json`

Tests:
- `tests/test_ces_motor_candidates.py`
- `tests/README.md`

Documentation:
- `README.md`
- `docs/open-loop-experiments.md`
- `docs/ces-motor-profile-commissioning.md`

Reviewed but **unchanged**:
- `oxygen_engine/drive_limits.py`: explicit higher-demand staging/authorization retained.
- `oxygen_engine/dispatcher.py`: existing completion, cleanup and ownership paths retained.
- Local, gitignored `dispatcher.json`: no flags, names, profiles, limits or policies changed;
  the pre-existing false acknowledgement/disabled motor assignments remain in place.
