from __future__ import annotations

import copy
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adapters.oxygen import OxygenAdapter
from .constant_pattern import APPROVED_CHANNEL, CONSTANT_PROPERTY, finite_number

ROLES = {'rzxa_voltage_demand': 'supply_voltage_demand', 'vesc_throttle': 'motor_throttle'}
# IDs here are historical regression evidence, not runtime identity requirements.
OXYGEN_DEMO_OUTPUTS = {
    'rzxa_voltage_demand': ('AO 3/1 Sim', '8898268430164033617', CONSTANT_PROPERTY),
    'vesc_throttle': ('AO 3/2 Sim', '8898268430164033618', 'TRION/SourceChannel_A_CONST/Const1'),
}
OXYGEN_DEMO_APPROVALS = {
    'rzxa_voltage_demand': {
        'AO 3/1 Sim': frozenset(output[2] for output in OXYGEN_DEMO_OUTPUTS.values()),
        'Test Aout2': frozenset({'TRION/SourceChannel_A_CONST/Const4'}),
    },
    'vesc_throttle': {
        'AO 3/2 Sim': frozenset(output[2] for output in OXYGEN_DEMO_OUTPUTS.values()),
        'Test Aout1': frozenset({CONSTANT_PROPERTY}),
    },
}
LIVE_FLAGS = {'enable_physical_aout', 'enable_rzxa_output_toggle', 'enable_real_rzxa_scpi',
              'require_explicit_hardware_authorization'}
PRECONDITIONS = {'supply_output_enabled_or_verified_external', 'motor_neutral',
                 'current_aout_must_be_zero_or_already_target', 'reject_other_start_values',
                 'supply_shutdown_permitted', 'supply_standalone_test', 'bus_voltage_validated'}
REQUIRES = {'supply_output_enable', 'measured_bus_feedback', 'motor_neutral',
            'no_unhandled_regeneration', 'supply_standalone_test', 'measured_bus_voltage', 'motor_enable'}
ROOT_KEYS = {'schema_version', 'execution_backend', 'simulation_only', 'connection', 'outputs',
             'timing', 'measurement_roles', 'interlocks', 'recording', 'instruments', 'live_control',
             'assignments', 'simulation'}


def keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f'{label}: unknown keys or not an object; allowed: {sorted(allowed)}')


def number(value, label, lower, upper):
    if type(value) not in (int, float):
        raise ValueError(f'{label} must be a JSON number, not a Boolean or numeric string')
    result = finite_number(value, label)
    if not lower <= result <= upper:
        raise ValueError(f'{label} must be in [{lower}, {upper}]')
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def simulation_authorized(value):
    return (isinstance(value, dict) and set(value) == {'simulation', 'physical'}
            and value['simulation'] is True and value['physical'] is False)


def confirm_demo_outputs(outputs, confirmed_outputs):
    required = {spec['channel'] for spec in outputs.values()}
    if len(required) != len(ROLES) or set(confirmed_outputs) != required:
        raise PermissionError(f'Confirm BOTH configured simulated, unconnected AOs using --confirm-demo-output: {sorted(required)}')
    if set(outputs) != set(ROLES) or any(
            spec['channel'] not in OXYGEN_DEMO_APPROVALS[role] for role, spec in outputs.items()):
        raise PermissionError('Only explicitly approved simulated-channel names may execute')


def validate_permissions(raw):
    if raw.get('simulation_only') is not True:
        raise ValueError('V2 is simulation-only; physical actuation is not implemented')
    flags = raw.get('live_control', {})
    keys(flags, LIVE_FLAGS, 'live_control')
    for flag in LIVE_FLAGS - {'require_explicit_hardware_authorization'}:
        if flags.get(flag) is not False:
            raise ValueError(f'{flag} must explicitly be false')
    if flags.get('require_explicit_hardware_authorization') is not True:
        raise ValueError('Explicit hardware authorization must remain required')


@dataclass(frozen=True)
class V2Profile:
    path: Path
    name: str
    output: str
    steps: tuple[dict, ...]
    completion: dict
    timeout_s: float
    requires: dict


