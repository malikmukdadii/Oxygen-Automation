"""Regenerative deceleration experiment runner on top of the V3 dispatcher write path."""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .constant_pattern import execute_timeline
from .dispatcher import MOTOR, SUPPLY, Dispatcher, HeldPattern
from .drive_limits import (
    DEFAULT_COMMISSIONED_DRIVE_LIMIT_V,
    PLANNING_CANDIDATES_V,
    resolve_effective_drive_max,
    validate_drive_limit_candidates,
)
from .energy_analysis import (
    EnergyMetrics,
    aggregate_trials,
    compute_metrics,
    synthetic_regen_series,
    write_comparison_csv,
)
from .errors import AuthorizationError, ConfigError, ControlError, InterlockError
from .memory_backend import MemoryBackend
from .pattern_segments import Segment, StrictRotatingHandler
from .schema import ABSOLUTE_AOUT_MAX_V, load_dispatcher_config, number, read_json

# Explicit clock domains for regen experiments (never conflate these):
# - host_monotonic: control deadlines / execute_timeline (dispatcher.clock)
# - host_wall_utc: JSONL log timestamps
# - acquisition: OXYGEN sync CSV time_s
CLOCK_DOMAINS = ('host_monotonic', 'host_wall_utc', 'acquisition')
# CSV↔trial association modes:
# A = acquisition markers in sync CSV
# B = explicit acquisition-time windows
# C = verified affine map host_monotonic → acquisition (only when verified=true)
ASSOCIATION_MODES = ('A_markers', 'B_acquisition_windows', 'C_verified_affine')


LOCKED_AOUT_NAMES = {
    MOTOR: 'Drive Control',
    SUPPLY: 'Takasago Voltage Control',
}
BRAKING_CONTROL_NAME = 'Braking Control'
EXPERIMENT_TYPE = 'regenerative_deceleration'


@dataclass
class TrialResult:
    trial_id: str
    comparison_axis: str
    release_duration_s: float
    bus_voltage_v: float
    drive_demand_v: float
    target_erpm: float | None
    valid: bool
    invalid_reason: str | None
    phase: str
    metrics: dict | None
    simulated: bool
    events: list[dict] = field(default_factory=list)
    csv_window_start_s: float | None = None
    csv_window_end_s: float | None = None
    marker_start: str | None = None
    marker_end: str | None = None
    feedback_mode: str = 'simulated'

    def as_dict(self) -> dict:
        return asdict(self)


def _keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ConfigError(f'{label}: unknown keys or not an object; allowed: {sorted(allowed)}')


