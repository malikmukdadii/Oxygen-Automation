# Dual-role dispatcher v2

**Physical-output addition:** [the hardware open-loop guide](hardware-open-loop.md) documents
the new separately authorized `oxygen_hardware` backend with real measured interlocks and
explicitly physical profiles. The historical simulation-only implementation described below
and the demo backend retain their original safety gates; neither becomes a physical backend
by changing a flag or using a simulated-channel name.

**Current status:** the operator reported 56 v2 tests passing and successful completion of the
memory demo. An added OXYGEN simulated-I/O bridge passed 69 targeted mock/v2 tests. For interactive
testing in OXYGEN now, use [the OXYGEN-demo guide](oxygen-demo.md). The historical fixture-only
design and deferred-validation instructions below describe the initial implementation pass;
repeating the memory demo is not a prerequisite to the OXYGEN trial.

No v2 tests, compilation, CLI validation, smoke tests or SCPI commands were executed during
this implementation pass. The previously reported 296 passing tests apply to the earlier
single-output project state, not this implementation. New tests are in
`tests/test_dispatcher_v2.py` and require separate authorization to run.

## Configuration and compatibility

For current configurable OXYGEN names, optional display labels/identity checks, automatic
constant-property resolution and the read-only `discover-aout` command, see
[AOUT discovery](aout-discovery.md). Discovery is reusable and independent of the existing
explicit role/name/property demo write authorization; it does not enable physical outputs. The OXYGEN example
uses `const_property: auto`; the memory-only fixture example keeps explicit fixture properties.

The canonical schema is version 2, validated by `dispatcher_v2_schema.py`. It combines:

- Logical output roles, physical channel identity fields, properties, mode/source, clamps,
  explicit per-role simulation/physical permissions and termination/completion policies.
- DO assignments and profiles, timing, independent measurement roles and mandatory interlocks.
- Reserved OXYGEN connection metadata, disabled instrument adapters and recording permission.
- Optional local log markers. Actual OXYGEN recording is not implemented and is refused.

Use `dispatcher.v2.simulation.json` for the six-button dual-role demonstration. The supplied
`dispatcher.v2.simulation.blueprint.json` is retained unchanged as a proposal; it is NOT an
executable configuration. The six supplied v2 profiles are integrated without rewriting them.
The original three profiles and `dispatcher.json` are not replaced.

Execution backends are intentionally distinct implementations of one versioned configuration:

1. `memory_fixture`: dual roles, in-process only; no sockets, OXYGEN client, RZ-XA client or
   physical writes. This is the new v2 simulation configuration's default and only dual-role backend.
2. `legacy_verified_oxygen_sim`: canonical compatibility wrapper around the original one-AO
   runner. It retains ONLY the already approved `AO 3/1 Sim` exception and neutral-on-completion
   behavior. It cannot run dual-role holds or consume fixture scenarios.

3. `oxygen_demo`: the later added, explicitly confirmed two-AO OXYGEN simulation path, using
   exact demo names, session-discovered IDs and per-channel Const0/Const1 properties. It uses real OXYGEN
   DO reads and simulated-AO writes, but independent FAKE bus telemetry. Physical hardware and
   RZ-XA control remain locked. See the separate demo guide for its confirmations and source-pinning policy.

Unversioned dispatcher files continue to use the original restricted runner. The CLI's existing
implicit bindings default is unchanged for backward compatibility; select the new canonical
simulation configuration explicitly with `--bindings`. The older engine's `config.example.json`
is NOT another source of dispatcher settings. Its abstractions informed the canonical roles,
measurement roles and separated permissions, but its hardware flags/operations are not imported.

## Fake identities versus unknown physical wiring

The new configuration leaves BOTH real channel names/IDs null and requires actual discovery.
Nothing assumes the second AO's identity or authorizes it by a similar name. Fixture IDs `101`
and `102`, with names beginning `FAKE`, belong solely to the in-process namespace. They are
not observed OXYGEN IDs and are never passed to an OXYGEN transport.

Preflight discovers and compares both fixture identities, properties, ranges, modes and units.
It rejects channel-ID/name aliasing, duplicate logical fixture bindings, changed context and
out-of-range readbacks. All writes are serialized under the fixture backend's lock. The old
OXYGEN transport/write block is not expanded.

## Profiles and button map

The table below shows the supplied defaults, not permitted slot numbers. Each enabled V2
assignment accepts any exact OXYGEN DOUT name in `trigger`, including chassis-controller
channels. Names must be unique; the OXYGEN backend requires exactly one match and a Boolean
`DO_Value`. Triggers are read only, and existing AOUT authorization remains unchanged.
Memory-fixture buttons and CLI scenarios use these configured names too; update scenario
button keys if you rename a fixture trigger. The existing limit of six assignments remains.

