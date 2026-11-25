"""Fast Regen accel+brake pattern (~4.35 s nominal) for sweeps and standalone tests.

Sequential throttle/brake mutual exclusion is encoded in the MultiOutputPattern:
drive returns to neutral before brake demand; brake returns to release before completion.
RZ-XA supply setpoint is intentionally not part of this pattern (held by the sweep runner).
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ConfigError
from .pattern_segments import MultiOutputPattern, Segment
from .schema import BRAKE, MOTOR, number

# Nominal defaults from the commissioning brief (all configurable).
DEFAULT_THROTTLE_TARGET_V = 1.6
DEFAULT_ACCEL_RAMP_S = 0.25
DEFAULT_ACCEL_HOLD_S = 1.5
DEFAULT_THROTTLE_RELEASE_S = 0.15
DEFAULT_TRANSITION_S = 0.10
DEFAULT_BRAKE_RAMP_S = 0.20
DEFAULT_BRAKE_HOLD_S = 1.5
DEFAULT_BRAKE_RELEASE_S = 0.15
DEFAULT_RECOVERY_S = 0.50


@dataclass(frozen=True)
class FastRegenConfig:
    throttle_target_v: float = DEFAULT_THROTTLE_TARGET_V
    brake_target_v: float = 0.20
    accel_ramp_s: float = DEFAULT_ACCEL_RAMP_S
    accel_hold_s: float = DEFAULT_ACCEL_HOLD_S
    throttle_release_s: float = DEFAULT_THROTTLE_RELEASE_S
    transition_s: float = DEFAULT_TRANSITION_S
    brake_ramp_s: float = DEFAULT_BRAKE_RAMP_S
    brake_hold_s: float = DEFAULT_BRAKE_HOLD_S
    brake_release_s: float = DEFAULT_BRAKE_RELEASE_S
    recovery_s: float = DEFAULT_RECOVERY_S
    # Optional RPM targets for full closed-loop phase early-termination (placeholders OK).
    accel_rpm_target: float | None = None
    brake_rpm_target: float | None = None
    rpm_kind: str = 'mechanical'  # mechanical | electrical
    pole_pairs: int | None = None

    @property
    def nominal_duration_s(self) -> float:
        return (
            self.accel_ramp_s + self.accel_hold_s + self.throttle_release_s + self.transition_s
            + self.brake_ramp_s + self.brake_hold_s + self.brake_release_s + self.recovery_s
        )

    def phase_schedule(self) -> tuple[tuple[str, float], ...]:
        return (
            ('accel_ramp', self.accel_ramp_s),
            ('accel_hold', self.accel_hold_s),
            ('throttle_release', self.throttle_release_s),
            ('transition', self.transition_s),
            ('brake_ramp', self.brake_ramp_s),
            ('brake_hold', self.brake_hold_s),
            ('brake_release', self.brake_release_s),
            ('recovery', self.recovery_s),
        )


def parse_fast_regen(raw: dict | None, *, motor_max_v: float, brake_max_v: float,
                     brake_release_v: float = 0.0) -> FastRegenConfig:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ConfigError('fast_regen must be an object')
    allowed = {
        'throttle_target_v', 'brake_target_v', 'accel_ramp_s', 'accel_hold_s',
        'throttle_release_s', 'transition_s', 'brake_ramp_s', 'brake_hold_s',
        'brake_release_s', 'recovery_s', 'accel_rpm_target', 'brake_rpm_target',
        'rpm_kind', 'pole_pairs',
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f'fast_regen: unknown keys {sorted(unknown)}')

    def duration(name, default):
        value = number(raw.get(name, default), name, 0.0, 300)
        return value

    throttle = number(raw.get('throttle_target_v', DEFAULT_THROTTLE_TARGET_V),
                      'throttle_target_v', 0.0, motor_max_v)
    brake = number(raw.get('brake_target_v', 0.20), 'brake_target_v', brake_release_v, brake_max_v)
    rpm_kind = raw.get('rpm_kind', 'mechanical')
    if rpm_kind not in {'mechanical', 'electrical'}:
        raise ConfigError("fast_regen.rpm_kind must be 'mechanical' or 'electrical'")
    accel_rpm = raw.get('accel_rpm_target')
    brake_rpm = raw.get('brake_rpm_target')
    if accel_rpm is not None:
        accel_rpm = number(accel_rpm, 'accel_rpm_target', 0, 1_000_000)
    if brake_rpm is not None:
        brake_rpm = number(brake_rpm, 'brake_rpm_target', 0, 1_000_000)
    pole_pairs = raw.get('pole_pairs')
    if pole_pairs is not None:
        if type(pole_pairs) is not int or pole_pairs < 1:
            raise ConfigError('pole_pairs must be a positive integer when set')
    return FastRegenConfig(
        throttle_target_v=throttle,
        brake_target_v=brake,
        accel_ramp_s=duration('accel_ramp_s', DEFAULT_ACCEL_RAMP_S),
        accel_hold_s=duration('accel_hold_s', DEFAULT_ACCEL_HOLD_S),
        throttle_release_s=duration('throttle_release_s', DEFAULT_THROTTLE_RELEASE_S),
        transition_s=duration('transition_s', DEFAULT_TRANSITION_S),
        brake_ramp_s=duration('brake_ramp_s', DEFAULT_BRAKE_RAMP_S),
        brake_hold_s=duration('brake_hold_s', DEFAULT_BRAKE_HOLD_S),
        brake_release_s=duration('brake_release_s', DEFAULT_BRAKE_RELEASE_S),
        recovery_s=duration('recovery_s', DEFAULT_RECOVERY_S),
        accel_rpm_target=accel_rpm,
        brake_rpm_target=brake_rpm,
        rpm_kind=rpm_kind,
        pole_pairs=pole_pairs,
    )


def _append_hold(segments_by_role, elapsed, duration, values, phase):
    """Append a hold (or zero-duration boundary marker). Zero duration advances phase only."""
    end = elapsed + duration
    for role, value in values.items():
        segments_by_role[role].append(Segment(elapsed, end, value, value, phase))
    return end


def _append_ramp(segments_by_role, elapsed, duration, role, start_v, end_v, others, phase):
    """Ramp one role; others hold. Zero-duration ramp jumps to end_v (no divide-by-zero)."""
    end = elapsed + duration
    for owned, value in others.items():
        if owned == role:
            # Zero-duration: start_v==end_v in the segment bookkeeping; value_at returns end_v.
            seg_start = end_v if duration <= 0 else start_v
            segments_by_role[owned].append(Segment(elapsed, end, seg_start, end_v, phase))
        else:
            segments_by_role[owned].append(Segment(elapsed, end, value, value, phase))
    return end


def build_fast_regen_pattern(config: FastRegenConfig, *, name: str = 'fast_regen',
                             motor_neutral_v: float = 0.0,
                             brake_release_v: float = 0.0) -> MultiOutputPattern:
    """Compile the mutual-exclusion Fast Regen MultiOutputPattern."""
    motor_segs: list[Segment] = []
    brake_segs: list[Segment] = []
    segments = {MOTOR: motor_segs, BRAKE: brake_segs}
    elapsed = 0.0
    drive = motor_neutral_v
    brake = brake_release_v

    # 1) Accel ramp
    elapsed = _append_ramp(segments, elapsed, config.accel_ramp_s, MOTOR,
                           drive, config.throttle_target_v,
                           {MOTOR: drive, BRAKE: brake}, 'accel_ramp')
    drive = config.throttle_target_v
    # 2) Accel hold
    elapsed = _append_hold(segments, elapsed, config.accel_hold_s,
                           {MOTOR: drive, BRAKE: brake}, 'accel_hold')
    # 3) Throttle release to neutral
    elapsed = _append_ramp(segments, elapsed, config.throttle_release_s, MOTOR,
                           drive, motor_neutral_v,
                           {MOTOR: drive, BRAKE: brake}, 'throttle_release')
    drive = motor_neutral_v
    # 4) Transition (both safe)
    elapsed = _append_hold(segments, elapsed, config.transition_s,
                           {MOTOR: drive, BRAKE: brake}, 'transition')
    # 5) Brake ramp
    elapsed = _append_ramp(segments, elapsed, config.brake_ramp_s, BRAKE,
                           brake, config.brake_target_v,
                           {MOTOR: drive, BRAKE: brake}, 'brake_ramp')
    brake = config.brake_target_v
    # 6) Brake hold
    elapsed = _append_hold(segments, elapsed, config.brake_hold_s,
                           {MOTOR: drive, BRAKE: brake}, 'brake_hold')
    # 7) Brake release
    elapsed = _append_ramp(segments, elapsed, config.brake_release_s, BRAKE,
                           brake, brake_release_v,
                           {MOTOR: drive, BRAKE: brake}, 'brake_release')
    brake = brake_release_v
    # 8) Recovery
    elapsed = _append_hold(segments, elapsed, config.recovery_s,
                           {MOTOR: drive, BRAKE: brake}, 'recovery')

    if not motor_segs or not brake_segs:
        raise ConfigError('Fast Regen pattern produced empty segments')
    return MultiOutputPattern(name, (MOTOR, BRAKE), {
        MOTOR: tuple(motor_segs),
        BRAKE: tuple(brake_segs),
    })


def electrical_to_mechanical_rpm(electrical_rpm: float, pole_pairs: int) -> float:
    if pole_pairs < 1:
        raise ConfigError('pole_pairs must be >= 1')
    return electrical_rpm / pole_pairs


def mechanical_to_electrical_rpm(mechanical_rpm: float, pole_pairs: int) -> float:
    if pole_pairs < 1:
        raise ConfigError('pole_pairs must be >= 1')
    return mechanical_rpm * pole_pairs
