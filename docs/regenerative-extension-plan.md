# Future coordinated regenerative demo: design only

## Scope

The two-output supply/drive sequencing milestone is now implemented separately by
`run-experiment`; see `efficiency-experiments.md`. It does not activate ADC2/Braking Control.
The three-output drive/brake coordination described below remains design-only.

This is a commissioning/design checklist, not an executable profile or implemented capability.
Preserve schema-3 `dispatcher.json`, existing per-output profiles and the working open-loop
engine. Do not add a competing dispatcher/backend or repurpose drive neutral as a brake command.
The current executor owns one output per profile; it cannot safely express coordinated
supply/drive/brake operation today.

Operator-provided hardware context: FLIPSKY 75100 VESC, ADC1/ADC2 physically wired, CAN
telemetry already decoded in OXYGEN. Offline code cannot verify VESC firmware, ADC app settings,
input polarity, brake semantics, electrical limits or regenerative energy acceptance.

| Physical OXYGEN name | Terminal | Logical role | Current state |
|---|---|---|---|
| Drive Control | AO2/1 | `vesc_throttle` | Active; keep exact name/role |
| Braking Control | AO2/2 | Future `vesc_brake` | Not authorized or commanded by current engine |
| Takasago Voltage Control | AO2/3 | `rzxa_voltage_demand` | Active; keep exact name/role |

## Required commissioning evidence before implementation

- Verify the exact ADC1/ADC2 app configuration, input transfer curves, deadbands, neutral and
  fault levels using this specific controller/firmware. Do not assume ADC2 automatically
  means regenerative braking or that zero volts is a verified brake-neutral command.
- Establish drive/brake mutual exclusion, transition ordering and any required deadtime.
  Verify behavior when both ADC inputs are non-neutral, an input is disconnected, CAN is
  lost, or the host process/SCPI connection fails. Never discover this through an energized demo.
- Verify phase current, battery/DC current, regenerative current, speed and thermal limits
  independently of Python. Confirm the DC source/sink's actual regenerative capability,
  operating voltage/current/power envelope, energy capacity and overvoltage behavior.
  A voltage-demand input does not establish that the RZ-XA installation can absorb energy.
- Verify an independent emergency stop/inhibit and safe fault sequence. Neither a neutral
  AOUT command nor SCPI acknowledgement proves deenergization. Decide whether immediate
  brake release or maintained braking is the safe response under each fault; do not assume.
- Verify signed, synchronized DC voltage/current power measurement at the relevant energy
  boundary. If claiming energy returned to a supply/sink, measure that boundary as well.
  Account for capacitor energy changes and measurement offsets; speed reduction is not proof
  of regenerative energy recovery. Confirm CAN speed units and scaling.

## Minimal extension within the existing engine

Implement only after requirements and commissioning limits are agreed:

1. Add the reviewed `vesc_brake` role to the existing schema, discovery, authorization and
   backend capability allowlist with its own verified neutral/range and exact physical channel.
   Reject identity aliasing across all three outputs. Do not rename the two existing roles or
   change their existing limits. Existing single-output V3 profiles must remain valid.
2. Add an explicitly selected coordinated sequence capability to the same dispatcher and
   schema, subject to a reviewed backward-compatible schema evolution. Use one ownership
   lock for every output participating in that sequence; no independent competing triggers.
   Keep the existing ramp/hold/checkpoint/assert/neutral semantics rather than introducing a
   second file format. Define how existing single-role profiles are composed before coding.
3. Use a single shared monotonic schedule with per-output rate/clamp/readback validation,
   deterministic drive-to-neutral-before-brake transitions, and completion rules for all roles.
   SCPI writes are serialized, not atomic or hard-real-time: specify and bound permissible
   skew, lateness and partial-update fault handling. Prevent overlapping drive and brake.
4. Add explicit safety interlocks for this coordinated mode: verified external readiness,
   brake/drive state, bus overvoltage and regenerative sink capability. Determine which must
   be enforced by independent hardware/controller and which may be monitored by software.
   Any added live protection needs fresh, independently measured signals and tested fault
   behavior, not inferred AOUT values or fabricated telemetry. These would be safety gates,
   not necessarily closed-loop waveform regulation. Do not weaken existing physical acknowledgements.
5. Define startup and termination for all three roles. Verify drive/brake safe states before
   granting writes. Preserve the existing supply-demand persistence policy unless a separately
   reviewed shutdown plan specifies otherwise. On partial failure, inhibit new profiles, avoid
   reconnect/replay, and report unknown states. Cleanup cannot guarantee writes after link loss.
6. Extend the existing memory backend and loopback mock tests for three independent identities,
   mutual exclusion, bounded write skew, loss of bus/sink readiness, partial writes, interrupts,
   neutral/readback failure and energy sign/reporting. Validate first without hardware and then
   follow a separately approved low-energy commissioning procedure.

## Staged future experiment

Use one explicitly coordinated run, not simultaneous DOUT presses:

- Establish and verify the commissioned supply operating point with brake and drive safe.
- Ramp drive demand and hold a measured speed/load condition.
- Ramp drive to its verified neutral; verify the transition before applying brake demand.
- Apply a bounded, commissioned brake-demand ramp/hold and measure returned DC energy.
- Release braking according to its verified safe sequence and verify completion states.
- Only after the baseline is commissioned, introduce small bounded bus-demand disturbances
  during a permitted phase. Never lower bus demand against active regeneration without an
  independently verified sink/overvoltage strategy and reviewed test limits.

No numerical brake voltages, regenerative current limits or bus-disturbance magnitudes are
specified here because they have not been commissioned. A future DOUT label should be proposed
to the operator before any rename. The present DOUTs and local assignments remain unchanged.
