# Configurable DO / profile / AOUT dispatcher

This page describes the original single-output compatibility workflow. For the new versioned,
network-free dual-role fixture backend, see [dispatcher v2](dispatcher-v2.md). Its implementation
and tests have been written but not executed pending separate authorization. Do not feed the
supplied proposal blueprint or v2 profiles to the unversioned runner.

`dispatch-patterns` is a persistent, serial dispatcher built on the tested isolated
constant-output transport and scheduler. It does not construct the older automation
engine or change its global safety flags.

## Current authorization

Only the operator-confirmed, genuinely simulated and unconnected `AO 3/1 Sim` is supported,
using its discovered string ID and `TRION/SourceChannel_A_CONST/Const0`, in volts.
Values must stay between 0 and 1 V. All assignments share this one output, but can use
different logical output names in a different configuration, provided profiles match.
The configured triggers can be `DO 3/1 Sim` through `DO 3/6 Sim`; they are read-only.
No DO reset, acquisition/recording, mode/source change, native waveform upload or
Takasago operation is permitted. Selecting other AO channels is refused, not enabled by
editing the configuration. Physical output support requires separate verification.

## Start on Windows

From the project directory, create a local configuration copy if you do not already have one:

```cmd
copy dispatcher.example.json dispatcher.json
```

Validate every enabled assignment and profile offline first:

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.json
```

Preview prints the endpoint, output ID, profile paths, nominal durations and peak voltages.
It opens no connection and writes no output. Validation errors stop before connection.
Then, with OXYGEN SCPI enabled and no competing automation client:

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.json --execute --confirm-simulated-output
```

This connects to the endpoint in the bindings file, irrespective of the older engine's
`connection.use_mock`. Do not add `--live` or enable the general engine's flags. Run only
one dispatcher/output controller against this AO. Mode must already be `ConstOutput`,
source `CONST0`, unit `V`, range must cover 0..1 V and the initial configured constant must
be zero. All trigger names/properties and AO identity/properties are checked before any
output write. A queued SCPI error refuses preflight; error reads consume returned entries.
The dispatcher never sends `*CLS`.

## Where to edit

In `dispatcher.json`:

| Field | Meaning |
|---|---|
| `connection` | OXYGEN host, TCP port and per-response timeout |
| `outputs.sim_aout.channel` | Exact approved AO name; currently only `AO 3/1 Sim` |
| `outputs.sim_aout.expected_id` | Verified channel ID as a quoted string, never a JSON number |
| `assignments[].name` | Unique name used in logs |
| `assignments[].enabled` | Boolean; disabled assignments are not loaded or monitored |
| `assignments[].trigger` | Exact DO button name; no duplicate enabled triggers |
| `assignments[].output` | Logical key in `outputs`, such as `sim_aout` |
| `assignments[].profile` | JSON profile path, relative to the bindings file or absolute |
| `timing` | Idle polling, stale-trigger limit, output interval and lateness budget |

The supplied configuration maps:

| Button | Profile file | Pattern |
|---|---|---|
| `DO 3/1 Sim` | `profiles/sim_aout_ramp_10s.json` | 0 to 1 V: 3 s up, 4 s hold, 3 s down |
| `DO 3/2 Sim` | `profiles/sim_aout_gentle_6s.json` | 0 to 0.5 V: 2 s up, 2 s hold, 2 s down |
| `DO 3/3 Sim` | `profiles/sim_aout_double_8s.json` | Two 0.75 V pulses with 1 s ramps/holds |

Change an assignment's `profile` to choose another pattern, or `trigger` to move its button.
Keep at least one enabled assignment. Up to six unique triggers and one logical output
are supported in this version. Adding several assignments does not permit concurrent output.
The bindings file is separate from the older engine's `config.json`, `do_commands` and
`aout_roles`; those settings are not silently mixed in.

## Create a pattern

Copy one of the new profile files to another filename in `profiles`, give it a unique
`name`, edit its steps and point an assignment's `profile` to it. The steps use the existing
profile schema:

- Begin with `set_aout`, `channel: "sim_aout"`, `value: 0`.
- Add `ramp_aout` steps with `channel`, `target` in 0..1 V and positive `duration_s`.
- Add `hold` steps with `duration_s`; they retain the preceding target.
- Ramp explicitly back to zero, then finish with `return_neutral`, `channel: "sim_aout"`.
- Every output step must use the logical key selected by the assignment, not a physical
  OXYGEN channel name. The binding resolves the physical output.