def load_experiment_config(path) -> dict:
    path = Path(path).resolve()
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ConfigError('Experiment config must be a JSON object')
    root = {
        'schema_version', 'experiment_type', 'name', 'description', 'simulation_only',
        'physical_enabled', 'dispatcher_bindings', 'commissioned_drive_limit_v',
        'higher_drive_limit_authorized', 'drive_limit_candidates', 'comparison',
        'bus_setpoints_v', 'speed_targets_erpm', 'release_durations_s', 'drive_demand_v',
        'accel', 'settle_s', 'recovery_observe_s', 'inter_trial_recovery_s', 'repetitions',
        'start_condition', 'energy', 'rzxa_completion_policy', 'output_dir',
        'illustrative_only', 'feedback_mode',
    }
    _keys(raw, root, 'experiment')
    if raw.get('schema_version') != 1:
        raise ConfigError('experiment schema_version must be 1')
    if raw.get('experiment_type') != EXPERIMENT_TYPE:
        raise ConfigError(f'experiment_type must be {EXPERIMENT_TYPE}')
    if type(raw.get('simulation_only')) is not bool or type(raw.get('physical_enabled')) is not bool:
        raise ConfigError('simulation_only and physical_enabled must be Boolean')
    feedback_mode = raw.get('feedback_mode', 'simulated')
    if feedback_mode not in {'simulated', 'measured'}:
        raise ConfigError("feedback_mode must be 'simulated' or 'measured'")
    raw['feedback_mode'] = feedback_mode
    if raw['physical_enabled'] is True:
        raise ConfigError(
            'Live physical regen execution remains disabled until verified commissioning; '
            'set physical_enabled=false (sim pass ≠ physical enable)')
    if raw['physical_enabled'] is True and raw['simulation_only'] is True:
        raise ConfigError('physical_enabled cannot be true while simulation_only is true')
    if raw.get('illustrative_only') is True and raw['physical_enabled'] is True:
        raise ConfigError('illustrative_only examples cannot enable physical execution')
    if feedback_mode == 'measured' and raw['simulation_only']:
        raise ConfigError('measured feedback_mode is incompatible with simulation_only')
    if feedback_mode == 'simulated' and not raw['simulation_only']:
        raise ConfigError('simulated feedback_mode requires simulation_only=true')

    number(raw.get('commissioned_drive_limit_v', DEFAULT_COMMISSIONED_DRIVE_LIMIT_V),
           'commissioned_drive_limit_v', 0.001, ABSOLUTE_AOUT_MAX_V)
    if type(raw.get('higher_drive_limit_authorized', False)) is not bool:
        raise ConfigError('higher_drive_limit_authorized must be Boolean')
    raw['drive_limit_candidates'] = list(validate_drive_limit_candidates(
        raw.get('drive_limit_candidates')))

    comparison = raw.get('comparison')
    if comparison not in {'release_duration', 'initial_speed', 'bus_voltage'}:
        raise ConfigError('comparison must be release_duration, initial_speed, or bus_voltage')

    buses = raw.get('bus_setpoints_v', [36.0])
    if not isinstance(buses, list) or not buses:
        raise ConfigError('bus_setpoints_v must be a nonempty list')
    raw['bus_setpoints_v'] = [number(v, 'bus setpoint', 1, 60) for v in buses]

    releases = raw.get('release_durations_s')
    if not isinstance(releases, list) or not releases:
        raise ConfigError('release_durations_s must be a nonempty list')
    raw['release_durations_s'] = [number(v, 'release duration', 0.0, 120) for v in releases]
    # Reject invalid timing that ignores ~100 ms+ SCPI latency for nonzero releases.
    for duration in raw['release_durations_s']:
        if 0 < duration < 0.05:
            raise ConfigError(
                f'release_duration {duration} s is below 50 ms planning floor '
                '(SCPI latency typically >= 100 ms; reject invalid timing)')

    drive = number(raw.get('drive_demand_v'), 'drive_demand_v', 0.0, ABSOLUTE_AOUT_MAX_V)
    raw['drive_demand_v'] = drive

    speeds = raw.get('speed_targets_erpm', [])
    if not isinstance(speeds, list):
        raise ConfigError('speed_targets_erpm must be a list')
    parsed_speeds = []
    for index, entry in enumerate(speeds):
        if isinstance(entry, (int, float)) and not isinstance(entry, bool):
            parsed_speeds.append({
                'target_erpm': number(entry, 'target_erpm', 0, 200000),
                'enabled': True,
                'commissioned': True,
                'max_speed_authorized': False,
            })
            continue
        _keys(entry, {'target_erpm', 'enabled', 'commissioned', 'max_speed_authorized'},
              f'speed_targets_erpm[{index}]')
        target = number(entry['target_erpm'], 'target_erpm', 0, 200000)
        if type(entry.get('enabled')) is not bool or type(entry.get('commissioned')) is not bool:
            raise ConfigError('speed target enabled/commissioned must be Boolean')
        if type(entry.get('max_speed_authorized', False)) is not bool:
            raise ConfigError('max_speed_authorized must be Boolean')
        if entry['enabled'] and not entry['commissioned']:
            raise ConfigError('enabled speed targets must be commissioned')
        parsed_speeds.append({
            'target_erpm': target,
            'enabled': entry['enabled'],
            'commissioned': entry['commissioned'],
            'max_speed_authorized': entry.get('max_speed_authorized', False),
        })
    if comparison == 'initial_speed' and not parsed_speeds:
        raise ConfigError('initial_speed comparison requires speed_targets_erpm')
    raw['speed_targets_erpm'] = parsed_speeds

    accel = raw.get('accel', {})
    _keys(accel, {'ramp_s', 'hold_s'}, 'accel')
    raw['accel'] = {
        'ramp_s': number(accel.get('ramp_s', 2.0), 'accel.ramp_s', 0.05, 120),
        'hold_s': number(accel.get('hold_s', 2.0), 'accel.hold_s', 0.0, 120),
    }
    raw['settle_s'] = number(raw.get('settle_s', 1.0), 'settle_s', 0.0, 120)
    raw['recovery_observe_s'] = number(raw.get('recovery_observe_s', 3.0), 'recovery_observe_s', 0.0, 300)
    raw['inter_trial_recovery_s'] = number(
        raw.get('inter_trial_recovery_s', 2.0), 'inter_trial_recovery_s', 0.0, 600)
    reps = raw.get('repetitions', 1)
    if type(reps) is not int or reps < 1 or reps > 50:
        raise ConfigError('repetitions must be an integer in [1, 50]')
    raw['repetitions'] = reps

    start = raw.get('start_condition', {})
    _keys(start, {'erpm_tolerance', 'bus_tolerance_v', 'require_measured_erpm'}, 'start_condition')
    raw['start_condition'] = {
        'erpm_tolerance': number(start.get('erpm_tolerance', 500), 'erpm_tolerance', 1, 50000),
        'bus_tolerance_v': number(start.get('bus_tolerance_v', 1.0), 'bus_tolerance_v', 0.01, 10),
        'require_measured_erpm': bool(start.get('require_measured_erpm', False)),
    }

    energy = raw.get('energy', {})
    _keys(energy, {
        'channel_map', 'use_synthetic_when_no_csv', 'csv_path', 'trial_windows',
        'polarity', 'time_unit', 'marker_column', 'association_mode', 'affine',
    }, 'energy')
    trial_windows = energy.get('trial_windows') or {}
    if trial_windows and not isinstance(trial_windows, dict):
        raise ConfigError('energy.trial_windows must be an object keyed by trial_id')
    for key, window in trial_windows.items():
        _keys(window, {
            'start_s', 'end_s', 'marker_start', 'marker_end', 'timebase',
        }, f'trial_windows[{key}]')
        if window.get('timebase', 'acquisition') != 'acquisition':
            raise ConfigError(
                f'trial_windows[{key}]: timebase must be acquisition '
                '(host_monotonic windows are not acquisition times)')
        if 'start_s' in window or 'end_s' in window:
            number(window['start_s'], 'window start', -1e9, 1e9)
            number(window['end_s'], 'window end', -1e9, 1e9)
            if window['end_s'] <= window['start_s']:
                raise ConfigError(f'trial_windows[{key}]: end_s must exceed start_s')
    association_mode = energy.get('association_mode')
    if association_mode is not None and association_mode not in ASSOCIATION_MODES:
        raise ConfigError(f'association_mode must be one of {ASSOCIATION_MODES}')
    affine = energy.get('affine')
    if affine is not None:
        _keys(affine, {'slope', 'offset_s', 'verified'}, 'energy.affine')
        number(affine.get('slope'), 'affine.slope', 1e-9, 1e9)
        number(affine.get('offset_s'), 'affine.offset_s', -1e9, 1e9)
        if type(affine.get('verified')) is not bool:
            raise ConfigError('energy.affine.verified must be Boolean')
        if association_mode == 'C_verified_affine' and affine.get('verified') is not True:
            raise ConfigError('Mode C requires energy.affine.verified=true')
    if energy.get('csv_path') and association_mode is None:
        raise ConfigError(
            'csv_path requires association_mode '
            '(A_markers | B_acquisition_windows | C_verified_affine)')
    raw['energy'] = {
        'channel_map': energy.get('channel_map') or {},
        'use_synthetic_when_no_csv': bool(energy.get('use_synthetic_when_no_csv', True)),
        'csv_path': energy.get('csv_path'),
        'trial_windows': trial_windows,
        'polarity': energy.get('polarity', 'rzxa_supply_positive'),
        'time_unit': energy.get('time_unit', 's'),
        'marker_column': energy.get('marker_column', 'marker'),
        'association_mode': association_mode,
        'affine': affine,
    }
    if raw['energy']['csv_path'] is not None and not isinstance(raw['energy']['csv_path'], str):
        raise ConfigError('energy.csv_path must be a string path when provided')

    policy = raw.get('rzxa_completion_policy', 'preserve_demand')
    if policy not in {'preserve_demand', 'hold_final'}:
        raise ConfigError('rzxa_completion_policy must preserve_demand/hold_final (no auto bus collapse)')
    raw['rzxa_completion_policy'] = policy
    raw['_path'] = str(path)
    return raw


