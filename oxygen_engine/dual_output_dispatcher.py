from __future__ import annotations

import json
import logging
import math
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .constant_pattern import execute_timeline, finite_number
from .dispatcher_v2_schema import load_v2_config, number
from .do_monitor import DiagnosticEdgeTracker
from .pattern_dispatcher import OutputPattern, Segment, StrictRotatingHandler
from .simulation_fixture import MemoryFixture, SimulationFinished, load_scenario

SUPPLY = 'rzxa_voltage_demand'
MOTOR = 'vesc_throttle'


class HeldPattern(OutputPattern):
    def value_at(self, elapsed_s):
        if elapsed_s >= self.duration_s:
            return self.segments[-1].end_v
        return super().value_at(elapsed_s)


@dataclass
class OutputState:
    value: float | None
    verified_at: float | None
    held_by: str | None = None
    status: str = 'readback_verified'


class DualOutputDispatcher:
    def __init__(self, config, backend, emit, *, clock=time.monotonic, wait=time.sleep,
                 timeline=execute_timeline):
        self.config, self.backend, self.emit = config, backend, emit
        self.clock, self.wait, self.timeline = clock, wait, timeline
        self.outputs = config.raw['outputs']
        self.simulated = config.raw['execution_backend'] != 'oxygen_hardware'
        self.output_only = not self.simulated and config.raw.get('hardware_mode') == 'output_only'
        self.states = {}
        self.active_profile = None
        self.active_role = None
        self.prepared = False
        self.started = 0
        self.completed = 0
        self.interrupted_cycle = False

    def snapshot(self, event='output_states'):
        self.emit({'event': event, 'simulated': self.simulated, 'active_profile': self.active_profile,
                   'active_role': self.active_role,
                   'outputs': {role: vars(state).copy() for role, state in self.states.items()}})

    def context(self, role):
        spec = self.outputs[role]
        key = spec.get('binding', spec.get('simulation_fixture'))
        discovered = self.backend.discover()
        fixture = discovered.get(key)
        oxygen_demo = self.config.raw['execution_backend'] in {'oxygen_demo', 'oxygen_hardware'}
        const_property = self.backend.constant_property(role) if oxygen_demo else spec['const_property']
        if oxygen_demo:
            expected = {'name': spec['channel'], 'id': self.backend.channel_id(role), 'mode': spec['mode'],
                        'source': self.backend.expected_source(role), 'property': const_property, 'unit': 'V'}
            fields = ('name', 'id', 'mode', 'source', 'property', 'unit')
        else:
            expected = self.config.raw['simulation']['outputs'][key]
            fields = ('name', 'id', 'mode', 'source', 'property', 'unit', 'min_v', 'max_v')
        if fixture is None or any(fixture.get(name) != expected.get(name) for name in fields):
            raise RuntimeError(f'{role}: fixture discovery/context changed')
        if (fixture['mode'] != spec['mode'] or fixture['source'] != expected['source'] or
                fixture['property'] != const_property or fixture['unit'] != 'V' or
                fixture['min_v'] > spec['min_aout_v'] or fixture['max_v'] < spec['max_aout_v']):
            raise RuntimeError(f'{role}: fixture does not cover authorized mode/property/range')
        return key

    def verify(self, role, expected=None):
        key = self.context(role)
        actual = number(self.backend.read_output(key), f'{role} readback',
                        self.outputs[role]['min_aout_v'], self.outputs[role]['max_aout_v'])
        if expected is None:
            expected = self.states[role].value
        if expected is None or not math.isclose(actual, expected, abs_tol=1e-6, rel_tol=0):
            self.states[role].status = 'unknown'
            raise RuntimeError(f'{role}: expected {expected}, read back {actual}')
        self.states[role].verified_at = self.clock()
        self.states[role].status = 'readback_verified'
        return actual

    def prepare(self):
        discovered = self.backend.discover()
        ids, names = set(), set()
        readings = {}
        for role, spec in self.outputs.items():
            key = self.context(role)
            fixture = discovered[key]
            if fixture['id'] in ids or fixture['name'] in names:
                raise RuntimeError('Output roles alias the same discovered fixture channel')
            ids.add(fixture['id'])
            names.add(fixture['name'])
            actual = number(self.backend.read_output(key), 'startup readback', spec['min_aout_v'], spec['max_aout_v'])
            if role == MOTOR and actual != spec['neutral_aout_v']:
                raise RuntimeError('Startup motor is not neutral; refusing all writes')
            readings[role] = actual
        for role, actual in readings.items():
            spec = self.outputs[role]
            self.backend.authorize(role, spec.get('binding', spec.get('simulation_fixture')), spec)
            self.states[role] = OutputState(actual, self.clock(), 'adopted_actual_readback' if actual else None)
        self.prepared = True
        self.snapshot('startup_actual_readback_no_state_restoration')
        if self.output_only:
            self.emit({'event': 'output_only_warning', 'simulated': False,
                       'message': 'No live bus, readiness or regen feedback. Setpoint readback does not establish physical safety; external protections are required.'})
        self.emit({'event': 'preflight_passed', 'simulated': self.simulated, 'backend': self.config.raw['execution_backend'],
                   'live_feedback_enabled': not self.simulated and not self.output_only,
                   'physical_mapping_verified': not self.simulated, 'output_enable_controlled': False})

    def sample(self, name, *, max_age=None):
        sample = self.backend.measure(name)
        limit = self.config.raw['interlocks']['max_feedback_age_s'] if max_age is None else max_age
        age = self.clock() - sample.sampled_at
        trusted = (sample.simulated is self.simulated and
                   sample.source.startswith('FAKE ' if self.simulated else 'OXYGEN '))
        if not trusted or not 0 <= age <= limit:
            raise RuntimeError(f'Missing/stale/untrusted independent feedback sample: {name}, age={age}')
        kind = self.config.raw['measurement_roles'][name]['kind']
        if kind == 'boolean' and type(sample.value) is not bool:
            raise RuntimeError(f'{name} must be Boolean')
        if kind == 'number':
            number(sample.value, name, -1000, 1000)
        self.emit({'event': 'independent_feedback', 'name': name, 'value': sample.value,
                   'age_s': age, 'source': sample.source, 'simulated': self.simulated})
        return sample.value

    def require_true(self, name):
        if self.sample(name) is not True:
            raise RuntimeError(f'Interlock not satisfied: {name}')

    def motor_neutral(self):
        self.verify(MOTOR, self.outputs[MOTOR]['neutral_aout_v'])

    def regen_safe(self):
        self.motor_neutral()
        if self.output_only:
            return
        self.require_true('regen_safe')
        self.require_true('supply_shutdown_permitted')

    def motor_interlocks(self, profile):
        interlocks = self.config.raw['interlocks']
        self.verify(SUPPLY)
        if not math.isclose(self.states[SUPPLY].value, interlocks['motor_supply_demand_v'], abs_tol=1e-6, rel_tol=0):
            raise RuntimeError('Supply demand readback is not the configured motor prerequisite')
        if self.output_only:
            return
        requirement = profile.requires.get('measured_bus_voltage', {})
        target = requirement.get('target_v', interlocks['bus_target_v'])
        if target != interlocks['bus_target_v']:
            raise RuntimeError('Profile cannot override configured bus-voltage target')
        tolerance = min(requirement.get('tolerance_v', interlocks['bus_tolerance_v']), interlocks['bus_tolerance_v'])
        max_age = min(requirement.get('max_age_s', interlocks['max_feedback_age_s']), interlocks['max_feedback_age_s'])
        measured = self.sample('dc_bus_voltage', max_age=max_age)
        if abs(measured - target) > tolerance:
            raise RuntimeError(f'Independent measured bus {measured} V is outside {target} +/- {tolerance} V')
        self.require_true('motor_input_mapping_verified')
        self.require_true('motor_physical_disable_verified')
        self.require_true('supply_external_ready')

    def assert_preconditions(self, profile):
        requested = {}
        for step in profile.steps:
            if step['type'] == 'assert_preconditions':
                requested.update(step['params'])
        if profile.output == MOTOR:
            self.motor_neutral()
            self.motor_interlocks(profile)
        else:
            self.motor_neutral()
        if requested.get('supply_output_enabled_or_verified_external') or 'supply_output_enable' in profile.requires:
            self.require_true('supply_external_ready')
        if requested.get('supply_shutdown_permitted') or profile.requires.get('no_unhandled_regeneration'):
            self.regen_safe()
        if requested.get('supply_standalone_test') or profile.requires.get('supply_standalone_test'):
            self.require_true('supply_standalone_test')
        if requested.get('motor_neutral') or profile.requires.get('motor_neutral'):
            self.motor_neutral()
        if requested.get('bus_voltage_validated'):
            self.motor_interlocks(profile)
        if requested.get('current_aout_must_be_zero_or_already_target') or requested.get('reject_other_start_values'):
            current = self.verify(profile.output)
            if not any(math.isclose(current, value, abs_tol=1e-6, rel_tol=0) for value in (0, profile.completion['value'])):
                raise RuntimeError('Repeat-safe supply profile requires zero or already-at-target readback')

    def command(self, role, value):
        spec = self.outputs[role]
        value = number(value, 'setpoint', spec['min_aout_v'], spec['max_aout_v'])
        self.context(role)
        self.emit({'event': 'write_attempt', 'simulated': self.simulated, 'output': role, 'requested_v': value,
                   'owner': self.active_profile})
        self.backend.write(role, value)
        self.verify(role, value)
        self.states[role].value = value
        self.states[role].held_by = self.active_profile if value != spec.get('neutral_aout_v', 0) else None
        self.emit({'event': 'setpoint_ack', 'simulated': self.simulated, 'output': role, 'readback_v': value})

    def compile(self, profile):
        value, elapsed = self.states[profile.output].value, 0.0
        segments, checkpoints = [], []
        for index, step in enumerate(profile.steps):
            kind, params = step['type'], step.get('params', {})
            if kind == 'set_aout':
                if value != params['value']:
                    raise RuntimeError('Initial set_aout cannot jump from an existing demand')
                if profile.output == SUPPLY:
                    self.require_true('supply_disabled')
            elif kind in {'hold', 'ramp_aout'}:
                duration = finite_number(params['duration_s'], 'duration')
                target = value if kind == 'hold' else finite_number(params['target'], 'target')
                if 'rate_per_s' in params and not math.isclose(params['rate_per_s'], abs(target - value) / duration, rel_tol=1e-9, abs_tol=1e-12):
                    raise RuntimeError('Ramp rate disagrees with actual starting readback')
                segments.append(Segment(elapsed, elapsed + duration, value, target, f'{index}_{kind}'))
                elapsed += duration
                value = target
            elif kind == 'checkpoint':
                checkpoints.append((elapsed, params['name']))
        return HeldPattern(profile.name, tuple(segments)), checkpoints

    def execute_profile(self, name, *, trigger_observed=None):
        if not self.prepared or self.active_profile is not None:
            raise RuntimeError('Dispatcher is not prepared or another profile owns an output')
        profile = self.config.profiles[name]
        role = profile.output
        self.verify(role)
        self.assert_preconditions(profile)
        if trigger_observed is not None and not 0 <= self.clock() - trigger_observed <= self.config.raw['timing']['max_trigger_gap_ms'] / 1000:
            self.emit({'event': 'trigger_stale', 'assignment': name, 'message': 'Precondition checks aged out the edge; no run'})
            return
        current = self.states[role].value
        self.active_profile, self.active_role = profile.name, role
        self.started += 1
        self.interrupted_cycle = True
        self.snapshot('profile_ownership_acquired')
        try:
            requirements = [s.get('params', {}) for s in profile.steps if s['type'] == 'assert_preconditions']
            repeat_guard = any(p.get('current_aout_must_be_zero_or_already_target') for p in requirements)
            if role == SUPPLY and repeat_guard and math.isclose(current, profile.completion['value'], abs_tol=1e-6, rel_tol=0):
                self.emit({'event': 'profile_noop_already_at_target', 'profile': profile.name, 'output': role})
                result = {'actual_elapsed_s': 0.0, 'repeat_safe_noop': True}
            else:
                pattern, checkpoints = self.compile(profile)
                sent = set()

                def progress(record):
                    if record.get('event') == 'progress':
                        for index, (at, checkpoint) in enumerate(checkpoints):
                            if index not in sent and record['elapsed_s'] >= at:
                                sent.add(index)
                                self.emit({'event': 'checkpoint', 'profile': profile.name, 'name': checkpoint,
                                           'local_marker_only': True, 'marker_enabled': self.config.raw['recording']['markers']})
                    self.emit({**record, 'profile': profile.name, 'output': role, 'simulated': self.simulated})

                def guard(value):
                    self.verify(role)
                    if role == MOTOR:
                        self.motor_interlocks(profile)
                    else:
                        self.motor_neutral()
                        if value < self.states[SUPPLY].value - 1e-9:
                            self.regen_safe()
                    self.command(role, value)

                def verify(value):
                    if role == MOTOR:
                        self.motor_interlocks(profile)
                    else:
                        self.motor_neutral()
                    self.verify(role, value)

                began = self.clock()
                result = self.timeline(pattern, guard, verify, progress,
                                       interval_s=self.config.raw['timing']['interval_ms'] / 1000,
                                       max_lateness_s=self.config.raw['timing']['max_lateness_ms'] / 1000,
                                       clock=self.clock, wait=self.wait)
                if self.clock() - began > profile.timeout_s:
                    raise TimeoutError('Profile exceeded timeout including I/O')
            self.verify(role, profile.completion['value'])
            self.states[role].held_by = profile.name if profile.completion['mode'] == 'hold_final' else None
            self.completed += 1
            self.interrupted_cycle = False
            self.emit({'event': 'pattern_cycle_complete', 'assignment': name, 'profile': profile.name,
                       'output': role, 'completion': profile.completion['mode'], 'simulated': self.simulated, **result})
            self.snapshot('profile_completion_state')
        except BaseException:
            self.snapshot('profile_interrupted_state')
            raise
        finally:
            self.active_profile = self.active_role = None

    def monitor(self):
        timing = self.config.raw['timing']
        trackers = {name: DiagnosticEdgeTracker(timing['max_trigger_gap_ms'] / 1000) for name in self.config.profiles}
        assignments = [a for a in self.config.raw['assignments'] if a.get('enabled', True)]
        last_verify = self.clock()
        self.emit({'event': 'dispatcher_ready', 'backend': self.config.raw['execution_backend'],
                   'message': 'LOW then HIGH starts once; busy presses ignored'})
        while True:
            ready = []
            for assignment in assignments:
                began = self.clock()
                value = self.backend.read_button(assignment['trigger'])
                now = self.clock()
                tracker = trackers[assignment['name']]
                if now - began > timing['max_trigger_gap_ms'] / 1000:
                    tracker.reset()
                    continue
                if type(value) is not bool:
                    raise RuntimeError('Invalid simulated DO level')
                observation = tracker.observe(value, now)
                self.emit({'event': 'trigger_sample', 'assignment': assignment['name'], **observation})
                if observation['edge'] != 'steady':
                    self.emit({'event': 'trigger_state', 'assignment': assignment['name'], **observation})
                if observation['press']:
                    ready.append((assignment['name'], now))
            if ready:
                name, observed = ready[0]
                if self.clock() - observed <= timing['max_trigger_gap_ms'] / 1000:
                    for ignored, _ in ready[1:]:
                        self.emit({'event': 'trigger_ignored', 'assignment': ignored, 'reason': 'config order wins; no queue'})
                    self.execute_profile(name, trigger_observed=observed)
                for tracker in trackers.values():
                    tracker.reset()
                last_verify = self.clock()
                self.emit({'event': 'dispatcher_ready', 'message': 'Fresh LOW then HIGH required on every button'})
            elif self.clock() - last_verify >= .5:
                for role in self.states:
                    self.verify(role)
                last_verify = self.clock()
            self.wait(timing['poll_ms'] / 1000)

    def cleanup(self):
        safe = True
        if not self.prepared:
            self.emit({'event': 'cleanup_not_authorized', 'message': 'No startup output writes or guessed restoration'})
            return True
        try:
            self.context(MOTOR)
            self.command(MOTOR, self.outputs[MOTOR]['neutral_aout_v'])
            self.emit({'event': 'motor_neutral_verified', 'simulated': self.simulated})
        except (Exception, KeyboardInterrupt) as exc:
            self.states[MOTOR].status = 'unknown'
            safe = False
            self.emit({'event': 'motor_neutral_unknown', 'error': str(exc)})
        try:
            self.verify(SUPPLY)
            self.emit({'event': 'supply_demand_preserved', 'value_v': self.states[SUPPLY].value,
                       'not_power_off': True, 'simulated': self.simulated})
        except (Exception, KeyboardInterrupt) as exc:
            self.states[SUPPLY].status = 'unknown'
            safe = False
            self.emit({'event': 'supply_demand_unknown', 'error': str(exc), 'no_supply_write_attempted': True})
        try:
            self.sample('dc_bus_voltage')
        except (Exception, KeyboardInterrupt) as exc:
            self.emit({'event': 'bus_feedback_unknown', 'error': str(exc), 'not_proof_of_deenergization': True})
        self.snapshot('termination_output_state')
        return safe

    def run(self):
        code, status = 1, 'fault'
        try:
            self.backend.connect()
            self.prepare()
            self.monitor()
        except SimulationFinished:
            code, status = (1, 'scenario_interrupted_active_profile') if self.interrupted_cycle else (0, 'simulation_complete')
        except KeyboardInterrupt:
            code, status = 130, 'cancelled'
        except Exception as exc:
            self.emit({'event': 'fault', 'error_type': type(exc).__name__, 'error': str(exc)})
        finally:
            try:
                if not self.cleanup():
                    code, status = 1, 'cleanup_unknown'
            finally:
                self.backend.close()
        self.emit({'event': 'dispatcher_summary', 'schema_version': 2, 'backend': self.config.raw['execution_backend'],
                   'status': status, 'started_cycles': self.started, 'completed_cycles': self.completed,
                   'physical_actuation': not self.simulated and getattr(self.backend, 'writes_attempted', 0) > 0,
                   'supply_output_enable_controlled': False,
                   'output_states': {role: vars(state).copy() for role, state in self.states.items()},
                   'physical_bus_state': 'not established by simulation' if self.simulated else 'not inferred from AOUT setpoints'})
        return code


