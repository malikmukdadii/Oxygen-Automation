"""Schema version 3: one config for open-loop multi-AOUT dispatch (memory or OXYGEN SCPI)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .adapters.oxygen import OxygenAdapter
from .constant_pattern import finite_number
from .errors import AuthorizationError, ConfigError

ROLES = {
    'rzxa_voltage_demand': 'supply_voltage_demand',
    'vesc_throttle': 'motor_throttle',
    'vesc_brake': 'motor_brake',
}
SUPPLY = 'rzxa_voltage_demand'
MOTOR = 'vesc_throttle'
BRAKE = 'vesc_brake'
BRAKING_CONTROL_NAME = 'Braking Control'
# Absolute schema ceiling (typical OXYGEN ConstOutput span). Operator max_aout_v is authoritative
# within this bound and must still fit the discovered hardware range. Examples keep 0.500 V supply.
ABSOLUTE_AOUT_MAX_V = 10.0
MOTOR_EXAMPLE_MAX_V = 0.4  # simulated-motor default clamp (not a physical hardware raise)
VERIFIED_GAIN = 80.0  # 1.000 V AOUT = 80 V bus (verified documentation)
SUPPLY_DEMAND_GRID = tuple((36.0 + index * 0.5) / VERIFIED_GAIN for index in range(9))
SUPPLY_DEMAND_STEP = 0.5 / VERIFIED_GAIN
LIVE_FLAGS = {
    'enable_physical_aout', 'enable_physical_brake', 'enable_rzxa_output_toggle',
    'enable_real_rzxa_scpi', 'require_explicit_hardware_authorization',
}
OPEN_LOOP_PRECONDITIONS = {
    'motor_neutral', 'brake_released', 'current_aout_must_be_zero_or_already_target',
    'reject_other_start_values',
}
CLOSED_LOOP_PRECONDITIONS = OPEN_LOOP_PRECONDITIONS | {
    'supply_output_enabled_or_verified_external', 'supply_shutdown_permitted',
    'supply_standalone_test', 'bus_voltage_validated',
}
REQUIRES_OPEN = {'motor_neutral', 'motor_enable', 'brake_released'}
REQUIRES_CLOSED = REQUIRES_OPEN | {
    'supply_output_enable', 'measured_bus_feedback', 'no_unhandled_regeneration',
    'supply_standalone_test', 'measured_bus_voltage',
}
HARDWARE_ACKNOWLEDGEMENTS = {
    'wiring_verified', 'supply_mapping_verified', 'motor_neutral_verified',
    'brake_release_verified', 'external_emergency_stop_verified', 'external_enable_control_verified',
}
OUTPUT_ONLY_ACKNOWLEDGEMENTS = {
    'no_live_feedback_acknowledged', 'external_bus_and_regen_protection_verified',
}


def keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ConfigError(f'{label}: unknown keys or not an object; allowed: {sorted(allowed)}')


def number(value, label, lower, upper):
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ConfigError(f'{label} must be a JSON number, not a Boolean or numeric string')
    result = finite_number(value, label)
    if not lower <= result <= upper:
        raise ConfigError(f'{label} must be in [{lower}, {upper}]')
    return result


def exact_name(value, label):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n\x00'):
        raise ConfigError(f'{label}: configure an exact nonempty OXYGEN channel name')
    return value


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


@dataclass(frozen=True)
class Profile:
    path: Path
    name: str
    output: str
    outputs: tuple[str, ...]
    steps: tuple[dict, ...]
    completion: dict
    timeout_s: float
    requires: dict

    @property
    def coordinated(self) -> bool:
        return len(self.outputs) > 1


@dataclass(frozen=True)
class DispatcherConfig:
    path: Path
    raw: dict
    profiles: dict[str, Profile]
    motor_sweeps: dict = None  # name -> MotorSweepConfig
    fast_regens: dict = None  # name -> FastRegenConfig
    stop_sweep_trigger: str | None = None

    def __post_init__(self):
        # dataclasses with mutable defaults: normalize None to empty dicts
        object.__setattr__(self, 'motor_sweeps', dict(self.motor_sweeps or {}))
        object.__setattr__(self, 'fast_regens', dict(self.fast_regens or {}))

    @property
    def mode(self) -> str:
        return self.raw['control']['mode']

    @property
    def transport(self) -> str:
        return self.raw['transport']

    @property
    def open_loop(self) -> bool:
        return self.mode == 'open_loop'

    @property
    def physical(self) -> bool:
        return self.transport == 'oxygen_scpi' and any(
            spec['authorization']['physical'] for spec in self.raw['outputs'].values()
        )

    @property
    def active_roles(self) -> frozenset[str]:
        return frozenset(self.raw['outputs'])

    @property
    def has_supply(self) -> bool:
        return SUPPLY in self.raw['outputs']

    @property
    def has_motor(self) -> bool:
        return MOTOR in self.raw['outputs']

    @property
    def has_brake(self) -> bool:
        return BRAKE in self.raw['outputs']


def _safe_level(role: str, spec: dict) -> float:
    if role == BRAKE:
        return float(spec['brake_release_aout_v'])
    if role == MOTOR:
        return float(spec.get('neutral_aout_v', 0))
    return float(spec.get('neutral_aout_v', spec.get('startup_aout_v', 0)))


def load_profile(path, output, spec, *, open_loop: bool, configured_outputs: dict | None = None,
                 parameters: dict | None = None):
    path = Path(path).resolve()
    raw = read_json(path)
    keys(raw, {'schema_version', 'name', 'description', 'output', 'outputs', 'timeout_s',
               'completion', 'steps', 'requires'}, str(path))
    if type(raw.get('schema_version')) is not int or raw['schema_version'] != 3:
        raise ConfigError(f'{path}: profiles require schema_version 3')
    if raw.get('output') != output or not isinstance(raw.get('name'), str) or not raw['name']:
        raise ConfigError('Profile name/output does not match assignment')
    configured = configured_outputs or {output: spec}
    declared = raw.get('outputs', [output])
    if not isinstance(declared, list) or not declared or any(role not in configured for role in declared):
        raise ConfigError('Profile outputs must list configured logical roles')
    if output not in declared:
        raise ConfigError('Profile primary output must appear in outputs')
    if len(set(declared)) != len(declared):
        raise ConfigError('Profile outputs cannot repeat roles')
    if BRAKE in declared and MOTOR not in declared:
        raise ConfigError('vesc_brake coordination requires vesc_throttle on the same profile')
    if SUPPLY in declared and len(declared) > 1:
        raise ConfigError('Supply profiles remain single-output; drive/brake coordination is separate')
    participating = tuple(declared)
    completion = raw.get('completion', {'mode': spec['completion_policy'], 'value': spec.get('neutral_aout_v', 0)})
    keys(completion, {'mode', 'value', 'operator_action_required_to_stop'}, 'completion')
    mode = completion.get('mode')
    steps = raw.get('steps')
    relative = isinstance(steps, list) and any(
        isinstance(step, dict) and step.get('type') == 'relative_aout_step' for step in steps)
    if spec['role'] == 'motor_throttle' and mode != 'return_neutral':
        raise ConfigError('Motor profiles must return to verified neutral')
    if spec['role'] == 'motor_brake':
        raise ConfigError('vesc_brake cannot be the primary assignment output; coordinate under vesc_throttle')
    if spec['role'] == 'supply_voltage_demand' and mode not in {'hold_final', 'verify_zero'}:
        raise ConfigError('Supply completion must explicitly hold_final or verify_zero')
    if relative:
        if (output != SUPPLY or not open_loop or mode != 'hold_final' or
                completion.get('value') != 'runtime_target' or len(steps) != 1 or
                raw.get('requires') != {'motor_neutral': True} or
                spec['min_aout_v'] != 0.0 or spec['max_aout_v'] != 0.5 or
                spec.get('mapping', {}).get('gain_bus_v_per_aout_v') != VERIFIED_GAIN):
            raise ConfigError('Relative supply step requires open_loop, 0..0.5 V, gain 80, motor_neutral and runtime_target hold_final')
        final = 'runtime_target'
    else:
        final = number(completion['value'], 'completion value', spec['min_aout_v'], spec['max_aout_v'])
    if mode in {'verify_zero', 'return_neutral'} and final != spec.get('neutral_aout_v', 0):
        raise ConfigError('Neutral/zero completion value must equal neutral')
    if mode == 'hold_final' and completion.get('operator_action_required_to_stop') is not True:
        raise ConfigError('Persistent hold requires operator_action_required_to_stop true')
    steps = raw.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 120:
        raise ConfigError('Provide 1..120 profile steps')
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict):
        raise ConfigError('Assignment parameters must be an object of finite JSON numbers')
    for name, value in parameters.items():
        exact_name(name, 'parameter name')
        if type(value) not in (int, float):
            raise ConfigError(f'Parameter {name!r} must be a finite JSON number')
        try:
            finite_number(value, f'Parameter {name!r}')
        except (ValueError, OverflowError) as exc:
            raise ConfigError(f'Parameter {name!r} must be a finite JSON number') from exc
    for step in steps:
        if not isinstance(step, dict) or step.get('type') != 'ramp_aout':
            continue
        params = step.get('params', {})
        if not isinstance(params, dict) or not isinstance(params.get('target'), dict):
            continue
        reference = params['target']
        keys(reference, {'parameter'}, 'ramp target parameter reference')
        name = exact_name(reference.get('parameter'), 'ramp target parameter name')
        if name not in parameters:
            raise ConfigError(f'{path}: missing assignment parameter {name!r}')
        value = parameters[name]
        channel = params.get('channel')
        if isinstance(channel, str) and channel in configured:
            channel_spec = configured[channel]
            number(value, f'Parameter {name!r} target for {channel}',
                   channel_spec['min_aout_v'], channel_spec['max_aout_v'])
        params['target'] = value
    allowed_pre = OPEN_LOOP_PRECONDITIONS if open_loop else CLOSED_LOOP_PRECONDITIONS
    allowed_req = REQUIRES_OPEN if open_loop else REQUIRES_CLOSED
    elapsed, started, timed = 0.0, False, 0
    targets = {role: None for role in participating}
    for step in steps:
        keys(step, {'type', 'params', 'description'}, 'step')
        kind, params = step.get('type'), step.get('params', {})
        allowed = {
            'set_aout': {'channel', 'value'},
            'ramp_aout': {'channel', 'target', 'duration_s', 'rate_per_s'},
            'hold': {'duration_s'},
            'return_neutral': {'channel'},
            'checkpoint': {'name'},
            'assert_preconditions': allowed_pre,
            'relative_aout_step': {'channel', 'direction'},
        }
        if kind not in allowed:
            raise ConfigError(f'Unsupported profile step: {kind}')
        keys(params, allowed[kind], kind)
        if kind == 'relative_aout_step':
            if params.get('channel') != SUPPLY or params.get('direction') not in ('up', 'down'):
                raise ConfigError('Relative supply step needs supply channel and direction up/down')
            continue
        if kind == 'assert_preconditions':
            if started or any(value is not True for value in params.values()):
                raise ConfigError('Preconditions must be true requirements before output steps')
            if open_loop and set(params) - OPEN_LOOP_PRECONDITIONS:
                raise ConfigError('Open-loop profiles cannot declare measurement-dependent preconditions')
            continue
        if kind == 'checkpoint':
            if not isinstance(params.get('name'), str) or not params['name']:
                raise ConfigError('Checkpoint needs a name')
            continue
        started = True
        channel = params.get('channel')
        if kind in {'set_aout', 'ramp_aout', 'return_neutral'}:
            if channel not in participating:
                raise ConfigError('A profile may write only its declared logical outputs')
            channel_spec = configured[channel]
        if kind == 'set_aout':
            value = number(params['value'], 'setpoint', channel_spec['min_aout_v'], channel_spec['max_aout_v'])
            safe = _safe_level(channel, channel_spec)
            if timed or value != safe:
                raise ConfigError('Only initial safe-level set_aout is supported; transitions must ramp')
            targets[channel] = value
        elif kind in {'ramp_aout', 'hold'}:
            elapsed += number(params['duration_s'], 'duration', .1, 300)
            timed += 1
            if kind == 'ramp_aout':
                new_target = number(params['target'], 'target', channel_spec['min_aout_v'], channel_spec['max_aout_v'])
                prior = targets[channel]
                if 'rate_per_s' in params:
                    rate = number(params['rate_per_s'], 'rate', 0, 100)
                    if prior is not None and not math.isclose(
                            rate, abs(new_target - prior) / params['duration_s'], rel_tol=1e-9, abs_tol=1e-12):
                        raise ConfigError('Ramp rate disagrees with duration and target')
                # Drive/brake mutual exclusion on the compiled timeline.
                if channel == BRAKE and MOTOR in targets:
                    drive = targets[MOTOR]
                    if drive is not None and drive != configured[MOTOR].get('neutral_aout_v', 0):
                        raise ConfigError('Zero drive before brake: vesc_throttle must be neutral before vesc_brake demand')
                if channel == MOTOR and BRAKE in targets:
                    brake = targets[BRAKE]
                    release = configured[BRAKE]['brake_release_aout_v']
                    if brake is not None and brake != release:
                        raise ConfigError('Release brake before drive: vesc_brake must be at release before vesc_throttle demand')
                targets[channel] = new_target
        else:
            if channel not in {MOTOR, BRAKE}:
                raise ConfigError('return_neutral applies only to vesc_throttle or vesc_brake')
            expected = _safe_level(channel, channel_spec)
            if targets[channel] != expected:
                raise ConfigError('return_neutral requires an explicit preceding ramp to the safe level')
    primary_final = targets[output]
    if not relative and (not 1 <= timed <= 100 or elapsed > 300 or primary_final != final):
        raise ConfigError('Need 1..100 timed steps, <=300 seconds, and final target matching completion')
    if BRAKE in participating and not relative:
        if targets[BRAKE] != configured[BRAKE]['brake_release_aout_v']:
            raise ConfigError('Coordinated profiles must finish with vesc_brake at brake_release_aout_v')
    timeout = number(raw.get('timeout_s', 300), 'timeout', .1 if relative else elapsed, 600)
    requires = raw.get('requires', {})
    keys(requires, allowed_req, 'requires')
    constants = {
        'supply_output_enable': 'external_or_explicitly_verified',
        'measured_bus_feedback': 'required_for_motor_interlock',
        'motor_enable': 'operator_verified',
        'motor_neutral': True,
        'brake_released': True,
        'no_unhandled_regeneration': True,
        'supply_standalone_test': True,
    }
    for name, value in requires.items():
        if name != 'measured_bus_voltage' and (value != constants[name] or
                (constants[name] is True and value is not True)):
            raise ConfigError(f'Unsupported requirement: {name}')
    if 'measured_bus_voltage' in requires:
        if open_loop:
            raise ConfigError('Open-loop profiles cannot require measured bus voltage')
        feedback = requires['measured_bus_voltage']
        keys(feedback, {'target_v', 'tolerance_v', 'max_age_s'}, 'measured_bus_voltage')
        number(feedback['target_v'], 'bus target', 1, 60)
        number(feedback['tolerance_v'], 'bus tolerance', .01, 5)
        number(feedback['max_age_s'], 'feedback age', .01, 1)
    return Profile(path, raw['name'], output, participating, tuple(steps), completion, timeout, requires)


def _validate_outputs(raw, *, physical: bool, open_loop: bool):
    from .drive_limits import (
        authorize_dispatcher_brake_range, authorize_dispatcher_motor_max, brake_role_keys, motor_role_keys,
    )

    outputs = raw.get('outputs')
    if not isinstance(outputs, dict) or not outputs or set(outputs) - set(ROLES):
        raise ConfigError(
            'outputs must configure at least one known role: '
            'rzxa_voltage_demand, vesc_throttle, and/or vesc_brake')
    names, ids, bindings = set(), set(), set()
    for role, spec in outputs.items():
        if role == MOTOR:
            allowed = motor_role_keys()
        elif role == BRAKE:
            allowed = brake_role_keys()
        else:
            allowed = {
                'role', 'channel', 'display_name', 'expected_id', 'channel_discovery_required', 'binding',
                'authorization', 'mode', 'source', 'const_property', 'min_aout_v', 'max_aout_v',
                'startup_aout_v', 'neutral_aout_v', 'neutral_verified', 'mapping_verified', 'mapping',
                'output_enable', 'completion_policy', 'on_cancel', 'on_fault', 'on_shutdown',
            }
        keys(spec, allowed, role)
        binding = exact_name(spec.get('binding'), f'{role} binding')
        if binding in bindings:
            raise ConfigError('Output bindings cannot alias')
        bindings.add(binding)
        if 'display_name' in spec:
            exact_name(spec['display_name'], 'display_name')
        if spec.get('role') != ROLES[role] or spec.get('mode') != 'ConstOutput':
            raise ConfigError('Role and ConstOutput mode are required')
        if spec.get('source') != 'DISCOVER_CONSTANT' and raw['transport'] == 'oxygen_scpi':
            raise ConfigError('OXYGEN SCPI outputs must use DISCOVER_CONSTANT')
        if raw['transport'] == 'memory' and spec.get('source') != 'CONST0':
            raise ConfigError('Memory transport uses CONST0 fixture sources')
        exact_name(spec.get('const_property'), 'const_property')
        if spec.get('channel_discovery_required') is not True:
            raise ConfigError('Channel discovery remains mandatory')
        permission = spec.get('authorization')
        if not isinstance(permission, dict) or set(permission) != {'simulation', 'physical'}:
            raise ConfigError('Each output requires authorization.simulation and authorization.physical')
        role_physical = permission.get('physical') is True
        if physical:
            if permission['simulation'] is not False or permission['physical'] is not True:
                raise ConfigError('Physical outputs require simulation false / physical true')
            if not isinstance(spec.get('channel'), str) or not spec['channel'].strip():
                raise ConfigError('Physical outputs need an exact OXYGEN channel name')
            names.add(exact_name(spec['channel'], role))
        else:
            if permission['simulation'] is not True or permission['physical'] is not False:
                raise ConfigError('Simulated/memory outputs require simulation true / physical false')
            channel = spec.get('channel')
            if raw['transport'] == 'oxygen_scpi':
                names.add(exact_name(channel, role))
            elif channel is not None:
                raise ConfigError('Memory transport leaves channel null; fixture name lives under simulation.outputs')
        if role == BRAKE and raw['transport'] == 'oxygen_scpi' and isinstance(spec.get('channel'), str):
            if exact_name(spec['channel'], role) != BRAKING_CONTROL_NAME:
                raise ConfigError(f'vesc_brake channel must be exactly {BRAKING_CONTROL_NAME!r}')
        cid = spec.get('expected_id')
        if cid is not None:
            OxygenAdapter._channel_id(cid)
            if cid in ids:
                raise ConfigError('Expected IDs cannot alias')
            ids.add(cid)
        # Operator-configured max is authoritative within the absolute ConstOutput span.
        if role == BRAKE:
            authorize_dispatcher_brake_range(spec, physical=physical and role_physical)
        else:
            number(spec.get('min_aout_v'), 'minimum', 0, 0)
        if role == MOTOR:
            authorize_dispatcher_motor_max(spec, physical=physical and role_physical)
        elif role == SUPPLY:
            number(spec['max_aout_v'], 'maximum', .001, ABSOLUTE_AOUT_MAX_V)
        if role == SUPPLY:
            policy = 'preserve_demand'
        elif role == BRAKE:
            policy = 'return_release'
        else:
            policy = 'return_neutral'
        if any(spec.get(k) != policy for k in ('on_cancel', 'on_fault', 'on_shutdown')):
            raise ConfigError(
                'Supply must preserve demand; motor must attempt neutral; brake must attempt release')
        if role == MOTOR:
            number(spec.get('neutral_aout_v'), 'motor neutral', 0, 0)
            if spec.get('completion_policy') != 'return_neutral':
                raise ConfigError('Motor completion_policy must be return_neutral')
            if physical and role_physical and spec.get('neutral_verified') is not True:
                raise ConfigError('Physical motor requires explicitly verified 0 V neutral')
            if not (physical and role_physical) and spec.get('neutral_verified') is not False:
                raise ConfigError('Simulated motor neutral_verified must remain false')
        elif role == BRAKE:
            if spec.get('completion_policy') != 'return_release':
                raise ConfigError('Brake completion_policy must be return_release')
        else:
            number(spec.get('startup_aout_v'), 'startup', 0, 0)
            if spec.get('completion_policy') != 'hold_final':
                raise ConfigError('Supply completion_policy must be hold_final')
            mapping = spec.get('mapping', {})
            if physical and role_physical:
                keys(mapping, {'type', 'gain_bus_v_per_aout_v', 'verified'}, 'verified supply mapping')
                if mapping.get('type') != 'linear_verified' or mapping.get('verified') is not True:
                    raise ConfigError('Declare verified supply mapping; never infer from channel names')
                number(mapping.get('gain_bus_v_per_aout_v'), 'verified supply gain', .001, 10000)
                if spec.get('output_enable') != 'EXTERNAL_VERIFIED' or spec.get('mapping_verified') is not True:
                    raise ConfigError('Physical supply requires EXTERNAL_VERIFIED enable and mapping_verified')
            else:
                keys(mapping, {'type', 'full_scale_aout_v', 'assumed_full_scale_bus_v',
                               'gain_bus_v_per_aout_v', 'verified'}, 'provisional mapping')
                if (mapping.get('type') != 'linear_provisional' or mapping.get('verified') is not False or
                        mapping.get('gain_bus_v_per_aout_v') != VERIFIED_GAIN):
                    raise ConfigError('Simulated supply documents gain 80 V/V as provisional, verified false')
                if spec.get('output_enable') != 'NOT_CONTROLLED_BY_AOUT' or spec.get('mapping_verified') is not False:
                    raise ConfigError('Simulated supply enable/mapping must remain unverified')
    if BRAKE in outputs and MOTOR not in outputs:
        raise ConfigError('vesc_brake requires an active vesc_throttle role in the same dispatcher')
    return outputs


def confirm_outputs(config, confirmed_outputs, *, live=False):
    """One confirmation path for oxygen_scpi: exact configured channel names; --live for physical."""
    if config.transport != 'oxygen_scpi':
        return
    required = {spec['channel'] for spec in config.raw['outputs'].values()}
    if set(confirmed_outputs) != required:
        raise AuthorizationError(
            f'Confirm every configured AOUT channel name with --confirm-output: {sorted(required)}')
    if config.physical and live is not True:
        raise AuthorizationError('Physical oxygen_scpi execution requires a single --live acknowledgement')


def load_dispatcher_config(path) -> DispatcherConfig:
    path = Path(path).resolve()
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ConfigError('Dispatcher config must be a JSON object')
    if type(raw.get('schema_version')) is not int or raw['schema_version'] != 3:
        raise ConfigError('Expected schema_version 3')
    root = {
        'schema_version', 'control', 'transport', 'connection', 'outputs', 'timing',
        'measurements', 'interlocks', 'recording', 'instruments', 'live_control',
        'assignments', 'simulation', 'hardware_authorization', 'open_loop_authorization',
        'motor_sweep_defaults',
    }
    keys(raw, root, 'dispatcher v3')
    control = raw.get('control')
    keys(control, {'mode'}, 'control')
    if control.get('mode') not in {'open_loop', 'closed_loop'}:
        raise ConfigError('control.mode must be open_loop or closed_loop')
    open_loop = control['mode'] == 'open_loop'
    transport = raw.get('transport')
    if transport not in {'memory', 'oxygen_scpi'}:
        raise ConfigError('transport must be memory or oxygen_scpi')
    flags = raw.get('live_control', {})
    keys(flags, LIVE_FLAGS, 'live_control')
    # Existing configurations without a brake role need no new enable flag.
    flags.setdefault('enable_physical_brake', False)
    raw['live_control'] = flags
    physical = False
    outputs_probe = raw.get('outputs')
    if isinstance(outputs_probe, dict):
        physical = any(
            isinstance(s, dict) and isinstance(s.get('authorization'), dict)
            and s['authorization'].get('physical') is True for s in outputs_probe.values()
        )
    if physical:
        if transport != 'oxygen_scpi':
            raise ConfigError('Physical output authorization requires transport oxygen_scpi')
        active = set(outputs_probe)
        expected = {
            'enable_physical_aout': True, 'enable_physical_brake': BRAKE in active,
            'enable_rzxa_output_toggle': False, 'enable_real_rzxa_scpi': False,
            'require_explicit_hardware_authorization': True,
        }
        if any(flags.get(k) is not v for k, v in expected.items()):
            raise ConfigError(
                'Physical live_control requires AOUT enable and enable_physical_brake true '
                'exactly when vesc_brake is configured; RZ-XA SCPI/toggling remain disabled')
        acks = raw.get('hardware_authorization', {})
        required_acks = {
            'wiring_verified', 'external_emergency_stop_verified', 'external_enable_control_verified',
        }
        if SUPPLY in active:
            required_acks.add('supply_mapping_verified')
        if MOTOR in active:
            required_acks.add('motor_neutral_verified')
        if BRAKE in active:
            required_acks.add('brake_release_verified')
        keys(acks, HARDWARE_ACKNOWLEDGEMENTS, 'hardware_authorization')
        if set(acks) != required_acks or any(acks.get(k) is not True for k in required_acks):
            raise ConfigError(
                f'Explicitly verify hardware acknowledgements for active roles: {sorted(required_acks)}')
        if open_loop:
            ola = raw.get('open_loop_authorization', {})
            keys(ola, OUTPUT_ONLY_ACKNOWLEDGEMENTS, 'open_loop_authorization')
            if set(ola) != OUTPUT_ONLY_ACKNOWLEDGEMENTS or any(v is not True for v in ola.values()):
                raise ConfigError(
                    'Open-loop physical mode requires acknowledging absent live feedback '
                    'and verified external bus/regen protection')
        elif 'open_loop_authorization' in raw:
            raise ConfigError('open_loop_authorization is only valid with control.mode open_loop')
    else:
        if any(flags.get(k) is not False for k in LIVE_FLAGS - {'require_explicit_hardware_authorization'}):
            raise ConfigError('Simulated/memory live actuation flags must be false')
        if flags.get('require_explicit_hardware_authorization') is not True:
            raise ConfigError('Explicit hardware authorization must remain required')
        if 'hardware_authorization' in raw or 'open_loop_authorization' in raw:
            raise ConfigError('Hardware authorization blocks are only for physical oxygen_scpi')

    connection = raw.get('connection', {})
    keys(connection, {'host', 'port', 'timeout_s'}, 'connection')
    exact_name(connection.get('host'), 'host')
    if type(connection.get('port')) is not int:
        raise ConfigError('port must be an integer')
    number(connection['port'], 'port', 1, 65535)
    number(connection.get('timeout_s'), 'timeout', .001, 5)

    outputs = _validate_outputs(raw, physical=physical, open_loop=open_loop)

    timing = raw.get('timing', {})
    keys(timing, {
        'poll_ms', 'max_trigger_gap_ms', 'interval_ms', 'max_lateness_ms', 'telemetry_poll_ms',
    }, 'timing')
    # Sweep-oriented defaults use 20 ms; schema floor is 10 ms (independently configurable).
    poll = number(timing['poll_ms'], 'poll_ms', 10, 250)
    if number(timing['max_trigger_gap_ms'], 'max_trigger_gap_ms', 10, 1000) <= poll:
        raise ConfigError('Maximum gap must exceed polling interval')
    number(timing['interval_ms'], 'interval_ms', 10, 250)
    number(timing['max_lateness_ms'], 'max_lateness_ms', 1, 1000)

    measurements = raw.get('measurements', {})
    if measurements is None:
        measurements = {}
    if not isinstance(measurements, dict):
        raise ConfigError('measurements must be an object')
    raw['measurements'] = measurements

    def _validate_measurement_entry(name, measurement, *, require_verified: bool):
        keys(measurement, {'kind', 'unit', 'channel', 'property', 'mapping_verified'}, name)
        # Supervisory sweep channels (open-loop global mode) may declare numeric RPM/bus.
        numeric_roles = {'dc_bus_voltage', 'erpm', 'rpm', 'motor_rpm', 'electrical_rpm'}
        expected_kind = 'number' if name in numeric_roles else 'boolean'
        if measurement.get('kind') != expected_kind:
            raise ConfigError(f'{name}: kind mismatch')
        if require_verified:
            if measurement.get('mapping_verified') is not True:
                raise ConfigError(f'{name}: verified hardware mapping required')
            channel = exact_name(measurement.get('channel'), f'{name} channel')
            if channel in {s['channel'] for s in outputs.values() if s.get('channel')}:
                raise ConfigError('Feedback cannot alias an AOUT command channel')
            if name == 'dc_bus_voltage':
                if measurement.get('unit') != 'V' or measurement.get('property') is not None:
                    raise ConfigError('DC bus uses numeric measurement channel in V')
            elif name in numeric_roles - {'dc_bus_voltage'}:
                if measurement.get('unit') not in {'rpm', 'erpm', '1/min'} or measurement.get('property') is not None:
                    raise ConfigError(f'{name}: numeric RPM channel uses unit rpm/erpm/1/min')
            else:
                exact_name(measurement.get('property'), f'{name} Boolean property')
        elif measurement.get('mapping_verified') is not False:
            raise ConfigError('Simulated/open-loop logging measurement mappings remain unverified')
        elif name in numeric_roles and name != 'dc_bus_voltage':
            # Open-loop / simulated supervisory RPM: unit optional but must be coherent when set.
            unit = measurement.get('unit')
            if unit is not None and unit not in {'rpm', 'erpm', '1/min'}:
                raise ConfigError(f'{name}: unit must be rpm, erpm, or 1/min when declared')

    if open_loop:
        # Optional logging-only channels; never required for control.
        for name, measurement in measurements.items():
            _validate_measurement_entry(name, measurement, require_verified=False)
    else:
        required = {
            'dc_bus_voltage', 'motor_input_mapping_verified', 'motor_physical_disable_verified',
            'supply_external_ready', 'regen_safe', 'supply_shutdown_permitted',
            'supply_standalone_test', 'supply_disabled',
        }
        if set(measurements) != required:
            raise ConfigError('Closed-loop requires the full independent measurement role set')
        for name, measurement in measurements.items():
            _validate_measurement_entry(name, measurement, require_verified=physical)

    # Independent open-loop telemetry interval (0 = no background poll; never blocks AOUT writes).
    has_optional_channels = open_loop and any(
        isinstance(m, dict) and m.get('channel') for m in measurements.values())
    if 'telemetry_poll_ms' in timing and timing['telemetry_poll_ms'] is not None:
        number(timing['telemetry_poll_ms'], 'telemetry_poll_ms', 0, 60000)
    else:
        timing['telemetry_poll_ms'] = 500 if has_optional_channels else 0

    active = set(outputs)
    interlocks = raw.get('interlocks', {})
    base = {'one_active_profile_global', 'external_emergency_stop_required_for_live'}
    cross = {
        'motor_supply_demand_v', 'supply_down_requires_motor_neutral', 'require_supply_demand_before_motor',
        'motor_supply_demands_v', 'supply_demand_grid_commissioned', 'supply_demand_step_v',
    }
    drive_brake = {'drive_brake_mutual_exclusion', 'zero_drive_before_brake', 'release_brake_before_drive'}
    feedback_keys = {
        'bus_target_v', 'bus_tolerance_v', 'max_feedback_age_s',
        'motor_requires_measured_bus_in_range', 'allow_unverified_simulated_interlock_values',
        'allow_unverified_live_interlock_values',
    }
    allowed_interlocks = set(base)
    if SUPPLY in active and MOTOR in active:
        allowed_interlocks |= cross
    elif MOTOR in active:
        # Motor-only: cross-role supply prereq may be explicitly disabled.
        allowed_interlocks |= {'require_supply_demand_before_motor'}
    if MOTOR in active and BRAKE in active:
        allowed_interlocks |= drive_brake
    if not open_loop:
        allowed_interlocks |= feedback_keys
    keys(interlocks, allowed_interlocks, 'interlocks')
    if interlocks.get('one_active_profile_global') is not True:
        raise ConfigError('one_active_profile_global must be true')
    if interlocks.get('external_emergency_stop_required_for_live') is not True:
        raise ConfigError('external_emergency_stop_required_for_live must be true')
    if MOTOR in active and BRAKE in active:
        for key in drive_brake:
            if interlocks.get(key) is not True:
                raise ConfigError(f'{key} must be true when vesc_throttle and vesc_brake are both configured')
    demand = None
    if SUPPLY in active and MOTOR in active:
        if type(interlocks.get('supply_down_requires_motor_neutral')) is not bool:
            raise ConfigError(
                'supply_down_requires_motor_neutral must be Boolean when both roles are active '
                '(true = verify motor neutral before supply motion)')
        if type(interlocks.get('require_supply_demand_before_motor')) is not bool:
            raise ConfigError('require_supply_demand_before_motor must be Boolean (default true in examples)')
        step_v = interlocks.get('supply_demand_step_v', 0.5)
        if step_v not in (0.05, 0.1, 0.25, 0.5, 1.0):
            raise ConfigError('supply_demand_step_v must be one of 0.05, 0.1, 0.25, 0.5, 1.0')
        # Relative manual RZ-XA steps remain on the fixed 0.5 V SUPPLY_DEMAND_GRID.
        # Motor prerequisites may list a finer commissioned grid (e.g. 0.25 V sweeps).
        from .voltage_grid import bus_to_aout, generate_voltage_points
        fine_grid = tuple(
            bus_to_aout(v) for v in generate_voltage_points(36.0, 40.0, step_v))
        if 'motor_supply_demands_v' in interlocks:
            demands = interlocks['motor_supply_demands_v']
            if 'motor_supply_demand_v' in interlocks:
                raise ConfigError('Choose scalar motor_supply_demand_v or discrete motor_supply_demands_v, not both')
            if not isinstance(demands, list) or not demands or len(demands) > 64:
                raise ConfigError('motor_supply_demands_v needs 1..64 commissioned discrete demands')
            for value in demands:
                number(value, 'commissioned supply demand', 0, outputs[SUPPLY]['max_aout_v'])
                if not any(math.isclose(value, allowed, abs_tol=1e-9, rel_tol=0) for allowed in fine_grid):
                    raise ConfigError(
                        f'Motor supply demand {value} is outside the commissioned 36..40 V / '
                        f'{step_v} V grid')
            if len(set(demands)) != len(demands):
                raise ConfigError('Duplicate commissioned supply demands')
            if not open_loop:
                raise ConfigError('Discrete supply demands currently require open_loop; measured bus protection is independent')
            if physical and interlocks.get('supply_demand_grid_commissioned') is not True:
                raise ConfigError('Physical discrete demands require supply_demand_grid_commissioned true after separate review')
        else:
            demand = number(interlocks['motor_supply_demand_v'], 'motor supply prerequisite',
                            0, outputs[SUPPLY]['max_aout_v'])
        if ('supply_demand_grid_commissioned' in interlocks and
                type(interlocks['supply_demand_grid_commissioned']) is not bool):
            raise ConfigError('supply_demand_grid_commissioned must be Boolean')
    elif MOTOR in active:
        if interlocks.get('require_supply_demand_before_motor', False) is not False:
            raise ConfigError('Motor-only configs must set require_supply_demand_before_motor false')
    if not open_loop:
        if interlocks.get('motor_requires_measured_bus_in_range') is not True:
            raise ConfigError('Closed-loop requires measured bus interlock')
        if interlocks.get('allow_unverified_live_interlock_values') is not False:
            raise ConfigError('Unverified live interlock values are forbidden')
        if not physical and interlocks.get('allow_unverified_simulated_interlock_values') is not True:
            raise ConfigError('Simulated closed-loop may allow unverified fixture interlocks')
        if physical and interlocks.get('allow_unverified_simulated_interlock_values') is not False:
            raise ConfigError('Physical closed-loop forbids unverified simulated interlocks')
        target = number(interlocks['bus_target_v'], 'bus target', 1, 60)
        number(interlocks['bus_tolerance_v'], 'bus tolerance', .01, 5)
        number(interlocks['max_feedback_age_s'], 'feedback age', .01, 1)
        if physical and demand is not None and SUPPLY in outputs:
            gain = outputs[SUPPLY]['mapping']['gain_bus_v_per_aout_v']
            if not math.isclose(demand * gain, target, rel_tol=1e-9, abs_tol=1e-6):
                raise ConfigError('Verified supply gain, motor demand prerequisite and bus target disagree')

    recording = raw.get('recording', {})
    keys(recording, {'enabled', 'markers'}, 'recording')
    if recording.get('enabled') is not False or type(recording.get('markers')) is not bool:
        raise ConfigError('Recording must remain disabled; markers are local logs only')
    if raw.get('instruments') != {'rzxa': {'adapter': 'disabled', 'enable_output_control': False}}:
        raise ConfigError('No RZ-XA adapter or output toggle is permitted')

    simulation = raw.get('simulation', {})
    if transport == 'memory':
        keys(simulation, {'namespace', 'outputs', 'telemetry', 'sample_interval_s', 'streaming'}, 'simulation')
        if simulation.get('namespace') != 'FAKE_IN_PROCESS_ONLY' or type(simulation.get('streaming')) is not bool:
            raise ConfigError('Memory fixtures must be explicitly fake')
        number(simulation['sample_interval_s'], 'sample interval', .01, 1)
        fixtures = simulation.get('outputs')
        expected = {spec['binding'] for spec in outputs.values()}
        if not isinstance(fixtures, dict) or set(fixtures) != expected:
            raise ConfigError('simulation.outputs must declare each output binding')
        for key, fixture in fixtures.items():
            keys(fixture, {'name', 'id', 'mode', 'source', 'property', 'unit', 'min_v', 'max_v', 'initial_v'}, key)
            OxygenAdapter._channel_id(str(fixture['id']))
            if fixture.get('mode') != 'ConstOutput' or fixture.get('unit') != 'V':
                raise ConfigError('Fixture outputs must be ConstOutput in V')
        if BRAKE in outputs:
            brake_binding = outputs[BRAKE]['binding']
            if fixtures[brake_binding].get('name') != BRAKING_CONTROL_NAME:
                raise ConfigError(
                    f'Memory vesc_brake fixture name must be exactly {BRAKING_CONTROL_NAME!r}')
        telemetry = simulation.get('telemetry', {}) or {}
        if not isinstance(telemetry, dict):
            raise ConfigError('simulation.telemetry must be an object')
        if set(telemetry) - set(measurements):
            raise ConfigError('simulation.telemetry keys must be declared under measurements')
        if not open_loop and set(telemetry) != set(measurements):
            raise ConfigError('Closed-loop memory telemetry must cover every measurement role')
        simulation['telemetry'] = telemetry
    elif simulation not in ({}, None) and simulation != {}:
        if physical:
            raise ConfigError('Physical oxygen_scpi must omit simulation fixtures')
        keys(simulation, {'namespace', 'outputs', 'telemetry', 'sample_interval_s', 'streaming'}, 'simulation')
        if simulation.get('namespace') != 'OXYGEN_SIMULATED_IO_FAKE_TELEMETRY':
            raise ConfigError('Demo oxygen_scpi simulation namespace must be explicit')
        if simulation.get('outputs') not in ({}, None):
            raise ConfigError('Demo SCPI discovers live channels; simulation.outputs must be empty')
        number(simulation.get('sample_interval_s', .1), 'sample interval', .01, 1)
        if type(simulation.get('streaming')) is not bool:
            raise ConfigError('streaming must be Boolean')
        telemetry = simulation.get('telemetry', {}) or {}
        if set(telemetry) - set(measurements):
            raise ConfigError('simulation.telemetry keys must be declared under measurements')
        simulation['telemetry'] = telemetry
    else:
        raw['simulation'] = {}

    assignments = raw.get('assignments')
    # Raised from 8 → 16 so CES eight-button set can coexist with additive sweep buttons.
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 16:
        raise ConfigError('Provide 1..16 assignments')
    profiles, triggers = {}, set()
    motor_sweeps, fast_regens = {}, {}
    stop_sweep_trigger = None
    enabled_count = 0
    sensor_names = {m.get('channel') for m in measurements.values() if isinstance(m, dict) and m.get('channel')}
    output_names = {s.get('channel') for s in outputs.values() if s.get('channel')}
    defaults = raw.get('motor_sweep_defaults') or {}
    if defaults and not isinstance(defaults, dict):
        raise ConfigError('motor_sweep_defaults must be an object')
    for assignment in assignments:
        keys(assignment, {
            'name', 'enabled', 'trigger', 'output', 'profile', 'parameters',
            'kind', 'motor_sweep', 'fast_regen',
        }, 'assignment')
        if type(assignment.get('enabled', True)) is not bool:
            raise ConfigError('Assignment enabled must be Boolean')
        if not assignment.get('enabled', True):
            continue
        enabled_count += 1
        name = exact_name(assignment.get('name'), 'assignment name')
        trigger = exact_name(assignment.get('trigger'), 'DOUT trigger')
        kind = assignment.get('kind', 'profile')
        if kind not in {'profile', 'motor_sweep', 'fast_regen', 'stop_sweep'}:
            raise ConfigError(
                "assignment.kind must be 'profile', 'motor_sweep', 'fast_regen', or 'stop_sweep'")
        if name in profiles or name in motor_sweeps or name in fast_regens or (
                kind == 'stop_sweep' and stop_sweep_trigger is not None):
            raise ConfigError('Assignment names must be unique')
        if trigger in triggers or trigger in output_names | sensor_names:
            raise ConfigError('Assignment triggers must be unique and independent of outputs')
        triggers.add(trigger)

        if kind == 'stop_sweep':
            if stop_sweep_trigger is not None:
                raise ConfigError('Only one stop_sweep assignment is allowed')
            if assignment.get('profile') or assignment.get('motor_sweep') or assignment.get('fast_regen'):
                raise ConfigError('stop_sweep assignments do not take profile/motor_sweep/fast_regen')
            if assignment.get('output') not in (None, MOTOR, SUPPLY, BRAKE):
                raise ConfigError('stop_sweep output is optional and informational only')
            stop_sweep_trigger = trigger
            continue

        output = assignment.get('output')
        if output not in outputs:
            raise ConfigError('Unknown output role')

        if kind == 'motor_sweep':
            if output != MOTOR:
                raise ConfigError('motor_sweep assignments must use output vesc_throttle')
            if SUPPLY not in outputs or BRAKE not in outputs:
                raise ConfigError('motor_sweep requires supply, throttle, and brake roles')
            if assignment.get('profile'):
                raise ConfigError('motor_sweep assignments use motor_sweep config, not profile')
            from .motor_sweep import deep_merge, parse_motor_sweep
            # Documented deep-merge: nested fast_regen / voltage_qualify / rpm_feedback
            # assignment keys override motor_sweep_defaults without wiping siblings.
            merged = deep_merge(defaults, assignment.get('motor_sweep') or {})
            parameters = assignment.get('parameters') or {}
            if isinstance(parameters, dict) and 'brake_target_v' in parameters:
                # Explicit assignment brake_target_v overrides shared Fast Regen defaults.
                fr = dict(merged.get('fast_regen') or {})
                fr['brake_target_v'] = parameters['brake_target_v']
                merged['fast_regen'] = fr
            if 'mode' not in merged:
                raise ConfigError(f'{name}: motor_sweep.mode is required')
            # Validate feedback channels exist before the assignment starts
            # (open-loop assignments do not require V/RPM channels).
            sweep_cfg = parse_motor_sweep(merged, outputs=outputs)
            if sweep_cfg.voltage_qualify is not None:
                channel = sweep_cfg.voltage_qualify.channel
                if channel not in measurements:
                    raise ConfigError(
                        f'{name}: voltage_qualify channel {channel!r} must be declared under measurements')
            if sweep_cfg.rpm_feedback is not None:
                channel = sweep_cfg.rpm_feedback.channel
                if channel not in measurements:
                    raise ConfigError(
                        f'{name}: rpm_feedback channel {channel!r} must be declared under measurements')
            motor_sweeps[name] = sweep_cfg
            continue

        if kind == 'fast_regen':
            if output != MOTOR:
                raise ConfigError('fast_regen assignments must use output vesc_throttle')
            if BRAKE not in outputs:
                raise ConfigError('fast_regen requires vesc_brake')
            if assignment.get('profile'):
                raise ConfigError('fast_regen assignments use fast_regen config, not profile')
            from .fast_regen import parse_fast_regen
            from .motor_sweep import deep_merge
            # Same deep-merge semantics as motor_sweep: defaults ← assignment.fast_regen.
            base = defaults.get('fast_regen') or {}
            override = assignment.get('fast_regen') or {}
            if base and not isinstance(base, dict):
                raise ConfigError('motor_sweep_defaults.fast_regen must be an object')
            if override and not isinstance(override, dict):
                raise ConfigError(f'{name}: fast_regen must be an object')
            regen_raw = deep_merge(dict(base) if isinstance(base, dict) else {},
                                   dict(override) if isinstance(override, dict) else {})
            parameters = assignment.get('parameters') or {}
            if isinstance(parameters, dict) and 'brake_target_v' in parameters:
                # Explicit assignment brake_target_v always overrides shared Fast Regen defaults.
                regen_raw['brake_target_v'] = parameters['brake_target_v']
            fast_regens[name] = parse_fast_regen(
                regen_raw,
                motor_max_v=outputs[MOTOR]['max_aout_v'],
                brake_max_v=outputs[BRAKE].get('brake_max_aout_v', outputs[BRAKE]['max_aout_v']),
                brake_release_v=outputs[BRAKE]['brake_release_aout_v'],
            )
            continue

        # Default: classic profile assignment
        filename = exact_name(assignment.get('profile'), 'profile path')
        parameters = assignment.get('parameters', {})
        if not isinstance(parameters, dict):
            raise ConfigError(f'{name}: assignment parameters must be an object of finite JSON numbers')
        if assignment.get('motor_sweep') or assignment.get('fast_regen'):
            raise ConfigError(f'{name}: profile assignments cannot declare motor_sweep/fast_regen')
        profile = load_profile(
            path.parent / filename, output, outputs[output], open_loop=open_loop,
            configured_outputs=outputs, parameters=parameters)
        if any(step['type'] == 'relative_aout_step' for step in profile.steps):
            if physical and interlocks.get('supply_demand_grid_commissioned') is not True:
                raise ConfigError('Physical relative steps require supply_demand_grid_commissioned true after separate review')
        if MOTOR not in outputs:
            if profile.requires.get('motor_neutral'):
                raise ConfigError(f'{name}: motor_neutral prerequisite needs an active vesc_throttle role')
            for step in profile.steps:
                if step['type'] == 'assert_preconditions' and step.get('params', {}).get('motor_neutral'):
                    raise ConfigError(
                        f'{name}: assert_preconditions.motor_neutral needs an active vesc_throttle role')
        if BRAKE not in outputs:
            if profile.requires.get('brake_released') or BRAKE in profile.outputs:
                raise ConfigError(f'{name}: brake coordination needs an active vesc_brake role')
            for step in profile.steps:
                params = step.get('params', {})
                if step['type'] == 'assert_preconditions' and params.get('brake_released'):
                    raise ConfigError(f'{name}: assert_preconditions.brake_released needs vesc_brake')
                if params.get('channel') == BRAKE:
                    raise ConfigError(f'{name}: profile writes vesc_brake but role is not configured')
        if SUPPLY not in outputs and output == MOTOR:
            # Motor-only is allowed; cross-role supply demand is gated by interlocks, not profile load.
            pass
        profiles[name] = profile
    if enabled_count < 1:
        raise ConfigError('At least one enabled assignment is required')
    if not profiles and not motor_sweeps and not fast_regens:
        raise ConfigError('At least one runnable assignment (profile, motor_sweep, or fast_regen) is required')
    return DispatcherConfig(
        path, raw, profiles,
        motor_sweeps=motor_sweeps,
        fast_regens=fast_regens,
        stop_sweep_trigger=stop_sweep_trigger,
    )
