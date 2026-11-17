# Quick start (schema v3 open-loop)

1. Create `.venv-scpi` and `pip install -r requirements.txt` (see README).
2. Offline validate:  
   `python -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.example.json`
3. Memory demo scenario:  
   `python -m oxygen_engine dispatch-patterns --bindings dispatcher.v3.example.json --execute --confirm-simulated-output --scenario scenarios/open_loop_fixture_demo.json`

| Button (memory example) | Action |
|---|---|
| DO 3/1 Sim | Supply ramp to 0.500 V, hold, persist |
| DO 3/4 Sim | Motor gentle ramp, return to 0 V |
| DO 3/2 Sim | Supply ramp down to 0 V |

LOW then HIGH triggers once. Busy presses are ignored. Ctrl+C cleanup returns motor to neutral and preserves supply demand.

Physical / OXYGEN demo commands and safety notes: see `README.md`.
