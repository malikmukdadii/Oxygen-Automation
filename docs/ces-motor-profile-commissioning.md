# CES motor profiles: staged dynamic drive and braking

## Status: memory rehearsal only, not a physical authorization

Physical observations on `Drive Control` (AO2/1, VESC ADC1) contradict the previous neutral
assumption: 0 V caused strong deceleration/braking, 0.1..0.2 V may be a braking region,
approximately 0.3 V may coast, and commands above it may accelerate. Neither torque,
zero-torque/coast nor regenerative energy acceptance has been characterized. No numeric
VESC motor-current, battery-current, regenerative-current limits or VESC ADC-configuration
export was found in the repository. Experiment abort thresholds and synthetic feedback
models are **not** controller settings or evidence of commissioned limits.

All four redesigned replacements are staged under `profiles/ces/`, using the actual schema-3
`set_aout`, `ramp_aout`, `hold`, `checkpoint`, and `return_neutral` steps. They require a
modeled 0.30 V neutral and therefore **cannot load under the physical neutral contract**.
The existing dispatcher permits this nonzero neutral only with `transport: memory`.
No physical fallback was changed to 0.30 V. Historical root motor profiles retain their
waveforms for legacy configuration/offline compatibility; their descriptions now warn that
they must not be used on this observed CES controller mapping. No RZ-XA profile was modified.

The local, gitignored `dispatcher.json` retains all eight trigger strings, assignment names,
profile paths, 0.40 V ceiling, neutral value and termination policies. Its four motor assignments
are disabled and its contradicted `neutral_verified` acknowledgement is now false. Existing
physical validation refuses this config before a connection or any write. **This intentionally
blocks supply dispatch too while a motor role with unverified neutral is configured.** Do not
bypass that refusal by declaring 0 V verified, removing the motor role, changing all fallbacks
to 0.30 V, or disabling the supply/motor interlocks. Independently inhibit the drive and review
the entire safe-state procedure first.

## Offline configuration and unchanged button identities

`examples/ces-motor-commissioning.json` is a new memory-only rehearsal config, not the live
operator file. It uses fake in-process AOUT fixtures, an initial supply demand of 0.50 V and
an initial modeled motor coast of 0.30 V. All physical actuation flags are false; both mapping
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

Every voltage below is **VESC AOUT demand in volts**, not bus voltage, torque or regulated
speed. Never apply the RZ-XA 80 V/V mapping to this output. All sequences begin and end at
modeled 0.30 V coast; none commands 0 V. Holds maintain demand, not measured wheel speed.
Durations are nominal playback time, excluding startup, verification and I/O delays.

### Motor Gentle — 10 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..3 | Smooth ramp 0.30 -> 0.60 V |
| 3..7 | Hold 0.60 V for sustained drive demand |
| 7..10 | Smooth ramp 0.60 -> 0.30 V; complete at modeled coast |

No intentional braking command is included. Physical rotation at this demand is a test
objective, not an outcome established by the memory backend.

### Run Regen Test — 18 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..4 | Ramp 0.30 -> 0.80 V, acceleration request |
| 4..8 | Hold 0.80 V, drive plateau |
| 8..10 | Ramp 0.80 -> 0.30 V, release to modeled coast |
| 10..12 | Hold 0.30 V, coast comparison window |
| 12..13 | Ramp 0.30 -> 0.10 V, distinct braking request |
| 13..15 | Hold 0.10 V, braking observation window |
| 15..16 | Ramp 0.10 -> 0.30 V, brake release |
| 16..18 | Hold 0.30 V, modeled coast; complete |

Phase-specific checkpoints mark acceleration, drive plateau, coast, braking and release.
The button name is retained; a braking command does **not** prove regeneration.

### Run Driving Cycle — 24 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..2 / 2..4 | Ramp 0.30 -> 0.40 V / hold 0.40 V |
| 4..5 / 5..6 | Ramp to 0.30 V / coast hold |
| 6..8 / 8..10 | Ramp 0.30 -> 0.60 V / hold 0.60 V |
| 10..11 / 11..12 | Ramp to 0.15 V / braking hold |
| 12..13 / 13..14 | Ramp to 0.30 V / coast hold |
| 14..16 / 16..19 | Ramp 0.30 -> 0.80 V / hold 0.80 V |
| 19..20 / 20..22 | Ramp to 0.10 V / braking hold |
| 22..23 / 23..24 | Ramp to 0.30 V / coast hold; complete |

Three progressively higher drive bursts, short coast windows, and distinguishable braking
requests replace ineffective low-level acceleration and long idle periods.

### Run Efficiency Scan — 22 seconds

| Time (s) | Demand / phase |
|---|---|
| 0..1 / 1..5 | Ramp 0.30 -> 0.40 V / 4 s hold |
| 5..6 / 6..10 | Ramp to 0.60 V / 4 s hold |
| 10..11 / 11..15 | Ramp to 0.80 V / 4 s hold |
| 15..16 / 16..20 | Ramp to 1.00 V / 4 s hold |
| 20..22 | Ramp to 0.30 V; complete |

There is no sub-coast acceleration stage. Four-second holds are candidate observation windows,
not guarantees of stable speed/power or valid efficiency. In OXYGEN, verify settling and
repeatability; revalidate timing if longer holds are required. No efficiency computation or
Matrix Sampler logic is added to Python.

## Limits and staged physical commissioning

