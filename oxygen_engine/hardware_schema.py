
"""Explicit physical-output authorization; no simulation normalization or fallback."""

import math
from pathlib import Path

from .adapters.oxygen import OxygenAdapter
from .dispatcher_v2_schema import LIVE_FLAGS, ROLES, ROOT_KEYS, V2Config, keys, load_v2_profile, number

MEASUREMENTS = {'dc_bus_voltage', 'motor_input_mapping_verified', 'motor_physical_disable_verified',
                'supply_external_ready', 'regen_safe', 'supply_shutdown_permitted',
                'supply_standalone_test', 'supply_disabled'}
HARDWARE_ACKNOWLEDGEMENTS = {'wiring_verified', 'supply_mapping_verified', 'motor_neutral_verified',
                             'external_emergency_stop_verified', 'external_enable_control_verified'}


def exact_name(value, label):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n\x00'):
        raise ValueError(f'{label}: configure an exact nonempty OXYGEN channel name')
    return value


def confirm_hardware_outputs(config, confirmed_outputs, *, live, interlocks_confirmed):
    if config.raw.get('execution_backend') != 'oxygen_hardware':
        raise PermissionError('Physical authorization requires oxygen_hardware configuration')
    required = {spec['channel'] for spec in config.raw['outputs'].values()}
    if live is not True or interlocks_confirmed is not True or set(confirmed_outputs) != required:
        raise PermissionError('Hardware execution requires --live, --confirm-hardware-interlocks, '
                              'and --confirm-physical-output for BOTH exact configured AOUT names')


