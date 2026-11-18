"""Unified dual-AOUT dispatcher: open-loop by default, closed-loop only with a real controller."""

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

from .closed_loop import require_controller
from .constant_pattern import execute_timeline, finite_number
from .do_monitor import DiagnosticEdgeTracker
from .errors import ControlError, ControlNotReady, InterlockError
from .memory_backend import MemoryBackend, SimulationFinished, load_scenario
from .pattern_segments import OutputPattern, Segment, StrictRotatingHandler
from .schema import SUPPLY_DEMAND_GRID, number

SUPPLY = 'rzxa_voltage_demand'
MOTOR = 'vesc_throttle'


def configuration_summary(config):
    enabled, disabled = [], []
    for assignment in config.raw['assignments']:
        record = {key: assignment.get(key) for key in ('name', 'trigger', 'output', 'profile')}
        if assignment.get('enabled', True):
            profile = config.profiles[assignment['name']]
            record.update(profile=str(profile.path), completion=profile.completion,
                          nominal_duration_s=sum(step.get('params', {}).get('duration_s', 0)
                                                 for step in profile.steps))
            enabled.append(record)
        else:
            disabled.append({**record, 'reason': 'enabled=false; trigger is not polled and will not respond',
                             'profile_validated': False})
    return {
        'assignments': enabled, 'disabled_assignments': disabled,
        'output_mappings': {
            role: {key: spec.get(key) for key in ('channel', 'display_name', 'binding', 'min_aout_v',
                                                 'max_aout_v', 'neutral_aout_v', 'expected_id')}
            for role, spec in config.raw['outputs'].items()
        },
        'trigger_policy': 'LOW then HIGH; no queue; polling pauses during profiles; pulses while busy may be missed',
    }


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
    last_confirmed_v: float | None = None
    last_attempted_v: float | None = None
    attempted_at: float | None = None
    last_readback_v: float | None = None
    readback_at: float | None = None