def run_v2_cli(args):
    config = load_v2_config(args.bindings)
    backend_name = config.raw['execution_backend']
    hardware = backend_name == 'oxygen_hardware'
    output_only = hardware and config.raw.get('hardware_mode') == 'output_only'
    if not hardware and (getattr(args, 'confirm_physical_output', []) or getattr(args, 'confirm_hardware_interlocks', False)):
        raise ValueError('Physical confirmations require the explicit oxygen_hardware backend')
    if args.live and not hardware:
        raise ValueError('--live requires execution_backend oxygen_hardware; simulation backends stay locked')
    if backend_name != 'memory_fixture' and getattr(args, 'scenario', None):
        raise ValueError('OXYGEN backends use real DOUT buttons; --scenario is memory_fixture only')
    triggers = {a['name']: a['trigger'] for a in config.raw['assignments'] if a.get('enabled', True)}
    events = load_scenario(args.scenario, config.raw.get('measurement_roles', {}), triggers.values()) if getattr(args, 'scenario', None) else []
    if args.execute:
        if hardware:
            from .hardware_schema import confirm_hardware_outputs

            if args.confirm_simulated_output or getattr(args, 'confirm_demo_output', []):
                raise ValueError('Hardware execution cannot use simulated/demo output confirmations')
            confirm_hardware_outputs(config, getattr(args, 'confirm_physical_output', []), live=args.live,
                                     interlocks_confirmed=getattr(args, 'confirm_hardware_interlocks', False))
        elif not args.confirm_simulated_output:
            raise ValueError('Execution requires --confirm-simulated-output')
        if backend_name == 'memory_fixture' and not getattr(args, 'scenario', None):
            raise ValueError('Fixture execution requires an explicit --scenario')
        if backend_name == 'oxygen_demo':
            from .dispatcher_v2_schema import confirm_demo_outputs

            confirm_demo_outputs(config.raw['outputs'], getattr(args, 'confirm_demo_output', []))
    plan = {'event': 'dispatcher_v2_plan', 'schema_version': 2, 'backend': backend_name,
            'fake_outputs_and_measurements': backend_name == 'memory_fixture', 'physical_actuation': False,
            'physical_actuation_requested': hardware and args.execute,
            'oxygen_demo_io': backend_name == 'oxygen_demo',
            'hardware_mode': config.raw.get('hardware_mode', 'feedback_protected') if hardware else None,
            'bus_feedback': 'NONE: output-only; external protections required' if output_only else (
                'real independent OXYGEN measurement' if hardware else 'FAKE independent injected sensor'),
            'scenario_events': len(events), 'assignments': [{'name': name, 'trigger': triggers[name], 'output': p.output,
            'completion': p.completion, 'profile': str(p.path)} for name, p in config.profiles.items()]}
    if not args.execute:
        print(json.dumps({**plan, 'connected': False, 'writes_attempted': 0}, indent=2, allow_nan=False))
        return 0
    default_log = {'memory_fixture': 'logs/dispatcher-v2-fixture.jsonl',
                   'oxygen_demo': 'logs/dispatcher-v2-oxygen-demo.jsonl',
                   'oxygen_hardware': 'logs/dispatcher-v2-hardware.jsonl'}[backend_name]
    path = Path(args.log_file or default_log).resolve()
    protected = {config.path} | {p.path for p in config.profiles.values()}
    if getattr(args, 'scenario', None):
        protected.add(Path(args.scenario).resolve())
    if any(candidate in protected for candidate in [path] + [path.with_name(path.name + f'.{i}') for i in range(1, 6)]):
        raise ValueError('Log or rotated backup would overwrite an input file')
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = StrictRotatingHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    failed = False
    run_id = uuid.uuid4().hex
    try:
        def emit(record):
            nonlocal failed
            if failed:
                return
            record = {'ts': datetime.now(timezone.utc).isoformat(), 'monotonic_s': time.monotonic(),
                      'run_id': run_id, 'backend': backend_name, **record}
            line = json.dumps(record, allow_nan=False)
            try:
                handler.emit(logging.LogRecord('dispatcher-v2', logging.INFO, '', 0, line, (), None))
                if record['event'] not in {'query', 'trigger_sample', 'independent_feedback', 'setpoint_ack', 'write_attempt'}:
                    print(line, flush=True)
            except OSError:
                failed = True
                raise

        emit(plan)
        if hardware:
            from .oxygen_hardware_backend import OxygenHardwareBackend

            backend = OxygenHardwareBackend(config, emit, confirmed_outputs=args.confirm_physical_output,
                                            live=args.live, interlocks_confirmed=args.confirm_hardware_interlocks)
        elif backend_name == 'oxygen_demo':
            from .oxygen_demo_backend import OxygenDemoBackend

            backend = OxygenDemoBackend(config, emit, confirmed_outputs=args.confirm_demo_output)
        else:
            backend = MemoryFixture(config, events=events)
        code = DualOutputDispatcher(config, backend, emit).run()
        if failed:
            print('V2 logging failed; execution stopped. Inspect termination state.', file=sys.stderr)
            return 1
        return code
    finally:
        handler.close()
