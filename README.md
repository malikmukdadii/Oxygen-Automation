# OXYGEN Automation

Schema **v3** dual-AOUT dispatcher for DEWETRON × Takasago CES work.
**Open-loop is the default.** Simulated and physical ConstOutput channels share one
parser, discovery path, timeline, SCPI write path, and cleanup policy.

No live OXYGEN connection is required for offline validation or the pytest suite.

## Architecture

```
dispatcher.v3*.json (schema_version 3)
  → control.mode: open_loop | closed_loop
  → transport: memory | oxygen_scpi
  → discovery by exact channel names
  → DO rising-edge triggers
  → Dispatcher (one active profile)
  → ConstOutput AOUT write + setpoint readback
```

| Role | Completion | Default clamp |
|------|------------|---------------|
| `rzxa_voltage_demand` | `hold_final` (persist demand) | 0 … 0.5 V |
| `vesc_throttle` | `return_neutral` (0 V) | 0 … 0.4 V (examples) |

Verified supply documentation: **1.000 V AOUT = 80 V bus** (`gain_bus_v_per_aout_v: 80`).
Open-loop does not read measurement channels. Closed-loop requires a registered
`ClosedLoopController`; otherwise the engine raises `ControlNotReady` and will
**not** silently fall back to open-loop.

## Windows setup

```powershell
python -m venv .venv-scpi
.\.venv-scpi\Scripts\python.exe -m pip install -r requirements.txt
```

Offline validate (no sockets):

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.example.json
```

Memory scenario (still offline):

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.example.json --execute --confirm-simulated-output --scenario scenarios/open_loop_fixture_demo.json
```

Simulated OXYGEN ConstOutput (operator must confirm both AO names; no physical gear):

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.oxygen-demo.example.json --execute --confirm-simulated-output --confirm-demo-output "Test Aout2" --confirm-demo-output "Test Aout1"
```

Physical open-loop (explicit confirms only; not run by this repo’s CI):

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.hardware.example.json --execute --live --confirm-hardware-interlocks --confirm-physical-output "Test Aout2" --confirm-physical-output "Test Aout1"
```

Read-only AOUT discovery:

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine discover-aout --bindings dispatcher.v3.oxygen-demo.example.json
```

## Config files

| File | Use |
|------|-----|
| `dispatcher.v3.example.json` | Memory open-loop (offline default) |
| `dispatcher.v3.oxygen-demo.example.json` | Simulated OXYGEN SCPI open-loop |
| `dispatcher.v3.hardware.example.json` | Physical OXYGEN SCPI open-loop |
| `profiles/rzxa_ramp_hold.json` | Supply ramp + hold |
| `profiles/rzxa_ramp_down.json` | Supply ramp to 0 V |
| `profiles/motor_gentle.json` | Motor gentle ramp to neutral |

Channel names and DO trigger names are configured in JSON — not hard-coded module slots.

## Tests

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q
```

Offline only. Passing tests do not commission wiring, enables, or bus voltage.

## Safety

- Software is not an emergency stop.
- Supply AOUT demand is not an RZ-XA enable/RUN command.
- Open-loop setpoint readback is not measured bus voltage.
- External inhibit / e-stop / regen protection remain mandatory for physical use.
