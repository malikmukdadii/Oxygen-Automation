# OXYGEN Automation (schema v3)

Open-loop-first dual-AOUT dispatcher for DEWETRON × Takasago.
Simulated and physical ConstOutput channels share one discovery, timeline, SCPI write path, and cleanup policy.

**Verified scale:** 1.000 V AOUT = 80 V bus (`gain_bus_v_per_aout_v: 80`).

## Workflow

```powershell
python -m venv .venv-scpi
.\.venv-scpi\Scripts\python.exe -m pip install -r requirements.txt

# Offline validate (no sockets)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.example.json

# Memory scenario
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.example.json --execute --confirm-simulated-output --scenario scenarios/open_loop_fixture_demo.json

# Simulated OXYGEN (confirm exact channel names; --live not used)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.oxygen-demo.example.json --execute --confirm-simulated-output --confirm-output "Test Aout2" --confirm-output "Test Aout1"

# Physical open-loop (single --live ack + confirm both AOUT names)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.hardware.example.json --execute --live --confirm-output "Test Aout2" --confirm-output "Test Aout1"

# Read-only AOUT discovery
.\.venv-scpi\Scripts\python.exe -m oxygen_engine discover-aout --bindings dispatcher.v3.oxygen-demo.example.json
```

## Control model

| Item | Behavior |
|------|----------|
| `control.mode` | `open_loop` (default) or `closed_loop` |
| Discovery | Full AOUT discovery once at connect; pinned for the session (no rediscover on ramp steps) |
| Supply | Ramps from any valid current setpoint; `hold_final` persists demand |
| Motor | Independent profiles; `return_neutral`; optional `require_supply_demand_before_motor` |
| Measurements | Optional in open_loop for **logging only**; never block or alter setpoints |
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
