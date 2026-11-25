"""Autonomous bidirectional regenerative motor characterization sweeps.

Runs on the existing Dispatcher SCPI/write path (no separate engine). Modes:

1. open_loop — fixed settle after AOUT ack; no measurement/RPM requirements
2. voltage_closed_loop — supervisory measured-bus qualification (not PID / not
   require_controller().correct())
3. full_closed_loop — voltage qualification + RPM-governed phase early-exit

Cooperative Stop Sweep cancellation is polled during waits and pattern playback.
"""

from __future__ import annotations

import math
import uuid
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
    poll_s: float = 0.020  # default 20 ms; schema/config floor 10 ms
    max_age_s: float = 0.250
    # Optional dynamic envelope during drive (explicit only; never invented).
    drive_envelope: dict | None = None


@dataclass(frozen=True)
class RpmFeedbackConfig:
    channel: str = 'erpm'
    kind: str = 'electrical'  # electrical measurement binding; convert via pole_pairs
    pole_pairs: int | None = None
    poll_s: float = 0.020  # default 20 ms; schema/config floor 10 ms
    max_age_s: float = 0.250
    accel_timeout_s: float = 5.0
    brake_timeout_s: float = 5.0


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


def parse_voltage_qualify(raw: dict | None) -> VoltageQualifyConfig | None:
    if raw is None:
        return None
    keys(raw, {
        'channel', 'tolerance_v', 'qualify_duration_s', 'timeout_s', 'poll_s', 'max_age_s',
        'drive_envelope',
    }, 'voltage_qualify')
    envelope = raw.get('drive_envelope')
    if envelope is not None:
        keys(envelope, {'tolerance_v', 'max_age_s'}, 'drive_envelope')
        envelope = {
            'tolerance_v': number(envelope['tolerance_v'], 'drive_envelope.tolerance_v', 0.01, 5),
            'max_age_s': number(envelope['max_age_s'], 'drive_envelope.max_age_s', 0.01, 1),
        }
    return VoltageQualifyConfig(
        channel=str(raw.get('channel', 'dc_bus_voltage')),
        tolerance_v=number(raw.get('tolerance_v', 0.10), 'tolerance_v', 0.01, 5),
        qualify_duration_s=number(raw.get('qualify_duration_s', 0.20), 'qualify_duration_s', 0.0, 60),
        timeout_s=number(raw.get('timeout_s', 3.0), 'timeout_s', 0.05, 120),
        poll_s=number(raw.get('poll_s', 0.020), 'poll_s', 0.01, 1),
        max_age_s=number(raw.get('max_age_s', 0.250), 'max_age_s', 0.01, 1),
        drive_envelope=envelope,
    )


