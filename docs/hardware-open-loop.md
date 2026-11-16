
# Explicit physical-AOUT open-loop operation

## Scope and status

`oxygen_hardware` is a separate, explicitly authorized physical constant-AOUT backend. It is not
the `oxygen_demo` backend with its simulated-channel allowlist disabled. The demo and memory
paths retain their original simulation restrictions. Do not use simulated-output confirmations
for real hardware, and do not rename a physical output to obtain a demo approval.

This path reuses exact-name AOUT discovery, string IDs, source/property session pins, the profile
executor, clamps, neutral completion and supply-demand persistence. Any unambiguous listed
`TRION/SourceChannel_A_CONST/Const<N>` can be used after physical authorization; there is no
`Const0`/`Const4` or simulated-name whitelist in the physical backend.

**New physical-backend tests are provided but have not been executed during implementation.
No physical outputs have been commissioned or operated by this implementation pass.** Run the
mock regressions and complete the actual machine-specific commissioning before execution.
Passing tests cannot verify wiring, external enables, voltage scaling, safe motor mechanics,
regeneration capability or the independent emergency stop.

The controller plays voltage-demand profiles open loop. Measured bus voltage only permits or
rejects a motor step; it never adjusts the commanded AOUT to regulate bus voltage. Therefore
requiring real feedback for this safety interlock does not make the controller closed loop.

## What the backend does not do

- No RZ-XA SCPI control or supply output-enable/RUN commands.
- No motor-enable or DOUT writes.
- No changes to existing AOUT modes, source selection or ranges.
- No reconnect, replay, setup load, acquisition start, recording commands or waveform upload.
- No fake telemetry, assumed bus voltage, or automatic substitution of operator statements for
  missing measured interlock inputs.
- No guaranteed emergency-stop response through this Python process. The independent physical
  inhibit/emergency stop is required because a blocked connection or failed process cannot
  guarantee a neutral command or electrical shutdown.

The supply and motor may be externally enabled, as requested, provided the verified external
enable/inhibit arrangements and actual interlocks are correct. Software does not switch them.

## Prepare a separate local configuration

From the project root, copy the example without overwriting an existing local setup:

```powershell
if (-not (Test-Path dispatcher.v2.hardware.local.json)) { Copy-Item dispatcher.v2.hardware.example.json dispatcher.v2.hardware.local.json }
```

This local filename is excluded by `.gitignore`. Keep the old demo configuration separate.
The example is intentionally incomplete and cannot execute unchanged.

Configure:

1. `connection`: host, port and response timeout of the selected OXYGEN instance.
2. Each output's `channel`: its exact physical OXYGEN name. Optional `expected_id` remains an
   additional identity check, never a discovery replacement. Keep `const_property: auto`.
3. `hardware_authorization`: verify each declared condition independently, then explicitly set
   its field true. These declarations are not measurements and do not establish readiness by themselves.
4. Supply `mapping.gain_bus_v_per_aout_v`: enter the verified numeric gain. No value is inferred
   from the channel name or filled from the old provisional mapping. For example, 80 V/V is correct
   **only if your verification established it**. The configured motor supply-demand prerequisite
   times this gain must agree with the configured bus target.
5. Every `measurement_roles` entry: actual OXYGEN input channel, actual Boolean property where
   applicable, and `mapping_verified: true` only after that mapping has been verified.
6. Assignments: exact DOUT trigger names and explicitly physical profiles under `profiles/hardware/`.

Physical authorization requires `simulation_only: false`, `enable_physical_aout: true`,
`require_explicit_hardware_authorization: true`, and per-output permissions
`simulation: false` / `physical: true`. `enable_rzxa_output_toggle` and `enable_real_rzxa_scpi`
remain false because those operations are not implemented. Changing these flags in a demo
configuration does not convert it into the hardware backend.

### Voltage limits and profiles

- Motor neutral is explicitly verified 0 V, as stated by the operator. Hardware schema permits a
  configured upper clamp up to 3.3 V. The example intentionally retains a 0.4 V test clamp and a
  0.25 V gentle-profile peak; a stated 3.3 V maximum does not automatically request full throttle.
