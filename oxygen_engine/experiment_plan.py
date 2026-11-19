"""Experiment plans compose the V3 writer; they do not replace dispatcher bindings."""

import copy
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError
from .schema import BRAKE, DispatcherConfig, exact_name, keys, number, read_json

SUPPLY = 'rzxa_voltage_demand'
MOTOR = 'vesc_throttle'
NAMES = {SUPPLY: 'Takasago Voltage Control', MOTOR: 'Drive Control'}
ALLOWED_EXPERIMENT_ROLES = frozenset(NAMES) | {BRAKE}
NUMERIC_UNITS = {'dc_bus_voltage': 'V', 'dc_current': 'A', 'dc_power': 'W', 'ac_power': 'W',
                 'erpm': 'rpm', 'motor_current': 'A', 'acquisition_time_s': 's',
                 'vesc_telemetry_age_s': 's', 'matrix_sample_count': ''}
BOOLEAN_ROLES = {'source_sink_ready', 'physical_fault', 'matrix_sample_valid'}
SAFETY_KEYS = {'max_age_s', 'bus_tolerance_v', 'bus_min_v', 'bus_max_v', 'max_abs_dc_current_a',
               'max_abs_motor_current_a', 'max_abs_erpm', 'bus_change_max_abs_erpm',
               'dc_power_consistency_w', 'minimum_dc_power_w', 'steady_bus_span_v',
               'steady_dc_current_span_a', 'steady_motor_current_span_a', 'steady_erpm_span'}
TIMING_KEYS = {'poll_s', 'supply_ramp_s', 'motor_ramp_s', 'neutral_ramp_s', 'bus_stable_s',
               'bus_timeout_s', 'neutral_timeout_s', 'settle_s', 'settle_timeout_s', 'capture_s',
               'max_supply_rate_v_per_s', 'max_motor_rate_v_per_s'}
ACKS = {'adc1_current_mode_verified', 'feedback_and_limits_verified', 'native_matrix_verified',
        'supply_scpi_fault_resolved'}


@dataclass(frozen=True)
class ExperimentPlan:
    path: Path
    raw: dict
    points: tuple