def plan_matrix(experiment: dict) -> list[dict]:
    """Expand comparison axes into trial plans; disabled candidates stay visible but skipped."""
    plans = []
    comparison = experiment['comparison']
    buses = experiment['bus_setpoints_v']
    releases = experiment['release_durations_s']
    speeds = experiment['speed_targets_erpm'] or [{
        'target_erpm': None, 'enabled': True, 'commissioned': True, 'max_speed_authorized': False,
    }]

    if comparison == 'release_duration':
        if len(buses) != 1:
            raise ConfigError('release_duration comparison keeps bus setpoint constant (one value)')
        bus = buses[0]
        speed = speeds[0]
        for release in releases:
            plans.append({
                'comparison_axis': comparison,
                'bus_voltage_v': bus,
                'release_duration_s': release,
                'target_erpm': speed.get('target_erpm'),
                'speed_enabled': speed.get('enabled', True),
                'speed_commissioned': speed.get('commissioned', True),
            })
    elif comparison == 'initial_speed':
        if len(buses) != 1 or len(releases) != 1:
            raise ConfigError('initial_speed comparison keeps bus and release constant')
        for speed in speeds:
            plans.append({
                'comparison_axis': comparison,
                'bus_voltage_v': buses[0],
                'release_duration_s': releases[0],
                'target_erpm': speed['target_erpm'],
                'speed_enabled': speed['enabled'],
                'speed_commissioned': speed['commissioned'],
                'max_speed_authorized': speed.get('max_speed_authorized', False),
            })
    else:  # bus_voltage
        if len(releases) != 1:
            raise ConfigError('bus_voltage comparison keeps release duration constant')
        speed = speeds[0]
        for bus in buses:
            plans.append({
                'comparison_axis': comparison,
                'bus_voltage_v': bus,
                'release_duration_s': releases[0],
                'target_erpm': speed.get('target_erpm'),
                'speed_enabled': speed.get('enabled', True),
                'speed_commissioned': speed.get('commissioned', True),
            })

    expanded = []
    for plan in plans:
        for rep in range(experiment['repetitions']):
            expanded.append({**plan, 'repetition': rep + 1})
    return expanded


def bus_demand_from_voltage(bus_v: float, gain: float) -> float:
    return number(bus_v / gain, 'bus demand', 0.0, ABSOLUTE_AOUT_MAX_V)


