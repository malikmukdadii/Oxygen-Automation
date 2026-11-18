# OXYGEN Automation (schema v3)

Open-loop-first dual-AOUT dispatcher for DEWETRON × Takasago.
Simulated and physical ConstOutput channels share one discovery, timeline, SCPI write path, and cleanup policy.

**Verified scale:** 1.000 V AOUT = 80 V bus (`gain_bus_v_per_aout_v: 80`).

## Operator config

Authoritative runtime file: **`dispatcher.json`** (gitignored; machine-specific).

```powershell
Copy-Item dispatcher.example.json dispatcher.json
# Edit channel names, triggers, active roles, and limits for this machine.
```

Illustrative variants live under `examples/` and must not be confused with the active operator file.

| File | Purpose |
|------|---------|
| `dispatcher.json` | Active operator config (local only) |
| `dispatcher.example.json` | Committed starter (memory dual-output) |
| `examples/memory-dual.json` | Offline dual-output fixture |
| `examples/supply-only.json` | Supply-only RZ-XA commissioning |
| `examples/oxygen-demo.json` | Simulated OXYGEN demo |
| `examples/physical-dual.json` | Physical dual-output template |
| `profiles/` | Shared profile library |

## Workflow

```powershell
python -m venv .venv-scpi
.\.venv-scpi\Scripts\python.exe -m pip install -r requirements.txt

# Offline validate active/default config
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns

# Supply-only example
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings examples/supply-only.json

# Memory scenario
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings examples/memory-dual.json --execute --confirm-simulated-output --scenario scenarios/open_loop_fixture_demo.json

# Simulated OXYGEN
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings examples/oxygen-demo.json --execute --confirm-simulated-output --confirm-output "Test Aout2" --confirm-output "Test Aout1"

# Physical open-loop
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.json --execute --live --confirm-output "Takasago Voltage Control" --confirm-output "Drive Control"

# Read-only tools (isolated; no AutomationEngine)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine discover-aout --bindings examples/oxygen-demo.json
.\.venv-scpi\Scripts\python.exe -m oxygen_engine watch-do --channel "DO 3/1 Sim"
.\.venv-scpi\Scripts\python.exe -m oxygen_engine list-profiles
```

## Control model

| Item | Behavior |
|------|----------|
| `control.mode` | `open_loop` (default) or `closed_loop` |
| Active roles | Configure supply-only, motor-only, or dual under `outputs` |
| Discovery | Full AOUT discovery once at connect for active roles only; pinned for the session |
| Supply | Ramps from any valid current setpoint; `hold_final` persists demand |
| Motor | Independent profiles; `return_neutral`; cross-role prereqs only when both roles are active |
| Limits | Operator `max_aout_v` is authoritative (within HW range); examples keep 0.500 V / 80 V/V |
| Measurements | Optional open_loop logging on `telemetry_poll_ms`; never sync on AOUT writes; missing sensors warn |
| Closed-loop | Requires a registered controller; otherwise `ControlNotReady` |

## Profiles

| Profile | Role |
|---------|------|
| `profiles/rzxa_ramp_hold.json` | Supply ramp + hold at 0.5 V |
| `profiles/rzxa_ramp_down.json` | Supply ramp to 0 V |
| `profiles/rzxa_steps.json` | Supply 0.45/0.50 characterization |
| `profiles/motor_gentle.json` | Gentle motor pulse |
| `profiles/motor_stop_go.json` | Stop/go pulses |
| `profiles/motor_cruise.json` | Multi-level cruise |
| `profiles/motor_repeat.json` | Repeated gentle pulses |
| `profiles/motor_step_response.json` | Extended demand-level characterization |
| `profiles/motor_coast_compare.json` | Matched demand trials with fast/slow release and neutral observation |

See [open-loop experiments](docs/open-loop-experiments.md) for durations, suggested existing DOUT
assignments, measurement interpretation and the inspected local enable flags. Disabled assignments
are not polled. Startup diagnostics list both enabled and disabled entries, exact AOUT mappings and
voltage limits. Active profiles pause DOUT polling; busy pulses can be unobservable and are not queued.

## Coordinated DC-to-AC experiments

The `run-experiment` layer sequences the two existing outputs through the validated V3 writer for
`grid` / `dynamic` plans. It preserves `dispatcher.json`, single-output profiles and working DOUT
assignments. Only one dispatcher/experiment CLI may own an OXYGEN endpoint at a time.

- `experiments/dc_ac_grid.example.json`: 36/38/40 V bus targets; 0.25/0.35 V drive enabled.
  The planned 0.45/0.55 V candidates remain disabled above today's 0.40 V clamp.
- `experiments/dynamic_motor.example.json`: bounded acceleration/current-demand plateaus and
  neutral coast observation; no active braking or regulated speed claim.

```powershell
# Offline preview; no connection
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-experiment --bindings dispatcher.json --experiment experiments/dc_ac_grid.example.json

# Accelerated no-I/O rehearsal; does not populate OXYGEN's real matrix
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-experiment --bindings dispatcher.json --experiment experiments/dc_ac_grid.example.json --memory --execute --confirm-simulated-output
```

See [commissioning, feedback bindings and expected timeline](docs/efficiency-experiments.md).

## Regenerative deceleration

`regenerative_deceleration` plans use the same V3 write path with phases A–F, commissioned drive
limits, trial-scoped sync-CSV energy analysis, and explicit simulated vs measured feedback.
Use the dedicated command or schema-dispatch via `run-experiment`:

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-regen-experiment --experiment experiments/regen_release_comparison.example.json
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-experiment --experiment experiments/regen_release_comparison.example.json --execute --confirm-simulated-output
.\.venv-scpi\Scripts\python.exe -m oxygen_engine analyze-regen-csv --csv path\to\sync.csv --map dc_power_w=PowerDC --trial-id t1 --window-start 1 --window-end 3
```

See [regenerative deceleration commissioning](docs/regenerative-deceleration-commissioning.md).
`Braking Control` remains unused. [Future ADC2 braking](docs/regenerative-extension-plan.md) is
still design-only. Live physical regen stays disabled until verified; sim pass ≠ physical enable.

## Tests

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q
```

Offline only. Passing tests do not commission hardware.

For SCPI error attribution, raw readback/fault diagnostics and the investigated ladder failure,
see [SCPI ramp fault investigation](docs/scpi-ramp-fault-investigation.md). A queued error after
a write does not prove whether the hardware applied it; failed commands are never retried automatically.

## Safety

Software is not an e-stop. Supply AOUT demand is not an RZ-XA enable. Open-loop readback is not measured bus voltage.