| Trigger | Assignment | Logical output | Normal completion |
|---|---|---|---|
| DO 3/1 Sim | rzxa_to_40_hold | rzxa_voltage_demand | 0.500 V held |
| DO 3/2 Sim | rzxa_down | rzxa_voltage_demand | Demand verified at 0 V |
| DO 3/3 Sim | rzxa_36_40_36 | rzxa_voltage_demand | 0.500 V held |
| DO 3/4 Sim | motor_gentle | vesc_throttle | Verified neutral |
| DO 3/5 Sim | motor_stop_go | vesc_throttle | Verified neutral |
| DO 3/6 Sim | motor_cruise | vesc_throttle | Verified neutral |

These DO names are fake button states in the v2 backend, NOT live OXYGEN reads. Scenario events
inject their levels. There are no cancel buttons and no DO polling during a running profile.

Existing step names remain: `set_aout`, `ramp_aout`, `hold`, `return_neutral`. V2 adds:

- `assert_preconditions`: explicit true requirements before output steps, not assertions that
  real hardware has already been commissioned. The runner checks them against fresh fixtures.
- `checkpoint`: local event/log marker at the corresponding timeline boundary.
- Top-level `completion`: supply `hold_final` or `verify_zero`; motor `return_neutral` by default.
- Top-level `requires`: independent bus feedback/external readiness/regen-safe requirements.

Profiles may touch ONLY their assigned logical role. Unknown steps/keys, unsupported requirement
values, insufficient timeouts, mismatching rates and out-of-clamp values are refused. Ramps start
from the actual readback adopted by the current process. Initial `set_aout` cannot jump an
existing demand: supply zero-set additionally needs the fake independent disabled-state sample.
Motor profiles must explicitly ramp back to neutral. At most 100 timed steps and 300 seconds
nominal duration are supported; steps are at least 0.1 seconds. Keep practical ramps much longer
than the communication/update interval. The software scheduler remains replaceable through the
runner's timeline interface; Function Generator/Replay and synchronous multi-output scheduling
are NOT implemented.

## Persistent output state

Each role has its own last verified setpoint, verification timestamp, held owner and status.
Only one profile may own active execution globally. A motor profile never writes the supply
role, including normal completion and cleanup. Supply-only profiles never reset motor throttle.

The 0.500 V supply profile accepts actual startup demand of zero or already-at-target only.
Already-at-target execution is a no-op with a logged completion, not a jump to zero. Other
starting values are rejected. The explicit down profile ramps from the current verified demand.

No startup state is restored from logs or cached application state. Preflight reads both outputs;
non-neutral motor startup refuses all writes. An existing nonzero supply demand may be adopted
within its clamp, but motor execution still requires all independent feedback checks. A new
MemoryFixture instance is a new fake plant; reusing the same fixture in tests models a controller
restart with persistent outputs. Neither behavior establishes what real hardware does on reboot.

Snapshots log ownership, last verified demand and unknown status. When status is `unknown`, the
logged value is historical application state, NOT a claim about the current output.

## Interlocks and independent telemetry

Before and during motor actuation:

- Read back supply demand and compare with its current acknowledged state AND the configured
  motor prerequisite (default 0.500 V).
- Require fresh independently injected bus measurement, default 40 V +/- 2 V, maximum age 1 s.
  A profile may tighten age/tolerance but cannot change the configured bus target.
- Require fresh fake confirmations of motor electrical mapping, physical-disable arrangements
  and external supply readiness. Actual mapping/neutral verification flags remain false.
- Refuse missing, stale, future-dated, wrongly typed or out-of-range samples.

Motor feedback/context is checked before each scheduled output update and periodically during
holds. This remains sampled software checking, not an instantaneous physical safety interlock.

Supply decreases additionally require actual motor-neutral readback and fresh `regen_safe` and
`supply_shutdown_permitted` fixture confirmations. Standalone supply characterization also needs
its explicit standalone-test confirmation. No power-return safety is inferred from throttle zero.

Fixture sensors are separately injected and periodically sampled from their own configured values.
They do NOT derive bus voltage from supply commands. Default fake bus voltage is zero even when
a 0.500 V demand is held. The example scenario injects 40 V later as a separate event. Stopping
fixture sensor streaming or injecting an unsafe value is available for future negative tests.
Every feedback record is marked simulated with a FAKE source. None can satisfy a live backend:
there is no live v2 backend or physical authorization path in this release.