class Dispatcher:
    def __init__(self, config, backend, emit, *, clock=time.monotonic, wait=time.sleep,
                 timeline=execute_timeline):
        self.config, self.backend, self.emit = config, backend, emit
        self.clock, self.wait, self.timeline = clock, wait, timeline
        self.outputs = config.raw['outputs']
        self.open_loop = config.open_loop
        self.physical = config.physical
        self.has_supply = config.has_supply
        self.has_motor = config.has_motor
        self.states = {}
        self.active_profile = None
        self.active_role = None
        self.prepared = False
        self.started = 0
        self.completed = 0
        self.interrupted_cycle = False
        self._last_telemetry_at = None
        if not self.open_loop:
            # Fail closed early if closed_loop was selected without a controller.
            require_controller()

    def output_records(self):
        return {role: {**vars(state), 'value_basis': 'constant_property_setpoint_readback',
                       'physical_voltage_established': False}
                for role, state in self.states.items()}

    def snapshot(self, event='output_states'):
        state = 'running' if self.active_profile else ('ready' if self.prepared else 'not_prepared')
        if event == 'profile_interrupted_state':
            state = 'interrupted'
        elif event == 'termination_output_state':
            state = 'stopped_unknown' if any(s.status == 'unknown' for s in self.states.values()) else 'stopped'
        self.emit({'event': event, 'simulated': not self.physical, 'active_profile': self.active_profile,
                   'dispatcher_state': state,
                   'active_role': self.active_role, 'control_mode': self.config.mode,
                   'outputs': self.output_records()})

    def context(self, role):
        spec = self.outputs[role]
        key = spec['binding']
        discovered = self.backend.discover()
        fixture = discovered.get(key)
        const_property = self.backend.constant_property(role)
        source = self.backend.expected_source(role)
        if fixture is None:
            raise ControlError(f'{role}: discovery/context changed')
        if self.config.transport == 'oxygen_scpi':
            if (fixture.get('name') != spec['channel'] or fixture.get('id') != self.backend.channel_id(role) or
                    fixture.get('mode') != spec['mode'] or fixture.get('source') != source or
                    fixture.get('property') != const_property or fixture.get('unit') != 'V'):
                raise ControlError(f'{role}: discovery/context changed')
        else:
            expected = self.config.raw['simulation']['outputs'][key]
            fields = ('name', 'id', 'mode', 'source', 'property', 'unit', 'min_v', 'max_v')
            if any(fixture.get(name) != expected.get(name) for name in fields):
                raise ControlError(f'{role}: fixture discovery/context changed')
        if (fixture['mode'] != spec['mode'] or fixture.get('source') != source or
                fixture['property'] != const_property or fixture['unit'] != 'V' or
                fixture['min_v'] > spec['min_aout_v'] or fixture['max_v'] < spec['max_aout_v']):
            raise ControlError(f'{role}: fixture does not cover authorized mode/property/range')
        return key

    def verify(self, role, expected=None):
        state = self.states[role]
        if expected is None:
            expected = state.value
        try:
            key = self.context(role)
            actual = number(self.backend.read_output(key), f'{role} readback',
                            self.outputs[role]['min_aout_v'], self.outputs[role]['max_aout_v'])
            state.last_readback_v, state.readback_at = actual, self.clock()
            if expected is None or not math.isclose(actual, expected, abs_tol=1e-6, rel_tol=0):
                raise ControlError(f'{role}: expected {expected}, read back {actual}')
        except Exception as exc:
            state.status = 'unknown'
            self.emit({'event': 'output_verification_failed', 'output': role, 'expected_v': expected,
                       'last_confirmed_v': state.last_confirmed_v, 'last_confirmed_at': state.verified_at,
                       'last_attempted_v': state.last_attempted_v, 'last_readback_v': state.last_readback_v,
                       'readback_at': state.readback_at, 'error_type': type(exc).__name__, 'error': str(exc),
                       'physical_voltage_established': False})
            raise
        state.verified_at = self.clock()
        state.status = 'readback_verified'
        state.last_confirmed_v = actual
        return actual

    def prepare(self):
        if not self.open_loop:
            require_controller()
        discovered = self.backend.discover()
        ids, names = set(), set()
        readings = {}
        for role, spec in self.outputs.items():
            key = self.context(role)
            fixture = discovered[key]
            if fixture['id'] in ids or fixture['name'] in names:
                raise ControlError('Output roles alias the same discovered channel')
            ids.add(fixture['id'])
            names.add(fixture['name'])
            actual = number(self.backend.read_output(key), 'startup readback', spec['min_aout_v'], spec['max_aout_v'])
            if role == MOTOR and actual != spec['neutral_aout_v']:
                raise ControlError('Startup motor is not neutral; refusing all writes')
            readings[role] = actual
        for role, actual in readings.items():
            spec = self.outputs[role]
            self.backend.authorize(role, spec['binding'], spec)
            now = self.clock()
            self.states[role] = OutputState(actual, now, 'adopted_actual_readback' if actual else None,
                                            last_confirmed_v=actual, last_readback_v=actual, readback_at=now)
        self.prepared = True
        self.snapshot('startup_actual_readback_no_state_restoration')
        if self.open_loop:
            self.emit({'event': 'open_loop_warning', 'simulated': not self.physical,
                       'message': 'No live bus, readiness or regen feedback. Setpoint readback does not '
                                  'establish physical safety; external protections are required.'})
        self.emit({'event': 'preflight_passed', 'simulated': not self.physical,
                   'transport': self.config.transport, 'control_mode': self.config.mode,
                   'live_feedback_enabled': not self.open_loop,
                   'physical_mapping_verified': self.physical, 'output_enable_controlled': False})
        self.emit({'event': 'dispatcher_startup_summary', 'dispatcher_state': 'ready',
                   **configuration_summary(self.config),
                   'resolved_outputs': {role: {'id': self.backend.channel_id(role),
                                              'property': self.backend.constant_property(role)}
                                        for role in self.outputs}})

    def sample(self, name, *, max_age=None):
        """Closed-loop / interlock sampling. Open-loop must use log_measurements() instead."""
        if self.open_loop:
            raise InterlockError(f'cannot satisfy control measurement request in open_loop: {name}')
        sample = self.backend.measure(name)
        limit = self.config.raw['interlocks']['max_feedback_age_s'] if max_age is None else max_age
        age = self.clock() - sample.sampled_at
        trusted = (sample.simulated is (not self.physical) and
                   sample.source.startswith('FAKE ' if not self.physical else 'OXYGEN '))
        if not trusted or not 0 <= age <= limit:
            raise InterlockError(f'Missing/stale/untrusted independent feedback sample: {name}, age={age}')
        kind = self.config.raw['measurements'][name]['kind']
        if kind == 'boolean' and type(sample.value) is not bool:
            raise InterlockError(f'{name} must be Boolean')
        if kind == 'number':
            number(sample.value, name, -1000, 1000)
        self.emit({'event': 'independent_feedback', 'name': name, 'value': sample.value,
                   'age_s': age, 'source': sample.source, 'simulated': not self.physical})
        return sample.value

    def log_measurements(self, *, deadline=None):
        """Open-loop optional logging only; never raises into control flow or alters setpoints."""
        if not self.open_loop:
            return
        for name, spec in self.config.raw.get('measurements', {}).items():
            if not isinstance(spec, dict) or not spec.get('channel'):
                continue
            if deadline is not None and self.clock() >= deadline:
                self.emit({
                    'event': 'telemetry_poll_deferred', 'logging_only': True,
                    'message': 'Telemetry budget exhausted; DO trigger polling continues first',
                })
                return
            try:
                sample = self.backend.measure(name)
                self.emit({'event': 'measurement_log', 'name': name, 'value': sample.value,
                           'source': sample.source, 'logging_only': True, 'simulated': not self.physical})
            except Exception as exc:
                self.emit({
                    'event': 'measurement_log_failed', 'name': name, 'error': str(exc),
                    'logging_only': True,
                    'aout_session_intact': not getattr(self.backend, 'transport', None)
                    or not getattr(self.backend.transport, 'invalid', False),
                })

    def maybe_poll_telemetry(self, *, budget_s=None):
        """Independent open-loop telemetry tick; never called from the AOUT write path."""
        if not self.open_loop:
            return
        interval_ms = self.config.raw['timing'].get('telemetry_poll_ms', 0) or 0
        if interval_ms <= 0:
            return
        now = self.clock()
        if self._last_telemetry_at is not None and now - self._last_telemetry_at < interval_ms / 1000:
            return
        self._last_telemetry_at = now
        deadline = None if budget_s is None else now + max(0.0, budget_s)
        self.log_measurements(deadline=deadline)

    def require_true(self, name):
        if self.sample(name) is not True:
            raise InterlockError(f'Interlock not satisfied: {name}')

    def motor_neutral(self):
        if not self.has_motor:
            raise InterlockError('motor_neutral prerequisite requires an active vesc_throttle role')
        self.verify(MOTOR, self.outputs[MOTOR]['neutral_aout_v'])

    def regen_safe(self):
        if self.has_motor:
            self.motor_neutral()
        if self.open_loop:
            return
        self.require_true('regen_safe')
        self.require_true('supply_shutdown_permitted')

    def motor_interlocks(self, profile):
        interlocks = self.config.raw['interlocks']
        if self.has_supply and interlocks.get('require_supply_demand_before_motor', False):
            actual = self.verify(SUPPLY)
            demands = (interlocks['motor_supply_demands_v'] if 'motor_supply_demands_v' in interlocks
                       else [interlocks['motor_supply_demand_v']])
            if not any(math.isclose(actual, demand, abs_tol=1e-6, rel_tol=0) for demand in demands):
                raise InterlockError('Supply demand readback is not a configured motor prerequisite; physical bus safety is not established')
        elif interlocks.get('require_supply_demand_before_motor'):
            raise InterlockError('require_supply_demand_before_motor needs an active supply role')
        if self.open_loop:
            return
        requirement = profile.requires.get('measured_bus_voltage', {})
        target = requirement.get('target_v', interlocks['bus_target_v'])
        if target != interlocks['bus_target_v']:
            raise InterlockError('Profile cannot override configured bus-voltage target')
        tolerance = min(requirement.get('tolerance_v', interlocks['bus_tolerance_v']), interlocks['bus_tolerance_v'])
        max_age = min(requirement.get('max_age_s', interlocks['max_feedback_age_s']), interlocks['max_feedback_age_s'])
        measured = self.sample('dc_bus_voltage', max_age=max_age)
        if abs(measured - target) > tolerance:
            raise InterlockError(f'Independent measured bus {measured} V is outside {target} +/- {tolerance} V')
        self.require_true('motor_input_mapping_verified')
        self.require_true('motor_physical_disable_verified')
        self.require_true('supply_external_ready')

    def assert_preconditions(self, profile):
        requested = {}
        for step in profile.steps:
            if step['type'] == 'assert_preconditions':
                requested.update(step['params'])
        wants_motor_neutral = bool(
            requested.get('motor_neutral') or profile.requires.get('motor_neutral'))
        if profile.output == MOTOR:
            if self.has_motor:
                self.motor_neutral()
            self.motor_interlocks(profile)
        elif wants_motor_neutral:
            # Explicit cross-role prerequisite — never silently skipped.
            self.motor_neutral()
        elif self.has_motor and self.config.raw['interlocks'].get('supply_down_requires_motor_neutral'):
            # Dual-output default: supply motion still requires verified motor neutral.
            self.motor_neutral()
        if not self.open_loop:
            if requested.get('supply_output_enabled_or_verified_external') or 'supply_output_enable' in profile.requires:
                self.require_true('supply_external_ready')
            if requested.get('supply_shutdown_permitted') or profile.requires.get('no_unhandled_regeneration'):
                self.regen_safe()
            if requested.get('supply_standalone_test') or profile.requires.get('supply_standalone_test'):
                self.require_true('supply_standalone_test')
            if requested.get('bus_voltage_validated'):
                self.motor_interlocks(profile)
        # Optional legacy guard — open-loop supply ramps may start from any valid setpoint.
        if requested.get('current_aout_must_be_zero_or_already_target') or requested.get('reject_other_start_values'):
            current = self.verify(profile.output)
            if not any(math.isclose(current, value, abs_tol=1e-6, rel_tol=0)
                       for value in (0, profile.completion['value'])):
                raise InterlockError('Repeat-safe supply profile requires zero or already-at-target readback')

    def command(self, role, value):
        if role not in self.outputs:
            raise ControlError(f'{role}: role is not an active configured output')
        spec = self.outputs[role]
        value = number(value, 'setpoint', spec['min_aout_v'], spec['max_aout_v'])
        if not self.open_loop:
            controller = require_controller()
            measured = {name: self.sample(name) for name in self.config.raw.get('measurements', {})}
            value = number(controller.correct(role, value, measured), 'corrected setpoint',
                           spec['min_aout_v'], spec['max_aout_v'])
        # Open-loop: never query telemetry on the AOUT write path.
        self.context(role)
        state = self.states[role]
        state.last_attempted_v, state.attempted_at = value, self.clock()
        self.emit({'event': 'write_attempt', 'simulated': not self.physical, 'output': role,
                   'requested_v': value, 'last_confirmed_v': state.last_confirmed_v,
                   'owner': self.active_profile, 'control_mode': self.config.mode})
        stage = 'backend_write_and_error_queue'
        try:
            self.backend.write(role, value)
            stage = 'readback_verification'
            actual = self.verify(role, value)
        except BaseException as exc:
            state.status = 'unknown'
            self.emit({'event': 'output_command_failed', 'output': role, 'owner': self.active_profile,
                       'stage': stage, 'attempted_v': value, 'last_confirmed_v': state.last_confirmed_v,
                       'last_confirmed_at': state.verified_at, 'last_readback_v': state.last_readback_v,
                       'error_type': type(exc).__name__, 'error': str(exc),
                       'physical_voltage_established': False, 'retry_attempted': False})
            raise
        self.states[role].value = value
        # Supply hold_final persists demand; motor returns to neutral when commanded there.
        if role == SUPPLY:
            self.states[role].held_by = self.active_profile
        else:
            self.states[role].held_by = None if value == spec.get('neutral_aout_v', 0) else self.active_profile
        self.emit({'event': 'setpoint_ack', 'simulated': not self.physical, 'output': role,
                   'requested_v': value, 'readback_v': actual, 'physical_voltage_established': False})

    def compile(self, profile):
        value, elapsed = self.states[profile.output].value, 0.0
        segments, checkpoints = [], []
        for index, step in enumerate(profile.steps):
            kind, params = step['type'], step.get('params', {})
            if kind == 'set_aout':
                if value != params['value']:
                    raise ControlError('Initial set_aout cannot jump from an existing demand')
                if profile.output == SUPPLY and not self.open_loop:
                    self.require_true('supply_disabled')
            elif kind in {'hold', 'ramp_aout'}:
                duration = finite_number(params['duration_s'], 'duration')
                target = value if kind == 'hold' else finite_number(params['target'], 'target')
                if 'rate_per_s' in params and not math.isclose(
                        params['rate_per_s'], abs(target - value) / duration, rel_tol=1e-9, abs_tol=1e-12):
                    raise ControlError('Ramp rate disagrees with actual starting readback')
                segments.append(Segment(elapsed, elapsed + duration, value, target, f'{index}_{kind}'))
                elapsed += duration
                value = target
            elif kind == 'checkpoint':
                checkpoints.append((elapsed, params['name']))
        return HeldPattern(profile.name, tuple(segments)), checkpoints

    def execute_profile(self, name, *, trigger_observed=None):
        if not self.prepared or self.active_profile is not None:
            raise ControlError('Dispatcher is not prepared or another profile owns an output')
        if self.interrupted_cycle or any(state.status == 'unknown' for state in self.states.values()):
            raise ControlError('Interrupted operation or unknown output state; explicit restart and review required')
        if not self.open_loop:
            require_controller()
        profile = self.config.profiles[name]
        role = profile.output
        relative = profile.steps[0]['type'] == 'relative_aout_step'
        completion_target = profile.completion['value']
        try:
            self.verify(role)
            self.assert_preconditions(profile)
            if relative:
                current = self.states[role].last_readback_v
                index = next((i for i, value in enumerate(SUPPLY_DEMAND_GRID)
                              if math.isclose(current, value, abs_tol=1e-6, rel_tol=0)), None)
                if index is None:
                    raise InterlockError('Relative supply step requires a commissioned 36..40 V / 0.5 V demand grid starting point; no automatic entry')
                direction = profile.steps[0]['params']['direction']
                adjacent = index + (1 if direction == 'up' else -1)
                completion_target = SUPPLY_DEMAND_GRID[max(0, min(adjacent, len(SUPPLY_DEMAND_GRID) - 1))]
        except Exception as exc:
            self.emit({'event': 'profile_precondition_failed', 'assignment': name, 'profile': profile.name,
                       'output': role, 'error_type': type(exc).__name__, 'error': str(exc),
                       'profile_started': False})
            raise
        if trigger_observed is not None and not 0 <= self.clock() - trigger_observed <= self.config.raw['timing']['max_trigger_gap_ms'] / 1000:
            self.emit({'event': 'trigger_stale', 'assignment': name,
                       'age_s': self.clock() - trigger_observed,
                       'message': 'Precondition checks aged out the edge; no run. Apply a fresh LOW then HIGH.'})
            return
        current = self.states[role].last_readback_v if relative else self.states[role].value
        self.active_profile, self.active_role = profile.name, role
        self.started += 1
        self.interrupted_cycle = True
        self.snapshot('profile_ownership_acquired')
        self.emit({'event': 'profile_started', 'assignment': name, 'profile': profile.name,
                   'output': role, 'dispatcher_state': 'running', 'button_polling': 'paused',
                   'message': 'Button edges are not observed during playback; no queue or DOUT cancel. Use independent physical stop if needed.'})
        try:
            requirements = [s.get('params', {}) for s in profile.steps if s['type'] == 'assert_preconditions']
            repeat_guard = any(p.get('current_aout_must_be_zero_or_already_target') for p in requirements)
            if relative:
                began = self.clock()
                if adjacent < 0 or adjacent >= len(SUPPLY_DEMAND_GRID):
                    self.emit({'event': 'relative_step_boundary_noop', 'profile': profile.name,
                               'output': role, 'direction': direction, 'readback_v': current,
                               'target_v': completion_target, 'hardware_write': False,
                               'physical_voltage_established': False})
                else:
                    self.verify(role)
                    self.motor_neutral()
                    self.emit({'event': 'relative_step_target', 'profile': profile.name, 'output': role,
                               'direction': direction, 'start_readback_v': current,
                               'target_v': completion_target,
                               'physical_voltage_established': False})
                    self.command(role, completion_target)
                self.motor_neutral()
                if self.clock() - began > profile.timeout_s:
                    raise TimeoutError('Relative supply step exceeded timeout including I/O')
                result = {'actual_elapsed_s': self.clock() - began,
                          'runtime_target_v': completion_target,
                          'boundary_noop': adjacent < 0 or adjacent >= len(SUPPLY_DEMAND_GRID)}
            elif role == SUPPLY and repeat_guard and math.isclose(current, profile.completion['value'], abs_tol=1e-6, rel_tol=0):
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
                                           'local_marker_only': True,
                                           'marker_enabled': self.config.raw['recording']['markers']})
                    self.emit({**record, 'profile': profile.name, 'output': role, 'simulated': not self.physical})

                def guard(value):
                    self.verify(role)
                    if role == MOTOR:
                        self.motor_interlocks(profile)
                    else:
                        if self.has_motor and self.config.raw['interlocks'].get(
                                'supply_down_requires_motor_neutral'):
                            self.motor_neutral()
                        if (self.has_supply and value < self.states[SUPPLY].value - 1e-9 and
                                self.has_motor):
                            self.regen_safe()
                    self.command(role, value)

                def verify(value):
                    if role == MOTOR:
                        self.motor_interlocks(profile)
                    elif self.has_motor and self.config.raw['interlocks'].get(
                            'supply_down_requires_motor_neutral'):
                        self.motor_neutral()
                    self.verify(role, value)

                began = self.clock()
                result = self.timeline(pattern, guard, verify, progress,
                                       interval_s=self.config.raw['timing']['interval_ms'] / 1000,
                                       max_lateness_s=self.config.raw['timing']['max_lateness_ms'] / 1000,
                                       clock=self.clock, wait=self.wait)
                if self.clock() - began > profile.timeout_s:
                    raise TimeoutError('Profile exceeded timeout including I/O')
            self.verify(role, completion_target)
            self.states[role].held_by = profile.name if profile.completion['mode'] == 'hold_final' else None
            self.completed += 1
            self.interrupted_cycle = False
            self.emit({'event': 'pattern_cycle_complete', 'assignment': name, 'profile': profile.name,
                       'output': role, 'completion': profile.completion['mode'],
                       'simulated': not self.physical, **result})
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
        self.emit({'event': 'dispatcher_ready', 'transport': self.config.transport,
                   'control_mode': self.config.mode, 'dispatcher_state': 'ready',
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
                    self.emit({'event': 'trigger_observation_discarded', 'assignment': assignment['name'],
                               'trigger': assignment['trigger'], 'read_s': now - began,
                               'message': 'DO read exceeded maximum trigger gap; resync LOW then HIGH required.'})
                    continue
                if type(value) is not bool:
                    raise ControlError('Invalid DO level')
                observation = tracker.observe(value, now)
                if observation['edge'] == 'baseline':
                    gap = observation['gap_s']
                    if gap is not None:
                        self.emit({'event': 'trigger_observation_discarded', 'assignment': assignment['name'],
                                   'trigger': assignment['trigger'], 'gap_s': gap,
                                   'message': 'Observation gap reset edge tracking; no edge inferred across missing samples.'})
                    if value:
                        self.emit({'event': 'trigger_not_armed', 'assignment': assignment['name'],
                                   'trigger': assignment['trigger'],
                                   'message': 'HIGH at baseline is not a press; set LOW then HIGH.'})
                self.emit({'event': 'trigger_sample', 'assignment': assignment['name'], **observation})
                if observation['edge'] != 'steady':
                    self.emit({'event': 'trigger_state', 'assignment': assignment['name'], **observation})
                if observation['press']:
                    ready.append((assignment['name'], now))
            if ready:
                name, observed = ready[0]
                if self.clock() - observed <= timing['max_trigger_gap_ms'] / 1000:
                    for ignored, _ in ready[1:]:
                        self.emit({'event': 'trigger_ignored', 'assignment': ignored,
                                   'reason': 'config order wins; no queue'})
                    self.execute_profile(name, trigger_observed=observed)
                else:
                    self.emit({'event': 'trigger_stale', 'assignment': name,
                               'age_s': self.clock() - observed,
                               'message': 'Polling other inputs aged out this edge; no run. Apply a fresh LOW then HIGH.'})
                for tracker in trackers.values():
                    tracker.reset()
                last_verify = self.clock()
                self.emit({'event': 'dispatcher_ready', 'dispatcher_state': 'ready',
                           'message': 'Polling resumed. Fresh LOW then HIGH required on every button; busy pulses were not queued.'})
            elif self.clock() - last_verify >= .5:
                for role in self.states:
                    self.verify(role)
                last_verify = self.clock()
            # Telemetry is secondary to DO edges: skip when a press is ready, and budget the poll.
            if not ready:
                self.maybe_poll_telemetry(budget_s=timing['poll_ms'] / 2000)
            self.wait(timing['poll_ms'] / 1000)

    def cleanup(self):
        safe = True
        if not self.prepared:
            self.emit({'event': 'cleanup_not_authorized',
                       'message': 'No startup output writes or guessed restoration'})
            return True
        if self.has_motor:
            try:
                self.context(MOTOR)
                neutral = self.outputs[MOTOR]['neutral_aout_v']
                if self.open_loop:
                    self.command(MOTOR, neutral)
                else:
                    # Closed-loop cleanup must not depend on feedback sampling.
                    self.backend.write(MOTOR, neutral)
                    self.verify(MOTOR, neutral)
                    self.states[MOTOR].value = neutral
                    self.states[MOTOR].held_by = None
                self.emit({'event': 'motor_neutral_verified', 'simulated': not self.physical})
            except (Exception, KeyboardInterrupt) as exc:
                self.states[MOTOR].status = 'unknown'
                safe = False
                self.emit({'event': 'motor_neutral_unknown', 'error': str(exc)})
        if self.has_supply:
            try:
                self.verify(SUPPLY)
                self.emit({'event': 'supply_demand_preserved', 'value_v': self.states[SUPPLY].value,
                           'not_power_off': True, 'simulated': not self.physical})
            except (Exception, KeyboardInterrupt) as exc:
                self.states[SUPPLY].status = 'unknown'
                safe = False
                self.emit({'event': 'supply_demand_unknown', 'error': str(exc),
                           'no_supply_write_attempted': True})
        # Open-loop cleanup must never sample measurements.
        if not self.open_loop:
            try:
                self.sample('dc_bus_voltage')
            except (Exception, KeyboardInterrupt) as exc:
                self.emit({'event': 'bus_feedback_unknown', 'error': str(exc),
                           'not_proof_of_deenergization': True})
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
        except ControlNotReady as exc:
            self.emit({'event': 'fault', 'error_type': type(exc).__name__, 'error': str(exc)})
            code, status = 2, 'control_not_ready'
        except Exception as exc:
            self.emit({'event': 'fault', 'error_type': type(exc).__name__, 'error': str(exc)})
        finally:
            try:
                if not self.cleanup():
                    code, status = 1, 'cleanup_unknown'
            finally:
                self.backend.close()
        self.emit({'event': 'dispatcher_summary', 'schema_version': 3,
                   'transport': self.config.transport, 'control_mode': self.config.mode,
                   'status': status, 'started_cycles': self.started, 'completed_cycles': self.completed,
                   'physical_actuation': self.physical and getattr(self.backend, 'writes_attempted', 0) > 0,
                   'supply_output_enable_controlled': False,
                   'output_states': self.output_records(),
                   'physical_bus_state': 'not established by open_loop/simulation'
                   if self.open_loop or not self.physical else 'not inferred from AOUT setpoints'})
        return code


def run_dispatch_cli(args):
    from .schema import load_dispatcher_config, confirm_outputs

    config = load_dispatcher_config(args.bindings)
    physical = config.physical
    confirmed = list(getattr(args, 'confirm_output', None) or [])
    # Accept legacy flag names as aliases for the unified --confirm-output list.
    confirmed.extend(getattr(args, 'confirm_demo_output', None) or [])
    confirmed.extend(getattr(args, 'confirm_physical_output', None) or [])
    if args.live and not physical:
        raise ValueError('--live requires physical oxygen_scpi authorization')
    if config.transport != 'memory' and getattr(args, 'scenario', None):
        raise ValueError('OXYGEN transport uses real DOUT buttons; --scenario is memory only')
    triggers = {a['name']: a['trigger'] for a in config.raw['assignments'] if a.get('enabled', True)}
    events = load_scenario(args.scenario, config.raw.get('measurements', {}), triggers.values()) if getattr(args, 'scenario', None) else []
    if args.execute:
        if physical and args.confirm_simulated_output:
            raise ValueError('Physical execution cannot use --confirm-simulated-output')
        if config.transport == 'oxygen_scpi':
            if not physical and not args.confirm_simulated_output:
                raise ValueError('Simulated oxygen_scpi execution requires --confirm-simulated-output')
            confirm_outputs(config, confirmed, live=args.live)
        elif not args.confirm_simulated_output:
            raise ValueError('Memory execution requires --confirm-simulated-output')
        if config.transport == 'memory' and not getattr(args, 'scenario', None):
            raise ValueError('Memory execution requires an explicit --scenario')
    plan = {
        'event': 'dispatcher_plan', 'schema_version': 3, 'transport': config.transport,
        'control_mode': config.mode,
        'fake_outputs_and_measurements': config.transport == 'memory',
        'physical_actuation': False,
        'physical_actuation_requested': physical and args.execute,
        'bus_feedback': (
            'NONE: open_loop; external protections required' if config.open_loop else
            ('real independent OXYGEN measurement' if physical else 'FAKE independent injected sensor')),
        'scenario_events': len(events),
        **configuration_summary(config),
    }
    if not args.execute:
        print(json.dumps({**plan, 'connected': False, 'writes_attempted': 0}, indent=2, allow_nan=False))
        return 0
    default_log = {
        'memory': 'logs/dispatcher-memory.jsonl',
        'oxygen_scpi': 'logs/dispatcher-oxygen.jsonl',
    }[config.transport]
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
                      'run_id': run_id, 'transport': config.transport, **record}
            line = json.dumps(record, allow_nan=False)
            try:
                handler.emit(logging.LogRecord('dispatcher', logging.INFO, '', 0, line, (), None))
                if record['event'] not in {'query', 'trigger_sample', 'independent_feedback',
                                           'setpoint_ack', 'write_attempt',
                                           'scpi_command_started', 'scpi_command_completed'}:
                    print(line, flush=True)
            except OSError:
                failed = True
                raise

        emit(plan)
        if config.transport == 'oxygen_scpi':
            from .oxygen_backend import OxygenBackend
            backend = OxygenBackend(config, emit, confirmed_outputs=confirmed, live=args.live)
        else:
            backend = MemoryBackend(config, events=events)
        from .output_lease import output_lease

        with output_lease(config):
            code = Dispatcher(config, backend, emit).run()
        if failed:
            print('Logging failed; execution stopped. Inspect termination state.', file=sys.stderr)
            return 1
        return code
    finally:
        handler.close()
