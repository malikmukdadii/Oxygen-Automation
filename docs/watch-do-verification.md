# Read-only OXYGEN digital control-button verification

## Status and scope

**The first real property-transition pilot is captured; the reliability milestone is PENDING.**
The supplied trace shows one OFF/ON/OFF cycle, exactly one rising event, no held-HIGH repeats,
and no observation errors. Independent operator-count confirmation and repeated/short-interaction
trials are still needed. No live-control flags are changed by this work or successful diagnostics.

The operator has validated communication with `DEWETRON,OXYGEN,00000001,8.2` and simulated
I/O sources. Selected results are preserved, with raw-versus-decoded provenance, in
`tests/fixtures/oxygen_8_2_observed.json`. In particular:

| Channel | Observed properties / values |
|---|---|
| `AO 3/1 Sim` | `Mode=(STRING,"ConstOutput")`, `Range=(RANGE,-10.0,"V",10.0,"V")`, `TRION/SourceChannel_A_CONST/Const0=(SCALAR,0.0,"V")` |
| `AO 3/2 Sim` | Same mode/range; its constant property is **`TRION/SourceChannel_A_CONST/Const1`**, not `Const0` |
| `DO 3/1 Sim` | `Mode=(STRING,"DIO")`, `Direction=(STRING,"output")`, `DO_Value=(BOOL,OFF)` |

These are configuration/readability observations, not physical voltage or feedback validation.

## Official sources checked

The title of the official English help page identifies it as **OXYGEN Online Help 8.2**.

