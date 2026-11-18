# Coordinated DC-to-AC experiments — commissioning guide

## Scope

`run-experiment` is a one-shot sequencing layer over the existing V3 `Dispatcher.command`,
`OxygenBackend`, discovery, authorization, bounds and constant-property readback checks. It is
not a second dispatcher/backend or a replacement for V3 single-output profiles.

Keep one authoritative `dispatcher.json`. The separate files under `experiments/` describe
experiment intent, feedback bindings and limits; they are not V3 profile files. No working
Ramp Enable, Ramp Disable or Motor Gentle assignments were changed. Do not run the DOUT
monitor concurrently with an experiment. Both CLI execution paths take a shared advisory
endpoint lease for their entire connect/run/cleanup lifecycle. This excludes another local
Python CLI owner using the same endpoint, not OXYGEN-native actions, external clients, or all
possible hostname aliases. Use one controller and inspect native DOUT actions before commissioning.

Only these two roles are allowed:

| Physical AOUT name | Logical role | Current configured clamp |
|---|---|---|
| Takasago Voltage Control | `rzxa_voltage_demand` | 0..0.50 V |
| Drive Control | `vesc_throttle` | 0..0.40 V; verified neutral 0 V |

`Braking Control` is neither configured nor commanded. ADC1 is in **Current mode**: plateaus
are current-demand input voltages, not controlled speed targets. The report of maximum unloaded
speed near 1.6 V is a warning, not a safe target. Even 0.25 V must be commissioned against actual
current, RPM, load and protection settings. The runner does not regulate speed or bus voltage.

## Plans and authoritative limits

- `experiments/dc_ac_grid.example.json`: 36/38/40 V bus targets and 0.25/0.35/0.45/0.55 V drive
  candidates. Only 0.25/0.35 are enabled. Therefore **six points execute and six are disabled**.
- `experiments/dynamic_motor.example.json`: accelerate to 0.25 V, increase to 0.35 V, then ramp
  to neutral and observe coast-down. All targets remain below today's clamp. It does not
  assert steady-state capture or active regenerative braking.

The verified supply scaling is 80 bus V per demand V: 36/38/40 V correspond to
0.450/0.475/0.500 V AOUT. This arithmetic is not actual bus feedback.

To permit a higher drive target, **all** of the following must agree, after physical commissioning:

1. The authoritative V3 `outputs.vesc_throttle.max_aout_v`.
2. The experiment's `commissioned_drive_limit_v`.
3. `higher_drive_limit_authorized: true` for a limit/target above 0.40 V.
4. That target's `enabled: true` and `commissioned: true`.
5. The global experiment commissioning acknowledgements and verified physical protection settings.

No value is raised or normalized automatically. Disabled out-of-range candidates remain in
point-numbered logs with their reason, but are never sent. The sample target `commissioned`
fields for the low inputs describe the proposed plan; global hardware commissioning starts
false, so the template is not hardware-ready. Current, ERPM, settling, slew-rate and bus bounds
in the examples are **uncommissioned initial settings**, not certified equipment limits. Review
all of them; do not simply set acknowledgements true to clear errors.

## Fresh feedback and native Matrix Sampler

Complete the experiment plan's `feedback` bindings with exact existing/derived OXYGEN channel
names. They must be unique, independent of AOUT commands and DOUT trigger inputs. Numeric
channels must expose the following normalized units through their `Unit` property:

| Role | Unit / meaning |
|---|---|
| `dc_bus_voltage` | V, actual independent DC bus measurement |
| `dc_current` | A, actual signed DC input current |
| `dc_power` | W, actual signed DC power from the commissioned DC analysis |
| `ac_power` | W, total three-phase active electrical power, not RMS V times RMS I |
| `erpm` | rpm, decoded CAN **electrical** RPM; do not silently substitute mechanical RPM |
| `motor_current` | A, decoded VESC motor current with verified scaling |
| `acquisition_time_s` | s, increasing acquired time/counter expressed in seconds, not a host constant |
| `vesc_telemetry_age_s` | s, actual age since the relevant CAN telemetry was received |
| `matrix_sample_count` | dimensionless (`""` or `"1"`), monotonically increasing accepted count for this native sampler |

Boolean roles require actual BOOL properties, named explicitly in `property`:

- `source_sink_ready`: real permission from the independently commissioned source/sink readiness
  logic. It must represent the supply's actual permitted operating/sinking condition.
- `physical_fault`: true when any relevant external fault/protection requires abort.
- `matrix_sample_valid`: native sample/window validity for the selected efficiency sampler.

The two Matrix roles are required for grid runs only, not dynamic runs. Do not bind these roles
to manual always-true flags or invent an accepted count. If this OXYGEN installation cannot
expose the native sampler's accepted count/validity as documented channels/properties, **hardware
grid execution cannot yet be commissioned**. Establish a supported export/API with the operator
or vendor; no undocumented Matrix SCPI command or GUI-click fallback is implemented.

Configure the existing native steady-state Matrix Sampler in OXYGEN. Its axes, signals, validity,
steady-state dwell, binning and gating must match the intended measurements. The experiment
never clicks the GUI, writes DOUTs, starts acquisition/recording, clears a matrix or sends a
Matrix trigger command. OXYGEN must already be acquiring and recording as required.

For each enabled point the runner waits for its own bus/current/ERPM settling window, records
the native accepted count, then observes the capture window. It requires fresh readings,
continued bus/readiness/protection validity, stable measurements, native validity throughout
that window and an increased accepted count. A readback of commanded setpoints is insufficient.

The observer does **not** prevent native samples during ramps or associate a native matrix cell
with a Python experiment ID. Configure native gating to reject unwanted transients and bind the
count to the exact sampler. A counter from another sampler is not evidence for this point.
The Python point log is an audit of the accepted window, not a readback/export of the matrix cell.

Feedback uses a fixed, allowlisted batch numeric transfer and pinned input identities on the
same serialized connection. The runner checks acquisition Started, query age, advancing acquired
time, CAN age, finite/type-correct values, current/ERPM/bus limits, DC power consistency and
source/sink fault/readiness. It never substitutes command-derived voltage for measured voltage.
A local query timestamp alone is not instrument sample time; commission the acquired-time/CAN-age
signals, numeric-transfer behavior and rates. These checks do not prove each physical sensor is
healthy or all channels are sampled simultaneously. Do not share the SCPI numeric list with
another controller. Large channel lists/slow transfers may fail the freshness or I/O limit;
that is a refusal, not permission to increase limits without measurement.

## Commissioning procedure

1. Resolve the known RZ-XA Const2/SCPI -200 property-loss issue using the separate investigation
   in `scpi-ramp-fault-investigation.md`, first with independently inhibited/isolated equipment.
   `supply_scpi_fault_resolved` starts false. This milestone does not fix that OXYGEN-side failure.
2. Verify wiring, 80 V/V scaling, 0 V neutral, ADC1 Current mode, source/sink operating envelope,
   independent stop/inhibit and VESC motor/battery current, ERPM and thermal/protection settings.
   Verify the plan's conservative software limits against that evidence. Python is not protection
   hardware, and command neutral does not establish a stopped motor.
3. Verify every feedback binding/unit, CAN age, polarity and instrument timing. Check actual DC
   power against V×I. Configure synchronized, appropriately ranged/bandwidth three-phase sensing.
   Establish signed power at the defined measurement boundary.
4. Commission native Matrix Sampler gating, count and validity before a grid run. The dynamic
   demo does not need `native_matrix_verified`, but still needs all safety feedback and other
   commissioning declarations. A controlled load/dynamometer may be required: a current-command
   motor with no load may accelerate continuously and never settle. The runner must time out or
   trip its ERPM limit rather than claim a successful point.
5. Copy the chosen example to an ignored `*.local.json` plan without overwriting existing files.
   Fill real bindings/limits, then acknowledge only verified conditions. Keep higher candidates
   disabled. Preview and rehearse offline with the same authoritative bindings.
6. Stop the persistent DOUT dispatcher before any separately approved hardware experiment. Native
   OXYGEN actions/other clients must not write these outputs. The experiment ignores DOUT triggers;
   use Ctrl+C for a software cancellation attempt and independent physical stop when required.