| Setting | Existing local physical config | New memory rehearsal |
|---|---|---|
| Motor AOUT max | 0.40 V, unchanged | 1.00 V, explicitly authorized **fake** fixture |
| Motor neutral | 0 V value retained but acknowledgement invalidated; physical config blocked | 0.30 V modeled coast, `neutral_verified: false` |
| Normal completion | Existing `return_neutral`, not reauthorized | Return to modeled 0.30 V |
| Cancel/fault/shutdown | Existing policies unchanged, not reauthorized | Modeled return to 0.30 V when backend is available |
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

Before any physical profile can be enabled:

1. Keep the motor independently inhibited and supply/source/sink protection verified. Obtain
   the actual VESC Tool configuration/export: ADC application/control mode, ADC voltage range,
   center/deadband, hysteresis, polarity, filtering, startup protection, signal loss and timeout
   behavior, and whether ADC1 requests torque, current, speed or braking in each region.
2. Record motor phase-current min/max, battery current min/max (including charge/regen),
   power/voltage/ERPM limits, low/high bus-voltage cutoffs, thermal limits and maximum acceptable
   regenerated energy. Verify supply sink capability and readiness independently over all
   approved bus demands. No numeric values are invented here.
3. Independently characterize coast/zero-torque, braking and drive spans using an approved
   isolated low-energy procedure. A 0.30 V hypothesis is not permission to move immediately to
   that voltage; a 0 V command may be strong braking, not safe neutral or an emergency stop.
4. Determine the physical safe response for cancellation, process death, communication loss,
   startup and shutdown. It may require an independently enforced inhibit/contactor/watchdog
   rather than another ADC value. Process death/network loss cannot reliably issue any AOUT
   command. Do not assume 0.30 V, 0 V or a successful property readback establishes safety.
5. Separately review and implement the distinction between normal coast completion and the
   approved physical fault response. **This change deliberately does not enable nonzero
   physical neutral or add a new unverified physical fallback.** The current single-neutral
   termination policy cannot represent different coast and fault commands safely without that
   decision. Do not re-enable the local `neutral_verified` flag or motor assignments yet.
6. Commission the complete usable ADC span, then raise limits in reviewed stages (e.g. 0.40,
   0.60, 0.80, 1.00 V). Select only profiles entirely within the authorized span: Gentle requires
   0.60 V, regen/cycle 0.80 V, scan 1.00 V. Braking down to 0.10/0.15 V requires separate
   authorization as well. Future 1.20/1.40/1.60 V levels are neither enabled nor needed here.
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

## Distinguishing drive, coast, braking and returned energy in OXYGEN

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
- **Coast candidate:** minimal commanded torque, speed decay under actual load and measured
  power near losses/offsets. Zero net DC power alone does not prove zero torque or no braking.
- **Braking:** measured deceleration under the low-ADC request. Friction, dissipation or
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
brake/coast phases, repeated completion, checkpoint ordering, startup no-jump, supply-demand
interlocks, global ownership, cancellation during drive/braking, fault cleanup in memory,
unknown state on disconnect without reconnect, physical nonzero-neutral rejection and staged
higher-limit validation through 1.60 V. Real hardware acceptance remains outstanding.

### Final offline results

| Check | Result |
|---|---|
| Focused candidate / experiment / regen tests, initial run | 109 passed in 16.27 s |
| `python -m compileall -q oxygen_engine tests` | Passed |
| Full `python -m pytest -q`, final run | **626 passed in 39.34 s; no failures or skips** |
| New eight-button preview | `transport: memory`, `connected: false`, `writes_attempted: 0`; durations 10/18/24/22 s |
| Local physical preview | Expected exit 2: disputed neutral acknowledgement prevents connection |
| `git diff --check` | Passed; Git notes normal LF-to-CRLF conversion for the historical scan file |
| Physical tests | None performed |

The first full run exposed a pre-existing test dependency on the developer's local operator
file. The default-config test now uses an isolated temporary project root and tests both
example fallback and operator-file selection; a separate regression proves an invalidated
physical neutral acknowledgement blocks the CLI. No physical authorization was restored to
make a test pass.

### Exact changed-file inventory

Source:
- `oxygen_engine/schema.py`
- `oxygen_engine/drive_limits.py`

New staged profile JSON:
- `profiles/ces/motor_gentle.json`
- `profiles/ces/motor_coast_compare.json`
- `profiles/ces/motor_stop_go.json`
- `profiles/ces/motor_three_level.json`

Historical motor JSON (descriptions/warnings only; old waveforms retained):
- `profiles/motor_gentle.json`
- `profiles/motor_coast_compare.json`
- `profiles/motor_stop_go.json`
- `profiles/motor_three_level.json`

New memory configuration/scenario:
- `examples/ces-motor-commissioning.json`
- `scenarios/ces_motor_candidates.json`

Tests:
- `tests/test_ces_motor_candidates.py`
- `tests/test_dispatcher_v3.py`
- `tests/test_experiments.py`
- `tests/README.md`

Documentation:
- `README.md`
- `docs/open-loop-experiments.md`
- `docs/ces-motor-profile-commissioning.md`

Local-only, gitignored safety changes (not included in a Git diff/PR):
- `dispatcher.json`: disable the four motor assignments and invalidate output
  `neutral_verified`; keep all eight names/paths, limits, neutral value and termination
  policies unchanged. This state must be preserved during review, not overwritten with a
  permissive example.