def load_v2_profile(path, output, spec, *, simulation_only=True):
    path = Path(path).resolve()
    raw = read_json(path)
    keys(raw, {'schema_version', 'name', 'description', 'simulation_only', 'output', 'timeout_s',
               'completion', 'steps', 'requires'}, str(path))
    if type(raw.get('schema_version')) is not int or raw['schema_version'] != 2 or raw.get('simulation_only') is not simulation_only:
        raise ValueError(f'V2 profiles require schema_version 2 and simulation_only {str(simulation_only).lower()}')
    if raw.get('output') != output or not isinstance(raw.get('name'), str) or not raw['name']:
        raise ValueError('Profile name/output does not match assignment')
    completion = raw.get('completion', {'mode': spec['completion_policy'], 'value': spec.get('neutral_aout_v', 0)})
    keys(completion, {'mode', 'value', 'operator_action_required_to_stop'}, 'completion')
    mode = completion.get('mode')
    if spec['role'] == 'motor_throttle' and mode != 'return_neutral':
        raise ValueError('Motor profiles must return to verified neutral')
    if spec['role'] == 'supply_voltage_demand' and mode not in {'hold_final', 'verify_zero'}:
        raise ValueError('Supply completion must explicitly hold_final or verify_zero')
    final = number(completion['value'], 'completion value', spec['min_aout_v'], spec['max_aout_v'])
    if mode in {'verify_zero', 'return_neutral'} and final != spec.get('neutral_aout_v', 0):
        raise ValueError('Neutral/zero completion value must equal neutral')
    if mode == 'hold_final' and completion.get('operator_action_required_to_stop') is not True:
        raise ValueError('Persistent hold requires operator_action_required_to_stop true')
    steps = raw.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 120:
        raise ValueError('Provide 1..120 profile steps')
    elapsed, target, started, timed = 0.0, None, False, 0
    for step in steps:
        keys(step, {'type', 'params', 'description'}, 'step')
        kind, params = step.get('type'), step.get('params', {})
        allowed = {'set_aout': {'channel', 'value'}, 'ramp_aout': {'channel', 'target', 'duration_s', 'rate_per_s'},
                   'hold': {'duration_s'}, 'return_neutral': {'channel'}, 'checkpoint': {'name'},
                   'assert_preconditions': PRECONDITIONS}
        if kind not in allowed:
            raise ValueError(f'Unsupported v2 step: {kind}')
        keys(params, allowed[kind], kind)
        if kind == 'assert_preconditions':
            if started or any(value is not True for value in params.values()):
                raise ValueError('Preconditions must be true requirements before output steps')
            continue
        if kind == 'checkpoint':
            if not isinstance(params.get('name'), str) or not params['name']:
                raise ValueError('Checkpoint needs a name')
            continue
        started = True
        if kind in {'set_aout', 'ramp_aout', 'return_neutral'} and params.get('channel') != output:
            raise ValueError('A profile may write only its assigned logical output')
        if kind == 'set_aout':
            value = number(params['value'], 'setpoint', spec['min_aout_v'], spec['max_aout_v'])
            if timed or value != spec.get('neutral_aout_v', 0):
                raise ValueError('Only initial neutral set_aout is supported; transitions must ramp')
            target = value
        elif kind in {'ramp_aout', 'hold'}:
            elapsed += number(params['duration_s'], 'duration', .1, 300)
            timed += 1
            if kind == 'ramp_aout':
                new_target = number(params['target'], 'target', spec['min_aout_v'], spec['max_aout_v'])
                if 'rate_per_s' in params:
                    rate = number(params['rate_per_s'], 'rate', 0, 100)
                    if target is not None and not math.isclose(rate, abs(new_target - target) / params['duration_s'], rel_tol=1e-9, abs_tol=1e-12):
                        raise ValueError('Ramp rate disagrees with duration and target')
                target = new_target
        else:
            if spec['role'] != 'motor_throttle' or target != spec['neutral_aout_v']:
                raise ValueError('return_neutral requires an explicit preceding motor ramp to neutral')
    if not 1 <= timed <= 100 or elapsed > 300 or target != final:
        raise ValueError('Need 1..100 timed steps, <=300 seconds, and final target matching completion')
    timeout = number(raw.get('timeout_s', 300), 'timeout', elapsed, 600)
    requires = raw.get('requires', {})
    keys(requires, REQUIRES, 'requires')
    constants = {'supply_output_enable': 'external_or_explicitly_verified',
                 'measured_bus_feedback': 'required_for_motor_interlock', 'motor_enable': 'operator_verified',
                 'motor_neutral': True, 'no_unhandled_regeneration': True, 'supply_standalone_test': True}
    for name, value in requires.items():
        if name != 'measured_bus_voltage' and (value != constants[name] or
                (constants[name] is True and value is not True)):
            raise ValueError(f'Unsupported requirement: {name}')
    if 'measured_bus_voltage' in requires:
        feedback = requires['measured_bus_voltage']
        keys(feedback, {'target_v', 'tolerance_v', 'max_age_s'}, 'measured_bus_voltage')
        number(feedback['target_v'], 'bus target', 1, 60)
        number(feedback['tolerance_v'], 'bus tolerance', .01, 5)
        number(feedback['max_age_s'], 'feedback age', .01, 1)
    return V2Profile(path, raw['name'], output, tuple(steps), completion, timeout, requires)