1. [Control instrument: Digital out state](https://docs.dewetron.cloud/doc/en/07_Instrument_properties/Instrument_properties.html#control-type-digital-out-state): only assigned DO channels are shown; the action depends on each channel's selected mode. This does **not** promise a particular SCPI property or momentary press/release behavior.
2. [Digital Input/Output modes](https://docs.dewetron.cloud/doc/en/06_Data_channels/Data_channels.html#digital-input-output-modes): Digital Out provides static Low/High; other modes include clock/time-signal functions. The documented chassis modes do not by themselves establish the meaning of the observed module-specific `DIO` literal. Verify the selected channel and UI behavior rather than inferring them from that string.
3. [Channel/property access](https://docs.dewetron.cloud/doc/scpi/Channellist_access.html): discover attribute names, then query `:CHANNELlist:PROPerty? "ID","property"`.
4. [Numeric values](https://docs.dewetron.cloud/doc/scpi/Measurement_values.html): channel-name selection, `REL-TIME`, scalar dimensions, ASCII values and aggregation/last-hold behavior.
5. [Acquisition state](https://docs.dewetron.cloud/doc/scpi/Acquisition_control.html), [error queue](https://docs.dewetron.cloud/doc/scpi/Error_handling.html), and [response headers](https://docs.dewetron.cloud/doc/scpi/Application_control.html).

`DO_Value` is used because it was actually discovered on this installation, not because the
Control help guarantees it tracks a UI action. No alternative property names are invented.

## Architecture and safety boundary

- `main.py` has a dedicated `watch-do` branch. It does not construct `AutomationEngine`, run profiles, start `DOMonitor`, or create any RZ-XA/output controller. `--config` and `--live` are not consulted; no configuration is rewritten.
- `channel_manager.py` was reviewed and remains unchanged. The diagnostic resolves exactly one current channel name using the adapter on **every connection** instead of trusting an old configured ID or the first duplicate name. IDs remain strings.
- `do_monitor.py` retains its existing engine architecture. A separate `DiagnosticEdgeTracker` performs observation-only edge tracking, with no callbacks or command dispatch. The existing engine monitor is not used by this diagnostic.
- The adapter now exposes a raw property payload and a shared pure typed parser, avoiding a second read for the parsed state. Existing adapter interfaces remain. Numeric transfer selection is separated from repeated value reads.
- `DiagnosticTransport` rejects output/property writes, markers, resets, setup loads, acquisition/store commands and compound messages. With no numeric opt-in, its wire traffic is queries only. There are no digital-output reset or cleanup writes.
- `--compare-numeric` explicitly permits only numeric `ITEMS`, `FORMat ASCII`, and `NUMber ALL` setup. It selects `REL-TIME` and the one DO channel; checks scalar dimensions and current acquisition state; and queries values thereafter. No sample rate, aggregation rate, acquisition or recording setting is changed. Transfer settings are **not restored** when exiting. Do not share the SCPI numeric list with another controller.
- Numeric observations must be binary, finite, acquired while `Started`, and have an advancing `REL-TIME`. Held/backward/overlarge timestamp gaps invalidate the numeric history. Unsupported numeric comparison falls back to property-only observation with a visible `numeric_unavailable` record; an explicitly selected numeric event source fails closed if unavailable.
- Error queries consume queue entries, but no `*CLS` is issued. Neither the diagnostic nor the automated tests authorize UI operation on connected hardware.

## Exact Windows commands

Run in Command Prompt from the project root using the existing `.venv-scpi` environment.
Close other SCPI/automation clients first, particularly in OXYGEN single-client mode.

### Property-only observation

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine watch-do --host 127.0.0.1 --port 10001 --channel "DO 3/1 Sim" --interval-ms 50 --timeout 0.5 --max-gap-ms 250 --duration 60 --log-file logs\do-property.jsonl
```

### Optional numeric comparison

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine watch-do --host 127.0.0.1 --port 10001 --channel "DO 3/1 Sim" --interval-ms 50 --timeout 0.5 --max-gap-ms 250 --compare-numeric --duration 60 --log-file logs\do-comparison.jsonl
```

### Counted trial: 20 activations, nominal one-second HIGH intervals

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine watch-do --host 127.0.0.1 --port 10001 --channel "DO 3/1 Sim" --interval-ms 50 --timeout 0.5 --max-gap-ms 250 --duration 90 --expected-presses 20 --interaction-ms 1000 --log-file logs\do-20-slow.jsonl
```

### Short-interaction trial: nominal 100 ms HIGH, faster requested polling

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine watch-do --host 127.0.0.1 --port 10001 --channel "DO 3/1 Sim" --interval-ms 10 --timeout 0.5 --max-gap-ms 250 --duration 90 --expected-presses 20 --interaction-ms 100 --log-file logs\do-20-short.jsonl
```

`--interaction-ms` is an operator-entered label, **not an imposed or measured UI pulse width**.
For additional trials change that label to 250 or 50 and use distinct log filenames. Only
claim durations supported by an independent UI/timing record. `--duration 0` runs until Ctrl+C.
The requested interval is start-to-start when possible; actual serial SCPI/console/file latency
can make it longer. Do not assume a 10 ms request yields 100 samples/second.

Logs append, with a unique `run_id` on every record. Omit `--log-file` for a new timestamped
file. Queries, replies, latency and query failures are logged to JSONL; samples, baselines,
transitions, errors and connection events are also displayed. UTC timestamps and a monotonic
clock are both included. Ctrl+C closes the socket and writes a summary; no output state is reset.
The default `--console events` displays unchanged status at most once per second and state
changes immediately. Use `--status-interval 0` for changes/events only or `--console samples`
for the original per-poll display. Repeated unchanged numeric mismatch/staleness notices are
also throttled on the console. Every record remains in JSONL, regardless of console mode;
polling, latency measurement, edge tracking and safety gates are unchanged.
Exit zero means the observation session was established and stopped normally, **not** that the
milestone passed. Review recovered errors and suppressed samples in the summary too.

## Safe manual procedure

1. **Independently verify isolation.** Prefer the known simulated DO source. A `Sim` suffix alone is not proof: confirm the source in OXYGEN hardware/channel settings. For a physical channel, the operator must confirm it is unconnected and cannot actuate equipment. Isolate motor drives, loads, relays and other hardware; no AOUT or Takasago work is part of this test.
2. Ensure no engine/profile/monitor process is running. Leave all `live_control` flags disabled. Keep acquisition/recording as they are; the diagnostic will not start or stop them. Numeric comparison needs already-running acquisition; if unavailable, remain property-only rather than changing it automatically.
3. Inspect the OXYGEN **Control** instrument configured for **Digital out state**. Ensure it contains **only the one intended isolated DO channel**. Confirm its operating mode is suitable for static Low/High, not frequency/clock/IRIG/PPS output. Do not operate a global control affecting multiple channels. If this cannot be confirmed safely, stop here.
4. Start the property-only command. Wait for `discovery`, `connected`, and a fresh `transition` with `edge="baseline"`. Check the reported exact name/ID and raw `DO_Value`. Make no UI changes for several seconds; there should be no `press_detected` record.
5. For one pilot activation, operate only that DO control in OXYGEN. Observe whether it is latched/toggling or momentary. Do **not** assume releasing the mouse restores Low. Observe raw payloads and state before, during, and after. Restore Low through the same safe UI control if required. Python never writes the state.
6. If the property tracks the UI, one observed OFF-to-ON change should produce one rising `transition` and exactly one diagnostic `press_detected`; holding ON must not repeat it. Returning OFF produces a falling transition but no new press. For a toggle, an ON click followed by an OFF click is **one activation cycle**, not two press events. The observed `observed_high_ms` on falling transitions is a sampled duration, not exact physical/UI timing.
7. Check startup-high suppression separately: safely leave the isolated channel ON, start a fresh property-only session, and wait. Its first HIGH must be baseline only, with zero presses. Restore OFF; only a subsequent fresh OFF-to-ON activation may count. No initial/stale/reconnecting HIGH should be counted.
8. Run the counted slow trial, beginning operations only after a fresh OFF baseline. Independently tally or timestamp 20 activation cycles; hold HIGH roughly one second and LOW long enough to be sampled (e.g. at least half a second). Compare **each** operation to the trace, not just the final total. A held ON counts once.
9. Repeat separate trials for shorter interactions, initially 250 ms and then 100 ms, optionally 50 ms, at requested 50 ms and 10 ms polling. Use an independent UI/timing record where possible; human click duration is not precise. Record actual observed sample gaps and latency. If interactions are too short to detect reliably, that is a failed reliability target, not a reason to synthesize events.
10. If a disconnection, timeout, malformed reply, SCPI error, or excessive gap occurs, the affected history is invalidated. Wait for a new fresh baseline. An interaction crossing that interval is **unobserved**, not a proven press. A communication interruption may be tested only using an independently safe communication-only method; do not restart acquisition, toggle outputs, or send mock fault commands to real OXYGEN to manufacture a failure.
11. Stop with Ctrl+C or let the trial finish. Capture the full log and summary, the UI source/mode, independent operation record and any mismatch. Leave flags disabled even if the trial succeeds.

## If `DO_Value` does not follow the UI

This is an observation to retain, not a mapping to mark verified:

- Save the original property-only trace, exact channel ID, UI assignment/mode, and operator times. A fixed property plus actual UI operations is stronger evidence than a static read alone.
- Run the optional comparison command on the **same isolated channel**. Property and measurement are sequential observations, so `comparison_skew_ms` and numeric aggregation/last-hold behavior matter. A single mismatch near a transition is not conclusive.
- If a fresh binary numeric value consistently tracks the UI while the property stays fixed, repeat counted trials with the explicitly selected numeric event source:

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine watch-do --host 127.0.0.1 --port 10001 --channel "DO 3/1 Sim" --interval-ms 50 --compare-numeric --event-source numeric --duration 90 --expected-presses 20 --log-file logs\do-numeric-source.jsonl
```

- Do not use a threshold to turn arbitrary nonbinary values into a DO state. Nonbinary/unavailable values are reported and not counted.
- If measurement comparison is unsupported or neither observation tracks the UI, inspect the logged live attribute list and official documentation. `--property "EXACT_DISCOVERED_NAME"` can observe another **explicitly selected, documented/readable Boolean property**, but the diagnostic rejects names absent from current discovery. Do not guess `Value`, `State`, or other properties. If there is no verified readable state, the milestone remains blocked pending DEWETRON guidance.

## Interpreting reliability and acceptance

The log distinguishes `sample`, `transition` (including `baseline`), and `press_detected`.
Only a fresh rising edge on `--event-source` emits `press_detected`. Property and numeric
rising counts are separate, avoiding double-counting the same interaction.

The summary reports source counts, manually expected count, count difference/ratio, errors,
timeouts, reconnections, stale samples, mismatches and the transition-history tail. Requested
interval is not measured latency. Poll-gap and individual query p50/p95/max statistics cover
the last 10,000 observations. Query records retain the complete latency history in the file.
Numeric freshness checks cannot guarantee physical feedback or eliminate every possible UI/SCPI
race. Aggregated numeric data may be nonbinary; that source will be marked unavailable.

A pulse entirely between polls is invisible. A low reset entirely between polls can also merge
two activations into one sustained high. Equal totals alone cannot rule out a missed activation
and a duplicate canceling each other out. `count_ratio` is only a comparison to the operator's
entered count, **not a measured probability of detection**.

Accept a defined operating envelope only after live traces show:

- One matched rising event per independently recorded activation, with no extras while held.
- No initial, reconnecting, invalid or cross-gap observation generates a press.
- The shortest supported interaction and minimum OFF reset duration have been explicitly tested.
- Actual latency/gap statistics support that target and any losses have been explained/retested.
- The chosen property or numeric source follows the real OXYGEN Control instrument, not just a mock.

Do not claim universal short-pulse reliability. If the polling envelope is insufficient, investigate
an officially supported time-resolved/event acquisition mechanism separately; do not make up a
property or enable output control. The software deliberately reports `milestone_verified=false`;
a human-reviewed operator record is required before updating the acceptance status below.

## Live results record (reliability trials pending)

| Trial | UI mode/source | Target/actual HIGH and LOW durations | Requested/actual gap | Expected activations | Matched events | Missing / extra | Errors / gaps | Result |
|---|---|---|---|---:|---:|---|---|---|
| Static communication/property reads | OXYGEN 8.2, simulated DO, DIO | No operator transition | N/A | N/A | N/A | N/A | No SCPI errors | Passed communication only |
| Property pilot `c09bbf56258f4ae1a876b1f20b3e5d6f` | OXYGEN 8.2, DO 3/1 Sim, DIO | Sampled HIGH 1674.83 ms; independent timing not supplied | Requested 50 ms; median 61.93 ms; maximum 63.84 ms | Independent count not supplied | 1 rising, 1 falling; no repeated rising | None apparent in captured cycle | 0 errors/timeouts/stale samples/reconnects; 81 valid samples | Property-to-event path demonstrated; repeated operator-correlated reliability **pending** |
| Startup HIGH / recovery | Pending | Pending | Pending | 0 initial events | Pending | Pending | Pending | **Pending** |
| Slow counted trial | Pending | Pending | Pending | 20 | Pending | Pending | Pending | **Pending** |
| Short-interaction trials | Pending | Pending | Pending | 20 each | Pending | Pending | Pending | **Pending** |

Selected consecutive sample windows and the pilot summary are preserved in
`tests/fixtures/oxygen_8_2_do_pilot.json`. Attach JSONL filenames/run IDs and independent
operator timestamps when filling the remaining rows. No unobserved trial result is inferred.

## Automated validation

```cmd
.\.venv-scpi\Scripts\python.exe -m pytest -q
```

Tests replay the observed 8.2 payloads and exercise header-on/off TCP sessions, parser errors,
startup HIGH, sustained HIGH, rearming, stale/late samples, query failures, errors, timeouts,
reconnections, numeric fallback, nonbinary values, held numeric clocks, source selection,
unsupported properties/channels, CLI engine isolation, log output and transport write rejection.
Mock-only state changes in tests are not commands that the live diagnostic can send.

Implementation validation: **183 tests passed** using `.venv-scpi`, Python compilation passed,
and the CLI help was checked. No live UI interaction was performed by the coding agent.
