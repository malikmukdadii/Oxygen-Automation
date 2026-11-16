# Configurable V2 AOUT discovery

For explicitly authorized physical outputs, use the separate `oxygen_hardware` backend described
in [physical open-loop operation](hardware-open-loop.md). The demo approvals below still apply
only to `oxygen_demo`; discovery alone does not grant physical write capabilities.

## Configure the selected OXYGEN instance

Copy `dispatcher.v2.oxygen-demo.json` to a machine-specific file. Keep it alongside the
original or update its profile paths (profile paths are relative to the bindings file).
Change `connection.host`, `connection.port`, and `connection.timeout_s` for that instance.
Only one OXYGEN connection is used; there is no simultaneous multi-machine execution.

For **each** logical output, `rzxa_voltage_demand` and `vesc_throttle`:

| Field | Meaning |
|---|---|
| `channel` | Exact OXYGEN channel name, case-sensitive, including spaces, module/slot notation, and punctuation. Required for SCPI discovery; never a display label. |
| `display_name` | Optional human-readable label; defaults to the logical role in reports. Does not affect discovery. |
| `expected_id` | Optional string identity check. Omit or set to null to discover the current ID without an additional saved-ID constraint. A supplied value must match the name-discovered ID. |
| `mode` | Keep `ConstOutput`. Discovery never changes the active mode. |
| `source` | Keep `DISCOVER_CONSTANT` for the OXYGEN backend. The actual source must already select a supported constant. |
| `const_property` | Use `auto` to resolve the selected constant's listed property. An explicit property path remains supported, but must equal the discovered active property. |
| `min_aout_v`, `max_aout_v` | Existing per-role clamps; discovery validates the channel's range covers them. These are not inferred from a name. |

For example, the supply role may use `channel` = `RZ-XA demand AO 8/7` and
`display_name` = `Supply voltage demand`, while the motor uses `channel` =
`Throttle AO 2/4`. Keep the logical role keys and existing safety settings unchanged.
Neither the channel name nor its ID establishes physical wiring, voltage scaling, motor
neutral, or simulation status. The provisional supply scale remains explicitly unverified.

The `memory_fixture` example remains offline, with null real-channel identities and explicit
fixture properties. `auto` is for OXYGEN discovery, not a way to turn fixture execution into
hardware execution. The historical simulation blueprint remains an unchanged proposal.

## Read-only diagnostic

When separately authorized to connect, run:

```powershell
python -m oxygen_engine discover-aout --bindings dispatcher.v2.oxygen-demo.json
```

Substitute your machine-specific bindings file. This command **does connect** to the configured
OXYGEN server, unlike the default offline `dispatch-patterns` preview. It never constructs the
profile executor, authorizes an output, or performs cleanup/neutral writes. No execution or
simulation-confirmation flags are needed for a read-only inspection of physical or simulated
channels. Hardware-control flags must still remain disabled in the configuration.

JSON output reports both roles, display names, exact configured names, string IDs, active modes
and sources, resolved properties, units, ranges, and current setpoints. It includes available
property names, typed discovered values, and configuration mismatches. Missing/ambiguous names,
property problems and connection failures produce a nonzero exit code. This is diagnostic
information, not permission to actuate the channels. Invalid JSON/schema configurations are
rejected before connecting.

The wire operations are limited to documented OXYGEN SCPI queries:

- `*IDN?`
- `:CHANNELlist:NAMes?`
- `:CHANNELlist:ITEM<ID>:ATTR:NAMes?`
- `:CHANNELlist:PROPerty? "ID","property"`

These reuse `OxygenAdapter` parsing and discovery. No mode, source, range, constant, acquisition,
recording, numeric-transfer configuration, reset, or error-queue clearing commands are sent.

## Source/property resolution and fail-closed behavior

Discovery requires exactly one name match per role and distinct channel IDs. IDs remain full
strings, including unsigned 64-bit values. `expected_id` is checked **after** name discovery;
it is never used as a fallback for a missing or ambiguous name.

