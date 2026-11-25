"""Autonomous bidirectional regenerative motor characterization sweeps.

Runs on the existing Dispatcher SCPI/write path (no separate engine). Modes:

1. open_loop — fixed settle after AOUT ack; no measurement/RPM requirements
2. voltage_closed_loop — supervisory measured-bus qualification (not PID / not
   require_controller().correct())
3. full_closed_loop — voltage qualification + RPM-governed phase transitions

Cooperative Stop Sweep cancellation is polled during waits and pattern playback.
"""

from __future__ import annotations

import math
import uuid
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from .errors import ConfigError, ControlError, InterlockError
from .fast_regen import FastRegenConfig, build_fast_regen_pattern, parse_fast_regen
from .pattern_segments import MultiOutputPattern
from .schema import BRAKE, MOTOR, SUPPLY, VERIFIED_GAIN, keys, number
from .voltage_grid import (
    bidirectional_visits,
    bus_to_aout,
    generate_voltage_points,
    visit_count,
)

SWEEP_MODES = ('open_loop', 'voltage_closed_loop', 'full_closed_loop')
TERMINAL_COMPLETE = 'complete'
TERMINAL_CANCELLED = 'cancelled'
TERMINAL_FAULT = 'fault'
EVENT_HISTORY_LIMIT = 256


class SweepCancelled(ControlError):
    """Operator cooperative stop (software cancel — not a physical E-stop)."""


class SweepFault(ControlError):
    """Sweep failed closed; no silent open-loop fallback."""


@dataclass
class SweepContext:
    """Sweep-local dynamic bus context (does not mutate global interlocks)."""

    assignment: str
    mode: str
    bus_target_v: float
    expected_aout_v: float
    acked_aout_v: float | None
    measured_bus_v: float | None
    feedback_policy: str
    direction: str
    visit_index: int
    round_trip_index: int | None
    motor_phase: str
    run_id: str
    trial_id: str


@dataclass(frozen=True)
class VoltageQualifyConfig:
    channel: str = 'dc_bus_voltage'
    tolerance_v: float = 0.10
    qualify_duration_s: float = 0.20
    timeout_s: float = 3.0
    poll_s: float = 0.020
    max_age_s: float = 0.250
    drive_envelope: dict | None = None
    require_advancing_samples: bool = True


@dataclass(frozen=True)
class RpmFeedbackConfig:
    channel: str = 'erpm'
    kind: str = 'electrical'
    pole_pairs: int | None = None
    poll_s: float = 0.020
    max_age_s: float = 0.250
    accel_timeout_s: float = 5.0
    brake_timeout_s: float = 5.0
    # Operator-configurable thresholds (no invented defaults that escalate throttle).
    initial_rpm_max: float | None = None
    accel_rpm_target: float | None = None  # alias path; prefer fast_regen targets
    brake_rpm_target: float | None = None
    rpm_tolerance: float = 0.0
    rpm_hysteresis: float = 0.0
    qualify_duration_s: float = 0.0
    recovery_rpm_max: float | None = None
    monitor_during_ramps: bool = True
    require_advancing_samples: bool = True


@dataclass(frozen=True)
class MotorSweepConfig:
    mode: str
    start_v: float = 36.0
    end_v: float = 40.0
    step_v: float = 0.25
    initial_direction: str = 'descending'
    round_trips: int | None = 1
    continuous: bool = False
    settle_s: float = 0.5
    post_drive_recover_s: float = 0.0
    fast_regen: FastRegenConfig = field(default_factory=FastRegenConfig)
    voltage_qualify: VoltageQualifyConfig | None = None
    rpm_feedback: RpmFeedbackConfig | None = None
    cancel_policy_motor: str = 'return_neutral'
    cancel_policy_brake: str = 'return_release'
    cancel_policy_supply: str = 'preserve_demand'

    @property
    def points(self) -> tuple[Decimal, ...]:
        return generate_voltage_points(self.start_v, self.end_v, self.step_v)


