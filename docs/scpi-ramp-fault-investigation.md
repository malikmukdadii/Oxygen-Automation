# RZ-XA ladder SCPI -200 investigation

## Scope and evidence

Offline investigation on `main`. No live OXYGEN connection or physical commands were issued.
The current `logs/dispatcher-oxygen.jsonl` and all three local rotations were parsed in full
(118,660 records at inspection). The requested failure is in **`logs/dispatcher-oxygen.jsonl.1`**,
run `670cc42acc944ca5ac3e568e5399e94d`; the current file instead contains a later successful run
terminated by the operator. Logs remain unmodified and are not committed by this investigation.

The local configuration and profile files were not edited. Identity and limits remain:

- `Takasago Voltage Control`, `rzxa_voltage_demand`, ID `5349994873749504051`,
  `TRION/SourceChannel_A_CONST/Const2`, configured 0..0.5 V.
- `Drive Control` remains `vesc_throttle`; motor neutral cleanup is still attempted.
- Profile `rzxa_36_38_40_ladder`: 23 s, 250 ms updates, configured maximum lateness 1000 ms.

### Observed sequence (UTC, 2026-11-17)

| Time | Evidence |
|---|---|
| 21:21:15.913414 | Last confirmed supply payload `(SCALAR,4.66669069E-1,"V")`; the Python requested value was `0.46666906916652806` |
| 21:21:16.057–16.059 | Pre-write supply read again returns the same valid payload; supply and motor read checks each see error queue zero |
| 21:21:16.058907 | Supply write attempt requests `0.4687519191667282`, serialized as `SCALAR,0.468751919,"V"` |
| 21:21:16.162278 | Immediately following `SYST:ERR?` returns `-200, "Execution error"` in 103.25 ms |
| 21:21:16.162632 | Interrupted snapshot retains the old value and incorrectly still says `readback_verified` (diagnostic defect fixed here) |
| 21:21:16.162766 | Run reports the SCPI exception; playback stops, no failed supply-write retry |
| 21:21:16.267 approximately | Cleanup successfully writes/verifies motor neutral and sees error queue zero |
| 21:21:16.267337 | Supply constant query returns literal `NONE`, not malformed scientific notation |
| 21:21:16.267423 onward | Supply reported unknown; summary `cleanup_unknown`; no supply restoration write |
| 21:22:14.998546 and 21:22:20.388737 | Two explicit later startups: supply attribute listings omit all `TRION/SourceChannel_A_CONST/Const<N>` properties, despite source still reading `(STRING,"CONST2")` |
| 21:23:01.084561 | A subsequent startup lists `Const2` again; a later run completes four cycles before cancellation |

Use the exact original JSONL timestamps for detailed correlation. The last confirmed *numeric
property* value is 0.466669069 V; the longer Python requested value differs only by serialization
rounding. Neither is an independently measured physical output or bus voltage. There is also an
earlier run with -200 observed during motor-neutral cleanup; queue attribution limits apply there too.

## Call path and error attribution

### Follow-up reproduction: held Ramp Enable input

Operator-supplied run `7a0754e822b34d509cff83519a99daaf` was also inspected in the local
logs, together with the supplied OXYGEN screenshot. This makes the reproduction more specific:

- Ramp Enable completes at 21:55:40.800531 UTC, holding demand at 0.5 V; its input remains HIGH.
- The dispatcher returns to ready. The HIGH input is explicitly logged as a baseline, not a
  second trigger. The ladder starts at 21:55:43.476582 from the actual 0.5 V demand.
- It successfully ramps **down** to 0.45 V and holds there. Failure recurs on the second ramp,
  at elapsed 10.2502024 s while rising toward 0.475 V.
- SCPI command 48 attempts `0.468751687` V after confirming approximately `0.466670352` V.
  It sees -200 in the next queue response. Whole failing I/O is 116.27 ms, command/error-query
  duration 109.50 ms and scheduling lateness 0.2024 ms. This is not a timeline timeout.
- The motor is verified neutral during cleanup. The supply payload is again literal `NONE`.
- The screenshot shows Mode `ConstOutput`, source `CONST2`, a blank constant-value field and
  a preview near **0.4688 V**. That rounded preview is closer to the attempted value than the
  last confirmed value (which would round to 0.4667 V). This supports possible partial internal
  application before the error/property loss, but a cached/UI preview is not independent
  physical AOUT or bus measurement.

Leaving the Ramp Enable DOUT HIGH does **not** keep a Python ramp writer alive: the executor
is synchronous, releases ownership at completion and retains only a setpoint. HIGH does not
start it again without a newly observed LOW-to-HIGH transition. The ladder is intended to
replace the held supply demand, not run concurrently with it. The backend does not write
DOUTs, AOUT Mode/source, or supply enable. External OXYGEN-native actions/scripts, a second
process or another SCPI controller are outside that ownership lock and remain unverified.

An offline memory regression now leaves Ramp Enable HIGH throughout a successful ramp/hold
followed by this exact ladder, asserts completion order and proves that all subsequent supply
writes belong to the ladder. It cannot reproduce or exclude an OXYGEN/driver defect.