def load_hardware_config(path, raw):
    keys(raw, (ROOT_KEYS - {'simulation'}) | {'hardware_authorization'}, 'hardware dispatcher')
    if type(raw.get('schema_version')) is not int or raw['schema_version'] != 2:
        raise ValueError('Hardware requires schema_version 2')
    if raw.get('execution_backend') != 'oxygen_hardware' or raw.get('simulation_only') is not False:
        raise ValueError('Hardware requires execution_backend oxygen_hardware and simulation_only false')
    flags = raw.get('live_control', {})
    keys(flags, LIVE_FLAGS, 'live_control')
    expected_flags = {'enable_physical_aout': True, 'enable_rzxa_output_toggle': False,
                      'enable_real_rzxa_scpi': False, 'require_explicit_hardware_authorization': True}
    if set(flags) != set(expected_flags) or any(flags[k] is not v for k, v in expected_flags.items()):
        raise ValueError('Hardware enables physical AOUT only; direct RZ-XA/output-enable control stays disabled')
    acknowledgements = raw.get('hardware_authorization', {})
    keys(acknowledgements, HARDWARE_ACKNOWLEDGEMENTS, 'hardware_authorization')
    if set(acknowledgements) != HARDWARE_ACKNOWLEDGEMENTS or any(v is not True for v in acknowledgements.values()):
        raise ValueError('Explicitly verify wiring, scaling, neutral, external enable control and independent emergency stop')
    connection = raw.get('connection', {})
    keys(connection, {'host', 'port', 'timeout_s'}, 'connection')
    exact_name(connection.get('host'), 'host')
    if type(connection.get('port')) is not int:
        raise ValueError('port must be an integer')
    number(connection['port'], 'port', 1, 65535)
    number(connection.get('timeout_s'), 'timeout', .001, 5)
    outputs = raw.get('outputs')
    if not isinstance(outputs, dict) or set(outputs) != set(ROLES):
        raise ValueError('Both physical output roles are required')
    names, ids, bindings = set(), set(), set()
    for role, spec in outputs.items():
        keys(spec, {'role', 'channel', 'display_name', 'expected_id', 'channel_discovery_required', 'binding',
                    'authorization', 'mode', 'source', 'const_property', 'min_aout_v', 'max_aout_v',
                    'startup_aout_v', 'neutral_aout_v', 'neutral_verified', 'mapping_verified', 'mapping',
                    'output_enable', 'completion_policy', 'on_cancel', 'on_fault', 'on_shutdown'}, role)
        name = exact_name(spec.get('channel'), role)
        binding = exact_name(spec.get('binding'), f'{role} binding')
        if name in names or binding in bindings:
            raise ValueError('Physical output mappings cannot alias')
        names.add(name)
        bindings.add(binding)
        if 'display_name' in spec:
            exact_name(spec['display_name'], 'display_name')
        cid = spec.get('expected_id')
        if cid is not None:
            OxygenAdapter._channel_id(cid)
            if cid in ids:
                raise ValueError('Expected hardware IDs cannot alias')
            ids.add(cid)
        if spec.get('role') != ROLES[role] or spec.get('mode') != 'ConstOutput' or spec.get('source') != 'DISCOVER_CONSTANT':
            raise ValueError('Hardware supports only the assigned role and discovered ConstOutput sources')
        exact_name(spec.get('const_property'), 'const_property')
        if spec.get('channel_discovery_required') is not True or spec.get('mapping_verified') is not True:
            raise ValueError('Physical mappings must be verified and discovery remains mandatory')
        permission = spec.get('authorization')
        if (not isinstance(permission, dict) or set(permission) != {'simulation', 'physical'} or
                permission['simulation'] is not False or permission['physical'] is not True):
            raise ValueError('Each physical output requires authorization simulation false / physical true')
        number(spec.get('min_aout_v'), 'minimum', 0, 0)
        number(spec.get('max_aout_v'), 'maximum', .001, .5 if role == 'rzxa_voltage_demand' else 3.3)
        policy = 'preserve_demand' if role == 'rzxa_voltage_demand' else 'return_neutral'
        if any(spec.get(k) != policy for k in ('on_cancel', 'on_fault', 'on_shutdown')):
            raise ValueError('Physical supply must preserve demand; motor must attempt neutral on termination')
        if role == 'vesc_throttle':
            number(spec.get('neutral_aout_v'), 'verified motor neutral', 0, 0)
            if spec.get('neutral_verified') is not True or spec.get('completion_policy') != 'return_neutral':
                raise ValueError('Physical motor requires explicitly verified 0 V neutral and return_neutral completion')
        else:
            number(spec.get('startup_aout_v'), 'documented startup', 0, 0)
            if spec.get('completion_policy') != 'hold_until_explicit_change' or spec.get('output_enable') != 'EXTERNAL_VERIFIED':
                raise ValueError('Supply hold and externally controlled output enable must be explicit')
            mapping = spec.get('mapping', {})
            keys(mapping, {'type', 'gain_bus_v_per_aout_v', 'verified'}, 'verified supply mapping')
            if mapping.get('type') != 'linear_verified' or mapping.get('verified') is not True:
                raise ValueError('Declare the verified supply mapping; it is never inferred from channel names')
            number(mapping.get('gain_bus_v_per_aout_v'), 'verified supply gain', .001, 10000)
    timing = raw.get('timing', {})
    keys(timing, {'poll_ms', 'max_trigger_gap_ms', 'interval_ms', 'max_lateness_ms'}, 'timing')
    poll = number(timing.get('poll_ms'), 'poll_ms', 20, 250)
    if number(timing.get('max_trigger_gap_ms'), 'max_trigger_gap_ms', 20, 1000) <= poll:
        raise ValueError('Trigger gap must exceed polling interval')
    number(timing.get('interval_ms'), 'interval_ms', 20, 250)
    number(timing.get('max_lateness_ms'), 'max_lateness_ms', 1, 1000)
    interlocks = raw.get('interlocks', {})
    required_true = {'motor_requires_measured_bus_in_range', 'supply_down_requires_motor_neutral',
                     'one_active_profile_global', 'external_emergency_stop_required_for_live'}
    keys(interlocks, required_true | {'bus_target_v', 'bus_tolerance_v', 'max_feedback_age_s',
         'motor_supply_demand_v', 'allow_unverified_simulated_interlock_values',
         'allow_unverified_live_interlock_values'}, 'hardware interlocks')
    if any(interlocks.get(k) is not True for k in required_true) or any(interlocks.get(k) is not False for k in (
            'allow_unverified_simulated_interlock_values', 'allow_unverified_live_interlock_values')):
        raise ValueError('Hardware requires measured-bus/neutral interlocks and forbids unverified/fake interlock values')
    target = number(interlocks.get('bus_target_v'), 'bus target', 1, 60)
    number(interlocks.get('bus_tolerance_v'), 'bus tolerance', .01, 5)
    number(interlocks.get('max_feedback_age_s'), 'feedback age', .01, 1)
    supply = outputs['rzxa_voltage_demand']
    demand = number(interlocks.get('motor_supply_demand_v'), 'motor supply prerequisite', 0, supply['max_aout_v'])
    if not math.isclose(demand * supply['mapping']['gain_bus_v_per_aout_v'], target, rel_tol=1e-9, abs_tol=1e-6):
        raise ValueError('Verified supply gain, motor demand prerequisite and bus target disagree')
    measurements = raw.get('measurement_roles')
    if not isinstance(measurements, dict) or set(measurements) != MEASUREMENTS:
        raise ValueError('Declare all independent hardware measurement/interlock roles; no fixture fallback exists')
    for name, measurement in measurements.items():
        keys(measurement, {'kind', 'unit', 'channel', 'property', 'mapping_verified'}, name)
        expected_kind = 'number' if name == 'dc_bus_voltage' else 'boolean'
        if measurement.get('kind') != expected_kind or measurement.get('mapping_verified') is not True:
            raise ValueError(f'{name}: an explicitly verified hardware measurement mapping is required')
        channel = exact_name(measurement.get('channel'), f'{name} real measurement')
        if channel in names:
            raise ValueError('Independent hardware feedback cannot alias an AOUT command')
        if name == 'dc_bus_voltage':
            if measurement.get('unit') != 'V' or measurement.get('property') is not None:
                raise ValueError('DC bus uses the real OXYGEN numeric measurement channel in V, not an output property')
        else:
            exact_name(measurement.get('property'), f'{name} Boolean property')
    if raw.get('instruments') != {'rzxa': {'adapter': 'disabled', 'enable_output_control': False}}:
        raise ValueError('Direct RZ-XA and output-enable commands are not implemented')
    recording = raw.get('recording', {})
    keys(recording, {'enabled', 'markers'}, 'recording')
    if recording.get('enabled') is not False or type(recording.get('markers')) is not bool:
        raise ValueError('Recording must remain disabled; markers are local logs only')
    assignments = raw.get('assignments')
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 6:
        raise ValueError('Provide 1..6 hardware assignments')
    profiles, triggers = {}, set()
    sensor_names = {m['channel'] for m in measurements.values()}
    for assignment in assignments:
        keys(assignment, {'name', 'enabled', 'trigger', 'output', 'profile'}, 'hardware assignment')
        if type(assignment.get('enabled', True)) is not bool:
            raise ValueError('Assignment enabled must be Boolean')
        if not assignment.get('enabled', True):
            continue
        name = exact_name(assignment.get('name'), 'assignment name')
        trigger = exact_name(assignment.get('trigger'), 'DOUT trigger')
        if name in profiles or trigger in triggers or trigger in names | sensor_names:
            raise ValueError('Hardware assignment names/triggers must be unique and independent of outputs/interlocks')
        output = assignment.get('output')
        if output not in outputs:
            raise ValueError('Unknown hardware output role')
        filename = exact_name(assignment.get('profile'), 'hardware profile path')
        profiles[name] = load_v2_profile(Path(path).parent / filename, output, outputs[output], simulation_only=False)
        triggers.add(trigger)
    if not profiles:
        raise ValueError('At least one enabled hardware profile is required')
    return V2Config(Path(path).resolve(), raw, profiles)