@dataclass(frozen=True)
class V2Config:
    path: Path
    raw: dict
    profiles: dict[str, V2Profile]


def load_v2_config(path):
    path = Path(path).resolve()
    raw = read_json(path)
    extra_keys = {'hardware_authorization'} if isinstance(raw, dict) and raw.get('execution_backend') == 'oxygen_hardware' else set()
    keys(raw, ROOT_KEYS | extra_keys, 'dispatcher v2')
    if type(raw.get('schema_version')) is not int or raw['schema_version'] != 2:
        raise ValueError('Expected schema_version 2')
    if raw.get('execution_backend') == 'oxygen_hardware':
        from .hardware_schema import load_hardware_config

        return load_hardware_config(path, raw)
    validate_permissions(raw)
    backend = raw.get('execution_backend')
    if backend not in {'memory_fixture', 'oxygen_demo'}:
        raise ValueError('Only memory_fixture or explicitly confirmed oxygen_demo execution is supported')
    connection = raw.get('connection', {})
    keys(connection, {'host', 'port', 'timeout_s'}, 'connection')
    if not isinstance(connection.get('host'), str) or not connection['host'] or type(connection.get('port')) is not int:
        raise ValueError('Reserved connection requires a host and integer TCP port')
    number(connection['port'], 'port', 1, 65535)
    number(connection['timeout_s'], 'response timeout', .001, 5)
    outputs = raw.get('outputs')
    if not isinstance(outputs, dict) or set(outputs) != set(ROLES):
        raise ValueError('Both rzxa_voltage_demand and vesc_throttle roles are required')
    actual_ids, actual_names, fixture_keys = set(), set(), set()
    for role, spec in outputs.items():
        keys(spec, {'role', 'channel', 'display_name', 'expected_id', 'channel_discovery_required', 'mode', 'source',
                    'const_property', 'min_aout_v', 'max_aout_v', 'startup_aout_v', 'neutral_aout_v',
                    'completion_policy', 'mapping', 'mapping_verified', 'neutral_verified', 'output_enable',
                    'simulation_fixture', 'authorization', 'on_cancel', 'on_fault', 'on_shutdown'}, role)
        source = 'DISCOVER_CONSTANT' if backend == 'oxygen_demo' else 'CONST0'
        if spec.get('role') != ROLES[role] or spec.get('mode') != 'ConstOutput' or spec.get('source') != source:
            raise ValueError('Role and allowed mode/source must match the canonical definition')
        if backend == 'oxygen_demo' and (not isinstance(spec.get('channel'), str) or not spec['channel'].strip()):
            raise ValueError('An exact nonempty OXYGEN channel name is required')
        if 'display_name' in spec and (not isinstance(spec['display_name'], str) or not spec['display_name'].strip()):
            raise ValueError('display_name must be a nonempty string')
        if backend == 'oxygen_demo':
            if not isinstance(spec.get('const_property'), str) or not spec['const_property']:
                raise ValueError('const_property must be auto or an explicit discovered property')
        elif spec.get('const_property') != CONSTANT_PROPERTY:
            raise ValueError('Only the fixture Const0 property is supported by memory_fixture')
        maximum = .5 if role == 'rzxa_voltage_demand' else .4
        if number(spec.get('min_aout_v'), 'minimum', 0, 0) != 0 or number(spec['max_aout_v'], 'maximum', .001, maximum) > maximum:
            raise ValueError('Invalid per-role clamp')
        if spec.get('mapping_verified') is not False:
            raise ValueError('Actual hardware mappings remain unverified in this release')
        if spec.get('channel_discovery_required') is not True:
            raise ValueError('Actual channel discovery remains required')
        for field, seen in [('expected_id', actual_ids), ('channel', actual_names)]:
            value = spec.get(field)
            if value is not None:
                if not isinstance(value, str) or not value or value in seen:
                    raise ValueError('Actual output mappings cannot alias')
                if field == 'expected_id':
                    OxygenAdapter._channel_id(value)
                seen.add(value)
        fixture = spec.get('simulation_fixture')
        if not isinstance(fixture, str) or not fixture or fixture in fixture_keys:
            raise ValueError('Distinct explicit fixture mappings are required')
        fixture_keys.add(fixture)
        if not simulation_authorized(spec.get('authorization')):
            raise ValueError('Per-output simulation authorization must be explicit; physical permission false')
        policy = 'preserve_demand' if role == 'rzxa_voltage_demand' else 'return_neutral'
        if any(spec.get(key) != policy for key in ('on_cancel', 'on_fault', 'on_shutdown')):
            raise ValueError('Supply must preserve demand; motor must attempt neutral on termination')
        if role == 'vesc_throttle':
            number(spec.get('neutral_aout_v'), 'motor neutral', 0, 0)
            if spec.get('neutral_aout_v') != 0 or spec.get('neutral_verified') is not False or spec.get('completion_policy') != 'return_neutral':
                raise ValueError('Motor neutral is zero in fixtures, not commissioned physical neutral')
        else:
            number(spec.get('startup_aout_v'), 'documented startup neutral', 0, 0)
            if spec.get('startup_aout_v') != 0 or spec.get('completion_policy') != 'hold_until_explicit_change' or spec.get('output_enable') != 'NOT_CONTROLLED_BY_AOUT':
                raise ValueError('Supply initialization/hold/enable policies must remain explicit')
            mapping = spec.get('mapping')
            keys(mapping, {'type', 'full_scale_aout_v', 'assumed_full_scale_bus_v', 'gain_bus_v_per_aout_v', 'verified'}, 'provisional mapping')
            if mapping.get('verified') is not False or mapping != {'type': 'linear_provisional', 'full_scale_aout_v': 10.0,
                           'assumed_full_scale_bus_v': 800.0, 'gain_bus_v_per_aout_v': 80.0, 'verified': False}:
                raise ValueError('Only the declared UNVERIFIED 80 V/V hypothesis is documented, never used as feedback')
    timing = raw.get('timing', {})
    keys(timing, {'poll_ms', 'max_trigger_gap_ms', 'interval_ms', 'max_lateness_ms'}, 'timing')
    poll = number(timing['poll_ms'], 'poll_ms', 20, 250)
    if number(timing['max_trigger_gap_ms'], 'max_trigger_gap_ms', 20, 1000) <= poll:
        raise ValueError('Maximum gap must exceed polling interval')
    number(timing['interval_ms'], 'interval_ms', 20, 250)
    number(timing['max_lateness_ms'], 'max_lateness_ms', 1, 1000)
    interlocks = raw.get('interlocks', {})
    keys(interlocks, {'bus_target_v', 'bus_tolerance_v', 'max_feedback_age_s', 'motor_supply_demand_v',
                      'motor_requires_measured_bus_in_range', 'supply_down_requires_motor_neutral',
                      'one_active_profile_global', 'allow_unverified_simulated_interlock_values',
                      'allow_unverified_live_interlock_values', 'external_emergency_stop_required_for_live'}, 'interlocks')
    for name in ('motor_requires_measured_bus_in_range', 'supply_down_requires_motor_neutral', 'one_active_profile_global',
                 'allow_unverified_simulated_interlock_values', 'external_emergency_stop_required_for_live'):
        if interlocks.get(name) is not True:
            raise ValueError(f'{name} must be true')
    if interlocks.get('allow_unverified_live_interlock_values') is not False:
        raise ValueError('Unverified live interlock values are forbidden')
    number(interlocks['bus_target_v'], 'bus target', 1, 60)
    number(interlocks['bus_tolerance_v'], 'bus tolerance', .01, 5)
    number(interlocks['max_feedback_age_s'], 'feedback age', .01, 1)
    number(interlocks['motor_supply_demand_v'], 'supply demand prerequisite', 0, outputs['rzxa_voltage_demand']['max_aout_v'])
    measurements = raw.get('measurement_roles')
    required = {'dc_bus_voltage', 'motor_input_mapping_verified', 'motor_physical_disable_verified',
                'supply_external_ready', 'regen_safe', 'supply_shutdown_permitted', 'supply_standalone_test', 'supply_disabled'}
    if not isinstance(measurements, dict) or set(measurements) != required:
        raise ValueError('All independent measurement/interlock roles must be declared')
    for name, measurement in measurements.items():
        keys(measurement, {'kind', 'unit', 'channel', 'mapping_verified'}, name)
        kind = 'number' if name == 'dc_bus_voltage' else 'boolean'
        if measurement.get('kind') != kind or measurement.get('mapping_verified') is not False:
            raise ValueError('Measurement kinds must match and actual mappings remain unverified')
        if measurement.get('channel') in {s.get('channel') for s in outputs.values() if s.get('channel')}:
            raise ValueError('Bus feedback cannot alias an AOUT command channel')
        if name == 'dc_bus_voltage' and measurement.get('unit') != 'V':
            raise ValueError('Bus measurement must be in V')
    recording = raw.get('recording', {})
    keys(recording, {'enabled', 'markers'}, 'recording')
    if recording.get('enabled') is not False or type(recording.get('markers')) is not bool:
        raise ValueError('OXYGEN recording is not implemented; enabled must be false, markers Boolean (local logs only)')
    if raw.get('instruments') != {'rzxa': {'adapter': 'disabled', 'enable_output_control': False}}:
        raise ValueError('No RZ-XA adapter or output toggle is permitted')
    simulation = raw.get('simulation')
    keys(simulation, {'namespace', 'outputs', 'telemetry', 'sample_interval_s', 'streaming'}, 'simulation')
    namespace = 'OXYGEN_SIMULATED_IO_FAKE_TELEMETRY' if backend == 'oxygen_demo' else 'FAKE_IN_PROCESS_ONLY'
    if simulation.get('namespace') != namespace or type(simulation.get('streaming')) is not bool:
        raise ValueError('Fixtures must be explicitly fake and streaming Boolean')
    number(simulation['sample_interval_s'], 'sample interval', .01, 1)
    fixtures = simulation.get('outputs')
    expected_fixtures = set() if backend == 'oxygen_demo' else fixture_keys
    if not isinstance(fixtures, dict) or set(fixtures) != expected_fixtures:
        raise ValueError('Memory fixtures must match bindings; OXYGEN demo must not invent fixture output IDs')
    ids, names = set(), set()
    for fixture in fixtures.values():
        keys(fixture, {'name', 'id', 'mode', 'source', 'property', 'unit', 'min_v', 'max_v', 'initial_v'}, 'fixture output')
        cid = OxygenAdapter._channel_id(fixture['id'])
        if cid in ids or not isinstance(fixture.get('name'), str) or not fixture['name'].startswith('FAKE ') or fixture['name'] in names:
            raise ValueError('Fixture names and IDs must be distinct and explicitly FAKE')
        ids.add(cid)
        names.add(fixture['name'])
        number(fixture['initial_v'], 'fixture initial value', fixture['min_v'], fixture['max_v'])
    if not isinstance(simulation.get('telemetry'), dict) or set(simulation['telemetry']) != required:
        raise ValueError('Inject all telemetry roles separately from output state')
    for name, value in simulation['telemetry'].items():
        if name == 'dc_bus_voltage':
            number(value, 'fake bus measurement', -1000, 1000)
        elif type(value) is not bool:
            raise ValueError(f'{name} must be a Boolean fixture sample')
    assignments = raw.get('assignments')
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 6:
        raise ValueError('Provide 1..6 bindings')
    profiles, triggers = {}, set()
    for assignment in assignments:
        keys(assignment, {'name', 'enabled', 'trigger', 'output', 'profile'}, 'assignment')
        if type(assignment.get('enabled', True)) is not bool:
            raise ValueError('Assignment enabled must be Boolean')
        if not assignment.get('enabled', True):
            continue
        name, trigger, output = assignment.get('name'), assignment.get('trigger'), assignment.get('output')
        if (not isinstance(trigger, str) or not trigger.strip() or
                any(char in trigger for char in '\r\n\x00')):
            raise ValueError('Assignment trigger must be an exact nonempty OXYGEN DOUT channel name')
        if not isinstance(name, str) or not name or name in profiles or trigger in triggers:
            raise ValueError('Assignment names/triggers must be valid and unique')
        if output not in outputs:
            raise ValueError('Unknown logical output')
        filename = assignment.get('profile')
        if not isinstance(filename, str) or not filename:
            raise ValueError('Profile must be a path')
        profiles[name] = load_v2_profile(path.parent / filename, output, outputs[output])
        triggers.add(trigger)
    if not profiles:
        raise ValueError('At least one enabled binding is required')
    return V2Config(path, raw, profiles)