### Offline commands (no OXYGEN connection)

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-experiment --bindings dispatcher.json --experiment experiments/dc_ac_grid.example.json
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-experiment --bindings dispatcher.json --experiment experiments/dc_ac_grid.example.json --memory --execute --confirm-simulated-output
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-experiment --bindings dispatcher.json --experiment experiments/dynamic_motor.example.json --memory --execute --confirm-simulated-output
```

Preview never connects. `--memory` mirrors the validated V3 bindings' clamps into the existing
MemoryBackend, removes physical permission and uses an accelerated virtual clock with explicit
FAKE plant/native-sampler values. It cannot commission a motor or modify OXYGEN's real matrix.
The fake plant has instantaneous bus/speed response and 85% electrical efficiency by construction.
The successful 12-point test uses a **test-only**, explicitly authorized 0.55 V bindings/plan pair;
it does not raise today's example or local hardware limit. Memory-example V3 schema limits are
unchanged.

After commissioning only, the physical command uses the selected local plan, `--execute --live`
and both exact `--confirm-output` names, just like the existing physical authorization path.
It must not include `--memory` or simulated confirmation. No physical execution is recommended
until all steps above are complete. No hardware run was performed during implementation.

## Expected timeline

For each bus row: verify motor command neutral and measured ERPM below the supply-change limit;
ramp supply demand in 5 s; require measured bus in tolerance continuously for 1 s (bounded by
10 s). For each enabled drive target: ramp in 4 s; settle for at least 3 s (bounded by 20 s);
capture for 5 s; ramp to neutral in 4 s. Before changing the next bus, wait for measured speed
to enter the configured safe window (at most 20 s). Low speed, not merely neutral command,
permits supply motion.

| Initial grid phase | Virtual-clock completion |
|---|---:|
| 36 V convergence | 6 s |
| Point 1: 36 V / 0.25 V, captured and neutral | 22 s |
| Point 2: 36 V / 0.35 V, captured and neutral | 38 s |
| Points 3–4: disabled 0.45/0.55 V | 38 s; no commands |
| 38 V convergence | 44 s |
| Points 5–6: 0.25/0.35 V | 60 / 76 s |
| Points 7–8: disabled | 76 s; no commands |
| 40 V convergence | 82 s |
| Points 9–10: 0.25/0.35 V | 98 / 114 s |
| Points 11–12: disabled | 114 s; no commands |

These are deterministic fake-plant results, not hardware timing guarantees. A separately
commissioned full 12-point rehearsal takes 210 virtual seconds. The dynamic plan takes 36 virtual
seconds: 6 s supply establishment, 4+3 s first demand, 4+5 s second demand, 4+10 s neutral/coast.
Real settling, native capture timing, initial supply readback, motor coast, SCPI latency and
software scheduling affect actual duration. Slew limits and existing V3 interval/lateness checks
remain enforced; Python/SCPI playback is not a hard-real-time continuous waveform generator.

## Results and fault behavior

JSONL defaults to `logs/experiment-<id>.jsonl`. Each point records experiment ID/number, commanded
bus/supply/ADC demand, actual mean DC voltage/current/power, AC active power, CAN ERPM/motor
current, sample count, settling/validity, native count before/after, and simulated/physical provenance.
A grid point is valid only after accepted measured capture **and verified neutral return**.
A neutral-return fault invalidates the point conservatively, although OXYGEN may already have
accepted a native matrix sample; do not interpret invalid Python status as undoing that sample.

Efficiency is `100 × mean(P_AC) / mean(P_DC)` only when positive DC input exceeds the configured
minimum and AC power is nonnegative; otherwise it is null/undefined. No efficiency is fabricated
from ADC voltage. Ratios are electrical DC-to-AC, not shaft/motor mechanical efficiency; inspect
calibration and sign conventions if results are implausible. No percentage is silently clamped.
Dynamic steps log observed measurements but explicitly do not claim valid native matrix points.
Zero throttle is not an active brake command. Claim regeneration only with independently measured
returned DC energy and a known energy destination.

Any SCPI error, invalid readback, connection loss, stale/invalid feedback, readiness/fault violation,
current/RPM excursion or bounded convergence/settling/capture failure aborts further experiment
commands. Cancellation and faults attempt existing motor-neutral cleanup without depending on
feedback availability; supply demand is preserved, not reset. Failed cleanup remains unknown.
There is no automatic write retry, reconnect or resume. An experiment object is one-shot. The
endpoint lease is released on exit/process termination, but that is not proof of electrical safety.
Independent physical protections must handle link loss, process failure and unobserved hazards.
