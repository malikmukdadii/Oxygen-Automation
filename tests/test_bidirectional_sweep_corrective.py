"""Corrective-review regression: cancel wait DI, freshness, RPM ramps, OxygenBackend numerics."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from oxygen_engine.dispatcher import BRAKE, MOTOR, SUPPLY, Dispatcher
from oxygen_engine.errors import ConfigError, InterlockError
from oxygen_engine.fast_regen import parse_fast_regen
from oxygen_engine.memory_backend import MemoryBackend, Sample
from oxygen_engine.motor_sweep import (
    EVENT_HISTORY_LIMIT,
    MotorSweepRunner,
    SweepCancelled,
    SweepFault,
    deep_merge,
    parse_motor_sweep,
    run_standalone_fast_regen,
)
from oxygen_engine.oxygen_backend import OxygenBackend, Sample as OxygenSample
from oxygen_engine.schema import load_dispatcher_config

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / 'examples' / 'ces-bidirectional-sweep.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += max(0.0, seconds)


def production_timeline(pattern, guard, verify, progress, *, interval_s, max_lateness_s, clock, wait):
    """Exercise real multi-timeline semantics with positive-duration waits (not wait(0) jumps)."""
    # Force the dispatcher production path by calling through when used as injection —
    # tests that need production path set dispatcher.timeline = execute_timeline.
    raise AssertionError('tests that inject this should instead use stock execute_timeline')


def _resolve_profiles(raw, base: Path):
    for assignment in raw['assignments']:
        if assignment.get('profile'):
            profile = Path(assignment['profile'])
            if not profile.is_absolute():
                assignment['profile'] = str((base.parent / profile).resolve())
    return raw


def _load(tmp_path, *, use_production_timeline=False):
    raw = json.loads(SWEEP.read_text(encoding='utf-8-sig'))
    raw = _resolve_profiles(raw, SWEEP)
    path = tmp_path / 'bindings.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock, records = Clock(), []
    backend = MemoryBackend(config, clock=clock)
    kwargs = dict(clock=clock, wait=clock.wait)
    if use_production_timeline:
        from oxygen_engine.constant_pattern import execute_timeline
        kwargs['timeline'] = execute_timeline
    else:
        # Boundary timeline with positive wait durations for cancel-wait tests.
        def boundary_with_wait(pattern, guard, verify, progress, *, interval_s, max_lateness_s, clock, wait):
            elapsed = 0.0
            for elapsed in pattern.boundaries:
                wait(interval_s)  # positive duration — would recurse if wait were monkey-patched
                values = pattern.values_at(elapsed)
                guard(values)
                verify(values)
                progress({'event': 'progress', 'elapsed_s': elapsed, 'phase': pattern.phase_at(elapsed),
                          'requested': values, 'lateness_ms': 0.0, 'configured_interval_ms': interval_s * 1000,
                          'mandatory_boundary': True})
            return {
                'actual_elapsed_s': elapsed, 'skipped_update_slots': 0,
                'maximum_lateness_ms': 0.0, 'nominal_duration_s': pattern.duration_s,
                'completion_lateness_ms': 0.0, 'configured_interval_ms': interval_s * 1000,
                'achieved_interval_ms': interval_s * 1000,
            }
        kwargs['timeline'] = boundary_with_wait
    dispatcher = Dispatcher(config, backend, records.append, **kwargs)
    return dispatcher, backend, clock, records, config


def _tiny_open_loop(outputs, **overrides):
    raw = {
        'mode': 'open_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
        },
    }
    raw.update(overrides)
    if 'fast_regen' in overrides:
        raw['fast_regen'] = {**{
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
        }, **overrides['fast_regen']}
    return parse_motor_sweep(raw, outputs=outputs)


# --- Finding 1: recursive cancel wait ---

def test_cancel_wait_no_recursion_on_positive_duration_timeline(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = _tiny_open_loop(config.raw['outputs'])
    dispatcher.backend.connect()
    dispatcher.prepare()
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_open_loop',
                              stop_trigger=config.stop_sweep_trigger)
    # If wait were monkey-patched to cancel_wait→_wait→dispatcher.wait, this recurses.
    result = runner.run()
    assert result.status == 'complete'
    assert clock.now > 0


def test_standalone_fast_regen_production_wait_path(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    dispatcher.backend.connect()
    dispatcher.prepare()
    dispatcher.command(SUPPLY, 0.5)
    fr = parse_fast_regen({
        'throttle_target_v': 0.4, 'brake_target_v': 0.2,
        'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
        'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
        'brake_release_s': 0.1, 'recovery_s': 0.1,
    }, motor_max_v=1.6, brake_max_v=3.3)
    result = run_standalone_fast_regen(dispatcher, fr, 'fast_regen_test')
    assert result['status'] == 'complete'


# --- Finding 7: deep-merge / brake_target_v ---

def test_deep_merge_and_brake_target_override(tmp_path):
    merged = deep_merge(
        {'fast_regen': {'throttle_target_v': 1.6, 'brake_target_v': 0.1, 'accel_hold_s': 1.5},
         'settle_s': 0.5},
        {'fast_regen': {'brake_target_v': 0.25}, 'mode': 'open_loop'},
    )
    assert merged['fast_regen']['throttle_target_v'] == 1.6
    assert merged['fast_regen']['brake_target_v'] == 0.25
    assert merged['fast_regen']['accel_hold_s'] == 1.5
    assert merged['settle_s'] == 0.5

    raw = json.loads(SWEEP.read_text(encoding='utf-8-sig'))
    raw = _resolve_profiles(raw, SWEEP)
    for assignment in raw['assignments']:
        if assignment.get('name') == 'motor_sweep_open_loop':
            assignment['parameters'] = {'brake_target_v': 0.33}
            assignment['motor_sweep'] = {
                'mode': 'open_loop', 'round_trips': 1, 'continuous': False,
                'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
            }
    path = tmp_path / 'm.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    assert config.motor_sweeps['motor_sweep_open_loop'].fast_regen.brake_target_v == pytest.approx(0.33)


# --- Finding 3: freshness ---

def test_cached_sample_does_not_advance_qualify_timer(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = parse_motor_sweep({
        'mode': 'voltage_closed_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'voltage_qualify': {
            'channel': 'dc_bus_voltage', 'tolerance_v': 0.5, 'qualify_duration_s': 0.1,
            'timeout_s': 0.25, 'poll_s': 0.02, 'max_age_s': 1.0,
            'require_advancing_samples': True,
        },
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
        },
    }, outputs=config.raw['outputs'])
    dispatcher.backend.connect()
    dispatcher.prepare()
    # Pin a single in-band sample that never advances.
    backend.inject('dc_bus_voltage', 40.0, sampled_at=clock.now)
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_voltage_closed_loop')
    with pytest.raises(SweepFault, match='no silent open-loop fallback'):
        runner.run()


def test_advancing_samples_qualify_success(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = parse_motor_sweep({
        'mode': 'voltage_closed_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'voltage_qualify': {
            'channel': 'dc_bus_voltage', 'tolerance_v': 0.5, 'qualify_duration_s': 0.05,
            'timeout_s': 2.0, 'poll_s': 0.02, 'max_age_s': 1.0,
            'require_advancing_samples': True,
        },
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
        },
    }, outputs=config.raw['outputs'])
    dispatcher.backend.connect()
    dispatcher.prepare()
    original = dispatcher.command

    def tracking(role, value):
        original(role, value)
        if role == SUPPLY:
            backend.inject('dc_bus_voltage', value * 80.0)  # unpinned → measure advances

    dispatcher.command = tracking
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_voltage_closed_loop')
    assert runner.run().status == 'complete'
    assert any(r.get('event') == 'sweep_voltage_qualified' for r in records)


# --- Finding 8: bounded events + cancel cleanup ---

def test_event_history_bounded_and_cancel_cleanup_independent(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = _tiny_open_loop(config.raw['outputs'], settle_s=2.0)
    dispatcher.backend.connect()
    dispatcher.prepare()
    runner = MotorSweepRunner(
        dispatcher, sweep, 'motor_sweep_open_loop',
        cancel_check=lambda: clock.now > 0.05)
    result = runner.run()
    assert result.status == 'cancelled'
    assert len(runner.events) <= EVENT_HISTORY_LIMIT
    assert any(r.get('event') == 'sweep_cancel_supply_preserved' for r in records)
    assert dispatcher.interrupted_cycle is False  # cleanup succeeded


def test_cancel_cleanup_reports_partial_failure(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = _tiny_open_loop(config.raw['outputs'], settle_s=2.0)
    dispatcher.backend.connect()
    dispatcher.prepare()
    runner = MotorSweepRunner(
        dispatcher, sweep, 'motor_sweep_open_loop',
        cancel_check=lambda: clock.now > 0.05)
    original_command = dispatcher.command

    def flaky(role, value):
        if role == MOTOR and runner._cancel_requested:
            raise RuntimeError('motor cleanup fault')
        return original_command(role, value)

    dispatcher.command = flaky
    result = runner.run()
    assert result.status == 'cancelled'
    assert result.cleanup_failures
    assert dispatcher.interrupted_cycle is True
    assert dispatcher.states[MOTOR].status == 'unknown'
    # Brake/supply policies still attempted independently.
    assert any(r.get('event') == 'sweep_cancel_brake_ok' for r in records) or any(
        r.get('event') == 'sweep_cancel_supply_preserved' for r in records)


# --- Finding 4: RPM during ramps ---

def test_rpm_monitored_during_accel_ramp(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = parse_motor_sweep({
        'mode': 'full_closed_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'voltage_qualify': {
            'channel': 'dc_bus_voltage', 'tolerance_v': 0.5, 'qualify_duration_s': 0.02,
            'timeout_s': 2.0, 'poll_s': 0.02, 'max_age_s': 1.0,
        },
        'rpm_feedback': {
            'channel': 'erpm', 'kind': 'electrical', 'poll_s': 0.02, 'max_age_s': 1.0,
            'accel_timeout_s': 2.0, 'brake_timeout_s': 2.0,
            'monitor_during_ramps': True, 'rpm_tolerance': 10,
        },
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.4, 'accel_hold_s': 10.0, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 10.0,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
            'accel_rpm_target': 500, 'brake_rpm_target': 50, 'rpm_kind': 'electrical',
        },
    }, outputs=config.raw['outputs'])
    dispatcher.backend.connect()
    dispatcher.prepare()
    original = dispatcher.command

    def tracking(role, value):
        original(role, value)
        if role == SUPPLY:
            backend.inject('dc_bus_voltage', value * 80.0)
        if role == MOTOR and value > 0.1:
            backend.inject('erpm', 600.0)
        if role == BRAKE and value > 0:
            backend.inject('erpm', 40.0)

    dispatcher.command = tracking
    backend.inject('dc_bus_voltage', 40.0)
    backend.inject('erpm', 0.0)
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_full_closed_loop')
    assert runner.run().status == 'complete'
    ramp_hits = [r for r in records if r.get('event') == 'sweep_rpm_target_reached' and r.get('during_ramp')]
    assert ramp_hits


# --- Finding 2: OxygenBackend numeric RPM ---

def test_oxygen_backend_measure_numeric_rpm_and_bus():
    backend = OxygenBackend.__new__(OxygenBackend)
    backend.lock = __import__('threading').RLock()
    backend.open_loop = True
    backend.physical = False
    backend.clock = lambda: 10.0
    backend.input_ids = {
        'sensor:dc_bus_voltage': ('Bus', '1'),
        'sensor:erpm': ('ERPM', '2'),
    }
    backend.config = Mock()
    backend.config.raw = {
        'measurements': {
            'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': 'Bus', 'property': None},
            'erpm': {'kind': 'number', 'unit': 'erpm', 'channel': 'ERPM', 'property': None},
            'regen_safe': {'kind': 'boolean', 'property': 'Flag', 'channel': 'FlagCh'},
        }
    }
    backend.adapter = Mock()
    backend.adapter.get_property_value = Mock(side_effect=lambda cid, prop: Mock(
        kind='STRING', values=('V',) if cid == '1' else ('erpm',)))
    backend.adapter.acquisition_state = Mock(return_value='Started')
    backend.adapter.read_selected_measurements = Mock(return_value=[40.0, 1234.0])
    backend.adapter.read_measurements = Mock(side_effect=lambda names: [40.0] if names == ['Bus'] else [1234.0])
    backend.transport = Mock()
    backend.transport.soft_telemetry = False
    backend.transport.experiment_numeric_names = ('Bus', 'ERPM')
    backend.transport.numeric_name = 'Bus'
    backend._numeric_order = ('dc_bus_voltage', 'erpm')
    backend._measure_seq = 0
    backend.error = Mock()

    bus = backend.measure('dc_bus_voltage')
    assert isinstance(bus, OxygenSample)
    assert bus.value == 40.0
    assert bus.freshness == 'host_scpi'
    assert bus.acquisition_time_s is None  # limitation documented
    rpm = backend.measure('erpm')
    assert rpm.value == 1234.0
    assert rpm.sequence == 2


def test_oxygen_backend_boolean_still_works():
    backend = OxygenBackend.__new__(OxygenBackend)
    backend.lock = __import__('threading').RLock()
    backend.open_loop = False
    backend.physical = True
    backend.clock = lambda: 1.0
    backend.input_ids = {'sensor:regen_safe': ('FlagCh', '9')}
    backend.config = Mock()
    backend.config.raw = {
        'measurements': {
            'regen_safe': {'kind': 'boolean', 'property': 'Flag', 'channel': 'FlagCh'},
        }
    }
    backend.adapter = Mock()
    backend._boolean = Mock(return_value=True)
    backend.transport = Mock()
    backend.transport.soft_telemetry = False
    backend.error = Mock()
    sample = backend.measure('regen_safe')
    assert sample.value is True


# --- Finding 5/6: production multi-timeline mandatory boundaries ---

def test_production_multi_timeline_respects_boundaries_under_lateness(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path, use_production_timeline=True)
    from oxygen_engine.fast_regen import build_fast_regen_pattern
    from oxygen_engine.constant_pattern import execute_timeline

    dispatcher.timeline = execute_timeline
    dispatcher.backend.connect()
    dispatcher.prepare()
    dispatcher.command(SUPPLY, 0.5)
    fr = parse_fast_regen({
        'throttle_target_v': 0.4, 'brake_target_v': 0.2,
        'accel_ramp_s': 0.04, 'accel_hold_s': 0.04, 'throttle_release_s': 0.04,
        'transition_s': 0.04, 'brake_ramp_s': 0.04, 'brake_hold_s': 0.04,
        'brake_release_s': 0.04, 'recovery_s': 0.04,
    }, motor_max_v=1.6, brake_max_v=3.3)
    pattern = build_fast_regen_pattern(fr)
    writes = []
    last = {MOTOR: None, BRAKE: None}

    def apply_values(values):
        for role in pattern.roles:
            target = values[role]
            if last[role] is None or abs(last[role] - target) > 1e-12:
                # Simulate SCPI delay without exceeding lateness budget.
                clock.now += 0.002
                dispatcher.command(role, target)
                writes.append((role, target))
                last[role] = target

    def verify(values):
        for role, target in values.items():
            dispatcher.verify(role, target)

    def progress(record):
        records.append(record)

    def wait(seconds):
        clock.wait(max(seconds, 0.0))

    result = dispatcher._run_multi_timeline(pattern, apply_values, verify, progress, wait=wait)
    assert result['actual_elapsed_s'] >= pattern.duration_s - 1e-6
    last_motor = 0.0
    for role, value in writes:
        if role == MOTOR:
            last_motor = value
        if role == BRAKE and value > 0:
            assert last_motor == 0.0
    assert any(r.get('mandatory_boundary') for r in records)


# --- Finding 9: sample_supervisory returns Sample ---

def test_sample_supervisory_returns_sample_object(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    dispatcher.backend.connect()
    dispatcher.prepare()
    backend.inject('dc_bus_voltage', 40.0)
    sample = dispatcher.sample_supervisory('dc_bus_voltage', max_age=1.0)
    assert hasattr(sample, 'value')
    assert sample.value == 40.0
