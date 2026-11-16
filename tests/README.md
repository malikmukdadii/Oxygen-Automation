# Tests

Run from the project root:

```bash
pip install -r requirements.txt
pytest -q
```

`test_behaviors.py` covers all ten behaviors required by the implementation specification section 10.

## V2 discovery and demo tests

The OXYGEN demo tests use `tests/fixtures/dispatcher_v2_demo_bindings.json`, not the editable
`dispatcher.v2.oxygen-demo.json`. The fixture uses fixed mock channel/button names and shared
profile files, with profiles resolved relative to the fixture. The backend tests replace its
connection port with a local mock server's ephemeral port. Your working AOUT/DOUT names and
machine endpoint can be edited without changing these mock tests or their seeded channels.
Do not use the test bindings as a machine configuration or as evidence of physical authorization.

From the project root, run the focused checks using the existing environment:

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q tests/test_dispatcher_v2.py tests/test_oxygen_demo_backend.py tests/test_aout_discovery.py
```

These are mock/in-memory tests, not validation of a connected OXYGEN setup. A real setup must
still pass read-only discovery with its own configured names/properties. Missing active-source
information is not replaced by a guess based on a channel name or a single constant property.
