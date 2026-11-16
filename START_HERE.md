# Quick start and copy/paste commands

## Scope of this build

The current executable dual-AOUT path is the OXYGEN DEMO backend, not a physical-hardware
backend. It requires both selected outputs to be genuinely simulated and unconnected.
It uses explicitly FAKE 40 V bus feedback and fake commissioning/regen confirmations.
Simulation success does not turn these fixtures into real measurements or enable hardware.
There is no physical motor/RZ-XA startup command in this build. Do not use the simulation
confirmation flags on connected physical outputs, rename physical channels to match the demo,
or enable hardware by changing config flags. A commissioned physical backend and real independent
feedback are still required. No RZ-XA output enable/disable or real RZ-XA SCPI is implemented.

## Moving the project to another PC

- Extract the project to a writable folder.
- Open Command Prompt in the folder containing requirements.txt and the oxygen_engine directory.
- Install Python 3.11 or newer if needed.
- Do not rely on a ZIP copy of .venv or .venv-scpi: virtual environments are not portable.
  Omit those folders from the ZIP and recreate .venv-scpi on the destination PC.
- No installation commands need repeating for normal restarts on a PC with a working environment.
- The demo connection is 127.0.0.1:10001 in dispatcher.v2.oxygen-demo.json: OXYGEN must run
  locally on that PC unless this endpoint is deliberately changed.

Create the environment once:

```cmd
py -3 -m venv .venv-scpi
```

Install dependencies once:

```cmd
.\.venv-scpi\Scripts\python.exe -m pip install -r requirements.txt
```

## Normal OXYGEN demo startup

1. Open OXYGEN and load the simulated demo setup; enable TCP SCPI on port 10001.
2. Stop other automation/output clients. Run only one output controller at a time.
3. Confirm AO 3/1 Sim AND AO 3/2 Sim are genuinely simulated and unconnected.
4. Both AOs must already be in ConstOutput, unit V, with a covering range and an existing
   constant source. Motor AO 3/2 Sim must initially be at 0 V. Start assigned DO buttons LOW.
5. Display both AO channels in OXYGEN's chart. Start polling with the command below.

Start interactive DEMO polling (not physical hardware):

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.oxygen-demo.json --execute --confirm-simulated-output --confirm-demo-output "AO 3/1 Sim" --confirm-demo-output "AO 3/2 Sim"
```

Wait for preflight_passed and dispatcher_ready. Current channel IDs are discovered by exact
unique name at every launch; there is no need to copy IDs between OXYGEN instances. They stay
pinned within the session. A mapping change or other preflight error stops execution; it does
not justify bypassing the checks.

## Button map

| OXYGEN button | Action | Nominal duration |
|---|---|---|
| DO 3/1 Sim | Supply AO 3/1: 0 to 0.500 V in 5 s, hold 3 s, then persist 0.500 V | 8 s, then hold |
| DO 3/2 Sim | Supply AO 3/1: ramp current demand to 0 V, with motor-neutral/regen-safe checks | 5 s |
| DO 3/3 Sim | Supply-only 0.450/0.500/0.450/0.500 V characterization; finish holding 0.500 V | 20 s, then hold |
| DO 3/4 Sim | Motor AO 3/2: 0 to 0.250 V in 3 s, hold 3 s, down in 3 s | 9 s |
| DO 3/5 Sim | Motor AO 3/2: 0.200 V and 0.350 V pulses, returning to zero | 14 s |
| DO 3/6 Sim | Motor AO 3/2: 0.200, 0.400, 0.250 V levels, then zero | 16 s |

Suggested demo sequence: DO 3/1, wait for completion, DO 3/4 (or 3/5 or 3/6), wait,
then DO 3/2 for explicit supply-demand ramp down. Motor patterns leave supply demand unchanged.
All voltages above are AO demand voltages, not actual measured DC-bus voltage.

LOW then HIGH triggers once. Held/startup HIGH does not repeat. Return LOW between presses.
Busy-time presses are ignored and not queued. There is no DO polling/cancel button during a run.

## Stop and restart

Press Ctrl+C in the terminal to stop. Cleanup attempts motor neutral, but intentionally
PRESERVES supply demand. Use DO 3/2 after a completed pattern to explicitly ramp supply demand
down before stopping if that is the intended demo state. It is not power-off or an enable toggle.

A disconnect or failed readback may prevent verified cleanup. Inspect the OXYGEN simulated
outputs if final state is unknown. A crash or killed process cannot guarantee output restoration.
On restart, supply state is read back rather than automatically zeroed; the supply-up profile
is a no-op if already at 0.500 V and refuses other nonzero starting values.

## Editing and logs

- Button assignments/output definitions: dispatcher.v2.oxygen-demo.json.
- Pattern definitions: profiles/*.json, using the six supplied v2 profiles as templates.
- Supply clamp: 0..0.500 V. Motor clamp: 0..0.400 V. Other physical outputs are not enabled.
- Stop, edit, then restart to apply changes; there is no hot reload.
- Log: logs/dispatcher-v2-oxygen-demo.jsonl; rotates at about 10 MiB with five backups.
- Full details: docs/oxygen-demo.md and docs/dispatcher-v2.md.

Optional offline validation of the demo configuration (not needed before every launch):

```cmd
.\.venv-scpi\Scripts\python.exe -m oxygen_engine dispatch-patterns --bindings dispatcher.v2.oxygen-demo.json
```
