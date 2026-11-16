# OXYGEN SCPI communication audit

## Authority and scope

Reviewed against the published [DEWETRON SCPI reference](https://docs.dewetron.cloud/doc/scpi/index.html), specifically:

- [Common commands](https://docs.dewetron.cloud/doc/scpi/Common_commands.html): `*IDN?`.
- [Channel list access](https://docs.dewetron.cloud/doc/scpi/Channellist_access.html): names/IDs, attribute names, properties and typed values.
- [Measurement values](https://docs.dewetron.cloud/doc/scpi/Measurement_values.html): numeric item selection, ASCII format, transfer count, dimensions and values.
- [Recording control](https://docs.dewetron.cloud/doc/scpi/Recording_control.html): textual store states and store commands.
- [Acquisition control](https://docs.dewetron.cloud/doc/scpi/Acquisition_control.html): acquisition states.
- [Markers](https://docs.dewetron.cloud/doc/scpi/Marker.html): marker addition.
- [Application control](https://docs.dewetron.cloud/doc/scpi/Application_control.html): optional response headers (`:COMMunicate:HEADer`).
- [Error handling](https://docs.dewetron.cloud/doc/scpi/Error_handling.html) and [property examples](https://docs.dewetron.cloud/doc/scpi/Examples.html).

Only OXYGEN communication and its mock/test/documentation paths were changed. No Takasago implementation or control changes were made. Passing mock tests does not establish hardware property semantics or firmware compatibility.

## Corrected command mapping

| Operation | Previous invented syntax | Documented syntax now used |
|---|---|---|
| Identification | `*IDN?` | `*IDN?` (retained; four-field response checked) |
| Channel discovery | `CHAN:LIST?` returning JSON | `:CHANNELlist:NAMes?` returning `(ID,name)` tuples or `NONE` |
| IDs / name resolution | `CHAN:ID?` | `:CHANNELlist:IDs? ["name",...]` returning quoted string IDs or `NONE` |
| Attribute names | `CHAN:PROP? ID` | `:CHANNELlist:ITEM<ID>:ATTR:NAMes?` |
| Property value | `CHAN:PROP? ID,property` | `:CHANNELlist:PROPerty? "ID","property"` |
| Recording state | `REC:STAT?` numeric boolean | `:STORe:STATe?` textual state |
| Recording commands | `REC:START`, `REC:STOP` | `:STORe:STARt`, `:STORe:STOP` (not used by smoke test) |
| Marker | `REC:MARK` | `:MARKer:ADD "label"` (not used by smoke test) |
| Measurement | `CHAN:MEAS? ID` | `:NUMeric:NORMal:ITEMS "name",...`, then `:NUMeric:NORMal:VALue?` |
| Acquisition state | none | `:ACQUisition:STATe?` |
| Error queue | `SYST:ERR?` | `SYST:ERR?` (documented short form retained) |

Discovery no longer invents channel types: channel discovery supplies IDs and names, not an instrument-independent type. IDs remain strings throughout, including values up to `18446744073709551615`. Numeric conversion of channel IDs is used only for range validation, never float conversion or storage.

`recording_state()` exposes `Armed`, `Paused`, `Started`, `Stopped`, or `Stopping`. The compatibility `recording_status()` treats every state except `Stopped` as an existing recording session, avoiding accidental starts while paused/armed/stopping. Acquisition states are `Stopped`, `Waiting_for_sync`, and `Started`.

`read_measurements(names)` configures the SCPI numeric output list by **channel name**, explicitly selects ASCII and transfer count `ALL`, requires `DIMS?` to report one scalar per selected channel, and checks the returned value count. `read_measurement(id)` discovers the corresponding name before using that path. These calls modify SCPI numeric transfer configuration, not acquisition or recording state. They are intentionally excluded from the smoke test. They do not restore a previous numeric transfer list; do not share numeric configuration with another controller.

## Parsing and transport

- Shared parser handles comma-separated values, parenthesized tuples, single/double quoted strings, and doubled quote escapes. Unquoted tokens are initially retained as strings to avoid ID precision loss.
- `get_property_value()` returns a `PropertyValue(kind, values)` for booleans, numbers, strings, `ENUM`, `SCALAR`, `RANGE`, `RATIO`, `CHANNEL_ID`, `CHANNEL_ID_VECTOR`, and typed examples such as `BOOL`, `STRING`, and `FLOAT`. Compound units and enum labels are retained. The legacy `get_property()` string interface remains for existing callers.
- Optional short/long response headers are accepted and checked against the query. No header-mode command is needed to connect. Inline negative SCPI error replies raise protocol errors; error-queue replies are decoded as code/message pairs with quoted commas/quotes preserved.
- Malformed structures, invalid IDs, unknown measurement IDs, unexpected states and incorrect numeric dimensions/counts fail explicitly. `NONE` is preserved as the documented absence response; callers can inspect the instrument error queue for rejected property/attribute requests.
- A timed-out or malformed transport response closes the connection and clears the receive buffer, preventing partial/late data from satisfying a subsequent query. Reconnect is explicit and never replays a command.
- Existing locking, reconnect limits and lifecycle architecture remain. SCPI string construction rejects line breaks/NUL, and transport rejects multiline messages.

## Mock and safety

The OXYGEN mock now emits documented tuple/string/typed/textual formats and supports response headers, numeric selection/values, acquisition/store state queries, error responses and existing fault-injection helpers. It rejects the old `CHAN`/`REC` syntax. Seeded AOUT/DO property names are **simulation fixtures only**.

Physical property writes are blocked in `OxygenAdapter.set_property()` even if existing live-control flags are set. No flags are enabled by this change. Live AOUT snapshots do not query assumed `Mode`, constant-output or waveform property names. Engine DO reads require an explicitly configured property plus the existing verification gate; candidate probing remains mock-only. Selected names and read-only value formats have since been observed on the user's simulated channels, but physical-output and UI-transition semantics remain unverified. The separate `watch-do` diagnostic can investigate those observations without starting the engine or enabling its gate.

## Read-only hardware discovery

Enable SCPI in OXYGEN System Settings > Remote Control, port `10001`. Disconnect other controllers when configured for single-client operation. From the project root in Windows PowerShell, with Python 3.11+ installed:

```powershell
py -3 -m venv .venv-scpi
.\.venv-scpi\Scripts\python.exe -m pip install -r requirements.txt
.\.venv-scpi\Scripts\python.exe -m oxygen_engine scpi-smoke --host 127.0.0.1 --port 10001 --timeout 5
.\.venv-scpi\Scripts\python.exe -m oxygen_engine scpi-smoke --host 127.0.0.1 --port 10001 --timeout 5 --aout "Test Aout1" --aout "Test Aout2"
.\.venv-scpi\Scripts\python.exe -m pytest -q
```

Substitute exact channel names from the first report for the example `--aout` names. The dedicated command ignores engine connection configuration and never constructs `AutomationEngine`, `AoutController`, a DO monitor, or an RZ-XA adapter. Its wire operations are limited to identification, channel discovery, selected attribute names, acquisition/recording state queries and error-queue queries. There are no output changes, motor actions, resets, setup loads, marker writes, acquisition or recording start/stop, or numeric configuration commands. Error-queue queries consume entries; `*CLS` is never sent. Exit status is nonzero for discovery/connection/protocol failures or queued SCPI errors.

The JSON report lists the exact live attribute names for each explicitly selected channel. Output-write semantics and DO UI transitions remain unverified until separately reviewed. The operator subsequently validated real OXYGEN 8.2 communication using simulated I/O sources: AO channel 1 exposes `Const0`, AO channel 2 exposes `Const1`, and DO channel 1 exposes `DO_Value` as `(BOOL,OFF)`. Single/multi-channel numeric reads also succeeded after accepting the observed `:NUM:DIMS` response header. Automated tests still do not operate the real installation.

Selected captured property payloads, decoded results and their provenance are preserved in
`tests/fixtures/oxygen_8_2_observed.json`. Raw wire replies were not captured for the decoded
channel-list/numeric output; the fixture explicitly distinguishes these from raw property payloads.
The next milestone uses the isolated [watch-do procedure](watch-do-verification.md), not engine dispatch.

## Validation

The pre-change suite passed: 13 tests. Added protocol fixtures independently based on the published examples plus header-on/header-off TCP integration tests. Coverage includes 64-bit IDs, quotes/commas, typed values, malformed replies, unknown channels, dimensions/count checks, SCPI errors, timeouts, fragmentation, command injection, the physical-write block, unverified live-property guards, and the smoke-test exact query-only command sequence. Run `python -m pytest -q` for the combined suite.

The pre-existing `.venv` in this workspace referenced a missing Python interpreter. Validation used the available Python installation with the existing requirements, without changing dependency versions or repairing the user's environment in place. The separate `.venv-scpi` commands above avoid that stale environment.