## Normal finish versus termination

| Event | Motor role | Supply role |
|---|---|---|
| Normal motor finish | Verify neutral | Do not write; verify persistent demand |
| Normal supply hold finish | No motor write | Verify requested held demand |
| Explicit supply-down finish | No motor write; neutrality required | Verify zero demand |
| Ctrl+C, fault or process shutdown | Attempt verified neutral on existing usable backend | Preserve last demand; inspect/read back and log |
| Lost connection/context or failed readback | Report unknown if neutrality cannot be verified | No guessed restoration or automatic zero; report unknown |

Preserving supply demand during a fault is intentional: automatically dropping a regeneration sink
can itself be unsafe. It is NOT a general hardware fail-safe. Supply enable, analog-programming
selection, operating mode, OVP/OCP and physical emergency stop remain external/manual. There are
no RZ enable/disable or SCPI writes. Zero demand is not power-off. A crash, power loss or killed
process may leave a real output at its last setting; software cannot guarantee deenergization.
External energy-management, physical-disable and emergency-stop design are unresolved requirements.

## Edge semantics and observability

Startup HIGH is suppressed; LOW then HIGH starts once. Held HIGH never repeats. Busy transitions
are ignored, not queued; every button resynchronizes after execution and needs fresh LOW/HIGH.
Simultaneous presses use configuration order. Stale edges, including edges aged during precondition
checks, do not start a profile. No concurrent tests, busy-time cancel button or reconnect/resume exists.

V2 logs use `logs/dispatcher-v2-fixture.jsonl`, rotating at 10 MiB with five backups. They include
ownership snapshots, independent fake samples, acknowledgements, checkpoints, cycle completions and
per-role termination state. A failed log sink faults execution once, then is disabled so cleanup
can still be attempted. Copy logs before rotation if permanent evidence is needed. Runtime state
is not restored from the log. This is not a Windows service or an unattended-operation qualification.

## Migration (future, explicit user action)

The migration command validates the existing unversioned dispatcher, copies its source to a unique
`.backup-<UTC>-<suffix>` file and writes a NEW destination. It never overwrites the source or any
existing destination. Paths are adjusted relative to the destination. It preserves the three-button
single-output workflow; it does not guess dual-role mappings or enable a supply hold. The older
engine config is rejected rather than blindly imported.

After separate authorization, an example migration command is:

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine migrate-dispatcher --source dispatcher.json --output dispatcher.v2.compat.json
```

Executing a migrated compatibility config still uses the original OXYGEN simulated-AO path. It is
NOT part of the network-free v2 fixture session below.

## Future simulation-only session - NOT executed yet

Do not run these until separately authorized. From the project root:

```cmd
.\.venv-scpi\Scripts\python.exe -m pytest -q tests/test_dispatcher_v2.py
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.simulation.json
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.simulation.json --execute --confirm-simulated-output --scenario scenarios\dual_aout_fixture_demo.json
```

The future demo should last approximately 27 seconds, using fake buttons and independently injected
sensors: supply ramp/hold, motor-only gentle profile, then explicit supply-down. It requires no
OXYGEN server, actual supply, VESC or second physical AO. Scenario events are relative to simulation
start; choose sufficiently long gaps to avoid intentionally ending a still-active profile. A stop
event during active execution is an interrupted-profile fault, not success.

Exit codes: offline validation/successful complete scenario 0; Ctrl+C with verified cleanup 130;
fault/refusal/unknown cleanup 1. No test result or successful demo is claimed by this document.

## Unresolved physical commissioning facts

- Actual second AO identity, both final role mappings, units, mode/source/property and electrical wiring.
- Applied RZ-XA external-demand calibration, polarity/isolation and reference scheme. The 800 V rating
  and 0..10 V demand suggest an UNVERIFIED 80 V/V hypothesis: 0.500 V might request 40 V. This is not
  bus feedback or verified calibration and is never used to generate telemetry.
- Actual external output-enable/RUN state, analog/CV selection, OVP/OCP and current/power limits.
- VESC electrical input mapping, physical neutral, disable mechanism and safe physical limits.
- Independent DC-bus sensor identity, calibration, freshness, tolerance and no-regeneration evidence.
- Safe response to application crash, disconnect, PC restart and energy returned to the bus.

Official references supplied for later commissioning against connected software/hardware:
https://docs.dewetron.cloud/doc/scpi/index.html
https://docs.dewetron.cloud/doc/en/03_Hardware/Hardware.html
https://www.takasago-ss.co.jp/products/power_electronics/dc/rz-xa/02.html