class RegenExperimentRunner:
    """Phases A–F using Dispatcher.command / verify / interlocks; no new SCPI backend."""

    def __init__(self, experiment: dict, dispatcher: Dispatcher, emit, *,
                 feedback_provider=None):
        self.experiment = experiment
        self.dispatcher = dispatcher
        self.emit = emit
        self.feedback_provider = feedback_provider
        self.faulted = False
        self.results: list[TrialResult] = []
        self._feedback_pinned = False
        self._feedback_ids: dict[str, str] = {}
        self._active_target_erpm: float | None = None
        self._active_trial_window: dict | None = None
        self.feedback_mode = experiment.get('feedback_mode', 'simulated')

    def pin_feedback_identities(self) -> dict[str, str]:
        """Pin optional experiment feedback names once; never rediscover every poll."""
        if self._feedback_pinned:
            return dict(self._feedback_ids)
        # Logical identities only in this milestone; physical channel IDs come from dispatcher session.
        identities = {
            'drive_control': LOCKED_AOUT_NAMES[MOTOR],
            'takasago_voltage_control': LOCKED_AOUT_NAMES[SUPPLY],
            'braking_control_unused': BRAKING_CONTROL_NAME,
            'erpm_feedback': 'VESC ERPM',
            'dc_bus_voltage': 'DC Bus Voltage',
            'dc_bus_current': 'DC Bus Current',
        }
        self._feedback_ids = identities
        self._feedback_pinned = True
        self.emit({'event': 'feedback_identities_pinned', 'identities': identities,
                   'rediscover_every_poll': False,
                   'feedback_mode': self.feedback_mode})
        return dict(identities)

    def acquire_lease(self):
        if not self.dispatcher.prepared:
            raise ControlError('Dispatcher must be prepared before experiment lease')
        if self.dispatcher.active_profile is not None:
            raise ControlError('Endpoint lease held by another profile/experiment')
        self.dispatcher.active_profile = self.experiment['name']
        self.dispatcher.active_role = None
        self.emit({'event': 'experiment_lease_acquired', 'experiment': self.experiment['name'],
                   'outputs': [SUPPLY, MOTOR], 'braking_control': 'unused'})

    def release_lease(self):
        self.dispatcher.active_profile = None
        self.dispatcher.active_role = None
        self.emit({'event': 'experiment_lease_released', 'experiment': self.experiment['name']})

    def _control_resolution_s(self) -> float:
        return max(self.dispatcher.config.raw['timing']['interval_ms'] / 1000.0, 0.02)

    def _ramp(self, role: str, target: float, duration_s: float, phase: str):
        """Absolute-deadline ramp via V3 execute_timeline (wait-then-command; no burst replay)."""
        current = self.dispatcher.verify(role)
        resolution = self._control_resolution_s()
        max_lateness_s = self.dispatcher.config.raw['timing']['max_lateness_ms'] / 1000.0
        if duration_s < 0:
            raise ConfigError(f'Ramp duration must be >= 0 (phase={phase})')
        if math.isclose(current, target, abs_tol=1e-9):
            cmd_at = self.dispatcher.clock()
            self.dispatcher.command(role, target)
            ack_at = self.dispatcher.clock()
            self.emit({
                'event': 'experiment_setpoint', 'phase': phase, 'output': role,
                'nominal_v': target, 'acknowledged_v': target,
                'cmd_monotonic_s': cmd_at, 'ack_monotonic_s': ack_at,
                'clock_domain': 'host_monotonic',
                'precise_ramp': False, 'note': 'already at target; single verify/write only',
            })
            return
        if duration_s == 0:
            cmd_at = self.dispatcher.clock()
            self.dispatcher.command(role, target)
            ack_at = self.dispatcher.clock()
            self.emit({
                'event': 'experiment_setpoint', 'phase': phase, 'output': role,
                'nominal_v': target, 'acknowledged_v': self.dispatcher.states[role].value,
                'cmd_monotonic_s': cmd_at, 'ack_monotonic_s': ack_at,
                'clock_domain': 'host_monotonic',
                'precise_ramp': False,
                'note': 'duration_s=0 is a single write, not a precise ramp',
            })
            return
        if duration_s < resolution:
            raise ConfigError(
                f'Ramp duration {duration_s} s is below control resolution {resolution} s '
                f'(phase={phase}); refuse invalid timing rather than invent a precise ramp')

        # Prefer the shared V3 timeline: absolute monotonic deadlines, wait before command,
        # lateness fault, no overdue burst replay.
        pattern = HeldPattern(
            f'regen_{phase}',
            (Segment(0.0, duration_s, current, target, phase),),
        )
        started = self.dispatcher.clock()

        def guard(value):
            if self.faulted:
                raise ControlError('Experiment faulted; no auto-resume')
            now = self.dispatcher.clock()
            # Never command before the absolute deadline for this elapsed setpoint.
            # execute_timeline already waits; this guard records cmd/ack on host_monotonic.
            cmd_at = now
            self.dispatcher.command(role, value)
            ack_at = self.dispatcher.clock()
            self.emit({
                'event': 'experiment_setpoint', 'phase': phase, 'output': role,
                'nominal_v': value, 'acknowledged_v': self.dispatcher.states[role].value,
                'cmd_monotonic_s': cmd_at, 'ack_monotonic_s': ack_at,
                'deadline_monotonic_s': started + pattern.duration_s,  # end bound; slots via timeline
                'scpi_latency_s': ack_at - cmd_at,
                'clock_domain': 'host_monotonic',
                'precise_ramp': True,
                'scheduler': 'execute_timeline',
                'note': 'wait-then-command absolute deadlines; no burst replay of overdue slots',
            })

        def verify(value):
            self.dispatcher.verify(role, value)

        def progress(record):
            self.emit({
                **record,
                'phase': phase,
                'output': role,
                'clock_domain': 'host_monotonic',
                'scheduler': 'execute_timeline',
            })

        result = execute_timeline(
            pattern, guard, verify, progress,
            interval_s=resolution,
            max_lateness_s=max_lateness_s,
            clock=self.dispatcher.clock,
            wait=self.dispatcher.wait,
        )
        self.emit({
            'event': 'experiment_ramp_complete', 'phase': phase, 'output': role,
            'clock_domain': 'host_monotonic', **result,
        })

    def read_erpm(self) -> float | None:
        self.pin_feedback_identities()
        if self.feedback_mode == 'measured':
            if self.feedback_provider is None:
                raise InterlockError('measured feedback_mode requires a measured ERPM provider')
            value = self.feedback_provider('erpm')
            if value is None:
                raise InterlockError('measured ERPM unavailable')
            return value
        if self.feedback_provider is not None:
            return self.feedback_provider('erpm')
        # Simulated only: demand-proxy ERPM is explicitly synthetic.
        demand = self.dispatcher.states[MOTOR].value or 0.0
        peak = self._active_target_erpm
        if peak is None:
            speeds = self.experiment.get('speed_targets_erpm') or []
            peak = speeds[0]['target_erpm'] if speeds and speeds[0].get('target_erpm') is not None else 5000.0
        return demand / max(self.experiment['drive_demand_v'], 1e-9) * float(peak)

    def read_bus_v(self) -> float | None:
        self.pin_feedback_identities()
        if self.feedback_mode == 'measured':
            if self.feedback_provider is None:
                raise InterlockError('measured feedback_mode requires a measured bus provider')
            value = self.feedback_provider('bus_v')
            if value is None:
                raise InterlockError('measured bus voltage unavailable')
            return value
        if self.feedback_provider is not None:
            return self.feedback_provider('bus_v')
        # Simulated only: infer from demand mapping (not measured bus).
        gain = self.dispatcher.outputs[SUPPLY]['mapping']['gain_bus_v_per_aout_v']
        return (self.dispatcher.states[SUPPLY].value or 0.0) * gain

    def phase_a_establish_bus(self, bus_v: float):
        gain = self.dispatcher.outputs[SUPPLY]['mapping']['gain_bus_v_per_aout_v']
        demand = bus_demand_from_voltage(bus_v, gain)
        max_supply = self.dispatcher.outputs[SUPPLY]['max_aout_v']
        if demand > max_supply + 1e-12:
            raise AuthorizationError(
                f'Bus {bus_v} V maps to {demand} V AOUT exceeding supply max {max_supply} V')
        if self.dispatcher.has_motor:
            self.dispatcher.motor_neutral()
        self.emit({'event': 'phase', 'phase': 'A_establish_dc_bus', 'bus_voltage_v': bus_v,
                   'supply_demand_v': demand})
        self._ramp(SUPPLY, demand, self.experiment['accel']['ramp_s'], 'A_establish_dc_bus')
        self.dispatcher.wait(self.experiment['settle_s'])
        self.dispatcher.verify(SUPPLY, demand)

    def phase_b_accelerate(self, drive_v: float, bus_v: float):
        self.emit({'event': 'phase', 'phase': 'B_controlled_accel', 'drive_demand_v': drive_v})
        # Reuse V3 verify path; trial bus demand is the established Phase A setpoint (not a
        # fixed dispatcher motor_supply_demand_v — bus sweeps change between trials only).
        gain = self.dispatcher.outputs[SUPPLY]['mapping']['gain_bus_v_per_aout_v']
        required = bus_demand_from_voltage(bus_v, gain)
        actual = self.dispatcher.verify(SUPPLY)
        if not math.isclose(actual, required, abs_tol=1e-6):
            raise InterlockError(
                f'Supply demand readback {actual} V is not the trial bus demand {required} V')
        self.dispatcher.verify(MOTOR, self.dispatcher.outputs[MOTOR]['neutral_aout_v'])
        self._ramp(MOTOR, drive_v, self.experiment['accel']['ramp_s'], 'B_controlled_accel')
        self.dispatcher.wait(self.experiment['accel']['hold_s'])

    def phase_c_verify_start(self, target_erpm: float | None, bus_v: float) -> str | None:
        self.emit({'event': 'phase', 'phase': 'C_starting_condition_verification'})
        cond = self.experiment['start_condition']
        measured_bus = self.read_bus_v()
        if measured_bus is not None and abs(measured_bus - bus_v) > cond['bus_tolerance_v']:
            return f'bus {measured_bus} V outside {bus_v} +/- {cond["bus_tolerance_v"]} V'
        if target_erpm is not None:
            erpm = self.read_erpm()
            if erpm is None:
                if cond['require_measured_erpm']:
                    return 'measured ERPM required but unavailable'
            elif abs(erpm - target_erpm) > cond['erpm_tolerance']:
                return f'ERPM {erpm} outside {target_erpm} +/- {cond["erpm_tolerance"]}'
        return None

    def phase_d_regen_decel(self, release_s: float):
        self.emit({'event': 'phase', 'phase': 'D_regen_decel', 'release_duration_s': release_s})
        neutral = self.dispatcher.outputs[MOTOR]['neutral_aout_v']
        self._ramp(MOTOR, neutral, release_s, 'D_regen_decel')
        self.dispatcher.verify(MOTOR, neutral)

    def phase_e_recovery_observe(self):
        self.emit({'event': 'phase', 'phase': 'E_recovery_observation',
                   'observe_s': self.experiment['recovery_observe_s']})
        self.dispatcher.wait(self.experiment['recovery_observe_s'])

    def phase_f_complete(self) -> None:
        self.emit({'event': 'phase', 'phase': 'F_trial_completion'})
        self.dispatcher.verify(MOTOR, self.dispatcher.outputs[MOTOR]['neutral_aout_v'])
        # RZ-XA policy: preserve demand (hold_final); never auto-collapse bus after regen.
        self.dispatcher.verify(SUPPLY)
        self.emit({
            'event': 'rzxa_completion_policy',
            'policy': self.experiment['rzxa_completion_policy'],
            'supply_demand_v': self.dispatcher.states[SUPPLY].value,
            'auto_resume_after_fault': False,
            'software_neutral_is_not_estop': True,
        })

    def _resolve_csv_binding(self, trial_id: str, host_log_window: dict | None) -> dict:
        """Map a trial to acquisition-domain CSV bounds. Never treat host_monotonic as acquisition."""
        energy_cfg = self.experiment['energy']
        mode = energy_cfg.get('association_mode')
        configured = (energy_cfg.get('trial_windows') or {}).get(trial_id)
        if mode == 'A_markers':
            binding = configured or {}
            if not (binding.get('marker_start') and binding.get('marker_end')):
                raise ConfigError(
                    f'Mode A requires acquisition marker_start/marker_end for trial_id={trial_id}')
            return {
                'marker_start': binding['marker_start'],
                'marker_end': binding['marker_end'],
                'timebase': 'acquisition',
                'association_mode': mode,
            }
        if mode == 'B_acquisition_windows':
            binding = configured or {}
            if binding.get('start_s') is None or binding.get('end_s') is None:
                raise ConfigError(
                    f'Mode B requires acquisition start_s/end_s for trial_id={trial_id}')
            if binding.get('timebase', 'acquisition') != 'acquisition':
                raise ConfigError('Mode B windows must use acquisition timebase')
            return {
                'start_s': binding['start_s'],
                'end_s': binding['end_s'],
                'timebase': 'acquisition',
                'association_mode': mode,
            }
        if mode == 'C_verified_affine':
            affine = energy_cfg.get('affine') or {}
            if affine.get('verified') is not True:
                raise ConfigError('Mode C refused: affine mapping is not verified')
            if not host_log_window or host_log_window.get('start_s') is None:
                raise ConfigError('Mode C requires host_monotonic log markers to transform')
            slope = float(affine['slope'])
            offset = float(affine['offset_s'])
            return {
                'start_s': slope * host_log_window['start_s'] + offset,
                'end_s': slope * host_log_window['end_s'] + offset,
                'timebase': 'acquisition',
                'association_mode': mode,
                'affine_verified': True,
            }
        raise ConfigError(f'Unsupported or missing association_mode for csv_path: {mode!r}')

    def analyze_trial(self, plan: dict, drive_v: float, trial_id: str,
                      host_log_window: dict | None) -> EnergyMetrics:
        from .energy_analysis import extract_trial_series, load_sync_csv

        energy_cfg = self.experiment['energy']
        if energy_cfg.get('csv_path'):
            if self.feedback_mode == 'simulated':
                raise ConfigError('Refusing measured sync CSV while feedback_mode=simulated')
            full = load_sync_csv(
                energy_cfg['csv_path'],
                channel_map=energy_cfg.get('channel_map'),
                simulated=False,
                polarity=energy_cfg.get('polarity', 'rzxa_supply_positive'),
                time_unit=energy_cfg.get('time_unit', 's'),
                marker_column=energy_cfg.get('marker_column', 'marker'),
            )
            binding = self._resolve_csv_binding(trial_id, host_log_window)
            series = extract_trial_series(
                full,
                trial_id=trial_id,
                window=((binding['start_s'], binding['end_s'])
                        if binding.get('start_s') is not None else None),
                marker_start=binding.get('marker_start'),
                marker_end=binding.get('marker_end'),
                allow_whole_series=False,
            )
        elif energy_cfg.get('use_synthetic_when_no_csv', True):
            if self.feedback_mode == 'measured':
                raise ConfigError('measured feedback_mode forbids synthetic energy series')
            start_erpm = plan.get('target_erpm') or self.read_erpm() or 5000.0
            series = synthetic_regen_series(
                release_duration_s=plan['release_duration_s'],
                start_erpm=start_erpm,
                bus_v=plan['bus_voltage_v'],
                drive_peak_v=drive_v,
                observe_s=self.experiment['recovery_observe_s'],
            )
            series.trial_id = trial_id
        else:
            raise ConfigError('No sync CSV provided and synthetic series disabled')
        return compute_metrics(series, bus_target_v=plan['bus_voltage_v'])

    def run_trial(self, plan: dict, drive_decision) -> TrialResult:
        trial_id = uuid.uuid4().hex[:12]
        events: list[dict] = []

        def capture(record):
            events.append(record)
            self.emit(record)

        # Temporarily swap emit for capture+forward — use outer emit already.
        if self.faulted:
            return TrialResult(
                trial_id=trial_id, comparison_axis=plan['comparison_axis'],
                release_duration_s=plan['release_duration_s'], bus_voltage_v=plan['bus_voltage_v'],
                drive_demand_v=drive_decision.effective_max_v, target_erpm=plan.get('target_erpm'),
                valid=False, invalid_reason='prior fault; no auto-resume', phase='faulted',
                metrics=None, simulated=True, events=events,
            )
        if not plan.get('speed_enabled', True):
            return TrialResult(
                trial_id=trial_id, comparison_axis=plan['comparison_axis'],
                release_duration_s=plan['release_duration_s'], bus_voltage_v=plan['bus_voltage_v'],
                drive_demand_v=self.experiment['drive_demand_v'], target_erpm=plan.get('target_erpm'),
                valid=False, invalid_reason='speed target disabled', phase='skipped',
                metrics=None, simulated=True, events=events,
            )
        if not plan.get('speed_commissioned', True):
            return TrialResult(
                trial_id=trial_id, comparison_axis=plan['comparison_axis'],
                release_duration_s=plan['release_duration_s'], bus_voltage_v=plan['bus_voltage_v'],
                drive_demand_v=self.experiment['drive_demand_v'], target_erpm=plan.get('target_erpm'),
                valid=False, invalid_reason='speed target not commissioned', phase='skipped',
                metrics=None, simulated=True, events=events,
            )

        drive_v = self.experiment['drive_demand_v']
        self._active_target_erpm = plan.get('target_erpm')
        marker_start = f'REGEN_D_START_{trial_id}'
        marker_end = f'REGEN_E_END_{trial_id}'
        try:
            self.phase_a_establish_bus(plan['bus_voltage_v'])
            self.phase_b_accelerate(drive_v, plan['bus_voltage_v'])
            reason = self.phase_c_verify_start(plan.get('target_erpm'), plan['bus_voltage_v'])
            if reason:
                self.dispatcher.command(MOTOR, self.dispatcher.outputs[MOTOR]['neutral_aout_v'])
                return TrialResult(
                    trial_id=trial_id, comparison_axis=plan['comparison_axis'],
                    release_duration_s=plan['release_duration_s'],
                    bus_voltage_v=plan['bus_voltage_v'], drive_demand_v=drive_v,
                    target_erpm=plan.get('target_erpm'), valid=False, invalid_reason=reason,
                    phase='C_starting_condition_verification', metrics=None,
                    simulated=self.feedback_mode == 'simulated', events=events,
                    feedback_mode=self.feedback_mode,
                )
            # Host-monotonic log markers only — NOT OXYGEN acquisition markers / CSV time_s.
            host_start = self.dispatcher.clock()
            self.emit({
                'event': 'trial_marker', 'marker': marker_start, 'trial_id': trial_id,
                'monotonic_s': host_start, 'clock_domain': 'host_monotonic',
                'oxygen_acquisition_marker': False,
                'note': 'JSONL/host log marker only; not an OXYGEN sync acquisition marker',
            })
            self.phase_d_regen_decel(plan['release_duration_s'])
            self.phase_e_recovery_observe()
            host_end = self.dispatcher.clock()
            self.emit({
                'event': 'trial_marker', 'marker': marker_end, 'trial_id': trial_id,
                'monotonic_s': host_end, 'clock_domain': 'host_monotonic',
                'oxygen_acquisition_marker': False,
                'note': 'JSONL/host log marker only; not an OXYGEN sync acquisition marker',
            })
            self.phase_f_complete()
            host_log_window = {
                'start_s': host_start,
                'end_s': host_end,
                'marker_start': marker_start,
                'marker_end': marker_end,
                'timebase': 'host_monotonic',
            }
            metrics = self.analyze_trial(plan, drive_v, trial_id, host_log_window)
            self.dispatcher.wait(self.experiment['inter_trial_recovery_s'])
            return TrialResult(
                trial_id=trial_id, comparison_axis=plan['comparison_axis'],
                release_duration_s=plan['release_duration_s'],
                bus_voltage_v=plan['bus_voltage_v'], drive_demand_v=drive_v,
                target_erpm=plan.get('target_erpm'), valid=True, invalid_reason=None,
                phase='complete', metrics=metrics.as_dict(),
                simulated=metrics.simulated, events=events,
                # Preserve host-monotonic log span for Mode C; never imply acquisition time.
                csv_window_start_s=None,
                csv_window_end_s=None,
                marker_start=marker_start, marker_end=marker_end,
                feedback_mode=self.feedback_mode,
            )
        except Exception as exc:
            self.faulted = True
            try:
                self.dispatcher.command(MOTOR, self.dispatcher.outputs[MOTOR]['neutral_aout_v'])
            except Exception as neutral_exc:  # noqa: BLE001
                self.emit({'event': 'motor_neutral_best_effort_failed', 'error': str(neutral_exc),
                           'state': 'UNKNOWN', 'software_neutral_is_not_estop': True})
            self.emit({'event': 'experiment_fault', 'error_type': type(exc).__name__,
                       'error': str(exc), 'auto_resume': False})
            return TrialResult(
                trial_id=trial_id, comparison_axis=plan['comparison_axis'],
                release_duration_s=plan['release_duration_s'],
                bus_voltage_v=plan['bus_voltage_v'], drive_demand_v=drive_v,
                target_erpm=plan.get('target_erpm'), valid=False, invalid_reason=str(exc),
                phase='fault', metrics=None, simulated=True, events=events,
            )

    def run(self) -> dict:
        self.pin_feedback_identities()
        motor = self.dispatcher.outputs[MOTOR]
        decision = resolve_effective_drive_max(
            dispatcher_max_v=motor['max_aout_v'],
            commissioned_drive_limit_v=self.experiment.get(
                'commissioned_drive_limit_v', DEFAULT_COMMISSIONED_DRIVE_LIMIT_V),
            higher_drive_limit_authorized=self.experiment.get(
                'higher_drive_limit_authorized', False),
            requested_v=self.experiment['drive_demand_v'],
        )
        self.emit({'event': 'drive_limit_resolved', **decision.__dict__})

        # Validate candidate table (disabled higher candidates remain planning-only).
        for candidate in self.experiment['drive_limit_candidates']:
            if candidate['enabled']:
                resolve_effective_drive_max(
                    dispatcher_max_v=motor['max_aout_v'],
                    commissioned_drive_limit_v=min(
                        self.experiment.get('commissioned_drive_limit_v',
                                            DEFAULT_COMMISSIONED_DRIVE_LIMIT_V),
                        candidate['value_v']),
                    higher_drive_limit_authorized=self.experiment.get(
                        'higher_drive_limit_authorized', False)
                    or candidate['value_v'] <= DEFAULT_COMMISSIONED_DRIVE_LIMIT_V + 1e-12,
                    requested_v=min(self.experiment['drive_demand_v'], candidate['value_v']),
                    candidate_enabled=candidate['enabled'],
                    candidate_commissioned=candidate['commissioned'],
                )

        plans = plan_matrix(self.experiment)
        self.acquire_lease()
        try:
            for plan in plans:
                result = self.run_trial(plan, decision)
                self.results.append(result)
                self.emit({'event': 'trial_result', **result.as_dict()})
                if self.faulted:
                    break
        finally:
            try:
                self.dispatcher.cleanup()
            finally:
                self.release_lease()

        valid_metrics = []
        for result in self.results:
            if not result.valid or not result.metrics:
                continue
            payload = dict(result.metrics)
            notes = payload.get('notes')
            if isinstance(notes, list):
                payload['notes'] = tuple(notes)
            valid_metrics.append(EnergyMetrics(**payload))
        summary = {
            'experiment': self.experiment['name'],
            'experiment_type': EXPERIMENT_TYPE,
            'comparison': self.experiment['comparison'],
            'simulated': not self.dispatcher.physical,
            'physical_enabled': self.experiment['physical_enabled'],
            'simulation_only': self.experiment['simulation_only'],
            'illustrative_only': self.experiment.get('illustrative_only', False),
            'drive_limit': decision.__dict__,
            'locked_aout_names': LOCKED_AOUT_NAMES,
            'braking_control': 'unused',
            'trials': [r.as_dict() for r in self.results],
            'aggregation_valid_trials': aggregate_trials(valid_metrics),
            'invalid_trials': [
                {'trial_id': r.trial_id, 'reason': r.invalid_reason, 'phase': r.phase}
                for r in self.results if not r.valid
            ],
            'planning_candidates_v': list(PLANNING_CANDIDATES_V),
            'note': 'sim pass does not enable physical actuation',
        }
        return summary


