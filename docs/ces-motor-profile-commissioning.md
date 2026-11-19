# CES motor profiles: current-demand acceleration and normal stopping

## Status: operator-authorized physical ADC1/ADC2 on the existing V3 dispatcher

Following PR #3 (`19c5fb8`), physical `vesc_brake` authorization is functional; it is no longer
unconditionally rejected. No new dispatcher, transport, current-setting control or telemetry
system is introduced. Operator-confirmed mapping is `Current No Reverse Brake ADC2`:

- `Drive Control`, AO2/1, ADC1: 0 V is zero drive demand; the wheel coasts with ADC2 released.
  Positive drive demand commands current/torque, not regulated speed. Immediate commands up
  to 1.6 V were manually tested on the secured motor.
- `Braking Control`, AO2/2, ADC2: 0 V releases braking. Nonzero requests are controlled by
  assignment parameters, independently of ADC1 and the configured ADC2 input span.
- `Takasago Voltage Control`, AO2/3: unchanged RZ-XA demand, 80 V/V and 0..0.5 V AOUT.

Existing VESC controller-current settings remain unchanged. Voltage is not a calibrated
measurement of amperes, torque or RPM. Constant positive demand need not produce constant
speed. Neither speed reduction nor an ADC2 command proves regenerated energy.

The legacy schema identifiers `neutral_aout_v`, `motor_neutral` and `return_neutral` retain
their existing meaning as zero-drive command/readback, not proof of stationary motion or
an emergency-stop function. The coordinated profiles additionally return ADC2 to release.
Waveform timing, drive peaks, transition ordering, mutual exclusion and global ownership
are preserved. Only the requested ADC2 ramp targets now use assignment parameter references.

The local, gitignored `dispatcher.json` is **not modified, replaced or enabled**. The operator
will install their physical configuration manually. Configured maxima and existing explicit
authorizations are authoritative; this change adds no lower software ceiling or hidden
commissioning gate. Output-demand readback remains distinct from physical bus/regen safety.

## Configuration and unchanged button identities

`examples/ces-physical-three-output.json` shows the physical three-output configuration.
It enables physical AOUT and brake control, authorizes all three exact physical channel
names, retains **1.60 V** drive authorization and specifies a **0..3.3 V** ADC2 input-command
span. Its true hardware acknowledgement fields represent explicit operator confirmations,
not feedback from Python. Review those existing confirmations for the actual installation.

`examples/ces-motor-commissioning.json` and `examples/ces-eight-button.json` remain memory-only
rehearsals with all physical enable/authorization flags false. Their brake input-command span
also is 0..3.3 V. No memory fixture is used as evidence of actual regenerative current.

Profile paths are relative to the configuration file: the examples use `../profiles/...`.
When manually transferring an example to root-level `dispatcher.json`, use `profiles/...`
instead. Do not copy over the operator file automatically. Parameters are read at startup;
restart the dispatcher to load edits rather than expecting live hot-reload.

The example preserves the inspected operator's eight names and logical functions:

| Exact current DOUT string | Assignment | Staged profile |
|---|---|---|
| `Ramp Enable` | `rzxa_to_40_hold` | Existing `rzxa_ramp_hold.json`, unchanged |
| `Ramp Disable` | `rzxa_down` | Existing `rzxa_ramp_down.json`, unchanged |
| `Motor Gentle` | `motor_gentle` | `profiles/ces/motor_gentle.json` (0.60 V) |
| `Run Regen Test` | `regen_test` | Default Trial B `motor_regen_trial_b.json` (1.60 V); Trial A coast via `motor_coast_compare.json` |
| `Run Driving Cycle` | `driving_cycle` | `profiles/ces/motor_stop_go.json` (0.60 / 1.00 / 1.60 V) |
| `Run Efficiency Scan` | `efficiency_scan` | `profiles/ces/motor_three_level.json` (0.40 / 0.80 / 1.20 / 1.60 V) |
| `RZ-XA Step Down` | `rzxa_voltage_down` | Existing `rzxa_voltage_down.json`, unchanged |
| `RZ-XA Step Up` | `rzxa_voltage_up` | Existing `rzxa_voltage_up.json`, unchanged |

These are operator-supplied channel strings, not inferred chassis identities.
`examples/ces-eight-button.json` matches the same eight exact DOUT strings (including
**RZ-XA Step Down/Up**). Do not overwrite gitignored local `dispatcher.json`. The scan retains
the historical `motor_three_level.json` basename; the waveform is four levels through 1.60 V.

## Brake intensity: assignment parameters, not output ceilings

Optional assignment `parameters` is an object of finite JSON numbers. The parameterized
`ramp_aout` target syntax is `{"parameter": "brake_target_v"}`. Resolution occurs before
waveform range, ordering, rate and completion validation; compilation sees numeric targets.
Existing numeric targets and schema version 3 continue working unchanged.