Two failures near 0.46875 V at the same ladder sample strengthen the case for a repeatable
constant-update/availability problem rather than an arbitrary missed button. They do not yet
separate numeric-value sensitivity from ramp phase, write count, acquisition/setup state or
an external action. A simple upper-range rejection is not supported: 0.5 V had already worked.
No quantization, source toggling or delay workaround is installed based on this correlation.

The final snapshot formerly said `ready` even after `cleanup_unknown`. This diagnostic-only
label is corrected to `stopped_unknown` (or `stopped` after verified cleanup), and interrupted
snapshots say `interrupted`. These labels do not imply OXYGEN's missing property was recovered.

To isolate the operator's held-HIGH hypothesis, a future **independently inhibited, physically
isolated** experiment could return Ramp Enable LOW after `dispatcher_ready`, observe that LOW,
and only then select the unchanged ladder. In this dispatcher LOW alone does not ramp down;
Ramp Disable is a separate profile and would change the initial condition, confounding this
comparison. Inspect any OXYGEN-native actions on the DOUT before testing. Do not repeat this
on an energized setup while supply state is unknown, or interpret a successful LOW-input run
as proof of root cause. The single-write experiment below remains the smaller value/update
availability test. Preserve the OXYGEN project and application/driver diagnostics for vendor
investigation rather than automatically changing Mode/source to make the field reappear.

1. `execute_timeline` uses an absolute monotonic schedule. It evaluates a demand at the actual
   elapsed time, calls the setpoint callback when the demand changes, and verifies at phase
   changes/periodic progress ticks. It skips overdue slots rather than replaying writes in bursts.
2. The dispatcher's callback verifies the owned output and motor-neutral prerequisites, then
   `Dispatcher.command` validates bounds and calls `OxygenBackend.write`.
3. `OxygenBackend.write` validates authorization and bounds again and serializes the pinned
   ID/property as one canonical SCALAR command using nine significant digits.
4. `ScpiTransport.command` holds its transport lock across one socket write and **one**
   `SYST:ERR?`. A nonzero entry raises `ScpiCommandError` (a `ScpiProtocolError` subclass).
   This typed error now retains command, code, message and raw queue payload. No queue drain
   or pre-write error query is added.
5. Only when that returns normally does `Dispatcher.command` read the property back and compare
   it to the request with the existing tolerance. A zero queue entry alone is not readback
   confirmation or proof of a physical voltage.
6. A separate valid property read normally also performs one error-queue check. Invalid property
   type/unit data aborts before that check, as before. On failure playback unwinds and attempts
   motor neutral, then read-only supply verification. Supply demand is never guessed/restored.

The lock prevents another command **on this transport** from interleaving between the write and
its queue check. It cannot establish when OXYGEN enqueued the error, which action generated it,
or whether another client/UI/device task contributed. `SYST:ERR?` is a queue read, not a
per-write transaction acknowledgement, and only one entry is consumed. The immediately prior
zero entries make this write the closest temporal association, but do not prove causality.

A device queue error does not itself desynchronize TCP or automatically invalidate the session;
this permits the existing independent motor-neutral cleanup attempt. In contrast an inline
negative error response to a non-error query, timeout or transport failure closes/invalidates
that stream under existing policies. Cleanup is still attempted but may fail. Neither case
reconnects/replays or suppresses the original fault.

## Most likely explanation and unresolved causes

The strongest explanation is **an OXYGEN-side constant-property availability/lifecycle failure**,
possibly in setup/channel graph updates or the TRION driver/constant setter. Evidence is not just
one `NONE`: the property was absent from two later enumerations roughly a minute later, then
reappeared. This is inconsistent with treating the entire incident as a proven millisecond-only
readback delay. It also does not establish that the property's type actually changed; `NONE`
means no usable value was returned, not that a physical voltage became zero.

Unverified alternatives include UI/setup edits, another controller/client, module/driver state
changes, an OXYGEN defect, and sensitivity to repeated updates. No OXYGEN internal diagnostic log,
firmware trace, UI-change audit or synchronized physical voltage trace is available here, so the
specific root cause and whether the attempted value reached the hardware remain unknown.

The command is within the configured range and discovered -10..10 V span. Earlier values with
the same serialization/scientific-notation readback format succeeded. There is no evidence here
that decimal precision, Python parsing, an out-of-range request or a hard-coded wrong ConstN
caused the failure. No precision change, delay/retry workaround or new constant selection is applied.

## Timing interpretation

The 108.96 ms `io_ms` at elapsed 10.0003 s describes the **previous successful** setpoint/verify
operation, not the failed sample. Progress messages are not emitted for every write, and before
this change failed I/O had no dedicated timeline record. The failed write's queue query itself
lasted about 103 ms. Other successful write/error checks also show roughly 100 ms latency.

A roughly 109 ms serialized operation fits within a 250 ms update interval, and logged active
scheduling lateness was sub-millisecond. The run failed with SCPI -200, not a timeline lateness
exception. No overlapping/burst writes are evidenced on this client. This is not proof that
OXYGEN's internal output updates can sustain 4 Hz in every state, nor does it explain the
roughly 100 ms round trip (server processing, TCP behavior and host scheduling were not isolated).
Changing cadence alone would not explain the persistent missing attribute without further evidence.
The production interval, timing limits and profile are unchanged.

