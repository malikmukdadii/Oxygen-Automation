# V3 open-loop experiment guide

## Current machine configuration

`dispatcher.json` remains the single authoritative, gitignored operator configuration. This
review did not modify it, its authorizations, output limits, assignment enable flags or names.
The inspected local file uses `transport: oxygen_scpi` and `control.mode: open_loop`.

| Physical name | Terminal | Logical role | Configured demand range |
|---|---|---|---|
| Drive Control | AO2/1 | `vesc_throttle` | 0..0.4 V; neutral 0 V |
| Takasago Voltage Control | AO2/3 | `rzxa_voltage_demand` | 0..0.5 V |
| Braking Control | AO2/2 | Future `vesc_brake` | Not configured or commanded by the current dispatcher |

These terminal assignments are operator-provided wiring information, not something established
by offline tests. The verified supply mapping in the local config is 80 bus V per demand V.
Demand readback establishes a configured setpoint, not actual voltage, readiness or stopped motion.

### Why three buttons did not respond

At the initial inspection, before the operator's update during this review, the assignments were:

| Existing DOUT | Assignment | Profile | Enabled |
|---|---|---|---|
| Ramp Enable | `rzxa_to_40_hold` | `profiles/rzxa_ramp_hold.json` | true |
| Ramp Disable | `rzxa_down` | `profiles/rzxa_ramp_down.json` | true |
| RZXA Char | `rzxa_steps` | `profiles/rzxa_steps.json` | false |
| Motor Gentle | `motor_gentle` | `profiles/motor_gentle.json` | true |
| Motor Stop Go | `motor_stop_go` | `profiles/motor_stop_go.json` | false |
| Motor Cruise | `motor_cruise` | `profiles/motor_cruise.json` | false |

A disabled assignment is not loaded for execution, and its DOUT is not polled. This explains
the original inactivity without a runtime trigger defect. The operator then updated the local
file while this review was in progress. The re-inspected current configuration has all six
assignments enabled; Ramp Enable, Ramp Disable and Motor Gentle are unchanged, and the other
three now select these profiles:

| Existing DOUT | Current assignment | Current profile | Enabled |
|---|---|---|---|
| RZXA Char | `rzxa_char_ladder` | `profiles/rzxa_36_38_40_ladder.json` | true |
| Motor Stop Go | `motor_coastdown_comparison` | `profiles/motor_coastdown_comparison.json` | true |
| Motor Cruise | `motor_three_level` | `profiles/motor_three_level.json` | true |

Both the original and updated characterization selections were exercised with enabled
assignments and fresh LOW-to-HIGH edges in isolated memory tests. Their success there is not
proof of live DOUT mapping, wiring or acquisition behavior. Restart the dispatcher to load
operator changes; the running process does not hot-reload the JSON. No DOUT renaming, enabling
or local config edits were performed by this review; the operator's changes are preserved.

## Experiment library

All rows below use schema 3 and write one existing logical output. Existing demands and clamps
are retained. Nominal durations exclude startup SCPI checks and software I/O overhead. Plateau
lengths are observation windows, not guarantees that speed or electrical transients have settled.

| Profile | Experiment | Nominal duration | Completion |
|---|---|---|---|
| `rzxa_ramp_hold.json` | Existing 5 s ramp to 0.5 V, 3 s hold | 8 s | Persistent 0.5 V demand |
| `rzxa_ramp_down.json` | Existing 5 s ramp to zero | 5 s | Verify zero demand |
| `rzxa_steps.json` | Repeated 0.45/0.50 V supply plateaus, 5 s each, checkpoint labels | 29 s | Persistent 0.5 V demand |
| `motor_gentle.json` | Existing 0.25 V gentle pulse | 8 s | Neutral |
| `motor_stop_go.json` | Existing 0.20/0.35 V pulses with a neutral interval | 14 s | Neutral |
| `motor_cruise.json` | 0.20/0.40/0.25 V demand plateaus, 5 s each, checkpoint labels | 24 s | Neutral |
| `motor_repeat.json` | Existing repeated 0.25 V pulse pair | 11 s | Neutral |
| `motor_step_response.json` | Existing extended 0.10/0.20/0.35 V levels and 0.25 V pulse | 34 s | Neutral |
| `motor_coast_compare.json` | Two 0.25 V trials: 1 s versus 5 s release ramp, 10 s neutral windows | 44 s | Neutral |
| `rzxa_36_38_40_ladder.json` | Operator-added 0.450/0.475/0.500 V ladder with 4/4/5 s holds | 23 s | Persistent 0.5 V demand |
| `motor_three_level.json` | Operator-added 0.12/0.20/0.28 V plateaus, 4 s each; 6 s neutral window | 31 s | Neutral |
| `motor_coastdown_comparison.json` | Operator-added 0.20/0.28 V trials with 6/8 s neutral windows | 35 s | Neutral |

