"""Offline memory tests for vesc_brake / Braking Control coordination."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxygen_engine.dispatcher import BRAKE, MOTOR, SUPPLY, Dispatcher
from oxygen_engine.errors import AuthorizationError, ConfigError, InterlockError
from oxygen_engine.memory_backend import MemoryBackend
from oxygen_engine.schema import load_dispatcher_config

ROOT = Path(__file__).resolve().parents[1]
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
                      'requested': values})
            wait(0)
        return {'actual_elapsed_s': elapsed, 'skipped_updates': 0, 'maximum_lateness_s': 0.0}
    elapsed = 0.0
    for segment in pattern.segments:
        elapsed = segment.end_s
        value = pattern.value_at(elapsed)
        guard(value)
        verify(value)
        progress({'event': 'progress', 'elapsed_s': elapsed, 'phase': segment.phase, 'requested_v': value})
        wait(0)
    return {'actual_elapsed_s': elapsed, 'skipped_updates': 0, 'maximum_lateness_s': 0.0}


def _load(tmp_path, raw=None):
    raw = json.loads(CES.read_text(encoding='utf-8-sig')) if raw is None else raw
    path = tmp_path / 'bindings.json'
    for assignment in raw['assignments']:
        profile = Path(assignment['profile'])
        if not profile.is_absolute():
            assignment['profile'] = str((CES.parent / profile).resolve())
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock, records = Clock(), []
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    return dispatcher, backend, clock, records


def test_ces_eight_button_loads_with_braking_control_fixture():
    config = load_dispatcher_config(CES)
    assert config.has_brake
    assert config.raw['outputs'][BRAKE]['brake_release_verified'] is False
    assert config.raw['live_control']['enable_physical_brake'] is False
    fixture = config.raw['simulation']['outputs'][config.raw['outputs'][BRAKE]['binding']]
    assert fixture['name'] == 'Braking Control'
    assert {a['trigger'] for a in config.raw['assignments'] if a.get('enabled', True)} == {
        'Ramp Enable', 'Ramp Disable', 'Motor Gentle', 'Run Regen Test', 'Run Driving Cycle',
        'Run Efficiency Scan', 'RZXA Voltage Down', 'RZXA Voltage Up',
    }


def test_physical_brake_authorization_rejected(tmp_path):
    raw = json.loads((ROOT / 'examples' / 'physical-dual.json').read_text(encoding='utf-8-sig'))
    raw['outputs'][BRAKE] = {
        'role': 'motor_brake',
        'channel': 'Braking Control',
        'display_name': 'Braking Control',
        'expected_id': None,
        'channel_discovery_required': True,
        'binding': 'physical_brake',
        'authorization': {'simulation': False, 'physical': True},
        'mode': 'ConstOutput',
        'source': 'DISCOVER_CONSTANT',
        'const_property': 'auto',
        'min_aout_v': 0.0,
        'max_aout_v': 0.4,
        'brake_min_aout_v': 0.0,
        'brake_max_aout_v': 0.4,
        'brake_release_aout_v': 0.0,
        'brake_release_verified': True,
        'completion_policy': 'return_release',
        'on_cancel': 'return_release',
        'on_fault': 'return_release',
        'on_shutdown': 'return_release',
    }
    raw['live_control']['enable_physical_brake'] = False
    raw['interlocks'].update({
        'drive_brake_mutual_exclusion': True,
        'zero_drive_before_brake': True,
        'release_brake_before_drive': True,
    })
    for assignment in raw['assignments']:
        assignment['profile'] = str((ROOT / 'examples' / assignment['profile']).resolve())
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ConfigError, match='Physical vesc_brake authorization is disabled'):
        load_dispatcher_config(path)


def test_enable_physical_brake_rejected(tmp_path):
    raw = json.loads(CES.read_text(encoding='utf-8-sig'))
    raw['live_control']['enable_physical_brake'] = True
    for assignment in raw['assignments']:
        assignment['profile'] = str((CES.parent / assignment['profile']).resolve())
    path = tmp_path / 'live-brake.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ConfigError, match='Live physical braking remains disabled'):
        load_dispatcher_config(path)


def _prepare_with_supply(dispatcher, backend):
    backend.connect()
    dispatcher.prepare()
    # CES rehearsal requires a commissioned supply demand before motor profiles.
    dispatcher.command(SUPPLY, 0.5)


def test_motor_gentle_verifies_brake_release_when_configured(tmp_path):
    dispatcher, backend, _, records = _load(tmp_path)
    _prepare_with_supply(dispatcher, backend)
    dispatcher.execute_profile('motor_gentle')
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[BRAKE].value == 0
    assert all(role != BRAKE or value == 0 for _, role, value in backend.commands)
    assert dispatcher.cleanup()


def test_regen_test_coordinates_drive_then_brake(tmp_path):
    dispatcher, backend, _, records = _load(tmp_path)
    _prepare_with_supply(dispatcher, backend)
    dispatcher.execute_profile('regen_test')
    roles_written = [role for _, role, _ in backend.commands]
    assert MOTOR in roles_written and BRAKE in roles_written
    # Never command brake while drive demand is non-zero: reconstruct ordered states.
    drive = brake = 0.0
    for _, role, value in backend.commands:
        if role == MOTOR:
            drive = value
        elif role == BRAKE:
            brake = value
        assert not (drive > 1e-9 and brake > 1e-9)
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[BRAKE].value == 0
    names = [r['name'] for r in records if r['event'] == 'checkpoint']
    assert 'brake_trial_begin' in names and 'coast_comparison_complete' in names


def test_driving_cycle_releases_brake_before_second_accel(tmp_path):
    dispatcher, backend, _, _ = _load(tmp_path)
    _prepare_with_supply(dispatcher, backend)
    dispatcher.execute_profile('driving_cycle')
    drive = brake = 0.0
    saw_second_accel = False
    for _, role, value in backend.commands:
        if role == MOTOR:
            if value > 0.3:
                saw_second_accel = True
                assert brake == 0
            drive = value
        elif role == BRAKE:
            brake = value
            # Release-level brake writes may occur while drive is active; demand must not.
            if value > 1e-9:
                assert drive == 0
    assert saw_second_accel
    assert dispatcher.states[BRAKE].value == 0


def test_runtime_rejects_brake_while_drive_active(tmp_path):
    dispatcher, backend, _, _ = _load(tmp_path, None)
    backend.connect()
    dispatcher.prepare()
    dispatcher.command(MOTOR, 0.2)
    with pytest.raises(InterlockError, match='Zero drive before brake'):
        dispatcher.command(BRAKE, 0.1)


def test_runtime_rejects_drive_while_brake_active(tmp_path):
    dispatcher, backend, _, _ = _load(tmp_path, None)
    backend.connect()
    dispatcher.prepare()
    dispatcher.command(BRAKE, 0.1)
    with pytest.raises(InterlockError, match='Release brake before drive'):
        dispatcher.command(MOTOR, 0.2)


def test_schema_rejects_overlapping_drive_brake_profile(tmp_path):
    raw = json.loads(CES.read_text(encoding='utf-8-sig'))
    profile = {
        'schema_version': 3,
        'name': 'bad_overlap',
        'output': 'vesc_throttle',
        'outputs': ['vesc_throttle', 'vesc_brake'],
        'timeout_s': 20,
        'completion': {'mode': 'return_neutral', 'value': 0.0},
        'requires': {'motor_neutral': True, 'motor_enable': 'operator_verified', 'brake_released': True},
        'steps': [
            {'type': 'assert_preconditions', 'params': {'motor_neutral': True, 'brake_released': True}},
            {'type': 'set_aout', 'params': {'channel': 'vesc_throttle', 'value': 0.0}},
            {'type': 'set_aout', 'params': {'channel': 'vesc_brake', 'value': 0.0}},
            {'type': 'ramp_aout', 'params': {'channel': 'vesc_throttle', 'target': 0.2, 'duration_s': 1.0}},
            {'type': 'ramp_aout', 'params': {'channel': 'vesc_brake', 'target': 0.1, 'duration_s': 1.0}},
            {'type': 'ramp_aout', 'params': {'channel': 'vesc_throttle', 'target': 0.0, 'duration_s': 1.0}},
            {'type': 'ramp_aout', 'params': {'channel': 'vesc_brake', 'target': 0.0, 'duration_s': 1.0}},
            {'type': 'return_neutral', 'params': {'channel': 'vesc_throttle'}},
            {'type': 'return_neutral', 'params': {'channel': 'vesc_brake'}},
        ],
    }
    profile_path = tmp_path / 'bad_overlap.json'
    profile_path.write_text(json.dumps(profile), encoding='utf-8')
    raw['assignments'] = [{
        'name': 'bad', 'trigger': 'Bad Button', 'output': 'vesc_throttle', 'profile': str(profile_path),
    }]
    path = tmp_path / 'cfg.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ConfigError, match='Zero drive before brake'):
        load_dispatcher_config(path)


def test_cleanup_releases_brake(tmp_path):
    dispatcher, backend, _, records = _load(tmp_path, None)
    backend.connect()
    dispatcher.prepare()
    dispatcher.command(BRAKE, 0.2)
    assert dispatcher.cleanup()
    assert dispatcher.states[BRAKE].value == 0
    assert any(r['event'] == 'brake_release_verified' for r in records)