def load_experiment(path, config):
    path = Path(path).resolve()
    raw = read_json(path)
    keys(raw, {'experiment_version', 'name', 'type', 'bus_targets_v', 'motor_targets', 'dynamic_steps',
               'commissioned_drive_limit_v', 'higher_drive_limit_authorized', 'commissioning',
               'timing', 'safety', 'feedback'}, 'experiment')
    if type(raw.get('experiment_version')) is not int or raw['experiment_version'] != 1:
        raise ConfigError('Expected experiment_version 1 (alongside unchanged V3 dispatcher bindings)')
    exact_name(raw.get('name'), 'experiment name')
    kind = raw.get('type')
    active = set(config.raw['outputs'])
    if (kind not in {'grid', 'dynamic'} or not config.open_loop or
            not set(NAMES).issubset(active) or active - ALLOWED_EXPERIMENT_ROLES):
        raise ConfigError(
            'Experiments require grid/dynamic open-loop V3 outputs with '
            'rzxa_voltage_demand + vesc_throttle (optional vesc_brake is ignored)')
    for role, name in NAMES.items():
        if config.physical and config.raw['outputs'][role]['channel'] != name:
            raise ConfigError(f'{role}: preserve exact channel {name!r}')
    if BRAKE in active and config.physical:
        raise ConfigError('Experiments do not authorize physical vesc_brake / Braking Control')
    gain = config.raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v']
    if gain != 80 or (config.physical and config.raw['outputs'][SUPPLY]['mapping']['verified'] is not True):
        raise ConfigError('Experiment requires verified 80 V/V supply scaling for physical execution')
    limit = number(raw.get('commissioned_drive_limit_v'), 'commissioned drive limit', 0, 3.3)
    if type(raw.get('higher_drive_limit_authorized')) is not bool:
        raise ConfigError('higher_drive_limit_authorized must be Boolean')
    if limit > .4 and raw['higher_drive_limit_authorized'] is not True:
        raise ConfigError('Limits above 0.40 V require separate higher-drive authorization')
    acks = raw.get('commissioning')
    keys(acks, ACKS, 'experiment commissioning')
    if set(acks) != ACKS or any(type(v) is not bool for v in acks.values()):
        raise ConfigError('Declare every experiment commissioning acknowledgement as Boolean')
    timing, safety = raw.get('timing'), raw.get('safety')
    keys(timing, TIMING_KEYS, 'experiment timing')
    keys(safety, SAFETY_KEYS, 'experiment safety')
    if set(timing) != TIMING_KEYS or set(safety) != SAFETY_KEYS:
        raise ConfigError('Declare every timing and safety bound')
    for key, value in timing.items():
        number(value, key, .01, 300)
    number(timing['poll_s'], 'feedback poll', .02, 1)
    for key, value in safety.items():
        number(value, key, 0, 100000)
    number(safety['max_age_s'], 'maximum feedback age', .05, 2)
    if (safety['bus_min_v'] >= safety['bus_max_v'] or safety['bus_tolerance_v'] <= 0 or
            safety['minimum_dc_power_w'] <= 0 or
            safety['bus_change_max_abs_erpm'] > safety['max_abs_erpm'] or
            timing['bus_timeout_s'] < timing['bus_stable_s'] or
            timing['settle_timeout_s'] < timing['settle_s'] or timing['capture_s'] < timing['poll_s'] * 2):
        raise ConfigError('Inconsistent experiment safety/window bounds')
    buses = raw.get('bus_targets_v')
    if not isinstance(buses, list) or not 1 <= len(buses) <= 20 or len(set(buses)) != len(buses):
        raise ConfigError('Provide 1..20 unique bus targets')
    for bus in buses:
        number(bus, 'bus target', max(.1, safety['bus_min_v']), safety['bus_max_v'])
        number(bus / gain, 'supply demand', 0, config.raw['outputs'][SUPPLY]['max_aout_v'])
    targets = raw.get('motor_targets') if kind == 'grid' else raw.get('dynamic_steps')
    forbidden = 'dynamic_steps' if kind == 'grid' else 'motor_targets'
    if forbidden in raw or (kind == 'dynamic' and len(buses) != 1):
        raise ConfigError('Grid uses motor_targets; dynamic uses dynamic_steps at one bus target')
    if not isinstance(targets, list) or not 1 <= len(targets) <= 30:
        raise ConfigError('Provide 1..30 motor targets/steps')
    points = []
    for bus in buses:
        for target in targets:
            allowed = {'adc_v', 'enabled', 'commissioned', 'disabled_reason'}
            if kind == 'dynamic':
                allowed |= {'ramp_s', 'hold_s', 'label'}
            keys(target, allowed, 'motor target')
            value = number(target.get('adc_v'), 'planned ADC1 demand', 0, 3.3)
            if type(target.get('enabled')) is not bool or type(target.get('commissioned')) is not bool:
                raise ConfigError('Each target needs Boolean enabled and commissioned fields')
            if target['enabled']:
                if target['commissioned'] is not True:
                    raise ConfigError('An enabled target must be separately commissioned')
                number(value, 'authorized ADC1 demand', 0, min(limit, config.raw['outputs'][MOTOR]['max_aout_v']))
                if value > .4 and raw['higher_drive_limit_authorized'] is not True:
                    raise ConfigError('ADC1 above 0.40 V is not authorized')
            else:
                exact_name(target.get('disabled_reason'), 'disabled target reason')
            if kind == 'dynamic':
                number(target.get('ramp_s'), 'dynamic ramp', .1, 120)
                number(target.get('hold_s'), 'dynamic hold', .1, 120)
                exact_name(target.get('label'), 'dynamic label')
            points.append({'point_number': len(points) + 1, 'bus_target_v': bus, **target})
    if not any(p['enabled'] for p in points):
        raise ConfigError('At least one operating point/step must be enabled')
    if kind == 'dynamic' and [p for p in points if p['enabled']][-1]['adc_v'] != 0:
        raise ConfigError('Dynamic sequences must finish with an explicit neutral step')
    feedback = raw.get('feedback')
    required = set(NUMERIC_UNITS) | BOOLEAN_ROLES
    if kind == 'dynamic':
        required -= {'matrix_sample_count', 'matrix_sample_valid'}
    keys(feedback, required, 'experiment feedback')
    if set(feedback) != required:
        raise ConfigError(f'Provide all feedback roles: {sorted(required)}')
    names = set()
    for role, spec in feedback.items():
        keys(spec, {'channel', 'unit', 'property'}, role)
        channel = spec.get('channel')
        if channel is not None:
            exact_name(channel, role)
            if channel in names or channel in NAMES.values():
                raise ConfigError('Feedback must be unique and independent of AOUT demands')
            names.add(channel)
        if role in NUMERIC_UNITS:
            if not isinstance(spec.get('unit'), str) or spec.get('property') is not None:
                raise ConfigError(f'{role}: numeric feedback needs exact unit and no property')
            allowed_units = {'', '1'} if role == 'matrix_sample_count' else {NUMERIC_UNITS[role]}
            if spec['unit'] not in allowed_units:
                raise ConfigError(f'{role}: use normalized engineering unit {NUMERIC_UNITS[role]!r}')
        elif spec.get('property') is not None:
            exact_name(spec['property'], f'{role} Boolean property')
    return ExperimentPlan(path, raw, tuple(points))