def parse_rpm_feedback(raw: dict | None) -> RpmFeedbackConfig | None:
    if raw is None:
        return None
    keys(raw, {
        'channel', 'kind', 'pole_pairs', 'poll_s', 'max_age_s',
        'accel_timeout_s', 'brake_timeout_s',
    }, 'rpm_feedback')
    kind = raw.get('kind', 'electrical')
    if kind not in {'electrical', 'mechanical'}:
        raise ConfigError("rpm_feedback.kind must be 'electrical' or 'mechanical'")
    pole_pairs = raw.get('pole_pairs')
    if pole_pairs is not None and (type(pole_pairs) is not int or pole_pairs < 1):
        raise ConfigError('rpm_feedback.pole_pairs must be a positive integer when set')
    return RpmFeedbackConfig(
        channel=str(raw.get('channel', 'erpm')),
        kind=kind,
        pole_pairs=pole_pairs,
        poll_s=number(raw.get('poll_s', 0.020), 'rpm poll_s', 0.01, 1),
        max_age_s=number(raw.get('max_age_s', 0.250), 'rpm max_age_s', 0.01, 1),
        accel_timeout_s=number(raw.get('accel_timeout_s', 5.0), 'accel_timeout_s', 0.1, 120),
        brake_timeout_s=number(raw.get('brake_timeout_s', 5.0), 'brake_timeout_s', 0.1, 120),
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
            # Placeholder binding — physical operation needs operator RPM targets.
            rpm = RpmFeedbackConfig()
        if fast.accel_rpm_target is None or fast.brake_rpm_target is None:
            # Allowed for offline/schema; full mode fails closed at runtime if targets missing
            # when physically needing RPM governance — tests inject targets.
            pass

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
    # Validate grid early (rejects incompatible spans).
    points = generate_voltage_points(start_v, end_v, step_v)
    if not continuous:
        visit_count(point_count=len(points), round_trips=round_trips, continuous=False)

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

    def as_dict(self) -> dict:
        return {
            'status': self.status,
            'run_id': self.run_id,
            'mode': self.mode,
            'visits_completed': self.visits_completed,
            'terminal_reason': self.terminal_reason,
            'software_stop_is_not_estop': True,
        }


class MotorSweepRunner:
    def __init__(self, dispatcher, config: MotorSweepConfig, assignment_name: str, *,
                 stop_trigger: str | None = None,
                 cancel_check: Callable[[], bool] | None = None):
        self.dispatcher = dispatcher
        self.config = config
        self.assignment = assignment_name
        self.stop_trigger = stop_trigger
        self._external_cancel = cancel_check
        self._cancel_requested = False
        self.run_id = uuid.uuid4().hex
        self.visits_completed = 0
        self.events: list[dict] = []

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
            try:
                if self.dispatcher.backend.read_button(self.stop_trigger) is True:
                    self._cancel_requested = True
                    return True
            except Exception:
                # Button read failures during cancel poll are reported by the wait path.
                raise
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

    def _wait(self, seconds: float, *, phase: str):
        """Cancellation-aware wait using the dispatcher clock/wait hooks."""
        if seconds <= 0:
            self._raise_if_cancelled(phase)
            return
        deadline = self.dispatcher.clock() + seconds
        poll = min(0.05, seconds)
        while True:
            self._raise_if_cancelled(phase)
            now = self.dispatcher.clock()
            if now >= deadline:
                return
            self.dispatcher.wait(min(poll, deadline - now))

    def _command_supply(self, bus_v: float, context: SweepContext):
        aout = bus_to_aout(bus_v, gain=self._supply_gain())
        spec = self.dispatcher.outputs[SUPPLY]
        aout = number(aout, 'sweep supply demand', spec['min_aout_v'], spec['max_aout_v'])
        self._raise_if_cancelled('supply_command')
        # Neutral/release verified before bus change.
        self.dispatcher.motor_neutral()
        self.dispatcher.brake_released()
        self.dispatcher.command(SUPPLY, aout)
        acked = self.dispatcher.verify(SUPPLY, aout)
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

    def _supply_gain(self) -> float:
        mapping = self.dispatcher.outputs[SUPPLY].get('mapping', {})
        return float(mapping.get('gain_bus_v_per_aout_v', VERIFIED_GAIN))

    def _qualify_voltage(self, context: SweepContext):
        cfg = self.config.voltage_qualify
        assert cfg is not None
        started = self.dispatcher.clock()
        in_band_since = None
        self._emit({
            'event': 'sweep_voltage_qualify_begin',
            'bus_target_v': context.bus_target_v,
            'tolerance_v': cfg.tolerance_v,
            'qualify_duration_s': cfg.qualify_duration_s,
            'timeout_s': cfg.timeout_s,
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
            measured = float(sample)
            context.measured_bus_v = measured
            in_band = abs(measured - context.bus_target_v) <= cfg.tolerance_v
            now = self.dispatcher.clock()
            if in_band:
                if in_band_since is None:
                    in_band_since = now
                elif now - in_band_since >= cfg.qualify_duration_s:
                    self._emit({
                        'event': 'sweep_voltage_qualified',
                        'bus_target_v': context.bus_target_v,
                        'measured_bus_v': measured,
                        'qualify_elapsed_s': now - in_band_since,
                        'physical_voltage_established': True,
                        'supervisory_only': True,
                    })
                    return measured
            else:
                in_band_since = None
            self._wait(cfg.poll_s, phase='voltage_qualify')

    def _mechanical_rpm(self, raw_value: float) -> float:
        cfg = self.config.rpm_feedback
        assert cfg is not None
        if cfg.kind == 'mechanical':
            return raw_value
        # electrical / ERPM binding
        if self.config.fast_regen.rpm_kind == 'electrical' and cfg.kind == 'electrical':
            # Compare in electrical domain if targets are electrical.
            if self.config.fast_regen.pole_pairs is None and cfg.pole_pairs is None:
                return raw_value
        pairs = self.config.fast_regen.pole_pairs or cfg.pole_pairs
        if self.config.fast_regen.rpm_kind == 'mechanical':
            if pairs is None:
                raise SweepFault('pole_pairs required to convert electrical RPM to mechanical')
            return raw_value / pairs
        return raw_value

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
        # Supply demand must remain unchanged during the drive event.
        supply_before = self.dispatcher.verify(SUPPLY)
        began = self.dispatcher.clock()
        phase_started = began
        last_phase = None
        actual_phase_durations: dict[str, float] = {}

        use_rpm = (
            self.config.mode == 'full_closed_loop'
            and self.config.rpm_feedback is not None
            and self.config.fast_regen.accel_rpm_target is not None
            and self.config.fast_regen.brake_rpm_target is not None
        )

        if use_rpm:
            result = self._run_fast_regen_rpm_governed(pattern, context, supply_before)
        else:
            if self.config.mode == 'full_closed_loop' and (
                    self.config.fast_regen.accel_rpm_target is None
                    or self.config.fast_regen.brake_rpm_target is None):
                raise SweepFault(
                    'full_closed_loop requires accel_rpm_target and brake_rpm_target; '
                    'no silent fallback to timed open-loop')
            result = self._run_pattern_timed(pattern, context, supply_before)

        # Log actual durations from progress events if present
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
        sent_boundaries = set()
        phase_marks: dict[str, float] = {}

        def apply_values(values_by_role):
            self._raise_if_cancelled(context.motor_phase or 'fast_regen')
            # Never advance/change supply mid drive/brake.
            if not math.isclose(d.verify(SUPPLY), supply_before, abs_tol=1e-6, rel_tol=0):
                raise SweepFault('RZ-XA demand changed during Fast Regen; refusing to continue')
            for role in pattern.roles:
                d.verify(role)
            for role in pattern.roles:
                target = values_by_role[role]
                prior = last_values[role]
                if prior is None or not math.isclose(prior, target, abs_tol=1e-12, rel_tol=0):
                    d.command(role, target)
                    last_values[role] = target

        def verify(values_by_role):
            for role, target in values_by_role.items():
                d.verify(role, target)
            if not math.isclose(d.verify(SUPPLY), supply_before, abs_tol=1e-6, rel_tol=0):
                raise SweepFault('RZ-XA demand changed during Fast Regen verify')

        def progress(record):
            phase = record.get('phase')
            if phase and phase != context.motor_phase:
                now = d.clock()
                if context.motor_phase and context.motor_phase in phase_marks:
                    actual_phase_durations = now - phase_marks[context.motor_phase]
                    self._emit({
                        'event': 'sweep_phase_duration',
                        'phase': context.motor_phase,
                        'actual_duration_s': actual_phase_durations,
                    })
                context.motor_phase = phase
                phase_marks[phase] = now
                self._emit({'event': 'sweep_phase', 'phase': phase,
                            'elapsed_s': record.get('elapsed_s'),
                            'trial_id': context.trial_id})
            # Never skip mandatory phase boundaries under lateness — timeline raises.
            d.emit({**record, 'assignment': self.assignment, 'trial_id': context.trial_id,
                    'sweep_mode': self.config.mode})

        # Patch wait to be cancel-aware during timeline sleeps.
        original_wait = d.wait

        def cancel_wait(seconds):
            self._wait(seconds, phase=context.motor_phase or 'fast_regen')

        d.wait = cancel_wait
        try:
            if len(pattern.roles) > 1:
                result = d._run_multi_timeline(pattern, apply_values, verify, progress)
            else:
                raise ControlError('Fast Regen requires motor+brake coordinated pattern')
        finally:
            d.wait = original_wait
        return result

    def _run_fast_regen_rpm_governed(self, pattern: MultiOutputPattern, context: SweepContext,
                                     supply_before: float) -> dict:
        """Phase transitions governed by RPM targets; early-terminate holds; timeouts fail closed."""
        d = self.dispatcher
        cfg = self.config.rpm_feedback
        fr = self.config.fast_regen
        assert cfg is not None
        neutral = d.outputs[MOTOR]['neutral_aout_v']
        release = d.outputs[BRAKE]['brake_release_aout_v']
        started = d.clock()
        schedule = list(fr.phase_schedule())

        def ensure_supply():
            if not math.isclose(d.verify(SUPPLY), supply_before, abs_tol=1e-6, rel_tol=0):
                raise SweepFault('RZ-XA demand changed during RPM-governed Fast Regen')

        def set_outputs(drive_v, brake_v, phase):
            self._raise_if_cancelled(phase)
            ensure_supply()
            context.motor_phase = phase
            # Mutual exclusion: never drive and brake together.
            if drive_v != neutral and brake_v != release:
                raise SweepFault('Drive/brake mutual exclusion violated')
            if brake_v != release:
                d.command(MOTOR, neutral)
                d.command(BRAKE, brake_v)
            elif drive_v != neutral:
                d.command(BRAKE, release)
                d.command(MOTOR, drive_v)
            else:
                d.command(MOTOR, neutral)
                d.command(BRAKE, release)

        def read_rpm():
            raw = d.sample_supervisory(cfg.channel, max_age=cfg.max_age_s)
            return self._mechanical_rpm(float(raw))

        def ramp(role, start_v, end_v, duration_s, phase, other_safe):
            if duration_s <= 0:
                if role == MOTOR:
                    set_outputs(end_v, other_safe, phase)
                else:
                    set_outputs(other_safe, end_v, phase)
                return
            began = d.clock()
            while True:
                self._raise_if_cancelled(phase)
                ensure_supply()
                elapsed = d.clock() - began
                if elapsed >= duration_s:
                    frac = 1.0
                else:
                    frac = elapsed / duration_s
                value = start_v + (end_v - start_v) * frac
                if role == MOTOR:
                    set_outputs(value, other_safe, phase)
                else:
                    set_outputs(other_safe, value, phase)
                if frac >= 1.0:
                    return
                self._wait(d.config.raw['timing']['interval_ms'] / 1000, phase=phase)

        def hold_until_rpm(drive_v, brake_v, phase, target_rpm, timeout_s, compare):
            began = d.clock()
            set_outputs(drive_v, brake_v, phase)
            while True:
                self._raise_if_cancelled(phase)
                ensure_supply()
                rpm = read_rpm()
                self._emit({'event': 'sweep_rpm_sample', 'phase': phase, 'rpm': rpm,
                            'target_rpm': target_rpm, 'trial_id': context.trial_id})
                if compare(rpm, target_rpm):
                    self._emit({'event': 'sweep_rpm_target_reached', 'phase': phase,
                                'rpm': rpm, 'target_rpm': target_rpm})
                    return
                if d.clock() - began > timeout_s:
                    raise SweepFault(
                        f'RPM target not reached in {phase} within {timeout_s}s '
                        f'(rpm={rpm}, target={target_rpm}); no silent timed fallback')
                self._wait(cfg.poll_s, phase=phase)

        # Execute schedule with RPM early-exit on holds.
        ramp(MOTOR, neutral, fr.throttle_target_v, fr.accel_ramp_s, 'accel_ramp', release)
        hold_until_rpm(fr.throttle_target_v, release, 'accel_hold', fr.accel_rpm_target,
                       cfg.accel_timeout_s, lambda rpm, tgt: rpm >= tgt)
        ramp(MOTOR, fr.throttle_target_v, neutral, fr.throttle_release_s, 'throttle_release', release)
        self._wait(fr.transition_s, phase='transition')
        ramp(BRAKE, release, fr.brake_target_v, fr.brake_ramp_s, 'brake_ramp', neutral)
        hold_until_rpm(neutral, fr.brake_target_v, 'brake_hold', fr.brake_rpm_target,
                       cfg.brake_timeout_s, lambda rpm, tgt: rpm <= tgt)
        ramp(BRAKE, fr.brake_target_v, release, fr.brake_release_s, 'brake_release', neutral)
        self._wait(fr.recovery_s, phase='recovery')
        ensure_supply()
        return {
            'actual_elapsed_s': d.clock() - started,
            'nominal_duration_s': pattern.duration_s,
            'rpm_governed': True,
            'schedule_phases': [name for name, _ in schedule],
        }

    def _apply_cancel_policies(self):
        d = self.dispatcher
        try:
            if MOTOR in d.outputs:
                d.command(MOTOR, d.outputs[MOTOR]['neutral_aout_v'])
        except Exception as exc:
            self._emit({'event': 'sweep_cancel_motor_failed', 'error': str(exc)})
            raise
        try:
            if BRAKE in d.outputs:
                d.command(BRAKE, d.outputs[BRAKE]['brake_release_aout_v'])
        except Exception as exc:
            self._emit({'event': 'sweep_cancel_brake_failed', 'error': str(exc)})
            raise
        # Preserve last RZ-XA demand (no auto bus collapse to 0).
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
                self._emit({'event': 'sweep_cancel_supply_unknown', 'error': str(exc)})
                raise

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
                # Complete recovery (motor neutral + brake release) before advance.
                d.motor_neutral()
                d.brake_released()
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
            self._apply_cancel_policies()
            d.interrupted_cycle = False
        except Exception as exc:
            status = TERMINAL_FAULT
            reason = f'{type(exc).__name__}: {exc}'
            self._emit({'event': 'sweep_fault', 'error_type': type(exc).__name__,
                        'error': str(exc), 'visits_completed': self.visits_completed,
                        'no_auto_resume': True})
            try:
                self._apply_cancel_policies()
            except Exception as cleanup_exc:
                self._emit({'event': 'sweep_fault_cleanup_failed',
                            'error': str(cleanup_exc)})
            # Fault leaves interrupted_cycle True — no auto-resume.
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
        )


def run_standalone_fast_regen(dispatcher, fast_regen: FastRegenConfig, assignment_name: str,
                              *, stop_trigger: str | None = None,
                              cancel_check: Callable[[], bool] | None = None) -> dict:
    """Assignable Fast Regen Test without voltage sweeping (uses current supply demand)."""
    if MOTOR not in dispatcher.outputs or BRAKE not in dispatcher.outputs:
        raise ConfigError('Fast Regen Test requires vesc_throttle and vesc_brake')
    runner = MotorSweepRunner(
        dispatcher,
        MotorSweepConfig(mode='open_loop', fast_regen=fast_regen, settle_s=0.0),
        assignment_name,
        stop_trigger=stop_trigger,
        cancel_check=cancel_check,
    )
    # Minimal path: just the pattern at the current bus demand.
    dispatcher.active_profile = assignment_name
    dispatcher.active_role = MOTOR
    dispatcher.active_roles = (MOTOR, BRAKE)
    dispatcher.interrupted_cycle = True
    dispatcher.started += 1
    try:
        dispatcher.motor_neutral()
        dispatcher.brake_released()
        if SUPPLY in dispatcher.outputs and dispatcher.config.raw['interlocks'].get(
                'require_supply_demand_before_motor'):
            # Use existing interlock path; supply already at operator demand.
            pass
        supply_before = dispatcher.verify(SUPPLY) if SUPPLY in dispatcher.outputs else None
        context = SweepContext(
            assignment=assignment_name, mode='open_loop',
            bus_target_v=aout_to_bus_safe(supply_before, dispatcher) if supply_before is not None else 0.0,
            expected_aout_v=supply_before or 0.0,
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
        dispatcher.completed += 1
        dispatcher.interrupted_cycle = False
        runner._emit({'event': 'fast_regen_test_complete', **result})
        return {'status': TERMINAL_COMPLETE, 'run_id': runner.run_id, **result}
    except SweepCancelled as exc:
        runner._apply_cancel_policies()
        dispatcher.interrupted_cycle = False
        return {'status': TERMINAL_CANCELLED, 'run_id': runner.run_id, 'reason': str(exc)}
    finally:
        dispatcher.sweep_context = None
        dispatcher.active_profile = dispatcher.active_role = None
        dispatcher.active_roles = ()


def aout_to_bus_safe(aout_v, dispatcher) -> float:
    gain = float(dispatcher.outputs[SUPPLY].get('mapping', {}).get(
        'gain_bus_v_per_aout_v', VERIFIED_GAIN))
    return float(aout_v) * gain