The resolver lists the channel's actual properties and reads `Mode`, `TRION/SourceChannel_A`,
`Unit`, `Range`, and the listed `TRION/SourceChannel_A_CONST/Const<N>` properties. Supported
mode/source representations are unambiguous SCPI STRING values. For example, selected source
`CONST1` must have exactly one corresponding listed `TRION/SourceChannel_A_CONST/Const1`.
Selecting `CONST0` requires `Const0`, regardless of output role, module/slot, or channel number.
The current constant must be a finite SCALAR in V; range must be a finite ordered RANGE in V
covering the configured clamps. Other/absent modes, unknown or ambiguous selectors/properties,
unsupported typed representations, unit/range errors, and explicit-property mismatches are
reported with discovered values and refused; they are never guessed or repaired.

At execution startup the reusable `AoutDiscovery` service pins both successfully validated
identities, modes, sources, properties, units and ranges. Before writes, the demo backend
revalidates them. A later change invalidates the session and revokes capabilities; restoration
of an old value does not silently re-enable execution. Close and restart to rediscover.
Setpoints are read afresh, not pinned, since authorized profiles intentionally change them.
Existing startup checks still require motor neutral and in-clamp readbacks before any write.

## Discovery is not write authorization

### DOUT names are independently configurable

Set `assignments[].trigger` to any exact OXYGEN DOUT name, regardless of module/slot or chassis
controller location. The `DO 3/1 Sim` through `DO 3/6 Sim` example names are not a V2 whitelist.
Up to six enabled assignments can use distinct names. Startup discovers a unique ID for each
trigger and requires a Boolean `DO_Value`; missing/ambiguous names, AOUT aliases and unsupported
properties fail closed. Resolution is logged as `do_channel_resolved`. No DOUT writes are
authorized, and selecting a physical DOUT as a trigger does not authorize physical AOUT writes.
Offline `dispatch-patterns` previews include exact configured trigger names. `discover-aout`
still reports AOUTs only; use `watch-do` to inspect each DOUT read-only before execution.
The memory-fixture CLI validates scenario button keys against configured trigger names too.

### Explicit simulated AOUT approvals

The `oxygen_demo` authorization remains separate from discovery. The operator confirmed that
`Test Aout2` and `Test Aout1` are genuinely simulated and unconnected after read-only discovery
on OXYGEN 8.2 reported `CONST4` and `CONST0` respectively. The role/name/property allowlist now is:

| Logical role | Exact approved name | Permitted constant property suffix |
|---|---|---|
| `rzxa_voltage_demand` | `AO 3/1 Sim` | `Const0` or `Const1` |
| `vesc_throttle` | `AO 3/2 Sim` | `Const0` or `Const1` |
| `rzxa_voltage_demand` | `Test Aout2` | `Const4` only |
| `vesc_throttle` | `Test Aout1` | `Const0` only |

Each property must still be discovered as the active `TRION/SourceChannel_A_CONST/Const<N>`
property. The new approval does not fix IDs: IDs are still discovered by exact unique name
and pinned for the session. Both selected names must be confirmed on each execution using
`--confirm-demo-output`, and per-output simulation permissions remain mandatory. Role swaps,
unapproved properties and arbitrary other names are rejected even with CLI confirmations.
A label containing `Sim`, changed IDs, or a configuration flag cannot commission physical outputs
or expand the allowlist. Names cannot prove simulation status; only use these approvals while
the channels are genuinely simulated and unconnected.

Thus changing machine configuration is sufficient for **discovery**, but newly named or
physical outputs remain read-only until a separately reviewed/authorized backend exists.
Do not rename physical channels to approved demo names. Software names cannot prove that a
channel is genuinely simulated and unconnected.

Supply-demand persistence, motor neutral handling, completion policies, output limits, fake
independent bus telemetry and interlocks are unchanged. Discovery does not establish actual
RZ-XA voltage scaling, supply output enable, electrical wiring or motor safety.

## Deferred validation

New coverage is in `tests/test_aout_discovery.py` and updated
`tests/test_oxygen_demo_backend.py`. It covers arbitrary names/slots, full IDs, both constant
sources, invalid properties/modes/ranges, identity checks and session changes, query-only
reporting, and unchanged simulation write restrictions. The tests have not been executed in
this implementation pass. Hardware connections and final validation require explicit approval.
