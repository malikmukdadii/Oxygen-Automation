# Regenerative deceleration experiment — commissioning

Offline software for a measurement-driven regenerative deceleration study on the existing
schema-v3 dispatcher. This document separates **sim-only**, **hardware-ready**, and **blocked**.

## Locked AOUT names

| Physical OXYGEN name | Logical role | This milestone |
|---|---|---|
| Drive Control | `vesc_throttle` | Active |
| Takasago Voltage Control | `rzxa_voltage_demand` | Active |
| Braking Control | (unused) | Not commanded |

Do not rename roles or use ADC2 / Matrix Sampler as a substitute for sync acquisition energy.

## CLI

`run-experiment` remains the grid/dynamic entry point. Regen plans are exposed via
`run-regen-experiment`, and `run-experiment` also schema-dispatches `regenerative_deceleration`.

```powershell
# Plan only (no connection)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-regen-experiment --experiment experiments/regen_release_comparison.example.json

# Offline memory execution (simulated energy labeled simulated)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine run-experiment --experiment experiments/regen_release_comparison.example.json --execute --confirm-simulated-output

# Analyze an OXYGEN sync CSV export for one trial window (never whole-file for multi-trial)
.\.venv-scpi\Scripts\python.exe -m oxygen_engine analyze-regen-csv --csv path\to\sync.csv --map dc_power_w=PowerDC --trial-id t1 --window-start 1.0 --window-end 3.0 --output-dir logs/regen-analysis
```

Example experiments (illustrative, `physical_enabled=false`):

- `experiments/regen_release_comparison.example.json`
- `experiments/regen_speed_comparison.example.json`
- `experiments/regen_bus_voltage_comparison.example.json`

## Drive limit commissioning (not automatic)

Effective drive max = lowest applicable authorized bound among:

1. `dispatcher.json` → `outputs.vesc_throttle.max_aout_v`
2. Experiment → `commissioned_drive_limit_v` + `higher_drive_limit_authorized`
3. Per-candidate / per-speed-target `enabled` + `commissioned`

Defaults remain **0.40 V**. Planning candidates 0.55 / 0.70 / 0.85 V stay disabled and
uncommissioned until physical evidence exists. Unauthorized requests are **rejected before a
hardware session** — never silently clamped. Editing a profile alone cannot bypass physical
authorization flags on a live dispatcher.

`sim pass ≠ physical enable.`

## Phases A–F

| Phase | Action |
|---|---|
| A | Establish DC bus (supply demand); motor neutral verified |
| B | Controlled accel of Drive Control to authorized demand |
| C | Starting-condition verification (bus / ERPM tolerances) |
| D | Regen decel: ramp Drive Control to 0 V neutral over configured release duration |
| E | Recovery observation window |
| F | Neutral verify, RZ-XA preserve/hold policy, validity, inter-trial recovery; **no auto-resume after fault** |

Comparisons: release duration, initial speed, bus voltage (36/38/40 V between trials only).

## Energy analysis

Authoritative electrical energy comes from OXYGEN **sync acquisition CSV**, trapezoidal
integration of \(P_{DC}(t)\) with zero-crossing split (energy and regen duration share the same
subinterval math). Sign convention: positive = RZ-XA supplying VESC; negative = toward RZ-XA.
Report **electrical energy-return ratio** (not mechanical efficiency). Offline synthetic series
are always labeled **simulated**.

### Clock domains (do not conflate)

| Domain | Use |
|---|---|
| `host_monotonic` | Control deadlines / `execute_timeline` ramp scheduling |
| `host_wall_utc` | JSONL log timestamps |
| `acquisition` | OXYGEN sync CSV `time_s` |

JSONL `trial_marker` events are **host_monotonic log markers only** — they are not OXYGEN
acquisition markers and must not be used as CSV time windows.

### CSV↔trial association modes

| Mode | Meaning |
|---|---|
| `A_markers` | Bind by acquisition marker column texts |
| `B_acquisition_windows` | Explicit `start_s`/`end_s` in acquisition time |
| `C_verified_affine` | Map host_monotonic→acquisition only when `affine.verified=true` |

Never apply the whole sync CSV to every trial. Never treat host_monotonic as acquisition time
unless Mode C is explicitly verified.

Example:

```powershell
.\.venv-scpi\Scripts\python.exe -m oxygen_engine analyze-regen-csv `
  --csv path\to\sync.csv `
  --map time_s=Time --map dc_power_w=PowerDC --map erpm=ERPM `
  --trial-id t1 --marker-start REGEN_D_START_t1 --marker-end REGEN_E_END_t1 `
  --output-dir logs/regen-analysis
```

## Feedback pinning

Feedback / trigger identities are pinned once at connect / experiment start. Poll loops must
not rediscover channel trees every sample. Mapping changes require an explicit restart.

## Hardware-ready vs blocked

| State | Meaning |
|---|---|
| Sim-only | Memory transport + synthetic or fixture series; examples ship here |
| Hardware-ready (future) | Locked names, commissioned limits, sync CSV mapping, E-stop, RZ-XA sink evidence |
| Blocked now | Live OXYGEN execution from `run-experiment`, ADC2 braking, auto raise to 3.3 V |

Software neutral is **not** an E-stop. Preserve SCPI `-200` diagnostics; no silent retry,
source change, reconnect, or resume after supply-property faults without operator acknowledgement.

## Remaining physical commissioning

1. Confirm ADC1 Current-mode transfer curve near the observed ~1.6 V max-speed region.
2. Raise `max_aout_v` / `commissioned_drive_limit_v` only with measured evidence; keep candidates staged.
3. Verify RZ-XA can accept returned energy at the chosen bus setpoints.
4. Pin and verify sync channels for DC V/I/P and VESC ERPM; export CSV mapping.
5. Independent E-stop / inhibit; fault behavior with link loss.
6. Only then consider enabling a non-illustrative experiment JSON with `physical_enabled` under a reviewed live procedure (not provided by offline tests).
