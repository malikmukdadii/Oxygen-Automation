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
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.json --execute --live --confirm-output "RZ-XA Demand" --confirm-output "VESC Throttle"

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

## Tests

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q
```

Offline only. Passing tests do not commission hardware.

## Safety

Software is not an e-stop. Supply AOUT demand is not an RZ-XA enable. Open-loop readback is not measured bus voltage.