def canonical_to_legacy(raw):
    keys(raw, ROOT_KEYS, 'canonical legacy configuration')
    validate_permissions(raw)
    if type(raw.get('schema_version')) is not int or raw['schema_version'] != 2 or raw.get('execution_backend') != 'legacy_verified_oxygen_sim':
        raise ValueError('Not a legacy compatibility configuration')
    if raw.get('measurement_roles') != {} or raw.get('interlocks') != {'one_active_profile_global': True} or raw.get('simulation') != {}:
        raise ValueError('Legacy compatibility cannot silently implement v2 interlocks/measurements')
    if raw.get('recording') != {'enabled': False, 'markers': False} or raw.get('instruments') != {'rzxa': {'adapter': 'disabled', 'enable_output_control': False}}:
        raise ValueError('Legacy recording/instrument operations remain disabled')
    outputs = {}
    for role, spec in raw['outputs'].items():
        if set(spec) != {'role', 'channel', 'expected_id', 'const_property', 'min_aout_v', 'max_aout_v', 'completion_policy', 'authorization'}:
            raise ValueError('Unexpected legacy output settings')
        if (spec['role'] != 'legacy_simulated' or spec['channel'] != APPROVED_CHANNEL or
                spec['const_property'] != CONSTANT_PROPERTY or spec['min_aout_v'] != 0 or spec['max_aout_v'] != 1 or
                spec['completion_policy'] != 'return_neutral' or not simulation_authorized(spec['authorization'])):
            raise ValueError('Legacy compatibility preserves ONLY the existing AO 3/1 simulation exception')
        outputs[role] = {'channel': spec['channel'], 'expected_id': spec['expected_id']}
    return {name: copy.deepcopy(raw[name]) for name in ('connection', 'timing', 'assignments')} | {'outputs': outputs}


