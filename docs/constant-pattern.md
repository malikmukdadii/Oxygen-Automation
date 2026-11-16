# One-shot simulated constant-output pattern

For editable profiles and multiple button assignments, use the separate
[configured dispatcher](configured-patterns.md). This page describes the original
fixed 3/4/3-second command and its single-button option.

This command is restricted to the operator-confirmed, genuinely simulated and unconnected
`AO 3/1 Sim`. It does not authorize physical outputs or any other channel.

The profile `profiles/sim_aout_ramp_10s.json` requests one cycle:

| Nominal time | Constant setpoint |
|---|---|
| 0–3 s | Ramp from 0 to 1 V |
| 3–7 s | Hold 1 V |
| 7–10 s | Ramp back to 0 V |

It uses software-timed constant-property updates, not an uploaded or hardware-clocked waveform.
There is no looping or function-generator configuration.

## Offline preview

From the project directory on Windows:

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine aout-pattern
```

Preview never connects or writes outputs. It creates a JSONL plan and a nominal reference CSV
in `logs`. At the default 50 ms interval the CSV contains 201 points. The CSV describes the
requested curve, not measured voltage or the exact timestamps of actual writes.

## Execute on the confirmed simulated channel

Enable the OXYGEN SCPI server on `127.0.0.1:10001` and close competing clients if necessary.
The channel must already have:

- Exact name `AO 3/1 Sim` and current string ID `8898268430164033617`.
- `Mode` equal to `ConstOutput`, `TRION/SourceChannel_A` equal to `CONST0`, and `Unit` equal to `V`.
- A discovered `Range` covering 0–1 V and `TRION/SourceChannel_A_CONST/Const0` initially at 0 V.
- No queued SCPI errors. Error queries consume returned entries; the command never sends `*CLS`.

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine aout-pattern --host 127.0.0.1 --port 10001 --channel "AO 3/1 Sim" --expected-id "8898268430164033617" --execute --confirm-simulated-output --log-file logs\first-10s-ramp.jsonl
```

`--confirm-simulated-output` is a per-run operator confirmation that this channel is genuinely
simulated and unconnected; a `Sim` name alone is not proof. If the ID changes after a setup
reload, discover and verify the current channel rather than guessing an ID.

Do not use `run`, `test-command`, or `--live` for this profile. No global live-control flags
need to be enabled. The dedicated command is independent of `connection.use_mock` and connects
to the specified endpoint only when `--execute` and the required confirmations are supplied.

## Scope and evidence

Preflight checks identity, exact name/ID, discovered attributes, existing mode/source/unit,
range, initial zero and SCPI errors before authorizing writes. It does not change mode or source.
Its only permitted write is a finite 0–1 V typed `SCALAR` update to the selected channel's
`TRION/SourceChannel_A_CONST/Const0` property. Each write is checked for SCPI errors and read back.

The command does not construct the automation engine, operate DOs or Takasago, start/stop
acquisition or recording, configure numeric transfers, or upload a native waveform.
The general adapter's physical-write block remains unchanged.

## Repeat from the simulated DO button

Add `--trigger-do "DO 3/1 Sim"` to keep the dedicated command waiting for button presses:

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine aout-pattern --host 127.0.0.1 --port 10001 --channel "AO 3/1 Sim" --expected-id "8898268430164033617" --execute --confirm-simulated-output --trigger-do "DO 3/1 Sim" --log-file logs\do-triggered-ramp.jsonl
```

- Start with the DO LOW, then click it HIGH to run one complete 3/4/3-second pattern.
- Held HIGH never repeats. Startup HIGH does not run; first return LOW, then press HIGH.
- LOW only rearms; it does not start a pattern or cancel an active pattern.
- DO polling pauses during each pattern. Transitions while busy are ignored, never queued.
  After completion a fresh LOW observation is required, then a later HIGH. If the DO is
  already LOW when polling resumes, that observation rearms it.
- The command reads only the uniquely discovered trigger's `DO_Value`; it never resets
  or writes the DO. Missing/invalid properties or SCPI errors stop the session.
- Default idle polling is 50 ms (`--poll-ms`); edges across gaps or read latency over
  250 ms (`--max-trigger-gap-ms`) are discarded and require a fresh LOW/HIGH sequence.
  Very short pulses can be missed; keep LOW and HIGH long enough to be sampled.
- Idle output context and configured zero are periodically checked, and zero/context
  are checked before each run. Each completed cycle logs `pattern_cycle_complete`
  with duration, lateness and verified configured zero. There are no concurrent cycles.
- `Ctrl+C` ends the waiting session or active cycle using the same cleanup rules below.
  Faults stop the entire session; there is no reconnect or resume.

Without `--execute`, trigger mode still performs only an offline preview.
Global engine/live permissions remain unchanged.

## Timing and validation status

Timing uses monotonic elapsed time and absolute deadlines. Expired update slots are skipped,
not replayed in a burst. Default updates are 50 ms with a 250 ms lateness budget; excessive
scheduler/I/O/completion lateness faults. Timing is best-effort, not a real-time guarantee.

Console output shows phase progress, faults and a final summary. JSONL retains queries,
write attempts, acknowledgements, readbacks and timing. The summary distinguishes nominal
and actual duration, skipped update slots and lateness. Readback verifies the configured
constant only, not physical voltage. The first operator-run one-shot on OXYGEN 8.2 reported
success, 10.1089 s actual duration and verified configured zero. Its roughly 109 ms verified
updates caused 82 nominal 50 ms slots to be skipped, as designed. This is evidence for the
one-shot simulated configuration only; DO-triggered operation still requires an operator trial.

## Stop and failure handling

`Ctrl+C` stops the cycle. After authorization, completion, cancellation or a fault attempts
one verified return to zero on the existing usable connection, provided the mode/source/unit
still match. There is no reconnect, automatic resume or command replay.

An interrupted in-flight transaction closes the connection because its reply is ambiguous.
A lost connection, changed context or failed final readback can prevent verified cleanup.
In that case the summary reports `final_state: unknown` and a fault: inspect the simulated
output in OXYGEN and restore zero manually. Do not interpret a failed command as proof of zero.

Successful cleanup reports `configured_zero_verified`. Exit codes are `0` for success,
`130` for cancellation with verified cleanup, and `1` for faults or unverified cleanup.
