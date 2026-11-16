# Dual-AOUT patterns in the local OXYGEN demo

The v2 memory demo was operator-tested: 56 tests passed, with all three scripted cycles complete.
The OXYGEN-demo bridge subsequently passed 69 targeted mock/v2 tests. That establishes mock
integration, not successful writes to the second AO in actual OXYGEN. The next trial is in the
operator's local OXYGEN simulated setup; no additional memory demo is a prerequisite.

For real equipment, this demo procedure is not the hardware execution path. Use the separate
[hardware open-loop guide](hardware-open-loop.md), configuration and physical-output confirmations.

## What connects

`dispatcher.v2.oxygen-demo.json` selects backend `oxygen_demo` and TCP `127.0.0.1:10001`.
It reads actual OXYGEN DO states and sends constant-property updates to these two previously
observed simulated channel names only (the table records historical properties, not selection rules):

| Role | Channel | Observed constant property |
|---|---|---|
| rzxa_voltage_demand | AO 3/1 Sim | TRION/SourceChannel_A_CONST/Const0 |
| vesc_throttle | AO 3/2 Sim | TRION/SourceChannel_A_CONST/Const1 |

The names/properties come from the saved operator discovery corpus
`tests/fixtures/oxygen_8_2_observed.json`. Historical IDs are not runtime requirements: each
connection resolves the exact, unique channel names to current string IDs and pins those IDs
for that session. Duplicate names, channel-ID collisions and changes during the session refuse
operation; they never silently retarget an output. Resolved IDs are logged as `demo_channel_resolved`.
`expected_id` is null in the demo example. If supplied in any OXYGEN V2 config, it is now an
additional enforced identity check, never a replacement for unique-name discovery. Remove stale
saved IDs or set them to null when intentionally switching setups.
This change does not relax the separate legacy runner's ID policy. Only use this backend if BOTH outputs are
genuinely simulated and unconnected. A name containing Sim alone does not establish that fact.
The CLI requires a separate explicit confirmation of each selected output.

The original dispatcher configuration, memory demo and profiles remain intact. The example now
uses `const_property: auto` and optional display labels. Arbitrary names are accepted for
read-only discovery, **not** demo write authorization. See [AOUT discovery](aout-discovery.md)
and the query-only `discover-aout --bindings dispatcher.v2.oxygen-demo.json` command.

### Operator-confirmed Test Aout setup

After the original demo, the operator reported passing tests and read-only discovery on
OXYGEN 8.2 for supply `Test Aout2` selecting `CONST4`/`Const4`, and motor `Test Aout1` selecting
`CONST0`/`Const0`. Both were explicitly confirmed genuinely simulated and unconnected.
These exact role/name/property combinations are now approved in addition to the original names;
this is not a blanket authorization for other discovered channels or properties.
Leave `const_property` as `auto` and keep all physical-control flags false. The backend still
discovers current IDs and active properties, validates mode/unit/range/readback, and requires
motor startup at zero before authorizing any writes. Confirm the selected names on every run.

With the working bindings selecting those outputs and the custom DOUT names, first rerun:

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q tests/test_dispatcher_v2.py tests/test_oxygen_demo_backend.py tests/test_aout_discovery.py
```

Then, only while both outputs remain simulated and unconnected, launch:

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.oxygen-demo.json --execute --confirm-simulated-output --confirm-demo-output "Test Aout2" --confirm-demo-output "Test Aout1"
```

Start all configured DOUT triggers LOW. Wait for `preflight_passed` and `dispatcher_ready`,
then press `Ramp Enable` LOW-to-HIGH for the supply ramp/hold. Return it LOW after completion.
Use `Motor Gentle` LOW-to-HIGH for the motor profile; the supply demand must stay at 0.500 V
and motor demand must finish at zero. Use `Ramp Disable` LOW-to-HIGH to request the explicit
supply ramp-down after motor completion. `Ramp Disable` is a profile trigger, not an emergency
stop or a mid-profile cancel button. Ctrl+C attempts motor neutral and preserves supply demand.
Bus feedback remains FAKE; this does not authorize real RZ-XA or motor actuation.

## Before launch

- Stop previous automation/output clients. This backend uses one serialized SCPI connection.
- Enable OXYGEN's SCPI server on port 10001.
- Confirm both selected AO channels are genuinely simulated and unconnected.
- Both AOs must already be in ConstOutput, with unit V and a range covering their clamps.
  The existing constant-source selection must match the approved role/name/property combination
  (`CONST0`/`CONST1` for the original demo names; `CONST4` for supply `Test Aout2`). The backend reads and pins
  the actual selector at startup (`DISCOVER_CONSTANT` policy), and refuses later changes.
  It never writes the selector or changes modes/ranges. It resolves the matching listed constant
  property from the actual selector, independently of channel number. Explicit property paths
  remain supported but must match that active selection. Mode/source/property/unit/range changes
  invalidate the session and revoke capabilities rather than silently rebinding outputs.
