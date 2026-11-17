# Quick start (schema v3)

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.example.json
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.supply-only.example.json
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.example.json --execute --confirm-simulated-output --scenario scenarios/open_loop_fixture_demo.json
```

| Button | Action |
|--------|--------|
| DO 3/1 Sim | Supply → 0.500 V, hold, persist |
| DO 3/3 Sim | Supply characterization steps |
| DO 3/4 Sim | Motor gentle → neutral (dual-output example) |
| DO 3/5 Sim | Motor stop/go → neutral |
| DO 3/6 Sim | Motor cruise → neutral |
| DO 3/2 Sim | Supply → 0 V |

Stock RZ-XA profiles work supply-only (`dispatcher.v3.supply-only.example.json`). Dual-output motor-neutral sequencing is selected with `interlocks.supply_down_requires_motor_neutral`.

See `README.md` for OXYGEN demo/physical commands.