def validate_experiment_plan(experiment_path, dispatcher_path) -> dict:
    experiment = load_experiment_config(experiment_path)
    config = load_dispatcher_config(dispatcher_path)
    if SUPPLY not in config.raw['outputs'] or MOTOR not in config.raw['outputs']:
        raise ConfigError('Regen experiment requires both rzxa_voltage_demand and vesc_throttle')
    # Locked physical names when oxygen_scpi / physical channel strings are present.
    for role, locked in LOCKED_AOUT_NAMES.items():
        channel = config.raw['outputs'][role].get('channel')
        if isinstance(channel, str) and channel.strip() and channel != locked:
            raise ConfigError(
                f'{role} channel must remain locked name {locked!r} (found {channel!r})')
    motor = config.raw['outputs'][MOTOR]
    decision = resolve_effective_drive_max(
        dispatcher_max_v=motor['max_aout_v'],
        commissioned_drive_limit_v=experiment.get(
            'commissioned_drive_limit_v', DEFAULT_COMMISSIONED_DRIVE_LIMIT_V),
        higher_drive_limit_authorized=experiment.get('higher_drive_limit_authorized', False),
        requested_v=experiment['drive_demand_v'],
    )
    plans = plan_matrix(experiment)
    return {
        'event': 'experiment_plan',
        'experiment': experiment['name'],
        'experiment_type': EXPERIMENT_TYPE,
        'dispatcher': str(Path(dispatcher_path).resolve()),
        'comparison': experiment['comparison'],
        'trial_count': len(plans),
        'drive_limit': decision.__dict__,
        'simulation_only': experiment['simulation_only'],
        'physical_enabled': experiment['physical_enabled'],
        'braking_control': 'unused',
        'connected': False,
        'writes_attempted': 0,
    }


