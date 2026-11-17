# Quick start (schema v3)

```powershell
Copy-Item dispatcher.example.json dispatcher.json
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings examples/supply-only.json
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings examples/memory-dual.json --execute --confirm-simulated-output --scenario scenarios/open_loop_fixture_demo.json
```

`dispatcher.json` is the active operator config (gitignored). Illustrative files stay under `examples/`.

| Button (memory example) | Action |
|--------|--------|
| DO 3/1 Sim | Supply → 0.500 V, hold, persist |
| DO 3/3 Sim | Supply characterization steps |
| DO 3/4 Sim | Motor gentle → neutral (dual-output) |
| DO 3/5 Sim | Motor stop/go → neutral |
| DO 3/6 Sim | Motor cruise → neutral |
| DO 3/2 Sim | Supply → 0 V |

Stock RZ-XA profiles work supply-only (`examples/supply-only.json`). Dual-output motor-neutral sequencing is selected with `interlocks.supply_down_requires_motor_neutral`.

See `README.md` for OXYGEN demo/physical commands.