The operator-added files were reviewed and tested without modification. Their targets fit
the current clamps. The ladder's checkpoint labels mark the ends of the holds. Its nominal
bus-voltage labels assume the verified 80 V/V mapping, not live feedback. The two coast
profiles answer different questions: `motor_coastdown_comparison` compares different starting
demands, while `motor_coast_compare` holds peak demand constant and varies release duration.
Neither establishes identical initial speed or active regenerative braking. The description
of the operator-added coast file states an intended measurement, not a Python measurement:
the profile itself does not query or integrate DC power; record it in OXYGEN separately.

Supply profiles can start from any valid current demand; the local config requires motor
neutral before/during supply motion. Motor profiles require neutral startup and, in the local
config, supply demand readback at 0.5 V. Motor profiles do not write supply demand. Test the
supply ramp first, then a motor profile. Supply ramp-down is not a motor stop command.

The modified characterization profiles preserve their existing voltage targets. The new
coast comparison peaks at 0.25 V, below the configured 0.4 V drive clamp. The fixed 10 s
neutral window does not verify that the motor has stopped before the second trial. Compare
trials only if measured initial speed/load/temperature are comparable; use separate runs or
longer, revalidated windows if necessary. Neutral behavior must be commissioned in VESC;
zero drive demand does not by itself prove passive freewheeling or absence of braking.

### DOUT mapping recommendations (no changes applied)

- Keep Ramp Enable, Ramp Disable and Motor Gentle as they are.
- Keep the operator's current RZXA Char ladder, Motor Cruise three-level and Motor Stop Go
  coast comparison selections listed above for the first characterization session.
- Alternatives for later sessions are `rzxa_steps.json` on RZXA Char, `motor_cruise.json` on
  Motor Cruise, or `motor_stop_go.json` / `motor_coast_compare.json` on Motor Stop Go. Select
  deliberately and record the chosen file in the run notes. Do not silently rename its DOUT.
  The startup summary shows the actual selected file so the button label need not be mistaken
  for the experiment identity. Restart after config changes.
- At most six assignments are supported. Select an existing slot for alternative experiments;
  do not introduce another runtime configuration or a parallel controller.

## Operator diagnostics and polling limits

The offline plan and runtime startup summary list enabled assignments with resolved profile
paths, nominal durations and completions, plus disabled assignments with `enabled=false`
explanations. Disabled profiles are explicitly labeled not validated by startup. The summary
also lists exact AOUT names/limits; runtime adds resolved string IDs and constant properties.

- `dispatcher_ready`: ready for an observed LOW then HIGH.
- `profile_started`: assignment/output running; button polling paused.
- `profile_precondition_failed`: assignment, profile, role and original reason; playback did
  not start. The existing fail-closed fault/cleanup behavior is preserved.
- `trigger_not_armed`: HIGH at startup/resync is a baseline, not a rising edge.
- `trigger_observation_discarded`: an overlong read or sample gap caused resync; no edge is
  inferred across the gap.
- `trigger_stale`: an observed edge aged out during other reads or precondition checks.
- `trigger_ignored`: simultaneous observed presses lost to config-order priority; no queue.
- Progress, checkpoints, cycle-complete and output-state records distinguish playback from idle.