- AO 3/2 Sim must initially be at configured neutral 0 V. Supply demand is read back, never
  automatically reset; the supply-up profile accepts zero or already-at-target 0.500 V only.
- Set each enabled assignment's `trigger` to the exact DOUT name shown in OXYGEN. Any module
  slot or chassis-controller DOUT is accepted; `DO 3/1 Sim` through `DO 3/6 Sim` are only the
  example names. Enabled trigger names must be distinct and expose a Boolean `DO_Value`.
  Missing/ambiguous names, AOUT aliases, and invalid properties refuse startup. These DOUTs
  are read only; physical DOUT reads do not authorize any physical AOUT writes.
- Start all configured DOUT triggers LOW. Their resolved IDs are logged as `do_channel_resolved`.

## Launch

An optional offline preview (does not connect):

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.oxygen-demo.json
```

Start interactive operation in the real OXYGEN demo:

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.oxygen-demo.json --execute --confirm-simulated-output --confirm-demo-output "AO 3/1 Sim" --confirm-demo-output "AO 3/2 Sim"
```

Do NOT add --scenario: OXYGEN UI buttons, not timed fixture events, trigger this backend.
Do NOT add --live or enable hardware flags. There is no physical-hardware backend, RZ-XA
communication, supply enable/disable, acquisition/recording start/stop or native waveform upload.
A preflight mismatch refuses writes; report that error rather than disabling safety checks.

## First interactive trial

Display AO 3/1 Sim and AO 3/2 Sim in OXYGEN's chart, preferably with a useful 0..1 V scale.
Wait for preflight_passed / dispatcher_ready and check that logs identify backend oxygen_demo.

1. Click DO 3/1 Sim HIGH: AO 3/1 ramps to 0.500 V over 5 seconds and holds for 3 seconds,
   then remains at 0.500 V. AO 3/2 does not change.
2. Return DO 3/1 Sim LOW after completion. Re-triggering at 0.500 V is a logged no-op,
   not a reset to zero.
3. Click DO 3/4 Sim HIGH: AO 3/2 runs the gentle 0..0.25 V pattern and returns to zero;
   AO 3/1 must stay at 0.500 V throughout.
4. Return the button LOW. Try DO 3/5 Sim (stop/go pulses) or DO 3/6 Sim (cruise steps).
5. Click DO 3/2 Sim HIGH to explicitly ramp AO 3/1 demand to zero, with motor neutral
   and fake regen-safe confirmations required. This is not a cancel button or power-off.
6. DO 3/3 Sim runs supply-only characterization and finishes holding 0.500 V.

Held/startup HIGH does not repeat. Busy presses are ignored, never queued. All buttons need a
fresh LOW then HIGH after each completed cycle. There is no DO polling/cancel button during a
pattern. Ctrl+C stops the process, attempts motor neutral and intentionally preserves supply
demand. Connection loss can prevent verification; inspect the simulated outputs if state is unknown.

## Independent FAKE bus feedback - demo only

Only the DO/AO I/O uses OXYGEN. DC bus telemetry and commissioning/regen confirmations are still
explicitly FAKE independent sensor fixtures in the demo configuration. The bus fixture is 40 V,
not computed from supply demand and not measured by OXYGEN or any instrument. Motor execution
additionally requires actual readback of the held 0.500 V AO demand, so a fake 40 V sample alone
does not start a motor profile from zero supply demand.

This setup validates button dispatch, channel isolation, waveform shape, persistent demand and
neutral behavior in OXYGEN's simulated I/O. It does NOT validate physical bus voltage, throttle
calibration, electrical enable/disable, regeneration safety or the provisional 80 V/V supply scale.
Do not connect hardware or relabel physical channels as Sim to use this backend. Physical mapping
and neutral flags remain false, all physical-control flags must remain false, and only the two
explicitly approved demo role/name/property bindings are eligible. Real hardware needs a separate commissioned backend
with independent actual feedback; editing these flags cannot enable one.

## Timing, state and logs

Default demo updates are 150 ms with a 500 ms lateness budget to allow the verified SCPI
write/readback path and per-role checks. These are software timing targets, not a hardware clock;
expired slots are skipped and excessive delay faults. Six-button idle scans may be slower than
the configured 50 ms sleep because all SCPI I/O is serialized.

Logs: `logs/dispatcher-v2-oxygen-demo.jsonl`, rotated at 10 MiB with five backups. Queries,
readbacks, source pins, fake feedback provenance and per-role state are retained. The final
summary distinguishes last verified demand from unknown output state. Supply demand can remain
nonzero after cancellation, fault or process exit; it is not an output-enable command and software
cannot guarantee physical deenergization. Inspect/restore the simulated demand explicitly as needed.

Edit profile steps or button assignments in this demo's bindings file, then stop and restart;
there is no hot reload. The existing per-role 0..0.500 V supply and 0..0.400 V motor clamps apply.