def migrate_legacy(source, destination):
    from .pattern_dispatcher import load_dispatcher_config

    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or source == destination:
        raise ValueError('Migration writes a new file only; destination must not exist')
    validated = load_dispatcher_config(source)
    raw = read_json(source)
    if 'schema_version' in raw:
        raise ValueError('Migration accepts only the existing unversioned dispatcher, not old engine config or v2')
    result = copy.deepcopy(raw)
    result['connection'] = {'host': validated.host, 'port': validated.port, 'timeout_s': validated.timeout_s}
    result['timing'] = {'poll_ms': validated.poll_s * 1000, 'max_trigger_gap_ms': validated.max_gap_s * 1000,
                        'interval_ms': validated.interval_s * 1000, 'max_lateness_ms': validated.max_lateness_s * 1000}
    result.update(schema_version=2, execution_backend='legacy_verified_oxygen_sim', simulation_only=True,
                  measurement_roles={}, interlocks={'one_active_profile_global': True}, simulation={},
                  recording={'enabled': False, 'markers': False},
                  instruments={'rzxa': {'adapter': 'disabled', 'enable_output_control': False}},
                  live_control={'enable_physical_aout': False, 'enable_rzxa_output_toggle': False,
                                'enable_real_rzxa_scpi': False, 'require_explicit_hardware_authorization': True})
    for output in result['outputs'].values():
        output.update(role='legacy_simulated', const_property=CONSTANT_PROPERTY, min_aout_v=0, max_aout_v=1,
                      completion_policy='return_neutral', authorization={'simulation': True, 'physical': False})
    for assignment in result['assignments']:
        if assignment.get('profile'):
            assignment['profile'] = os.path.relpath(source.parent / assignment['profile'], destination.parent)
    canonical_to_legacy(result)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = source.with_name(source.name + f'.backup-{stamp}-{uuid.uuid4().hex[:8]}')
    shutil.copy2(source, backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')
    return {'event': 'migration_complete', 'source_unchanged': str(source), 'backup': str(backup), 'output': str(destination)}