| Assignment | Editable parameters in `dispatcher.json` | Example request |
|---|---|---|
| `regen_test`, Trial B | `brake_target_v` | 0.20 V |
| `driving_cycle` | `brake_light_v`, `brake_heavy_v` | 0.15 V, 0.20 V |

For example, set `parameters` to `{"brake_target_v": 0.20}` on the existing regen assignment,
or `{"brake_light_v": 0.15, "brake_heavy_v": 0.20}` on the existing driving-cycle assignment.
To tune braking, edit only these numbers, not the profiles or output ceiling. Preview lists
the selected parameters. No missing value is silently defaulted, clamped or replaced by the
maximum. Missing references, strings, Booleans, nulls, nonfinite numbers and out-of-range
resolved targets produce explicit configuration errors before connection/writes.

`max_aout_v` / `brake_max_aout_v` specify the permitted ADC2 command range, **not the requested
brake intensity**. The example's 3.3 V upper bound reflects the configured analog input span;
it does not assert acceptable regenerative current at every value. A 0.20 V request remains
0.20 V even when the ceiling is 3.3 V. No 80 V/V supply scaling applies to either VESC input.

## Waveform commands and timing

Every voltage below is **VESC AOUT current demand in volts**, not a calibrated torque,
current in amperes, bus voltage or regulated speed. Never apply the RZ-XA 80 V/V mapping
to this output. All sequences begin and end at ADC1 zero demand; ADC2 finishes released.
Holds maintain demand, not measured wheel speed; the wheel may continue accelerating.
Durations are nominal playback time, excluding startup, verification and I/O delays.

### Motor Gentle — 10 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..3 | Smooth ramp 0 -> 0.60 V, modest acceleration demand |
| 3..7 | Hold 0.60 V for sustained drive demand |
| 7..10 | Smooth ramp 0.60 -> 0 V; normal completion |

The final ramp removes drive demand; it is not an ADC2 brake command or proof of a stopped wheel.
Actual RPM, stopping time and electrical power must be measured; memory does not model them.

### Run Regen Test - Trial A coast / Trial B regen (~16-18 s)

Operator-tested ADC1 peak is **1.60 V**. Same DOUT selects Trial A or B by assignment profile.

**Trial A** (`motor_coast_compare.json`): accel 0->1.60 V / hold / ->0 V, ADC2 stays at **0 V**
release, observe passive coast. **Trial B** (`motor_regen_trial_b.json`, CES default): same
accel, then prompt ADC2 braking with assignment `brake_target_v` (example 0.20 V). Trial A
remains coast-only; Trial B remains the default. Neither timing nor drive peaks changed.
Unmatched open-loop timing is **not** proven regeneration or matched start RPM. Physical
ADC2 uses the existing explicit authorization/release confirmations, not ADC1 authorization.

### Run Driving Cycle - ~23 seconds

Peaks **0.60 / 1.00 / 1.60 V** with distinct coast and parameter-tuned ADC2 brake intervals;
brake released before every accel; drive-to-zero+verify before every brake. Three progressively
higher current demands illustrate acceleration rates, not regulated RPM setpoints. Only the
two requested intensities use `brake_light_v` and `brake_heavy_v`; timing is unchanged.

### Run Efficiency Scan - ~22 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..1 / 1..5 | Ramp 0 -> 0.40 V / 4 s hold |
| 5..6 / 6..10 | Ramp to 0.80 V / 4 s hold |
| 10..11 / 11..15 | Ramp to 1.20 V / 4 s hold |
| 15..16 / 16..20 | Ramp to 1.60 V / 4 s hold |
| 20..22 | Ramp to 0 V; normal completion |

Levels **0.40 / 0.80 / 1.20 / 1.60 V** preserve the operator-tested ADC1 upper-command
authorization. ADC2 remains released for the entire scan. Compare measured acceleration,
current, power and RPM before treating any level as a steady operating point; do not infer
improvement from voltage alone. An unloaded sequential scan cannot establish equal initial
conditions or mechanical efficiency.

A positive-current hold need not settle to constant RPM. Compare time-aligned transient
measurements, or use independently controlled load/feedback if steady-state points are needed.
Mechanical efficiency requires appropriate torque and speed measurements plus synchronized
electrical input; demand and RPM alone are insufficient. OXYGEN performs all energy integration,
Matrix Sampler calculations and visualization; no such Python analysis is added.

## Limits and staged physical commissioning

