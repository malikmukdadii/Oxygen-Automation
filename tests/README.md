# Tests

Offline only. Do not point these at live OXYGEN or physical outputs.

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q
```

Primary coverage for the unified dispatcher:

- `tests/test_dispatcher_v3.py` — schema v3, open-loop cleanup, closed-loop not-ready, memory scenario
- `tests/test_aout_discovery.py` — read-only AOUT discovery

Legacy mock-engine / SCPI / watch-do / constant-pattern tests remain for the diagnostic stack.
