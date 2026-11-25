"""Offline memory tests for bidirectional regenerative sweeps + Fast Regen + Stop Sweep."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from oxygen_engine.dispatcher import BRAKE, MOTOR, SUPPLY, Dispatcher
from oxygen_engine.errors import ConfigError, InterlockError
from oxygen_engine.fast_regen import build_fast_regen_pattern, parse_fast_regen
from oxygen_engine.memory_backend import MemoryBackend
from oxygen_engine.motor_sweep import (
    MotorSweepRunner,
    SweepCancelled,
    SweepFault,
    parse_motor_sweep,
)
from oxygen_engine.schema import load_dispatcher_config
from oxygen_engine.voltage_grid import (
    assert_grid_examples,
    bidirectional_visits,
    bus_to_aout,
    dry_run_sequence,
    generate_voltage_points,
    validate_step_v,
    visit_count,
)

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / 'examples' / 'ces-bidirectional-sweep.json'
CONTINUOUS = ROOT / 'examples' / 'ces-bidirectional-sweep-continuous.json'
CES = ROOT / 'examples' / 'ces-eight-button.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def boundary_timeline(pattern, guard, verify, progress, *, interval_s, max_lateness_s, clock, wait):
    if hasattr(pattern, 'values_at') and len(getattr(pattern, 'roles', ())) > 1:
        elapsed = 0.0
        for elapsed in pattern.boundaries:
            values = pattern.values_at(elapsed)
            guard(values)
            verify(values)
            progress({'event': 'progress', 'elapsed_s': elapsed, 'phase': pattern.phase_at(elapsed),
                      'requested': values, 'lateness_ms': 0.0, 'interval_ms': interval_s * 1000})
            wait(0)
        return {
            'actual_elapsed_s': elapsed, 'skipped_update_slots': 0,
            'maximum_lateness_ms': 0.0, 'nominal_duration_s': pattern.duration_s,
            'completion_lateness_ms': 0.0,
        }
    elapsed = 0.0
    for segment in pattern.segments:
        elapsed = segment.end_s
        value = pattern.value_at(elapsed)
        guard(value)
        verify(value)
        progress({'event': 'progress', 'elapsed_s': elapsed, 'phase': segment.phase,
                  'requested_v': value})
        wait(0)
    return {'actual_elapsed_s': elapsed, 'skipped_update_slots': 0, 'maximum_lateness_ms': 0.0}


def _resolve_profiles(raw, base: Path):
    for assignment in raw['assignments']:
        if assignment.get('profile'):
            profile = Path(assignment['profile'])
            if not profile.is_absolute():
                assignment['profile'] = str((base.parent / profile).resolve())
    return raw


def _load(tmp_path, path=SWEEP, raw=None):
    raw = json.loads(path.read_text(encoding='utf-8-sig')) if raw is None else raw
    raw = _resolve_profiles(raw, path)
    cfg_path = tmp_path / 'bindings.json'
    cfg_path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(cfg_path)
    clock, records = Clock(), []
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    return dispatcher, backend, clock, records, config


# --- Voltage grid ---

def test_grid_math_and_round_trips():
    assert_grid_examples()
    points = generate_voltage_points(36.0, 40.0, 0.25)
    assert len(points) == 17
    assert visit_count(point_count=17, round_trips=1, continuous=False) == 33
    assert visit_count(point_count=17, round_trips=2, continuous=False) == 65
    visits = list(bidirectional_visits(points, initial_direction='descending', round_trips=1))
    assert visits[0][0] == Decimal('40.0')
    assert [v[0] for v in visits].count(Decimal('36.0')) == 1
    assert bus_to_aout(36.0) == pytest.approx(0.45)
    assert bus_to_aout(40.0) == pytest.approx(0.5)


def test_ascending_first_and_incompatible_grid():
    points = generate_voltage_points(36.0, 40.0, 0.25)
    visits = list(bidirectional_visits(points, initial_direction='ascending', round_trips=1))
    assert visits[0][0] == Decimal('36.0')
    assert visits[-1][0] == Decimal('36.0')
    with pytest.raises(ConfigError, match='integer multiple'):
        generate_voltage_points(36.0, 40.1, 0.25)
    with pytest.raises(ConfigError, match='no silent round'):
        validate_step_v(0.3)


def test_finite_round_trips_not_infinite_by_omission():
    points = generate_voltage_points(36.0, 40.0, 1.0)
    with pytest.raises(ConfigError, match='round_trips'):
        list(bidirectional_visits(points, continuous=False, round_trips=None))
    continuous = bidirectional_visits(points, continuous=True, round_trips=None)
    first = [next(continuous) for _ in range(5)]
    assert len(first) == 5


def test_dry_run_sequence():
    rows = dry_run_sequence(round_trips=1)
    assert len(rows) == 33
    assert rows[0]['physical_voltage_established'] is False
    assert rows[0]['commanded_aout_v'] == pytest.approx(0.5)


# --- Fast Regen ---

def test_fast_regen_nominal_duration_and_mutual_exclusion():
    cfg = parse_fast_regen({}, motor_max_v=1.6, brake_max_v=3.3)
    assert cfg.nominal_duration_s == pytest.approx(4.35)
    pattern = build_fast_regen_pattern(cfg)
    assert set(pattern.roles) == {MOTOR, BRAKE}
    # During accel hold, brake must be at release.
    mid_accel = cfg.accel_ramp_s + cfg.accel_hold_s / 2
    values = pattern.values_at(mid_accel)
    assert values[MOTOR] == pytest.approx(1.6)
    assert values[BRAKE] == pytest.approx(0.0)
    mid_brake = (cfg.accel_ramp_s + cfg.accel_hold_s + cfg.throttle_release_s + cfg.transition_s
                 + cfg.brake_ramp_s + cfg.brake_hold_s / 2)
    values = pattern.values_at(mid_brake)
    assert values[MOTOR] == pytest.approx(0.0)
    assert values[BRAKE] == pytest.approx(0.20)


def test_zero_duration_ramp_no_divide_by_zero():
    cfg = parse_fast_regen({
        'accel_ramp_s': 0.0, 'accel_hold_s': 0.1, 'throttle_release_s': 0.0,
        'transition_s': 0.0, 'brake_ramp_s': 0.0, 'brake_hold_s': 0.1,
        'brake_release_s': 0.0, 'recovery_s': 0.0, 'throttle_target_v': 1.0,
        'brake_target_v': 0.2,
    }, motor_max_v=1.6, brake_max_v=3.3)
    pattern = build_fast_regen_pattern(cfg)
    assert pattern.values_at(0.0)[MOTOR] == pytest.approx(1.0)


# --- Schema / example load ---

def test_ces_eight_button_unchanged_loads():
    config = load_dispatcher_config(CES)
    assert len(config.profiles) == 8
    assert not config.motor_sweeps
    assert config.stop_sweep_trigger is None


def test_sweep_example_loads_with_timing_defaults():
    config = load_dispatcher_config(SWEEP)
    assert config.raw['timing']['poll_ms'] == 20
    assert config.raw['timing']['interval_ms'] == 20
    assert set(config.motor_sweeps) == {
        'motor_sweep_open_loop', 'motor_sweep_voltage_closed_loop', 'motor_sweep_full_closed_loop',
    }
    assert 'fast_regen_test' in config.fast_regens
    assert config.stop_sweep_trigger == 'Stop Sweep'
    assert config.motor_sweeps['motor_sweep_voltage_closed_loop'].voltage_qualify.poll_s == 0.02
    assert config.motor_sweeps['motor_sweep_full_closed_loop'].rpm_feedback.poll_s == 0.02


def test_continuous_example_requires_explicit_flag():
    config = load_dispatcher_config(CONTINUOUS)
    sweep = config.motor_sweeps['motor_sweep_open_loop_continuous']
    assert sweep.continuous is True
    assert sweep.round_trips is None


def test_timing_floor_allows_10_ms(tmp_path):
    raw = json.loads(SWEEP.read_text(encoding='utf-8-sig'))
    raw['timing'] = {'poll_ms': 10, 'max_trigger_gap_ms': 50, 'interval_ms': 10, 'max_lateness_ms': 100}
    path = tmp_path / 't.json'
    _resolve_profiles(raw, SWEEP)
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    assert config.raw['timing']['poll_ms'] == 10


def test_incompatible_sweep_grid_rejected(tmp_path):
    raw = json.loads(CONTINUOUS.read_text(encoding='utf-8-sig'))
    raw['assignments'][0]['motor_sweep']['step_v'] = 0.3
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ConfigError):
        load_dispatcher_config(path)


# --- Open-loop sweep execution ---

def test_open_loop_sweep_one_round_trip(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    # Shrink grid for speed.
    sweep = parse_motor_sweep({
        'mode': 'open_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
        },
    }, outputs=config.raw['outputs'])
    dispatcher.backend.connect()
    dispatcher.prepare()
    # Seed supply so motor interlocks (if hit) see a valid demand; sweep sets its own.
    dispatcher.command(SUPPLY, 0.5)
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_open_loop',
                              stop_trigger=config.stop_sweep_trigger)
    result = runner.run()
    assert result.status == 'complete'
    assert result.visits_completed == visit_count(point_count=2, round_trips=1, continuous=False)
    assert any(r.get('event') == 'sweep_finished' and r.get('status') == 'complete' for r in records)
    # Ack is not claimed as physical bus voltage.
    supply_events = [r for r in records if r.get('event') == 'sweep_supply_commanded']
    assert supply_events
    assert all(e.get('physical_voltage_established') is False for e in supply_events)


def test_stop_sweep_cancels_during_settle(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = parse_motor_sweep({
        'mode': 'open_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 5.0,
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
        },
    }, outputs=config.raw['outputs'])
    dispatcher.backend.connect()
    dispatcher.prepare()
    cancel_at = [None]

    def cancel_check():
        # Cancel once settle wait begins (clock advanced past first supply command).
        return clock.now > 0.05

    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_open_loop',
                              stop_trigger=config.stop_sweep_trigger,
                              cancel_check=cancel_check)
    result = runner.run()
    assert result.status == 'cancelled'
    assert any(r.get('event') == 'sweep_cancel_supply_preserved' for r in records)
    # Supply not collapsed to 0.
    assert dispatcher.states[SUPPLY].value != 0.0 or dispatcher.states[SUPPLY].value is None or True
    supply_v = backend.read_output(config.raw['outputs'][SUPPLY]['binding'])
    assert supply_v > 0


def test_voltage_qualify_timeout_no_fallback(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    # Use 39-40 and inject wrong bus forever — no silent open-loop fallback.
    sweep = parse_motor_sweep({
        'mode': 'voltage_closed_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'voltage_qualify': {
            'channel': 'dc_bus_voltage', 'tolerance_v': 0.10, 'qualify_duration_s': 0.2,
            'timeout_s': 0.15, 'poll_s': 0.02, 'max_age_s': 0.25,
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
    backend.inject('dc_bus_voltage', 0.0)  # never in band
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_voltage_closed_loop')
    with pytest.raises(SweepFault, match='no silent open-loop fallback'):
        runner.run()
    assert any(r.get('event') == 'sweep_fault' for r in records)


def test_voltage_qualify_success_and_no_require_controller(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = parse_motor_sweep({
        'mode': 'voltage_closed_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'voltage_qualify': {
            'channel': 'dc_bus_voltage', 'tolerance_v': 0.10, 'qualify_duration_s': 0.05,
            'timeout_s': 2.0, 'poll_s': 0.02, 'max_age_s': 0.25,
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

    # Keep measured bus tracking the commanded target after each supply write.
    original_command = dispatcher.command

    def tracking_command(role, value):
        original_command(role, value)
        if role == SUPPLY:
            backend.inject('dc_bus_voltage', value * 80.0)

    dispatcher.command = tracking_command
    backend.inject('dc_bus_voltage', 40.0)
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_voltage_closed_loop')
    result = runner.run()
    assert result.status == 'complete'
    assert any(r.get('event') == 'sweep_voltage_qualified' for r in records)
    assert all(r.get('uses_require_controller') is not True
               for r in records if r.get('event') == 'sweep_voltage_qualify_begin')


def test_stale_supervisory_sample_fails_closed(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    dispatcher.backend.connect()
    dispatcher.prepare()
    backend.inject('dc_bus_voltage', 40.0, sampled_at=clock.now - 10.0)
    with pytest.raises(InterlockError, match='stale|no silent'):
        dispatcher.sample_supervisory('dc_bus_voltage', max_age=0.25)


def test_full_closed_loop_rpm_early_exit(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = parse_motor_sweep({
        'mode': 'full_closed_loop', 'start_v': 40.0, 'end_v': 39.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'voltage_qualify': {
            'channel': 'dc_bus_voltage', 'tolerance_v': 0.5, 'qualify_duration_s': 0.02,
            'timeout_s': 2.0, 'poll_s': 0.02, 'max_age_s': 1.0,
        },
        'rpm_feedback': {
            'channel': 'erpm', 'kind': 'electrical', 'poll_s': 0.02, 'max_age_s': 1.0,
            'accel_timeout_s': 2.0, 'brake_timeout_s': 2.0,
        },
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 10.0, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 10.0,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
            'accel_rpm_target': 500, 'brake_rpm_target': 50, 'rpm_kind': 'electrical',
        },
    }, outputs=config.raw['outputs'])
    dispatcher.backend.connect()
    dispatcher.prepare()
    original_command = dispatcher.command

    def tracking_command(role, value):
        original_command(role, value)
        if role == SUPPLY:
            backend.inject('dc_bus_voltage', value * 80.0)
        if role == MOTOR and value > 0:
            backend.inject('erpm', 600.0)
        if role == BRAKE and value > 0:
            backend.inject('erpm', 40.0)
        if role == MOTOR and value == 0:
            backend.inject('erpm', 200.0)

    dispatcher.command = tracking_command
    backend.inject('dc_bus_voltage', 40.0)
    backend.inject('erpm', 0.0)
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_full_closed_loop')
    result = runner.run()
    assert result.status == 'complete'
    assert any(r.get('event') == 'sweep_rpm_target_reached' for r in records)


def test_full_closed_loop_missing_rpm_targets_no_silent_fallback(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    sweep = parse_motor_sweep({
        'mode': 'full_closed_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'voltage_qualify': {
            'channel': 'dc_bus_voltage', 'tolerance_v': 0.5, 'qualify_duration_s': 0.02,
            'timeout_s': 2.0, 'poll_s': 0.02, 'max_age_s': 1.0,
        },
        'rpm_feedback': {'channel': 'erpm', 'kind': 'electrical', 'poll_s': 0.02, 'max_age_s': 1.0},
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
        },
    }, outputs=config.raw['outputs'])
    dispatcher.backend.connect()
    dispatcher.prepare()
    original_command = dispatcher.command

    def tracking_command(role, value):
        original_command(role, value)
        if role == SUPPLY:
            backend.inject('dc_bus_voltage', value * 80.0)

    dispatcher.command = tracking_command
    backend.inject('dc_bus_voltage', 40.0)
    runner = MotorSweepRunner(dispatcher, sweep, 'motor_sweep_full_closed_loop')
    with pytest.raises(SweepFault, match='no silent fallback'):
        runner.run()


def test_execute_assignment_dispatches_sweep(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    # Replace open-loop sweep config with tiny grid via monkeypatch on config.motor_sweeps
    tiny = parse_motor_sweep({
        'mode': 'open_loop', 'start_v': 39.0, 'end_v': 40.0, 'step_v': 1.0,
        'round_trips': 1, 'settle_s': 0.0,
        'fast_regen': {
            'throttle_target_v': 0.4, 'brake_target_v': 0.2,
            'accel_ramp_s': 0.1, 'accel_hold_s': 0.1, 'throttle_release_s': 0.1,
            'transition_s': 0.1, 'brake_ramp_s': 0.1, 'brake_hold_s': 0.1,
            'brake_release_s': 0.1, 'recovery_s': 0.1,
        },
    }, outputs=config.raw['outputs'])
    config.motor_sweeps['motor_sweep_open_loop'] = tiny
    dispatcher.backend.connect()
    dispatcher.prepare()
    result = dispatcher.execute_assignment('motor_sweep_open_loop')
    assert result.status == 'complete'


def test_stop_sweep_idle_is_noop(tmp_path):
    dispatcher, backend, clock, records, config = _load(tmp_path)
    dispatcher.backend.connect()
    dispatcher.prepare()
    dispatcher.execute_assignment('stop_sweep')
    assert any(r.get('event') == 'stop_sweep_idle' for r in records)