| Setting | Physical three-output example | Memory examples |
|---|---|---|
| Motor AOUT max | **1.60 V**, commissioned limit 1.60 V / higher authorization true | Same ceiling and explicit higher authorization |
| Legacy `neutral_aout_v` | 0 V zero drive; `neutral_verified: true` is operator confirmation | 0 V; `neutral_verified: false` |
| ADC2 range / release | 0..3.3 V span / 0 V release, explicitly confirmed | Same span/release; confirmation false |
| ADC2 request | Assignment parameters: 0.20 V Trial B; 0.15/0.20 V cycle examples | Same defaults, editable per assignment |
| Normal completion | Drive zero and brake release verified by setpoint readback | Same; not proof of stationary motion |
| Cancel/fault/shutdown | Existing zero-drive then brake-release attempts, supply preserved | Same policy with memory readback |
| Supply range / gain | 0..0.50 V / 80 V/V, unchanged | Same, supply role only |
| Physical authorization | All three roles true; explicit brake enable and confirmations required | All physical flags/permissions false |

Previously, output-level higher-limit fields were mandatory for simulations but could be
absent for a physical clamp above 0.40 V. This gap is closed: above 0.40 V, **both** transports
must explicitly provide a sufficient `commissioned_drive_limit_v` and
`higher_drive_limit_authorized: true`. They cannot silently clamp or bypass per-profile bounds.
A matching number/Boolean records the operator's authorization; it is not live feedback.
The legacy 0.40 V default is not an additional ceiling when these fields authorize 1.60 V.
Existing experiment-level authorizations still apply to experiment runners. Authorization at
one layer does not bypass the other.

### Normal stopping versus safety-critical termination

`Dispatcher.prepare()` verifies a zero-demand startup readback; it does not verify stationary
motion. Normal profile completion verifies zero drive and brake release property readbacks.
`Dispatcher.run()` calls the same `cleanup()` path after cancellation, faults and shutdown:
the existing policy attempts motor zero demand, then brake release, then preserves/verifies supply demand. No
supply shutdown/ramp is inserted into a motor profile. On communication loss that command
may be impossible; state is reported unknown, not stopped. There is no automatic reconnect
or retry/continuation of a failed profile command. Cleanup is a separate best-effort attempt,
not profile resumption. Startup before authorization performs no guessed cleanup. In the
closed-loop backend, authorized brake release has the same readiness-independent exception
as zero drive; non-release brake commands retain their existing readiness check.

**Limitation:** completion, cancellation, fault and shutdown currently share the one legacy
`neutral_aout_v`/`return_neutral` command. The software does not independently inhibit the
drive and cannot represent a separate, safety-qualified hardware fault response with these
fields. A successful zero-demand readback establishes neither stopped motion nor safe energy
flow. Process death cannot run cleanup at all. Independent emergency stop/inhibition,
watchdog/failsafe behavior and source/sink protection must cover those failures. If the VESC
review requires a different fault response, separately specify and validate that response
before changing the engine or enabling live profiles. This revision makes no such assumption.

### Operator workflow using existing confirmations

1. Prepare the operator configuration manually, preserving exact names, source discovery and
   the confirmed 1.60 V ADC1 ceiling/commissioned limit/higher authorization. Keep all existing
   controller-current settings unchanged. Set `enable_physical_aout` and
   `enable_physical_brake` true; each physical role uses simulation false / physical true.
   Keep RZ-XA SCPI and output toggling disabled. The physical example shows these settings.
2. Record the existing wiring, zero-drive, brake-release, external enable and emergency-stop
   confirmations. Brake release uses both `outputs.vesc_brake.brake_release_verified: true`
   and `hardware_authorization.brake_release_verified: true`. Open-loop external bus/regen
   protection acknowledgements remain required. Startup reads all selected AOUT properties;
   it refuses a nonzero drive or unreleased brake without guessing a startup write.
3. Preview without writes using
   `python -m oxygen_engine dispatch-patterns --bindings examples/ces-physical-three-output.json`.
   Choose the assignment brake parameters separately from the input-command span. When the
   operator chooses to execute, the existing CLI still requires `--execute --live` and exact
   `--confirm-output` names for **all three** channels, including `Braking Control`. No new
   brake-specific CLI switch, numeric cap or hidden commissioning acknowledgement is added.
4. Tune only the requested brake parameters in `dispatcher.json`, then restart and inspect
   the preview/startup summary. Use OXYGEN's independently verified bus/current/power and RPM
   signals to assess actual braking and returned power within the existing controller-current
   and source/sink protections. A permitted 3.3 V command span is not a statement that all
   brake intensities are acceptable. Software demand is not an emergency stop; retain the
   independent physical protections already confirmed by the operator.

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
- **Coast versus braking:** ADC1 zero with ADC2 released removes drive demand. Nonzero ADC2
  requests braking; actual speed/current response must be measured. Friction, dissipation or
  capacitor charging can decelerate the wheel without returning useful energy to the source/sink.
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
- `profiles/ces/motor_coast_compare.json` (Run Regen Test Trial A coast)
- `profiles/ces/motor_regen_trial_b.json` (Run Regen Test Trial B regen, CES default)
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