- Each ramp/hold must be 0.1..300 seconds; total duration must be at most 300 seconds.
  At most 100 ramp/hold steps are accepted. Very short steps may be poorly resolved by
  software timing and SCPI latency; start with ramps of at least one second.
- `rate_per_s` can be omitted: target and duration define the ramp. If supplied, it must
  agree with the absolute voltage change divided by duration. Remove or update it when
  changing the original 10-second profile.
- `timeout_s` must be finite and at least the sum of all durations. Initial/final zero
  steps take no nominal timeline time; preflight and cleanup are outside nominal duration.
- Recording, expected-condition checks, instrument operations, markers, native waveforms,
  mid-pattern instantaneous set steps and other step types are refused.

Stop with `Ctrl+C`, inspect the final state, edit, validate offline, then restart. Configuration
and profiles are loaded once at startup: no hot reload or partially applied edits.
The new flexible profiles are for `dispatch-patterns`; `aout-pattern` intentionally retains
its original fixed 3/4/3-second validator.

## Button and concurrency rules

1. Startup HIGH never starts a pattern. Observe LOW, then a later HIGH.
2. A valid rising edge runs the assigned pattern once. LOW only rearms; it never starts
   or cancels an active pattern. Holding HIGH cannot repeat.
3. DO polling pauses while a pattern runs. All busy-time transitions are ignored, never
   queued. After completion all buttons resynchronize; LOW must be observed again before
   another HIGH. A button already LOW at that point can rearm immediately.
4. If multiple rising edges are seen in the same scan, the first assignment in configuration
   order wins; the others are logged as ignored. No automatic replay follows.
5. Edges across stale observations are discarded. Defaults are 50 ms idle polling and a
   250 ms maximum gap/read latency. Multiple buttons and slow responses increase actual
   scan time. Keep both LOW and HIGH long enough to be sampled; very short pulses can be missed.
6. Idle configured zero and mode/source/unit are checked periodically, and zero/context
   are checked before and after each cycle. No automatic button reset is sent.

## Timing, stop and faults

Software timing uses monotonic absolute deadlines, interpolates from elapsed time and
skips expired updates rather than stretching the profile or replaying a burst. Defaults:
50 ms update interval, 250 ms lateness budget. SCPI writes are error-checked and read back.
This is not hardware-clocked waveform generation. The first operator-run one-shot took
10.1089 seconds with roughly 109 ms verified updates, so skipped nominal 50 ms slots are
expected on that installation. Readbacks establish configuration, not physical voltage.

`Ctrl+C` stops the dispatcher. Completion, cancellation or faults attempt verified zero
on the existing usable connection and matching context. There is no reconnect, automatic
resume or output-command replay. An interrupted in-flight transaction closes its ambiguous
connection. Disconnect, changed context or failed readback may prevent cleanup; final state
then reports `unknown`, and the simulated output must be inspected/restored manually.
See [the constant-pattern cleanup procedure](constant-pattern.md) for details.

Exit code `130` means cancelled with no cleanup fault; `1` means fault/refusal, including
unverified cleanup. Offline validation returns `0`. A persistent execution ends only on
cancellation or fault. After a fault inspect output state, restore zero if necessary, fix
the cause and restart deliberately. Do not automatically resume a half-finished pattern.

## Long-running use and logs

The process can wait indefinitely between presses, but it has not undergone a 24-hour soak
trial. This is not yet a qualified unattended Windows service: keep the terminal/session
running, prevent sleep as appropriate and supervise initial trials. Faults intentionally exit;
there is no service installer, auto-restart or reconnect loop in this version.

Default log: `logs/pattern-dispatcher.jsonl`, or select `--log-file`. It appends JSONL and
rotates at approximately 10 MiB, retaining five backups plus the active file (about 60 MiB
total). Rotation overwrites the oldest evidence, so copy logs elsewhere if full history
is required. Each process invocation has a unique `run_id`; cycle events identify the
assignment/profile. Raw queries, readbacks and idle samples stay in logs; console output
shows transitions, cycle progress, faults and summaries without printing every idle sample.
Log I/O failure stops execution and disables the failed sink so it cannot block the
existing-connection zero cleanup attempt. In that case logs may be incomplete: inspect
the output rather than assuming that a missing final summary establishes zero.

A normal cycle ends with `pattern_cycle_complete` and `configured_zero_verified`.
Shutdown logs both the shared `pattern_summary` and a `dispatcher_summary` with started
and completed cycle counts. A faulted cycle is not counted as complete.
