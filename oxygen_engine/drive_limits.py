"""Commissioned drive-demand limit resolution (not an automatic raise to full scale)."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import AuthorizationError, ConfigError
from .schema import ABSOLUTE_AOUT_MAX_V, MOTOR, MOTOR_EXAMPLE_MAX_V, number


DEFAULT_COMMISSIONED_DRIVE_LIMIT_V = MOTOR_EXAMPLE_MAX_V
PLANNING_CANDIDATES_V = (0.40, 0.55, 0.70, 0.85)


@dataclass(frozen=True)
class DriveLimitDecision:
    effective_max_v: float
    dispatcher_max_v: float
    experiment_commissioned_v: float
    higher_authorized: bool
    reasons: tuple[str, ...]


def _candidate(entry, *, label: str) -> dict:
    if not isinstance(entry, dict):
        raise ConfigError(f'{label}: drive limit candidate must be an object')
    allowed = {'value_v', 'enabled', 'commissioned'}
    if set(entry) - allowed:
        raise ConfigError(f'{label}: unknown keys {sorted(set(entry) - allowed)}')
    value = number(entry.get('value_v'), f'{label} value_v', 0.001, ABSOLUTE_AOUT_MAX_V)
    if type(entry.get('enabled')) is not bool or type(entry.get('commissioned')) is not bool:
        raise ConfigError(f'{label}: enabled and commissioned must be Boolean')
    if entry['enabled'] and not entry['commissioned']:
        raise ConfigError(f'{label}: enabled targets must be commissioned')
    return {'value_v': value, 'enabled': entry['enabled'], 'commissioned': entry['commissioned']}


def validate_drive_limit_candidates(candidates) -> tuple[dict, ...]:
    if candidates is None:
        return tuple(
            {'value_v': value, 'enabled': value == DEFAULT_COMMISSIONED_DRIVE_LIMIT_V,
             'commissioned': value == DEFAULT_COMMISSIONED_DRIVE_LIMIT_V}
            for value in PLANNING_CANDIDATES_V
        )
    if not isinstance(candidates, list) or not candidates:
        raise ConfigError('drive_limit_candidates must be a nonempty list')
    parsed = tuple(_candidate(entry, label=f'candidate[{index}]') for index, entry in enumerate(candidates))
    values = [entry['value_v'] for entry in parsed]
    if len(set(values)) != len(values):
        raise ConfigError('drive_limit_candidates values must be unique')
    return parsed


def resolve_effective_drive_max(
    *,
    dispatcher_max_v: float,
    commissioned_drive_limit_v: float,
    higher_drive_limit_authorized: bool,
    requested_v: float | None = None,
    candidate_enabled: bool = True,
    candidate_commissioned: bool = True,
) -> DriveLimitDecision:
    """Effective max is the lowest applicable authorized bound. Never silently clamps."""
    dispatcher_max = number(dispatcher_max_v, 'dispatcher max_aout_v', 0.001, ABSOLUTE_AOUT_MAX_V)
    commissioned = number(
        commissioned_drive_limit_v, 'commissioned_drive_limit_v', 0.001, ABSOLUTE_AOUT_MAX_V)
    if type(higher_drive_limit_authorized) is not bool:
        raise ConfigError('higher_drive_limit_authorized must be Boolean')
    if type(candidate_enabled) is not bool or type(candidate_commissioned) is not bool:
        raise ConfigError('candidate enabled/commissioned must be Boolean')

    reasons: list[str] = []
    if commissioned > dispatcher_max + 1e-12:
        raise AuthorizationError(
            'commissioned_drive_limit_v exceeds dispatcher outputs.vesc_throttle.max_aout_v; '
            'raise the dispatcher clamp only after physical commissioning')
    if commissioned > DEFAULT_COMMISSIONED_DRIVE_LIMIT_V + 1e-12 and not higher_drive_limit_authorized:
        raise AuthorizationError(
            f'commissioned_drive_limit_v {commissioned} V requires higher_drive_limit_authorized=true')
    if not candidate_commissioned:
        raise AuthorizationError('Drive target is not commissioned')
    if not candidate_enabled:
        raise AuthorizationError('Drive target is disabled')

    effective = min(dispatcher_max, commissioned)
    reasons.append(f'dispatcher_max={dispatcher_max:.3f} V')
    reasons.append(f'commissioned_drive_limit={commissioned:.3f} V')
    if higher_drive_limit_authorized:
        reasons.append('higher_drive_limit_authorized=true')
    else:
        reasons.append('higher_drive_limit_authorized=false')

    if requested_v is not None:
        requested = number(requested_v, 'requested drive demand', 0.0, ABSOLUTE_AOUT_MAX_V)
        if requested > effective + 1e-12:
            raise AuthorizationError(
                f'Requested drive demand {requested} V exceeds effective authorized max '
                f'{effective} V ({"; ".join(reasons)}). Refusing before hardware session; '
                'no silent clamp.')
    return DriveLimitDecision(
        effective_max_v=effective,
        dispatcher_max_v=dispatcher_max,
        experiment_commissioned_v=commissioned,
        higher_authorized=higher_drive_limit_authorized,
        reasons=tuple(reasons),
    )


def authorize_dispatcher_motor_max(spec: dict, *, physical: bool) -> float:
    """Schema helper: allow >0.40 V only with explicit commissioning acknowledgement."""
    max_v = number(spec['max_aout_v'], 'maximum', 0.001, ABSOLUTE_AOUT_MAX_V)
    commissioned = spec.get('commissioned_drive_limit_v', DEFAULT_COMMISSIONED_DRIVE_LIMIT_V)
    commissioned = number(commissioned, 'commissioned_drive_limit_v', 0.001, ABSOLUTE_AOUT_MAX_V)
    higher = spec.get('higher_drive_limit_authorized', False)
    if type(higher) is not bool:
        raise ConfigError('higher_drive_limit_authorized must be Boolean')
    if max_v > commissioned + 1e-12:
        raise ConfigError('vesc_throttle.max_aout_v cannot exceed commissioned_drive_limit_v')
    if max_v > DEFAULT_COMMISSIONED_DRIVE_LIMIT_V + 1e-12:
        if not higher:
            raise ConfigError(
                'Raising vesc_throttle.max_aout_v above 0.40 V requires '
                'higher_drive_limit_authorized=true and a commissioned_drive_limit_v')
        if not physical and not higher:
            raise ConfigError('Simulated motor clamp remains 0.4 V unless explicitly authorized')
    elif higher and commissioned <= DEFAULT_COMMISSIONED_DRIVE_LIMIT_V + 1e-12:
        # Authorization flag without a raised commissioned limit is allowed but inert.
        pass
    return max_v


def motor_role_keys() -> set[str]:
    return {
        'role', 'channel', 'display_name', 'expected_id', 'channel_discovery_required', 'binding',
        'authorization', 'mode', 'source', 'const_property', 'min_aout_v', 'max_aout_v',
        'startup_aout_v', 'neutral_aout_v', 'neutral_verified', 'mapping_verified', 'mapping',
        'output_enable', 'completion_policy', 'on_cancel', 'on_fault', 'on_shutdown',
        'commissioned_drive_limit_v', 'higher_drive_limit_authorized',
    }


def assert_role_is_motor(role: str) -> None:
    if role != MOTOR:
        raise ConfigError('Drive limit commissioning applies only to vesc_throttle')