Playback is synchronous. The dispatcher does not poll DOUTs or optional background telemetry
inside an active profile. It cannot count or identify pulses entirely within that interval;
a busy warning describes the limitation, not evidence that a specific press was seen. After
playback the trackers reset. A button already HIGH becomes a baseline; return it LOW, allow
at least a polling interval plus communication margin for observation, then set it HIGH.

The polling interval is a best-effort software interval, not a guaranteed detectable pulse
width. Multiple SCPI reads, network latency and setpoint verification add delay. Diagnostic
gap/stale reports do not detect every missed pulse. Ramp Disable is not a cancellation input
while a profile runs. Independent physical stop/inhibit remains necessary. Ctrl+C attempts
motor neutral and preserves supply demand; connection loss can prevent neutral cleanup.

## Measurements and repeatability

Record in OXYGEN using the existing decoded CAN channels and independently verified voltage/
current/power inputs. Start acquisition/recording manually using your established procedure;
these profiles do not start acquisition or recording. Local `checkpoint` records are JSONL
markers only; they do not insert OXYGEN acquisition markers. Align the recorded AOUT demands
and acquisition timebase with the JSONL sequence and note PC/instrument timing uncertainty.

Recommended trace groups:

1. Supply characterization: actual DC bus voltage, DC current, signed DC power, supply demand,
   load state and temperatures. The nominal 36/40 V equivalents are not measurements. This
   neutral-motor experiment is not a loaded supply characterization unless a known load is present.
2. Motor plateaus: drive demand, measured DC voltage/current/power, total three-phase active
   electrical power and CAN speed. Demand is not regulated speed. Verify whether CAN reports
   mechanical RPM or electrical RPM; use verified pole-pair scaling for any conversion.
3. Release comparison: same signals plus neutral-demand windows, repeated at comparable load,
   temperature and initial measured speed. Report release-ramp duration separately from
   subsequent speed decay at neutral. These experiments do not estimate shaft power or
   efficiency without torque/mechanical measurements.

For three-phase power use appropriate synchronized voltage/current sensing, polarity,
PWM-capable bandwidth and the measurement topology selected in OXYGEN. Do not equate
RMS voltage times RMS current with active power. Establish DC sign convention independently:
positive may be defined as power into the drive; negative is returned across that measurement
point. For this convention, integrate the negative portion to compute returned energy.
State sensor location, offsets and uncertainty. Energy into a bus capacitor alone does not
establish energy accepted by the supply/sink. Speed decay or a negative CAN indication alone
is not proof of recovered DC energy. Do not label these neutral-release tests regenerative
braking unless actual returned DC energy is measured and its destination is established.

The local config has empty `measurements` and `telemetry_poll_ms: 0`; this review leaves them
unchanged. Existing optional Python logging is idle-only, not synchronized high-rate capture,
and its current schema only supports `dc_bus_voltage` as numeric (other named roles are
Boolean). Use OXYGEN-native recording for DC current, three-phase power and numeric CAN speed;
this work does not broaden telemetry schema or make it a control feedback requirement.

## Offline validation

From the project root:

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.json
```

The second command is preview only: no `--execute`, no connection or physical writes.
Tests use memory fixtures and existing loopback mock SCPI servers, not live OXYGEN. The new
library tests execute each V3 profile twice with a virtual clock using the production timeline,
check voltage bounds and neutral completion, preserve the unowned output and verify checkpoint
order. Supply profiles are tested from 0, 0.2 and 0.5 V. Trigger/diagnostic tests include enabled
characterization assignments, baseline HIGH, stale/gapped observations and an unobservable
busy pulse. Offline success does not commission physical wiring, load or protections.

Legacy files without `schema_version: 3`, including `regenerative_braking.json`, `voltage_sweep.json`,
`city_drive.json` and the `sim_aout_*` files, belong to older/mock workflows. They are not V3
experiments, are not assigned in this machine's dispatcher, and were not migrated or enabled.
In particular the legacy regenerative file exceeds the current motor clamp and does not
control the separate braking channel. Do not commission it as a physical V3 braking profile.

Future coordinated braking is described separately in `regenerative-extension-plan.md`;
no braking output or coordinated executor was implemented here.