## Focused diagnostic changes

- `OxygenTransport`: per-command IDs and start/completion/failure records, send-completion status,
  command phase, elapsed time and explicit temporal-only queue association. The immediate queue
  query carries the same ID. Successful transaction chatter remains in JSONL, not the console.
- `ScpiTransport`: typed queue error preserving raw entry; raw wire reply retained when a query
  fails header/error validation. No additional queries, draining, retries or reconnects.
- `OxygenBackend`: failures include exact channel/ID/property, canonical command and wire-rounded
  demand. Readback failures preserve the unparsed payload, parsed kind/values representation and
  whether the failure was querying, parsing, type/unit validation, queue checking or finite checking.
- `Dispatcher`: mark a failed write/readback uncertain immediately, including the interrupted
  snapshot. Preserve `last_confirmed_v`, `last_attempted_v`, their times and the latest valid
  `last_readback_v` separately. The legacy `value` remains the last confirmed requested/adopted
  demand for compatibility; it is not updated merely because a socket send returned.
  A later mismatch may update `last_readback_v` without overwriting `last_confirmed_v`.
- Output records/acknowledgements explicitly say physical voltage is not established by setpoint
  readback. Independent measured DC/physical voltage must come from actual instrumentation,
  never from these fields or the gain calculation.
- `execute_timeline`: failed operation event with nominal elapsed time, requested demand, phase,
  setpoint-versus-verify stage, I/O duration, interval and lateness limits; original exception re-raised.

A fresh valid cleanup read can reconfirm the previous demand if it matches. If it is unavailable,
out of range or different, state stays unknown. The last confirmed value is historical evidence,
not permission to resume a profile. Cleanup does not retry the failed supply write.

## Offline regression coverage

`tests/test_scpi_ramp_faults.py` uses a scripted device and mocked socket object; all network
socket creation is forbidden in those tests. The real backend, authorization checks, transport
command/error logic, readback parser, dispatcher and timeline execute with a virtual clock.
Coverage includes:

- Mid-ramp -200 with a missing property, applied write or rejected write; identical queue errors
  do not prove identical device outcomes.
- Mid-ramp `NONE`, STRING, wrong unit, malformed scalar, nonfinite scalar, mismatched and out-of-range
  readback, with response headers on and off.
- Property unavailable before the next write: no new write attempt is falsely attributed.
- Inline negative query response: raw reply retained, stream invalidated, no reconnect.
- Motor-neutral cleanup succeeds when the stream and motor property remain healthy; unknown
  motor/supply states remain explicit when cleanup cannot be verified.
- Exactly one failed supply send, no subsequent supply write or replay, no extra connection.
- Error IDs/queue payloads, failed-I/O timing and unchanged bounds. A socket-send failure must
  not be reported using an old `last_error` entry.

Run all offline tests with:

```powershell
.\.venv-scpi\Scripts\python.exe -m pytest -q
```

## Smallest controlled hardware experiment (proposed, not executed)

Do not start by replaying the full ladder, clearing the error queue blindly, changing source
properties, or automatically retrying the failed write. A prior unknown state must be resolved
by independent inspection and the established external inhibit/shutdown procedure first.

1. Physically isolate the supply demand from an energized power stage (or use an independently
   commissioned safe low-energy load/fixture), inhibit the motor independently, and verify
   emergency stop/protections. Use one SCPI controller and no UI/setup changes. Record the
   exact OXYGEN build, module/driver/firmware, acquisition state and any operator actions.
2. Start with **read-only** snapshots of the pinned channel ID/name, Mode, source, Unit, Range,
   attribute listing and constant payload. Capture one explicit pre-experiment queue entry;
   if nonzero, preserve it and stop rather than attributing it to a new write. If the property
   is missing or non-SCALAR, stop: this already distinguishes availability from ramp scheduling.
3. Only if the property, safe baseline and hardware fixture are verified, issue **one** authorized
   in-range small demand transition near the observed region (for example 0.466666667 to
   0.46875 V only if that transition is safe for the isolated fixture). Preserve the immediate
   error entry and canonical command. This is a new controlled test, not a retry of the old run.
4. Make bounded **read-only** observations immediately and at about 250 ms and 1 s: constant
   reply plus attributes/source, with recorded timing and independently measured AOUT voltage.
   No further output writes, automatic retry or reconnection after an error. Use independent
   physical shutdown if needed; a failed connection cannot guarantee cleanup.
5. A disappearing property after this single sparse write argues against a client deadline/burst
   explanation. Read-only recovery without another write supports transient server availability;
   persistent absence with source still CONST2 supports a setup/driver property-lifecycle fault.
   A queued error before any write exposes an attribution confounder. Success of one isolated
   write does not prove a 4 Hz ramp is safe; any later rate comparison requires a separately
   reviewed low-energy procedure and must vary one factor at a time.

Do not infer which voltage was physically held from -200 or `NONE`; use the independent trace.
No executable hardware experiment, new profile, changed schema or timing workaround is installed.