def validate_physical_plan(plan):
    required = ACKS if plan.raw['type'] == 'grid' else ACKS - {'native_matrix_verified'}
    if not all(plan.raw['commissioning'][key] for key in required):
        raise ConfigError('Complete physical experiment commissioning, including resolving the supply SCPI fault')
    for role, spec in plan.raw['feedback'].items():
        exact_name(spec.get('channel'), f'{role} real feedback channel')
        if role in BOOLEAN_ROLES:
            exact_name(spec.get('property'), f'{role} Boolean property')


def memory_mirror(config):
    """No-I/O rehearsal of validated bindings, retaining their authoritative voltage clamps.

    This is an in-memory simulation object, never a saved dispatcher configuration or physical
    authorization shortcut. It can rehearse an explicitly commissioned physical clamp >0.4 V
    without changing the existing V3 memory-example schema ceiling.
    """
    raw = copy.deepcopy(config.raw)
    raw['transport'] = 'memory'
    raw.pop('hardware_authorization', None)
    raw.pop('open_loop_authorization', None)
    for flag in raw['live_control']:
        raw['live_control'][flag] = flag == 'require_explicit_hardware_authorization'
    fixtures = {}
    for index, (role, spec) in enumerate(raw['outputs'].items()):
        spec.update(channel=None, expected_id=None, source='CONST0', const_property='TRION/SourceChannel_A_CONST/Const0',
                    authorization={'simulation': True, 'physical': False})
        fixtures[spec['binding']] = {'name': f'FAKE {role}', 'id': str(101 + index), 'mode': 'ConstOutput',
                                     'source': 'CONST0', 'property': spec['const_property'], 'unit': 'V',
                                     'min_v': spec['min_aout_v'], 'max_v': spec['max_aout_v'], 'initial_v': 0.0}
    raw['simulation'] = {'namespace': 'FAKE_IN_PROCESS_ONLY', 'outputs': fixtures, 'streaming': False,
                         'sample_interval_s': .1, 'telemetry': {}}
    raw['measurements'] = {}
    raw['timing']['telemetry_poll_ms'] = 0
    return DispatcherConfig(config.path, raw, config.profiles)
