# OXYGEN Automation

Configurable OXYGEN SCPI discovery and simulation-first dual-AOUT profile dispatch.
**The current V2 backend is simulation-only, not a commissioned physical-hardware controller.**
Passing mock tests or running OXYGEN simulated outputs does not authorize physical actuation.

## Getting started on Windows

Use Python 3.11 or newer. From the repository root:

```powershell
py -3 -m venv .venv-scpi
.\.venv-scpi\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path dispatcher.v2.oxygen-demo.json)) { Copy-Item dispatcher.v2.oxygen-demo.example.json dispatcher.v2.oxygen-demo.json }
```

Edit the local `dispatcher.v2.oxygen-demo.json` for the selected OXYGEN instance and exact
channel names. The example uses the explicitly approved simulated `Test Aout2`/`Const4` supply
and `Test Aout1`/`Const0` motor setup; names alone cannot prove that outputs are simulated.
Do not connect physical equipment to use this example. All physical-control flags must stay false.

Validate the local configuration without connecting or writing outputs:

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.oxygen-demo.json
```

See the discovery and demo guides below for read-only connection checks and explicitly confirmed
simulated execution. Tests are described in `tests/README.md`; they are not physical-hardware validation.

### Local files and Git

Commit source, tests, profiles, documentation, and example configurations. `.gitignore` excludes
virtual environments, IDE state, Python/test caches, runtime logs, secrets, backups, and local
`config.json`, `dispatcher.json`, and `dispatcher.v2.oxygen-demo.json`. Additional machine-specific
configurations can use a `*.local.json` name. Local settings are not deleted or changed by Git setup.
Use `config.example.json`, `dispatcher.example.json`, or `dispatcher.v2.oxygen-demo.example.json`
as the corresponding shareable template. Do not commit credentials or replace the examples with
commissioned physical-hardware settings.

## OXYGEN simulated dual-AOUT operation

The operator-run memory demo completed all three cycles and 56 tests passed. The new
OXYGEN-demo bridge subsequently passed 69 targeted mock/v2 tests. Use
`dispatcher.v2.oxygen-demo.json` to read OXYGEN's DO buttons and drive the two previously
discovered simulated AOs. Separate confirmation of BOTH genuine simulated, unconnected
outputs is required. Bus feedback remains explicitly FAKE; physical hardware stays locked.
OXYGEN demo IDs are resolved by exact unique name on each connection, not matched against
historical IDs. They remain pinned within the session; mapping changes stop execution.

## Configurable AOUT discovery

In `dispatcher.v2.oxygen-demo.json`, set each output's `channel` to its exact OXYGEN name,
optionally add a separate `display_name`, and use `"const_property": "auto"`. The logical roles
remain `rzxa_voltage_demand` and `vesc_throttle`. Discovery enumerates channels, requires unique
names/IDs, and resolves the constant property from the actual selected source, never the slot
or channel number. A supplied `expected_id` is now an enforced identity check; omit it or use
null when switching setups without a saved-ID constraint. Explicit properties remain supported
but must match discovery. Validated IDs, source/property, mode, unit and range are session-pinned.

The new read-only command is `python -m oxygen_engine discover-aout --bindings dispatcher.v2.oxygen-demo.json`.
It connects to the configured host/port and reports both outputs and mismatches without changing
any settings. Copy the bindings file and change names/connection fields to inspect another
machine. **Discovery does not authorize writes:** execution requires explicitly approved
simulated role/name/property bindings and confirmation of both selected names on every run.
The operator-confirmed `Test Aout2`/`Const4` supply and `Test Aout1`/`Const0` motor mappings are
approved in addition to the original demo names. Other discovered names/properties remain read-only.
Supply persistence, motor neutral, clamps and interlocks are unchanged.

### Configurable DOUT triggers

Each V2 assignment's `trigger` accepts any exact OXYGEN DOUT channel name, including channels
on other module slots or the chassis controller. The example `DO 3/1 Sim` through `DO 3/6 Sim`
names are defaults, not a whitelist. Set each `assignments[].trigger` to your own exact name;
enabled assignments must use distinct triggers (up to six assignments). Offline previews show
the configured trigger names. The OXYGEN backend discovers each trigger by unique name and
requires a Boolean `DO_Value`; missing/ambiguous channels or invalid properties refuse startup.
DOUTs are only read as buttons, never written. This does not broaden AOUT write authorization.
Memory-fixture buttons and CLI scenario validation also use the configured trigger names.

See [the configuration and discovery workflow](docs/aout-discovery.md) for supported property
representations, failure behavior and safety boundaries. New tests are provided but have not
been rerun during repository preparation. The operator has reported passing targeted tests and
successful OXYGEN simulated execution; this does not establish physical-hardware readiness.

See [the interactive OXYGEN demo procedure](docs/oxygen-demo.md) for the direct launch
command, button map and chart checks. No additional memory-demo run is required first.

## Dispatcher v2 implementation background

The supplied six v2 profiles now have a versioned loader and a dual-role in-process fixture
dispatcher. For memory-only operation use `dispatcher.v2.simulation.json`, not the unchanged blueprint, with
`dispatch-patterns --bindings ...`. This new backend opens no network connection and cannot
write physical outputs. Actual channel identities remain unresolved; mock IDs are explicitly fake.

Normal supply completion can hold demand while motor-only profiles return to neutral.
Cancel/fault/shutdown attempts motor neutral and preserves supply demand; none of this is
proof of actual bus voltage or deenergization. Live control and RZ-XA commands remain locked.

The existing unversioned three-button workflow remains available. `migrate-dispatcher`
creates a backed-up canonical compatibility configuration without overwriting `dispatcher.json`.
The original implementation pass deliberately deferred test execution. Subsequent operator
and mock test results are recorded above; the operator subsequently reported successful
dual-AOUT OXYGEN simulated execution. Physical operation remains outside this backend's scope.

See [the v2 implementation and future simulation guide](docs/dispatcher-v2.md) for schema,
interlocks, migration, unresolved wiring/calibration and commands requiring separate authorization.

## Supplied proposal background

The blueprint remains proposal/scaffolding, not an executable dispatcher configuration.
The following background and commissioning constraints are preserved from the handoff.

## Purpose
- One logical AOUT (`rzxa_voltage_demand`) represents the RZ-XA-6k-H external analog **voltage demand**.
- One (`vesc_throttle`) represents the VESC analog input.
- The RZ-XA `ramp_40v_hold` stops *updating* the AOUT when it reaches **0.500 V** and intentionally **persists** that demand; it does not disable the supply or change motor control. It does not prove 40 V appears on DC bus.
- Separate supply-down profile restores the analog demand to 0 V **only after verifying motor neutral/no regeneration**. Do not switch off a regen sink while energy is returning.
- Motor-only profiles leave supply demand unchanged but require **independently measured** stable DC-bus voltage and verified enable/neutral configuration for live operation.

## Provisional scale / IMPORTANT
- The RZ-XA-6k-H is rated 800 V. Its external analog voltage input is described as 0–10 V in product literature. The exact applied mapping/mode on *this unit* is not confirmed.
- If 10 V corresponds to 800 V, 0.500 V commands 40 V, gain 80 bus V / AOUT V. It is a **hypothesis**, not validated hardware control.
- The AOUT command doesn't assert the RZ-XA's physical output enable/RUN state. It cannot by itself establish energized DC bus.
- Confirm correct analog input pins, scaling mode, analog-reference polarity, compliance, grounding/isolations, limits, and output enable independently.
- The VESC throttle profile values 0.2, 0.25, 0.35, 0.4 V are *simulation placeholders*, not validated physical throttle commands.

## Files
- `dispatcher.v2.simulation.blueprint.json`: proposed unified versioned dispatcher schema.
- `profiles/rzxa_demand_ramp_40v_hold.json`: ramp 0 to 0.5 V over 5s, hold 3s, persist last demand.
- `profiles/rzxa_demand_ramp_down.json`: ramp back to 0 V after neutral and regen checks.
- `profiles/rzxa_demand_36_40_36.json`: isolated source characterization using provisional 0.45/0.5 V demands; finishes at 0.5 V.
- `profiles/motor_gentle_ramp.json`, `motor_stop_go.json`, `motor_cruise_steps.json`: motor-only simulated waveforms; return to neutral.
- `COPILOT_HANDOFF.md`: complete implementation handoff.

## Migration constraints
Do **not** overwrite the user's working `dispatcher.json` or existing tests. `config.example.json` from the older engine and the current dispatcher are different schemas. Merge logically, not by copying the old JSON wholesale. In particular, preserve DO edge behavior and existing one-AOUT safety restrictions. The expected_id for second AOUT is **unknown**, not synthesized.

## Simulation design
Model distinct simulated supply demand, simulated motor throttle, and independent simulated measured DC bus feedback. Do not magically treat a supply command as measured bus. Use explicit simulation plant logic or injectable sensor fixtures. The software must fail closed if live DC bus readback is absent or stale.

## Testing request
Generate test code as implementation proceeds, but **do not run** it, compile the project, connect to OXYGEN, or touch hardware during this implementation pass. The user will authorize a final simulation-only run later.