- Initial supply-demand clamp is 0..0.5 V. Raising this requires a separate implementation review.
- Default bus interlock is 40 +/- 2 V, with a maximum feedback-query age of 1 second. These are
  initial test settings, not inferred characteristics of your wiring or supply. Keep the supply
  profile target, verified gain, motor demand prerequisite and measured-bus requirements consistent.
- Hardware profiles must have `simulation_only: false`. Simulation profiles are refused by this
  backend, so they cannot silently become physical commands. The physical supply ramp omits the
  demo's initial `set_aout` step; it reads/adopts the current demand and accepts only zero or the
  already-requested final demand, without requiring an externally enabled supply to be disabled.
- All physical profile transitions remain ramps, and the motor must explicitly finish at neutral.

### Real measurement/interlock bindings

`dc_bus_voltage` is an independent OXYGEN numeric measurement channel in V, never the supply
AOUT command. Its `Unit` must report V and OXYGEN acquisition must already be `Started`.
The backend uses the documented numeric-transfer setup/read operations already in `OxygenAdapter`.
These configure the SCPI numeric list, not physical outputs. Use a single controller for that
numeric list; do not share numeric-transfer configuration with another controller.

Boolean roles require actual configured OXYGEN properties returning BOOL/BOOLEAN values:

| Role | Meaning |
|---|---|
| `motor_input_mapping_verified` | Verified physical throttle-input mapping/interlock status |
| `motor_physical_disable_verified` | Independently verified physical inhibit/disable arrangement; not a command to enable the motor |
| `supply_external_ready` | Actual external supply-ready condition; checked again before each non-neutral demand write |
| `regen_safe` | Actual permission that regeneration conditions permit supply ramp-down |
| `supply_shutdown_permitted` | Actual permission for the requested supply ramp-down |
| `supply_standalone_test` | Verified standalone supply-test/interlock status |
| `supply_disabled` | Actual disabled-state indication for any profile step requiring it; may be false while the supply is externally enabled |

Do not bind these roles to arbitrary always-true demo buttons. The source must accurately represent
its verified hardware condition. There is deliberately no in-process constant/fixture fallback.
The current software contract requires these roles, even when a particular profile does not use
all of them. Missing channels/properties or unsupported types refuse startup without AOUT writes.

Samples are timestamped at the start of the SCPI read. Slow/stale responses and fake sample
provenance are rejected. This is query-age validation, not instrument-side acquisition timestamp
validation; commission the selected live channel, acquisition and sample rate accordingly.
Motor-demand readback is a commanded setpoint, not proof of actual motor neutral or stopped motion.

## Deferred validation and execution

First run the mock tests; they do not contact your OXYGEN instance:

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q tests/test_dispatcher_v2.py tests/test_oxygen_demo_backend.py tests/test_aout_discovery.py tests/test_oxygen_hardware_backend.py
```

After completing the local configuration, validate it offline:

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.hardware.local.json
```

Inspect the configured outputs read-only, without physical-output authorization:

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine discover-aout --bindings dispatcher.v2.hardware.local.json
```

Only after the tests, real mappings and independent physical protections have been verified,
explicitly authorize each actual output for a run (replace names if your local configuration differs):

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.hardware.local.json --execute --live --confirm-hardware-interlocks --confirm-physical-output "Test Aout2" --confirm-physical-output "Test Aout1"
```

This command can write to real equipment. It cannot be combined with `--scenario`,
`--confirm-simulated-output` or `--confirm-demo-output`. AOUT IDs, properties, units and ranges
remain pinned; changes revoke the physical session rather than silently retargeting it.

Start configured triggers LOW, wait for hardware `preflight_passed` and `dispatcher_ready`, and
apply one fresh LOW-to-HIGH edge at a time. The supply-up profile holds its final demand. Start
motor motion only after real measured bus feedback is inside the configured window. Explicit
supply-down requires motor command neutral and the actual regen/shutdown permissions.

Only one profile runs at once. There is no DOUT polling/cancel button during a profile; a trigger
named `Ramp Disable` is not an emergency stop. Ctrl+C attempts verified motor neutral and preserves
supply demand. Connection loss can prevent even that neutral write; neither process exit nor
zero AOUT demand proves electrical deenergization. Follow the independently verified physical
shutdown/inhibit procedure. Logs are written to `logs/dispatcher-v2-hardware.jsonl` and correctly
identify physical operations rather than labeling them simulated.