def deep_merge(base: dict, override: dict) -> dict:
    """Documented nested merge: assignment overrides win; nested dicts merge recursively."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def parse_voltage_qualify(raw: dict | None) -> VoltageQualifyConfig | None:
    if raw is None:
        return None
    keys(raw, {
        'channel', 'tolerance_v', 'qualify_duration_s', 'timeout_s', 'poll_s', 'max_age_s',
        'drive_envelope', 'require_advancing_samples',
    }, 'voltage_qualify')
    envelope = raw.get('drive_envelope')
    if envelope is not None:
        keys(envelope, {'tolerance_v', 'max_age_s'}, 'drive_envelope')
        envelope = {
            'tolerance_v': number(envelope['tolerance_v'], 'drive_envelope.tolerance_v', 0.01, 5),
            'max_age_s': number(envelope['max_age_s'], 'drive_envelope.max_age_s', 0.01, 1),
        }
    advancing = raw.get('require_advancing_samples', True)
    if type(advancing) is not bool:
        raise ConfigError('require_advancing_samples must be Boolean')
    return VoltageQualifyConfig(
        channel=str(raw.get('channel', 'dc_bus_voltage')),
        tolerance_v=number(raw.get('tolerance_v', 0.10), 'tolerance_v', 0.01, 5),
        qualify_duration_s=number(raw.get('qualify_duration_s', 0.20), 'qualify_duration_s', 0.0, 60),
        timeout_s=number(raw.get('timeout_s', 3.0), 'timeout_s', 0.05, 120),
        poll_s=number(raw.get('poll_s', 0.020), 'poll_s', 0.01, 1),
        max_age_s=number(raw.get('max_age_s', 0.250), 'max_age_s', 0.01, 1),
        drive_envelope=envelope,
        require_advancing_samples=advancing,
    )


def parse_rpm_feedback(raw: dict | None) -> RpmFeedbackConfig | None:
    if raw is None:
        return None
    keys(raw, {
        'channel', 'kind', 'pole_pairs', 'poll_s', 'max_age_s',
        'accel_timeout_s', 'brake_timeout_s', 'initial_rpm_max',
        'accel_rpm_target', 'brake_rpm_target', 'rpm_tolerance', 'rpm_hysteresis',
        'qualify_duration_s', 'recovery_rpm_max', 'monitor_during_ramps',
        'require_advancing_samples',
    }, 'rpm_feedback')
    kind = raw.get('kind', 'electrical')
    if kind not in {'electrical', 'mechanical'}:
        raise ConfigError("rpm_feedback.kind must be 'electrical' or 'mechanical'")
    pole_pairs = raw.get('pole_pairs')
    if pole_pairs is not None and (type(pole_pairs) is not int or pole_pairs < 1):
        raise ConfigError('rpm_feedback.pole_pairs must be a positive integer when set')
    advancing = raw.get('require_advancing_samples', True)
    monitor_ramps = raw.get('monitor_during_ramps', True)
    if type(advancing) is not bool or type(monitor_ramps) is not bool:
        raise ConfigError('RPM Boolean flags must be Boolean')

    def optional_rpm(name):
        if name not in raw or raw[name] is None:
            return None
        return number(raw[name], name, 0, 1_000_000)

    return RpmFeedbackConfig(
        channel=str(raw.get('channel', 'erpm')),
        kind=kind,
        pole_pairs=pole_pairs,
        poll_s=number(raw.get('poll_s', 0.020), 'rpm poll_s', 0.01, 1),
        max_age_s=number(raw.get('max_age_s', 0.250), 'rpm max_age_s', 0.01, 1),
        accel_timeout_s=number(raw.get('accel_timeout_s', 5.0), 'accel_timeout_s', 0.1, 120),
        brake_timeout_s=number(raw.get('brake_timeout_s', 5.0), 'brake_timeout_s', 0.1, 120),
        initial_rpm_max=optional_rpm('initial_rpm_max'),
        accel_rpm_target=optional_rpm('accel_rpm_target'),
        brake_rpm_target=optional_rpm('brake_rpm_target'),
        rpm_tolerance=number(raw.get('rpm_tolerance', 0.0), 'rpm_tolerance', 0.0, 1_000_000),
        rpm_hysteresis=number(raw.get('rpm_hysteresis', 0.0), 'rpm_hysteresis', 0.0, 1_000_000),
        qualify_duration_s=number(raw.get('qualify_duration_s', 0.0), 'rpm qualify_duration_s', 0.0, 60),
        recovery_rpm_max=optional_rpm('recovery_rpm_max'),
        monitor_during_ramps=monitor_ramps,
        require_advancing_samples=advancing,
    )


def parse_motor_sweep(raw: dict, *, outputs: dict) -> MotorSweepConfig:
    if not isinstance(raw, dict):
        raise ConfigError('motor_sweep must be an object')
    keys(raw, {
        'mode', 'start_v', 'end_v', 'step_v', 'initial_direction', 'round_trips', 'continuous',
        'settle_s', 'post_drive_recover_s', 'fast_regen', 'voltage_qualify', 'rpm_feedback',
        'cancel_policy_motor', 'cancel_policy_brake', 'cancel_policy_supply',
    }, 'motor_sweep')
    mode = raw.get('mode')
    if mode not in SWEEP_MODES:
        raise ConfigError(f"motor_sweep.mode must be one of {SWEEP_MODES}")
    continuous = raw.get('continuous', False)
    if type(continuous) is not bool:
        raise ConfigError('continuous must be Boolean')
    round_trips = raw.get('round_trips', None if continuous else 1)
    if continuous:
        if round_trips is not None:
            raise ConfigError('continuous sweeps must omit round_trips (or set null)')
    else:
        if type(round_trips) is not int or round_trips < 1:
            raise ConfigError(
                'Finite sweeps require integer round_trips >= 1; '
                'omitted/invalid is not infinite — set continuous=true explicitly')
    initial_direction = raw.get('initial_direction', 'descending')
    if initial_direction not in {'ascending', 'descending'}:
        raise ConfigError("initial_direction must be 'ascending' or 'descending'")

    motor = outputs[MOTOR]
    brake = outputs[BRAKE]
    fast = parse_fast_regen(
        raw.get('fast_regen'),
        motor_max_v=motor['max_aout_v'],
        brake_max_v=brake.get('brake_max_aout_v', brake['max_aout_v']),
        brake_release_v=brake['brake_release_aout_v'],
    )
    vq = parse_voltage_qualify(raw.get('voltage_qualify'))
    rpm = parse_rpm_feedback(raw.get('rpm_feedback'))
    if mode == 'open_loop':
        if vq is not None or rpm is not None:
            raise ConfigError('open_loop sweep cannot declare voltage_qualify or rpm_feedback')
    elif mode == 'voltage_closed_loop':
        if vq is None:
            vq = VoltageQualifyConfig()
        if rpm is not None:
            raise ConfigError('voltage_closed_loop sweep cannot declare rpm_feedback')
    else:
        if vq is None:
            vq = VoltageQualifyConfig()
        if rpm is None:
            rpm = RpmFeedbackConfig()

    for name, expected in (
        ('cancel_policy_motor', 'return_neutral'),
        ('cancel_policy_brake', 'return_release'),
        ('cancel_policy_supply', 'preserve_demand'),
    ):
        if raw.get(name, expected) != expected:
            raise ConfigError(f'{name} must be {expected!r}')

    start_v = number(raw.get('start_v', 36.0), 'start_v', 1, 60)
    end_v = number(raw.get('end_v', 40.0), 'end_v', 1, 60)
    step_v = number(raw.get('step_v', 0.25), 'step_v', 0.01, 10)
    points = generate_voltage_points(start_v, end_v, step_v)
    if not continuous:
        visit_count(point_count=len(points), round_trips=round_trips, continuous=False)

    # Validate the full command grid against authorized AOUT ranges before any physical write.
    gain = float(outputs[SUPPLY].get('mapping', {}).get('gain_bus_v_per_aout_v', VERIFIED_GAIN))
    for point in points:
        aout = bus_to_aout(point, gain=gain)
        number(aout, 'sweep supply demand grid', outputs[SUPPLY]['min_aout_v'], outputs[SUPPLY]['max_aout_v'])
    number(fast.throttle_target_v, 'fast_regen.throttle_target_v',
           motor['min_aout_v'], motor['max_aout_v'])
    number(fast.brake_target_v, 'fast_regen.brake_target_v',
           brake['brake_release_aout_v'], brake.get('brake_max_aout_v', brake['max_aout_v']))

    return MotorSweepConfig(
        mode=mode,
        start_v=start_v,
        end_v=end_v,
        step_v=step_v,
        initial_direction=initial_direction,
        round_trips=round_trips,
        continuous=continuous,
        settle_s=number(raw.get('settle_s', 0.5), 'settle_s', 0.0, 60),
        post_drive_recover_s=number(raw.get('post_drive_recover_s', 0.0),
                                    'post_drive_recover_s', 0.0, 60),
        fast_regen=fast,
        voltage_qualify=vq,
        rpm_feedback=rpm,
    )


@dataclass
class SweepResult:
    status: str
    run_id: str
    mode: str
    visits_completed: int
    terminal_reason: str | None = None
    events: list[dict] = field(default_factory=list)
    cleanup_failures: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            'status': self.status,
            'run_id': self.run_id,
            'mode': self.mode,
            'visits_completed': self.visits_completed,
            'terminal_reason': self.terminal_reason,
            'cleanup_failures': list(self.cleanup_failures),
            'software_stop_is_not_estop': True,
        }


def _sample_freshness_key(sample) -> tuple:
    """Key for detecting sample *change* only.

    Host-SCPI timestamps and query-sequence counters are **not** verified acquisition
    freshness. Only ``freshness == 'acquisition'`` with ``acquisition_time_s`` counts as
    acquisition-domain identity; otherwise the key is explicitly ``host_scpi``.
    """
    freshness = getattr(sample, 'freshness', 'host_scpi')
    acq = getattr(sample, 'acquisition_time_s', None)
    seq = getattr(sample, 'sequence', None)
    if freshness == 'acquisition' and acq is not None:
        return ('acquisition', acq, seq if seq is not None else -1)
    # Host query stamp/sequence — change detection only; never claim acquisition verified.
    return ('host_scpi', sample.sampled_at, seq if seq is not None else -1)


def _acquisition_freshness_verified(sample) -> bool:
    return (getattr(sample, 'freshness', 'host_scpi') == 'acquisition'
            and getattr(sample, 'acquisition_time_s', None) is not None)


class MotorSweepRunner:
    def __init__(self, dispatcher, config: MotorSweepConfig, assignment_name: str, *,
                 stop_trigger: str | None = None,
                 cancel_check: Callable[[], bool] | None = None,
                 sleep: Callable[[float], None] | None = None):
        self.dispatcher = dispatcher
        self.config = config
        self.assignment = assignment_name
        self.stop_trigger = stop_trigger
        self._external_cancel = cancel_check
        # Retained underlying wait — never assign cancel_wait onto dispatcher.wait (recursion).
        self._sleep = dispatcher.wait if sleep is None else sleep
        self._cancel_requested = False
        self.run_id = uuid.uuid4().hex
        self.visits_completed = 0
        self.events: deque[dict] = deque(maxlen=EVENT_HISTORY_LIMIT)
        self.cleanup_failures: list[str] = []
        self._last_supply_check = None
        self._confirmed = {MOTOR: None, BRAKE: None, SUPPLY: None}

    def request_cancel(self, reason: str = 'stop_sweep'):
        self._cancel_requested = True
        self._emit({'event': 'sweep_cancel_requested', 'reason': reason,
                    'software_stop_is_not_estop': True})

    def cancel_requested(self) -> bool:
        if self._cancel_requested:
            return True
        if self._external_cancel is not None and self._external_cancel():
            self._cancel_requested = True
            return True
        if self.stop_trigger:
            if self.dispatcher.backend.read_button(self.stop_trigger) is True:
                self._cancel_requested = True
                return True
        return False

    def _emit(self, record: dict):
        payload = {
            'run_id': self.run_id,
            'assignment': self.assignment,
            'sweep_mode': self.config.mode,
            **record,
        }
        self.events.append(payload)
        self.dispatcher.emit(payload)

    def _raise_if_cancelled(self, phase: str):
        if self.cancel_requested():
            raise SweepCancelled(f'Sweep cancelled during {phase} (software stop — not E-stop)')

    def _cancel_poll_s(self) -> float:
        return max(0.01, self.dispatcher.config.raw['timing']['poll_ms'] / 1000)

    def _wait(self, seconds: float, *, phase: str):
        """Cancellation-aware wait using the retained underlying sleep (no recursion)."""
        if seconds <= 0:
            self._raise_if_cancelled(phase)
            return
        deadline = self.dispatcher.clock() + seconds
        poll = min(self._cancel_poll_s(), seconds)
        while True:
            self._raise_if_cancelled(phase)
            now = self.dispatcher.clock()
            if now >= deadline:
                return
            self._sleep(min(poll, deadline - now))

    def _supply_gain(self) -> float:
        mapping = self.dispatcher.outputs[SUPPLY].get('mapping', {})
        return float(mapping.get('gain_bus_v_per_aout_v', VERIFIED_GAIN))

    def _command_role(self, role, value):
        """Skip unchanged output writes; still preserve auth via command path when writing."""
        prior = self._confirmed[role]
        if prior is not None and math.isclose(prior, value, abs_tol=1e-12, rel_tol=0):
            return prior
        self.dispatcher.command(role, value)
        self._confirmed[role] = value
        return value

    def _verify_supply_cached(self, expected: float, *, force: bool = False):
        # Reduce redundant supply verifies: re-check on phase boundaries / force only.
        if (not force and self._last_supply_check is not None and
                math.isclose(self._last_supply_check, expected, abs_tol=1e-6, rel_tol=0)):
            return self._last_supply_check
        actual = self.dispatcher.verify(SUPPLY, expected)
        self._last_supply_check = actual
        self._confirmed[SUPPLY] = actual
        return actual

    def _command_supply(self, bus_v: float, context: SweepContext):
        aout = bus_to_aout(bus_v, gain=self._supply_gain())
        spec = self.dispatcher.outputs[SUPPLY]
        aout = number(aout, 'sweep supply demand', spec['min_aout_v'], spec['max_aout_v'])
        self._raise_if_cancelled('supply_command')
        self.dispatcher.motor_neutral()
        self.dispatcher.brake_released()
        self._confirmed[MOTOR] = self.dispatcher.outputs[MOTOR]['neutral_aout_v']
        self._confirmed[BRAKE] = self.dispatcher.outputs[BRAKE]['brake_release_aout_v']
        self._command_role(SUPPLY, aout)
        acked = self._verify_supply_cached(aout, force=True)
        context.expected_aout_v = aout
        context.acked_aout_v = acked
        context.measured_bus_v = None
        self._emit({
            'event': 'sweep_supply_commanded',
            'bus_target_v': bus_v,
            'commanded_aout_v': aout,
            'acked_aout_v': acked,
            'physical_voltage_established': False,
            'message': 'AOUT ack/readback is not verified physical bus voltage',
        })
        return aout

    def _is_advancing(self, sample, last_key) -> bool:
        key = _sample_freshness_key(sample)
        if last_key is None:
            return True
        return key > last_key

    def _qualify_voltage(self, context: SweepContext):
        cfg = self.config.voltage_qualify
        assert cfg is not None
        started = self.dispatcher.clock()
        in_band_since = None
        last_fresh_key = None
        freshness_note = None
        self._emit({
            'event': 'sweep_voltage_qualify_begin',
            'bus_target_v': context.bus_target_v,
            'tolerance_v': cfg.tolerance_v,
            'qualify_duration_s': cfg.qualify_duration_s,
            'timeout_s': cfg.timeout_s,
            'require_advancing_samples': cfg.require_advancing_samples,
            'supervisory_only': True,
            'uses_require_controller': False,
        })
        while True:
            self._raise_if_cancelled('voltage_qualify')
            elapsed = self.dispatcher.clock() - started
            if elapsed > cfg.timeout_s:
                raise SweepFault(
                    f'Voltage qualification timeout after {elapsed:.3f}s '
                    f'(target {context.bus_target_v} ± {cfg.tolerance_v} V); '
                    'no silent open-loop fallback')
            sample = self.dispatcher.sample_supervisory(cfg.channel, max_age=cfg.max_age_s)
            if not _acquisition_freshness_verified(sample) and freshness_note is None:
                freshness_note = (
                    'limitation: host SCPI timestamps / query sequence are not verified '
                    'acquisition freshness; qualify uses host-side sample change only')
                self._emit({
                    'event': 'sweep_freshness_limitation', 'message': freshness_note,
                    'freshness': getattr(sample, 'freshness', 'host_scpi'),
                    'acquisition_freshness_verified': False,
                })
            measured = float(sample.value)
            context.measured_bus_v = measured
            advancing = self._is_advancing(sample, last_fresh_key)
            if advancing:
                last_fresh_key = _sample_freshness_key(sample)
            in_band = abs(measured - context.bus_target_v) <= cfg.tolerance_v
            now = self.dispatcher.clock()
            if not in_band:
                in_band_since = None
            elif cfg.require_advancing_samples and not advancing:
                # Cached/non-advancing sample must not start or advance the qualify timer.
                pass
            else:
                if in_band_since is None:
                    in_band_since = now
                if now - in_band_since >= cfg.qualify_duration_s:
                    self._emit({
                        'event': 'sweep_voltage_qualified',
                        'bus_target_v': context.bus_target_v,
                        'measured_bus_v': measured,
                        'qualify_elapsed_s': now - in_band_since,
                        'physical_voltage_established': True,
                        'supervisory_only': True,
                        'freshness': getattr(sample, 'freshness', 'host_scpi'),
                        'acquisition_freshness_verified': _acquisition_freshness_verified(sample),
                    })
                    return measured
            self._wait(cfg.poll_s, phase='voltage_qualify')

    def _target_domain_rpm(self, raw_value: float) -> float:
        """Convert measurement into the domain of fast_regen RPM targets."""
        cfg = self.config.rpm_feedback
        fr = self.config.fast_regen
        assert cfg is not None
        pairs = fr.pole_pairs or cfg.pole_pairs
        measured_kind = cfg.kind
        target_kind = fr.rpm_kind
        if measured_kind == target_kind:
            return raw_value
        if pairs is None:
            raise SweepFault(
                f'pole_pairs required to convert {measured_kind} RPM to {target_kind} before commands')
        if measured_kind == 'electrical' and target_kind == 'mechanical':
            return raw_value / pairs
        if measured_kind == 'mechanical' and target_kind == 'electrical':
            return raw_value * pairs
        return raw_value

    def _read_rpm_sample(self):
        cfg = self.config.rpm_feedback
        assert cfg is not None
        sample = self.dispatcher.sample_supervisory(cfg.channel, max_age=cfg.max_age_s)
        return sample, self._target_domain_rpm(float(sample.value))

    def _run_fast_regen(self, context: SweepContext):
        pattern = build_fast_regen_pattern(
            self.config.fast_regen,
            name=f'{self.assignment}_fast_regen',
            motor_neutral_v=self.dispatcher.outputs[MOTOR]['neutral_aout_v'],
            brake_release_v=self.dispatcher.outputs[BRAKE]['brake_release_aout_v'],
        )
        self._emit({
            'event': 'sweep_fast_regen_begin',
            'trial_id': context.trial_id,
            'bus_target_v': context.bus_target_v,
            'nominal_duration_s': self.config.fast_regen.nominal_duration_s,
            'throttle_target_v': self.config.fast_regen.throttle_target_v,
            'brake_target_v': self.config.fast_regen.brake_target_v,
        })
        supply_before = self._verify_supply_cached(
            context.acked_aout_v if context.acked_aout_v is not None
            else self.dispatcher.states[SUPPLY].value,
            force=True)

        use_rpm = self.config.mode == 'full_closed_loop' and self.config.rpm_feedback is not None
        accel_target = None
        brake_target = None
        if use_rpm:
            fr = self.config.fast_regen
            rpm_cfg = self.config.rpm_feedback
            accel_target = fr.accel_rpm_target if fr.accel_rpm_target is not None else rpm_cfg.accel_rpm_target
            brake_target = fr.brake_rpm_target if fr.brake_rpm_target is not None else rpm_cfg.brake_rpm_target
            if accel_target is None or brake_target is None:
                raise SweepFault(
                    'full_closed_loop requires accel_rpm_target and brake_rpm_target; '
                    'no silent fallback to timed open-loop')
            self._preflight_full_closed_loop(accel_target, brake_target)
            result = self._run_fast_regen_rpm_governed(
                pattern, context, supply_before, accel_target, brake_target)
        else:
            result = self._run_pattern_timed(pattern, context, supply_before)

        self._emit({
            'event': 'sweep_fast_regen_complete',
            'trial_id': context.trial_id,
            'actual_elapsed_s': result.get('actual_elapsed_s'),
            'nominal_duration_s': pattern.duration_s,
            'supply_unchanged': True,
            'supply_aout_v': supply_before,
            **{k: v for k, v in result.items() if k != 'actual_elapsed_s'},
        })
        return result

    def _run_pattern_timed(self, pattern: MultiOutputPattern, context: SweepContext,
                           supply_before: float) -> dict:
        d = self.dispatcher
        last_values = {role: None for role in pattern.roles}
        phase_marks: dict[str, float] = {}

        def apply_values(values_by_role):
            self._raise_if_cancelled(context.motor_phase or 'fast_regen')
            self._verify_supply_cached(supply_before, force=False)
            if not math.isclose(
                    d.states[SUPPLY].last_confirmed_v
                    if d.states[SUPPLY].last_confirmed_v is not None else supply_before,
                    supply_before, abs_tol=1e-6, rel_tol=0):
                # Force re-read if cache suspect.
                if not math.isclose(d.verify(SUPPLY), supply_before, abs_tol=1e-6, rel_tol=0):
                    raise SweepFault('RZ-XA demand changed during Fast Regen; refusing to continue')
            for role in pattern.roles:
                target = values_by_role[role]
                prior = last_values[role]
                if prior is None or not math.isclose(prior, target, abs_tol=1e-12, rel_tol=0):
                    self._command_role(role, target)
                    last_values[role] = target

        def verify(values_by_role):
            for role, target in values_by_role.items():
                d.verify(role, target)
                self._confirmed[role] = target
            self._verify_supply_cached(supply_before, force=True)

        def progress(record):
            phase = record.get('phase')
            if phase and phase != context.motor_phase:
                now = d.clock()
                if context.motor_phase and context.motor_phase in phase_marks:
                    self._emit({
                        'event': 'sweep_phase_duration',
                        'phase': context.motor_phase,
                        'actual_duration_s': now - phase_marks[context.motor_phase],
                    })
                context.motor_phase = phase
                phase_marks[phase] = now
                # Phase boundary: re-verify supply once.
                self._verify_supply_cached(supply_before, force=True)
                self._emit({'event': 'sweep_phase', 'phase': phase,
                            'elapsed_s': record.get('elapsed_s'),
                            'trial_id': context.trial_id,
                            'mandatory_boundary': record.get('mandatory_boundary')})
            d.emit({**record, 'assignment': self.assignment, 'trial_id': context.trial_id,
                    'sweep_mode': self.config.mode})

        def cancel_wait(seconds):
            self._wait(seconds, phase=context.motor_phase or 'fast_regen')

        if len(pattern.roles) > 1:
            return d._run_multi_timeline(pattern, apply_values, verify, progress, wait=cancel_wait)
        raise ControlError('Fast Regen requires motor+brake coordinated pattern')

    def _rpm_compare_accel(self, rpm, target, cfg: RpmFeedbackConfig) -> bool:
        return rpm >= (target - cfg.rpm_tolerance)

    def _rpm_compare_brake(self, rpm, target, cfg: RpmFeedbackConfig) -> bool:
        return rpm <= (target + cfg.rpm_tolerance)

    def _preflight_full_closed_loop(self, accel_target: float, brake_target: float):
        """Validate RPM ordering, domains, pole-pairs, and feedback before first motor/brake write."""
        cfg = self.config.rpm_feedback
        fr = self.config.fast_regen
        assert cfg is not None
        if accel_target <= brake_target:
            raise SweepFault(
                f'full_closed_loop RPM ordering invalid: accel_rpm_target ({accel_target}) '
                f'must be greater than brake_rpm_target ({brake_target})')
        measured_kind = cfg.kind
        target_kind = fr.rpm_kind
        if measured_kind not in {'electrical', 'mechanical'}:
            raise SweepFault(f'rpm_feedback.kind invalid: {measured_kind!r}')
        if target_kind not in {'electrical', 'mechanical'}:
            raise SweepFault(f'fast_regen.rpm_kind invalid: {target_kind!r}')
        pairs = fr.pole_pairs or cfg.pole_pairs
        if measured_kind != target_kind and pairs is None:
            raise SweepFault(
                f'pole_pairs required before first output to convert {measured_kind} ↔ {target_kind}')
        # Feedback availability — fail closed before commanding motor/brake.
        sample = self.dispatcher.sample_supervisory(cfg.channel, max_age=cfg.max_age_s)
        number(sample.value, cfg.channel, -1_000_000, 1_000_000)
        self._emit({
            'event': 'sweep_full_closed_loop_preflight_ok',
            'accel_rpm_target': accel_target,
            'brake_rpm_target': brake_target,
            'measured_kind': measured_kind,
            'target_kind': target_kind,
            'pole_pairs': pairs,
            'feedback_channel': cfg.channel,
            'acquisition_freshness_verified': _acquisition_freshness_verified(sample),
        })

    def _run_fast_regen_rpm_governed(self, pattern: MultiOutputPattern, context: SweepContext,
                                     supply_before: float, accel_target: float,
                                     brake_target: float) -> dict:
        d = self.dispatcher
        cfg = self.config.rpm_feedback
        fr = self.config.fast_regen
        assert cfg is not None
        neutral = d.outputs[MOTOR]['neutral_aout_v']
        release = d.outputs[BRAKE]['brake_release_aout_v']
        started = d.clock()
        schedule = list(fr.phase_schedule())
        last_fresh_key = None

        def ensure_supply(*, force=False):
            actual = self._verify_supply_cached(supply_before, force=force)
            if not math.isclose(actual, supply_before, abs_tol=1e-6, rel_tol=0):
                raise SweepFault('RZ-XA demand changed during RPM-governed Fast Regen')

        def set_outputs(drive_v, brake_v, phase, *, verify_boundary=False):
            self._raise_if_cancelled(phase)
            ensure_supply(force=verify_boundary)
            context.motor_phase = phase
            if drive_v != neutral and brake_v != release:
                raise SweepFault('Drive/brake mutual exclusion violated')
            # Explicit sequential boundaries: never skip release/neutral acks.
            if brake_v != release:
                self._command_role(MOTOR, neutral)
                if verify_boundary:
                    d.verify(MOTOR, neutral)
                    self._confirmed[MOTOR] = neutral
                self._command_role(BRAKE, brake_v)
                if verify_boundary:
                    d.verify(BRAKE, brake_v)
                    self._confirmed[BRAKE] = brake_v
            elif drive_v != neutral:
                self._command_role(BRAKE, release)
                if verify_boundary:
                    d.verify(BRAKE, release)
                    self._confirmed[BRAKE] = release
                self._command_role(MOTOR, drive_v)
                if verify_boundary:
                    d.verify(MOTOR, drive_v)
                    self._confirmed[MOTOR] = drive_v
            else:
                self._command_role(MOTOR, neutral)
                self._command_role(BRAKE, release)
                if verify_boundary:
                    d.verify(MOTOR, neutral)
                    d.verify(BRAKE, release)
                    self._confirmed[MOTOR] = neutral
                    self._confirmed[BRAKE] = release

        def read_rpm():
            nonlocal last_fresh_key
            sample, rpm = self._read_rpm_sample()
            advancing = self._is_advancing(sample, last_fresh_key)
            if advancing:
                last_fresh_key = _sample_freshness_key(sample)
            elif cfg.require_advancing_samples:
                raise InterlockError(
                    'RPM sample not advancing (cached/stale); no silent continue')
            return rpm, sample

        def wait_rpm_condition(drive_v, brake_v, phase, target, timeout_s, compare, *,
                               hold_outputs=True):
            began = d.clock()
            met_since = None
            if hold_outputs:
                set_outputs(drive_v, brake_v, phase, verify_boundary=True)
            while True:
                self._raise_if_cancelled(phase)
                ensure_supply(force=False)
                rpm, sample = read_rpm()
                self._emit({'event': 'sweep_rpm_sample', 'phase': phase, 'rpm': rpm,
                            'target_rpm': target, 'trial_id': context.trial_id,
                            'freshness': getattr(sample, 'freshness', 'host_scpi')})
                if compare(rpm, target, cfg):
                    now = d.clock()
                    if met_since is None:
                        met_since = now
                    if now - met_since >= cfg.qualify_duration_s:
                        self._emit({'event': 'sweep_rpm_target_reached', 'phase': phase,
                                    'rpm': rpm, 'target_rpm': target})
                        return rpm
                else:
                    met_since = None
                if d.clock() - began > timeout_s:
                    raise SweepFault(
                        f'RPM target not reached in {phase} within {timeout_s}s '
                        f'(rpm={rpm}, target={target}); no silent timed fallback')
                self._wait(cfg.poll_s, phase=phase)

        # Optional initial RPM gate (operator-configured; never invents a limit).
        if cfg.initial_rpm_max is not None:
            set_outputs(neutral, release, 'initial_rpm_qual', verify_boundary=True)
            wait_rpm_condition(
                neutral, release, 'initial_rpm_qual', cfg.initial_rpm_max, cfg.accel_timeout_s,
                lambda rpm, tgt, c: rpm <= tgt + c.rpm_hysteresis)

        def ramp(role, start_v, end_v, duration_s, phase, other_safe, rpm_target=None, compare=None,
                 timeout_s=None):
            """Ramp toward end_v. Returns (current_command_v, early_rpm_qualified).

            On early RPM qualify during accel/brake: keep the current acknowledged
            command — do **not** jump to the full target. Caller transitions from
            that current value toward neutral/release and skips hold reapplication.
            """
            # Boundary: confirm safe other channel before ramp starts.
            if role == MOTOR:
                set_outputs(start_v, other_safe, phase, verify_boundary=True)
            else:
                set_outputs(other_safe, start_v, phase, verify_boundary=True)
            current = start_v
            if duration_s <= 0:
                if role == MOTOR:
                    set_outputs(end_v, other_safe, phase, verify_boundary=True)
                else:
                    set_outputs(other_safe, end_v, phase, verify_boundary=True)
                return end_v, False
            began = d.clock()
            met_since = None
            froze_at = None
            while True:
                self._raise_if_cancelled(phase)
                ensure_supply(force=False)
                elapsed = d.clock() - began
                frac = 1.0 if elapsed >= duration_s else elapsed / duration_s
                if froze_at is None:
                    value = start_v + (end_v - start_v) * frac
                    current = value
                else:
                    # Freeze at first in-band sample while qualify_duration accumulates
                    # (same semantics as hold-phase RPM qualification).
                    current = froze_at
                if role == MOTOR:
                    set_outputs(current, other_safe, phase, verify_boundary=False)
                else:
                    set_outputs(other_safe, current, phase, verify_boundary=False)
                if cfg.monitor_during_ramps and rpm_target is not None and compare is not None:
                    rpm, sample = read_rpm()
                    self._emit({
                        'event': 'sweep_rpm_sample', 'phase': phase, 'rpm': rpm,
                        'target_rpm': rpm_target, 'trial_id': context.trial_id,
                        'during_ramp': True,
                        'freshness': getattr(sample, 'freshness', 'host_scpi'),
                        'acquisition_freshness_verified': _acquisition_freshness_verified(sample),
                        'qualify_duration_s': cfg.qualify_duration_s,
                    })
                    if compare(rpm, rpm_target, cfg):
                        now = d.clock()
                        if met_since is None:
                            met_since = now
                            froze_at = current
                            if role == MOTOR:
                                set_outputs(current, other_safe, phase, verify_boundary=True)
                            else:
                                set_outputs(other_safe, current, phase, verify_boundary=True)
                        if now - met_since >= cfg.qualify_duration_s:
                            self._emit({
                                'event': 'sweep_rpm_target_reached', 'phase': phase,
                                'rpm': rpm, 'target_rpm': rpm_target, 'during_ramp': True,
                                'command_at_qualify_v': current,
                                'full_target_v': end_v,
                                'jumped_to_full_target': False,
                                'qualify_duration_s': cfg.qualify_duration_s,
                            })
                            return current, True
                    else:
                        met_since = None
                        froze_at = None
                    if timeout_s is not None and d.clock() - began > timeout_s:
                        raise SweepFault(
                            f'RPM target not reached during {phase} within {timeout_s}s')
                if froze_at is None and frac >= 1.0:
                    if role == MOTOR:
                        set_outputs(end_v, other_safe, phase, verify_boundary=True)
                    else:
                        set_outputs(other_safe, end_v, phase, verify_boundary=True)
                    return end_v, False
                self._wait(
                    cfg.poll_s if froze_at is not None
                    else d.config.raw['timing']['interval_ms'] / 1000,
                    phase=phase)

        accel_cmd, early_accel = ramp(
            MOTOR, neutral, fr.throttle_target_v, fr.accel_ramp_s, 'accel_ramp', release,
            rpm_target=accel_target, compare=self._rpm_compare_accel, timeout_s=cfg.accel_timeout_s)
        if early_accel:
            # Transition from current acknowledged command toward neutral — skip hold
            # that would reapply the full throttle target.
            ramp(MOTOR, accel_cmd, neutral, fr.throttle_release_s, 'throttle_release', release)
        else:
            wait_rpm_condition(fr.throttle_target_v, release, 'accel_hold', accel_target,
                               cfg.accel_timeout_s, self._rpm_compare_accel)
            ramp(MOTOR, fr.throttle_target_v, neutral, fr.throttle_release_s, 'throttle_release',
                 release)
        self._wait(fr.transition_s, phase='transition')
        # Neutral must be acked before brake demand (explicit boundary).
        set_outputs(neutral, release, 'transition', verify_boundary=True)
        brake_cmd, early_brake = ramp(
            BRAKE, release, fr.brake_target_v, fr.brake_ramp_s, 'brake_ramp', neutral,
            rpm_target=brake_target, compare=self._rpm_compare_brake, timeout_s=cfg.brake_timeout_s)
        if early_brake:
            # Transition from current brake command toward release — skip hold reapplication.
            ramp(BRAKE, brake_cmd, release, fr.brake_release_s, 'brake_release', neutral)
        else:
            wait_rpm_condition(neutral, fr.brake_target_v, 'brake_hold', brake_target,
                               cfg.brake_timeout_s, self._rpm_compare_brake)
            ramp(BRAKE, fr.brake_target_v, release, fr.brake_release_s, 'brake_release', neutral)
        set_outputs(neutral, release, 'brake_release', verify_boundary=True)
        self._wait(fr.recovery_s, phase='recovery')
        if cfg.recovery_rpm_max is not None:
            wait_rpm_condition(neutral, release, 'recovery', cfg.recovery_rpm_max,
                               cfg.brake_timeout_s,
                               lambda rpm, tgt, c: rpm <= tgt + c.rpm_hysteresis)
        ensure_supply(force=True)
        return {
            'actual_elapsed_s': d.clock() - started,
            'nominal_duration_s': pattern.duration_s,
            'rpm_governed': True,
            'schedule_phases': [name for name, _ in schedule],
            'monitor_during_ramps': cfg.monitor_during_ramps,
        }

    def _apply_cancel_policies(self) -> list[str]:
        """Attempt motor/brake/supply policies independently; never claim success on failure."""
        d = self.dispatcher
        failures: list[str] = []
        if MOTOR in d.outputs:
            try:
                # Force the write path (do not skip via unchanged-cache) so cleanup is observable.
                self._confirmed[MOTOR] = None
                d.command(MOTOR, d.outputs[MOTOR]['neutral_aout_v'])
                d.verify(MOTOR, d.outputs[MOTOR]['neutral_aout_v'])
                self._confirmed[MOTOR] = d.outputs[MOTOR]['neutral_aout_v']
                self._emit({'event': 'sweep_cancel_motor_ok'})
            except Exception as exc:
                failures.append(f'motor:{exc}')
                d.states[MOTOR].status = 'unknown'
                self._emit({'event': 'sweep_cancel_motor_failed', 'error': str(exc)})
        if BRAKE in d.outputs:
            try:
                self._confirmed[BRAKE] = None
                d.command(BRAKE, d.outputs[BRAKE]['brake_release_aout_v'])
                d.verify(BRAKE, d.outputs[BRAKE]['brake_release_aout_v'])
                self._confirmed[BRAKE] = d.outputs[BRAKE]['brake_release_aout_v']
                self._emit({'event': 'sweep_cancel_brake_ok'})
            except Exception as exc:
                failures.append(f'brake:{exc}')
                d.states[BRAKE].status = 'unknown'
                self._emit({'event': 'sweep_cancel_brake_failed', 'error': str(exc)})
        if SUPPLY in d.outputs:
            try:
                value = d.verify(SUPPLY)
                self._emit({
                    'event': 'sweep_cancel_supply_preserved',
                    'aout_v': value,
                    'not_power_off': True,
                    'software_stop_is_not_estop': True,
                })
            except Exception as exc:
                failures.append(f'supply:{exc}')
                d.states[SUPPLY].status = 'unknown'
                self._emit({'event': 'sweep_cancel_supply_unknown', 'error': str(exc)})
        self.cleanup_failures = failures
        if failures:
            self._emit({
                'event': 'sweep_cancel_cleanup_incomplete',
                'failures': failures,
                'successful_cleanup': False,
                'software_stop_is_not_estop': True,
            })
        return failures

    def run(self) -> SweepResult:
        d = self.dispatcher
        if not d.prepared:
            raise ControlError('Dispatcher must be prepared before motor sweep')
        if SUPPLY not in d.outputs or MOTOR not in d.outputs or BRAKE not in d.outputs:
            raise ConfigError('Motor sweeps require rzxa_voltage_demand, vesc_throttle, and vesc_brake')
        if d.active_profile is not None:
            raise ControlError('Another profile owns outputs; sweep not started')

        points = self.config.points
        timing = d.config.raw['timing']
        self._emit({
            'event': 'sweep_started',
            'mode': self.config.mode,
            'point_count': len(points),
            'planned_visits': visit_count(
                point_count=len(points),
                round_trips=self.config.round_trips,
                continuous=self.config.continuous,
            ),
            'initial_direction': self.config.initial_direction,
            'round_trips': self.config.round_trips,
            'continuous': self.config.continuous,
            'start_v': self.config.start_v,
            'end_v': self.config.end_v,
            'step_v': self.config.step_v,
            'fast_regen_nominal_s': self.config.fast_regen.nominal_duration_s,
            'event_history_limit': EVENT_HISTORY_LIMIT,
            'timing': {
                'dout_poll_ms': timing.get('poll_ms'),
                'aout_interval_ms': timing.get('interval_ms'),
                'max_lateness_ms': timing.get('max_lateness_ms'),
                'voltage_feedback_poll_s': (
                    None if self.config.voltage_qualify is None
                    else self.config.voltage_qualify.poll_s),
                'rpm_feedback_poll_s': (
                    None if self.config.rpm_feedback is None
                    else self.config.rpm_feedback.poll_s),
            },
            'software_stop_is_not_estop': True,
        })

        d.active_profile = self.assignment
        d.active_role = MOTOR
        d.active_roles = (SUPPLY, MOTOR, BRAKE)
        d.interrupted_cycle = True
        d.started += 1
        status = TERMINAL_FAULT
        reason = None
        try:
            # Full closed-loop: validate RPM/domains/feedback before any sweep AOUT write.
            if self.config.mode == 'full_closed_loop' and self.config.rpm_feedback is not None:
                fr = self.config.fast_regen
                rpm_cfg = self.config.rpm_feedback
                accel_t = fr.accel_rpm_target if fr.accel_rpm_target is not None else rpm_cfg.accel_rpm_target
                brake_t = fr.brake_rpm_target if fr.brake_rpm_target is not None else rpm_cfg.brake_rpm_target
                if accel_t is None or brake_t is None:
                    raise SweepFault(
                        'full_closed_loop requires accel_rpm_target and brake_rpm_target; '
                        'no silent fallback to timed open-loop')
                self._preflight_full_closed_loop(accel_t, brake_t)
            for voltage, direction, visit_index, trip in bidirectional_visits(
                    points,
                    initial_direction=self.config.initial_direction,
                    round_trips=self.config.round_trips,
                    continuous=self.config.continuous,
            ):
                self._raise_if_cancelled('between_visits')
                trial_id = uuid.uuid4().hex
                bus_v = float(voltage)
                context = SweepContext(
                    assignment=self.assignment,
                    mode=self.config.mode,
                    bus_target_v=bus_v,
                    expected_aout_v=bus_to_aout(bus_v, gain=self._supply_gain()),
                    acked_aout_v=None,
                    measured_bus_v=None,
                    feedback_policy=self.config.mode,
                    direction=direction,
                    visit_index=visit_index,
                    round_trip_index=trip,
                    motor_phase='idle',
                    run_id=self.run_id,
                    trial_id=trial_id,
                )
                d.sweep_context = context
                self._emit({
                    'event': 'sweep_visit_begin',
                    'visit_index': visit_index,
                    'round_trip_index': trip,
                    'bus_target_v': bus_v,
                    'direction': direction,
                    'trial_id': trial_id,
                })
                self._command_supply(bus_v, context)
                if self.config.mode == 'open_loop':
                    self._wait(self.config.settle_s, phase='settle')
                else:
                    self._qualify_voltage(context)
                self._run_fast_regen(context)
                if self.config.post_drive_recover_s > 0:
                    self._wait(self.config.post_drive_recover_s, phase='post_drive_recover')
                d.motor_neutral()
                d.brake_released()
                self._confirmed[MOTOR] = d.outputs[MOTOR]['neutral_aout_v']
                self._confirmed[BRAKE] = d.outputs[BRAKE]['brake_release_aout_v']
                self.visits_completed += 1
                self._emit({
                    'event': 'sweep_visit_complete',
                    'visit_index': visit_index,
                    'trial_id': trial_id,
                    'bus_target_v': bus_v,
                    'measured_bus_v': context.measured_bus_v,
                    'acked_aout_v': context.acked_aout_v,
                    'physical_voltage_established': context.measured_bus_v is not None,
                })
            status = TERMINAL_COMPLETE
            reason = None
            d.completed += 1
            d.interrupted_cycle = False
        except SweepCancelled as exc:
            status = TERMINAL_CANCELLED
            reason = str(exc)
            self._emit({'event': 'sweep_cancelled', 'reason': reason,
                        'software_stop_is_not_estop': True,
                        'visits_completed': self.visits_completed})
            failures = self._apply_cancel_policies()
            # Successful cleanup only when every policy succeeded; else remain interrupted/unknown.
            d.interrupted_cycle = bool(failures)
        except Exception as exc:
            status = TERMINAL_FAULT
            reason = f'{type(exc).__name__}: {exc}'
            self._emit({'event': 'sweep_fault', 'error_type': type(exc).__name__,
                        'error': str(exc), 'visits_completed': self.visits_completed,
                        'no_auto_resume': True})
            failures = self._apply_cancel_policies()
            d.interrupted_cycle = True
            if failures:
                self._emit({'event': 'sweep_fault_cleanup_incomplete', 'failures': failures,
                            'successful_cleanup': False})
            raise
        finally:
            d.sweep_context = None
            d.active_profile = d.active_role = None
            d.active_roles = ()
            self._emit({
                'event': 'sweep_finished',
                'status': status,
                'terminal_reason': reason,
                'visits_completed': self.visits_completed,
                'cleanup_failures': list(self.cleanup_failures),
                'distinguished': {
                    'cancel': status == TERMINAL_CANCELLED,
                    'fault': status == TERMINAL_FAULT,
                    'complete': status == TERMINAL_COMPLETE,
                },
            })

        return SweepResult(
            status=status,
            run_id=self.run_id,
            mode=self.config.mode,
            visits_completed=self.visits_completed,
            terminal_reason=reason,
            events=list(self.events),
            cleanup_failures=tuple(self.cleanup_failures),
        )


def run_standalone_fast_regen(dispatcher, fast_regen: FastRegenConfig, assignment_name: str,
                              *, stop_trigger: str | None = None,
                              cancel_check: Callable[[], bool] | None = None,
                              sleep: Callable[[float], None] | None = None) -> dict:
    """Assignable Fast Regen Test without voltage sweeping (uses current supply demand)."""
    if MOTOR not in dispatcher.outputs or BRAKE not in dispatcher.outputs:
        raise ConfigError('Fast Regen Test requires vesc_throttle and vesc_brake')
    if SUPPLY not in dispatcher.outputs:
        raise ConfigError('Fast Regen Test requires rzxa_voltage_demand (motor supply prerequisite)')
    runner = MotorSweepRunner(
        dispatcher,
        MotorSweepConfig(mode='open_loop', fast_regen=fast_regen, settle_s=0.0),
        assignment_name,
        stop_trigger=stop_trigger,
        cancel_check=cancel_check,
        sleep=sleep,
    )
    dispatcher.active_profile = assignment_name
    dispatcher.active_role = MOTOR
    dispatcher.active_roles = (SUPPLY, MOTOR, BRAKE)
    dispatcher.interrupted_cycle = True
    dispatcher.started += 1
    try:
        dispatcher.motor_neutral()
        dispatcher.brake_released()
        # Respect configured motor supply prerequisite (discrete/scalar grid), not merely
        # any readable RZ-XA demand. Check before sweep_context is set so the grid guard runs.
        class _StandaloneProfile:
            requires = {}
            output = MOTOR

        dispatcher.sweep_context = None
        dispatcher.motor_interlocks(_StandaloneProfile())
        supply_before = dispatcher.verify(SUPPLY)
        context = SweepContext(
            assignment=assignment_name, mode='open_loop',
            bus_target_v=aout_to_bus_safe(supply_before, dispatcher),
            expected_aout_v=supply_before,
            acked_aout_v=supply_before,
            measured_bus_v=None,
            feedback_policy='open_loop',
            direction='none',
            visit_index=0,
            round_trip_index=0,
            motor_phase='idle',
            run_id=runner.run_id,
            trial_id=uuid.uuid4().hex,
        )
        dispatcher.sweep_context = context
        result = runner._run_fast_regen(context)
        dispatcher.motor_neutral()
        dispatcher.brake_released()
        # Preserve last RZ-XA demand (verify unchanged after pattern).
        supply_after = dispatcher.verify(SUPPLY)
        if not math.isclose(supply_after, supply_before, abs_tol=1e-6, rel_tol=0):
            raise SweepFault('RZ-XA demand changed during standalone Fast Regen')
        dispatcher.completed += 1
        dispatcher.interrupted_cycle = False
        runner._emit({'event': 'fast_regen_test_complete', **result,
                      'supply_aout_v': supply_before, 'rzxa_demand_preserved': True})
        return {'status': TERMINAL_COMPLETE, 'run_id': runner.run_id, **result,
                'supply_aout_v': supply_before}
    except SweepCancelled as exc:
        failures = runner._apply_cancel_policies()
        dispatcher.interrupted_cycle = bool(failures)
        return {'status': TERMINAL_CANCELLED, 'run_id': runner.run_id, 'reason': str(exc),
                'cleanup_failures': failures, 'software_stop_is_not_estop': True}
    except Exception as exc:
        # Independent fault cleanup for non-cancel exceptions (same policies as sweep).
        runner._emit({
            'event': 'fast_regen_test_fault', 'error_type': type(exc).__name__,
            'error': str(exc), 'no_auto_resume': True,
        })
        failures = runner._apply_cancel_policies()
        dispatcher.interrupted_cycle = True
        if failures:
            runner._emit({
                'event': 'fast_regen_fault_cleanup_incomplete', 'failures': failures,
                'successful_cleanup': False,
            })
        return {
            'status': TERMINAL_FAULT, 'run_id': runner.run_id,
            'reason': f'{type(exc).__name__}: {exc}',
            'cleanup_failures': failures, 'interrupted': True,
            'no_auto_resume': True,
        }
    finally:
        dispatcher.sweep_context = None
        dispatcher.active_profile = dispatcher.active_role = None
        dispatcher.active_roles = ()


def aout_to_bus_safe(aout_v, dispatcher) -> float:
    gain = float(dispatcher.outputs[SUPPLY].get('mapping', {}).get(
        'gain_bus_v_per_aout_v', VERIFIED_GAIN))
    return float(aout_v) * gain