def _resolve_dispatcher_bindings(experiment: dict, experiment_path: Path, bindings_arg: str | None) -> Path:
    root = Path(__file__).resolve().parent.parent
    defaults = {
        (root / 'dispatcher.json').resolve(),
        (root / 'dispatcher.example.json').resolve(),
    }
    candidates = []
    configured = experiment.get('dispatcher_bindings')
    # Prefer experiment-declared bindings over the generic CLI default dispatcher path.
    if configured:
        candidates.extend([
            Path(configured),
            root / configured,
            experiment_path.parent / configured,
            experiment_path.parent.parent / configured,
        ])
    if bindings_arg:
        binding_path = Path(bindings_arg)
        resolved = binding_path.resolve()
        explicit = resolved not in defaults or not configured
        if explicit:
            candidates.insert(0, binding_path)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise ConfigError(f'Could not resolve dispatcher bindings from {candidates!r}')


def run_experiment_cli(args) -> int:
    import logging

    experiment_path = Path(args.experiment).resolve()
    experiment = load_experiment_config(experiment_path)
    dispatcher_path = _resolve_dispatcher_bindings(
        experiment, experiment_path, getattr(args, 'bindings', None))

    plan = validate_experiment_plan(experiment_path, dispatcher_path)
    if not args.execute:
        print(json.dumps(plan, indent=2, allow_nan=False))
        return 0

    if experiment['physical_enabled']:
        raise AuthorizationError(
            'Physical regen execution is blocked in this milestone (offline-only development). '
            'sim pass ≠ physical enable.')
    if not args.confirm_simulated_output:
        raise AuthorizationError('Offline execution requires --confirm-simulated-output')

    config = load_dispatcher_config(dispatcher_path)
    if config.transport != 'memory':
        raise AuthorizationError('Offline regen experiment execution uses memory transport only')
    if config.physical:
        raise AuthorizationError('Refusing physical dispatcher bindings for offline experiment run')

    output_dir = Path(args.output_dir or experiment.get('output_dir') or 'logs/regen-experiments')
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{experiment['name']}-{uuid.uuid4().hex[:8]}.jsonl"
    handler = StrictRotatingHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8')
    run_id = uuid.uuid4().hex
    runner = None
    summary = None

    def emit(record):
        payload = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'monotonic_s': time.monotonic(),
            'run_id': run_id,
            **record,
        }
        line = json.dumps(payload, allow_nan=False)
        handler.emit(logging.LogRecord('regen', logging.INFO, '', 0, line, (), None))
        if record.get('event') not in {'experiment_setpoint', 'setpoint_ack', 'write_attempt'}:
            print(line, flush=True)

    backend = MemoryBackend(config)
    dispatcher = Dispatcher(config, backend, emit)
    try:
        emit(plan)
        backend.connect()
        dispatcher.prepare()
        runner = RegenExperimentRunner(experiment, dispatcher, emit)
        summary = runner.run()
    finally:
        backend.close()
        handler.close()

    if summary is None:
        return 1

    results_path = output_dir / f"{experiment['name']}-results.json"
    comparison_rows = []
    for trial in summary['trials']:
        row = {
            'trial_id': trial['trial_id'],
            'valid': trial['valid'],
            'invalid_reason': trial['invalid_reason'],
            'comparison_axis': trial['comparison_axis'],
            'release_duration_s': trial['release_duration_s'],
            'bus_voltage_v': trial['bus_voltage_v'],
            'drive_demand_v': trial['drive_demand_v'],
            'target_erpm': trial['target_erpm'],
            'simulated': trial['simulated'],
        }
        if trial['metrics']:
            row.update({
                'e_return_j': trial['metrics']['e_return_j'],
                'e_in_j': trial['metrics']['e_in_j'],
                'electrical_energy_return_ratio': trial['metrics']['electrical_energy_return_ratio'],
                'peak_neg_power_w': trial['metrics']['peak_neg_power_w'],
                'regen_duration_s': trial['metrics']['regen_duration_s'],
            })
        comparison_rows.append(row)
    csv_path = output_dir / f"{experiment['name']}-comparison.csv"
    write_comparison_csv(csv_path, comparison_rows)
    summary['artifacts'] = {
        'jsonl': str(log_path),
        'results_json': str(results_path),
        'comparison_csv': str(csv_path),
    }
    results_path.write_text(json.dumps(summary, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'event': 'experiment_summary', **summary['aggregation_valid_trials'],
                      'artifacts': summary['artifacts'],
                      'invalid_count': len(summary['invalid_trials'])}, indent=2))
    return 0 if runner is not None and not runner.faulted else 1
