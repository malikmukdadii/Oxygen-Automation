# Tests

Offline only. Do not point these at live OXYGEN or physical outputs.

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q
```

Primary coverage for the unified dispatcher:

- `tests/test_dispatcher_v3.py` — schema v3, open-loop cleanup, closed-loop not-ready, memory scenario
- `tests/test_aout_discovery.py` — read-only AOUT discovery
- `tests/test_experiments.py` — coordinated grid/dynamic memory integration, explicit higher-limit
  commissioning gates, native capture validity, feedback faults, endpoint ownership and scripted SCPI
- `tests/test_scpi_ramp_faults.py` — mid-ramp errors/readback failures with no live socket creation
- `tests/test_rzxa_relative_step.py` — all nine relative-demand grid points, no-write boundaries,
  invalid/unknown readback, runtime completion, motor-neutral/discrete prerequisites, physical
  commissioning gate, global ownership, and eight-button latency/rising-edge regressions
- `tests/test_ces_motor_candidates.py` — staged 10/18/24/22 s current-demand waveforms, zero-demand
  normal completion, exact acceleration/deceleration windows, eight-name button rehearsal,
  cancellation/fault/communication loss, unchanged supply, positive-demand startup rejection,
  and explicit higher-limit authorization through 1.60 V

The 12-point experiment test uses a test-only explicitly authorized 0.55 V configuration mirrored
into memory. The shipped grid still enables only 0.25/0.35 V, producing six valid fake points and
six disabled candidates. It does not raise the operator's 0.40 V clamp or populate a real matrix.
See `docs/efficiency-experiments.md` for offline commands and commissioning requirements.

Legacy mock-engine / SCPI / watch-do / constant-pattern tests remain for the diagnostic stack.
