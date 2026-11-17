"""Offline tests for schema v3 open-loop dispatcher (no live OXYGEN)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from oxygen_engine.closed_loop import register_controller
from oxygen_engine.dispatcher import MOTOR, SUPPLY, Dispatcher
from oxygen_engine.errors import ControlNotReady, InterlockError
from oxygen_engine.main import main
from oxygen_engine.memory_backend import MemoryBackend
from oxygen_engine.schema import ConfigError, load_dispatcher_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'dispatcher.v3.example.json'
HARDWARE = ROOT / 'dispatcher.v3.hardware.example.json'
DEMO = ROOT / 'dispatcher.v3.oxygen-demo.example.json'
SCENARIO = ROOT / 'scenarios' / 'open_loop_fixture_demo.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def boundary_timeline(pattern, guard, verify, progress, *, interval_s, max_lateness_s, clock, wait):
    """Deterministic timeline: hit each segment end once."""
    elapsed = 0.0
    for segment in pattern.segments:
        elapsed = segment.end_s
        value = pattern.value_at(elapsed)
        guard(value)
        verify(value)
        progress({'event': 'progress', 'elapsed_s': elapsed, 'phase': segment.phase, 'requested_v': value})
        wait(0)
    return {'actual_elapsed_s': elapsed, 'skipped_updates': 0, 'maximum_lateness_s': 0.0}


@pytest.fixture(autouse=True)
def _clear_controller():
    register_controller(None)
    yield
    register_controller(None)


def test_example_config_loads_open_loop_without_measurements():
    config = load_dispatcher_config(EXAMPLE)
    assert config.mode == 'open_loop'
    assert config.transport == 'memory'
    assert config.raw['measurements'] == {}
    assert set(config.profiles) == {'rzxa_to_40_hold', 'rzxa_down', 'motor_gentle'}


def test_hardware_and_demo_examples_load():
    assert load_dispatcher_config(HARDWARE).physical
    assert load_dispatcher_config(DEMO).transport == 'oxygen_scpi'
    assert not load_dispatcher_config(DEMO).physical


def test_open_loop_refuses_nonempty_measurements(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    raw['measurements'] = {
        'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': None, 'mapping_verified': False}
    }
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ConfigError, match='empty or omitted'):
        load_dispatcher_config(path)


def test_closed_loop_without_controller_is_not_ready(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    raw['control'] = {'mode': 'closed_loop'}
    raw['measurements'] = {
        'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': None, 'mapping_verified': False},
        'motor_input_mapping_verified': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'motor_physical_disable_verified': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'supply_external_ready': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'regen_safe': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'supply_shutdown_permitted': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'supply_standalone_test': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'supply_disabled': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
    }
    raw['interlocks'].update({
        'bus_target_v': 40.0,
        'bus_tolerance_v': 2.0,
        'max_feedback_age_s': 1.0,
        'motor_requires_measured_bus_in_range': True,
        'allow_unverified_simulated_interlock_values': True,
        'allow_unverified_live_interlock_values': False,
    })
    raw['simulation']['telemetry'] = {name: (40.0 if name == 'dc_bus_voltage' else True)
                                      for name in raw['measurements']}
    # Closed-loop profiles still need open-loop-compatible profile files for this check;
    # schema will reject open-loop profiles' missing measured requirements when loading assignments
    # that use open-loop profiles — use a minimal assignment with rewritten profiles.
    for assignment in raw['assignments']:
        assignment['enabled'] = assignment['name'] == 'rzxa_to_40_hold'
    # Provide a closed-loop capable supply profile
    profile = {
        'schema_version': 3,
        'name': 'closed_loop_hold',
        'output': 'rzxa_voltage_demand',
        'timeout_s': 30,
        'completion': {'mode': 'hold_final', 'value': 0.5, 'operator_action_required_to_stop': True},
        'requires': {'motor_neutral': True, 'supply_output_enable': 'external_or_explicitly_verified',
                     'measured_bus_feedback': 'required_for_motor_interlock'},
        'steps': [
            {'type': 'assert_preconditions',
             'params': {'motor_neutral': True, 'current_aout_must_be_zero_or_already_target': True}},
            {'type': 'ramp_aout', 'params': {'channel': 'rzxa_voltage_demand', 'target': 0.5, 'duration_s': 5.0}},
            {'type': 'hold', 'params': {'duration_s': 3.0}},
        ],
    }
    profile_path = tmp_path / 'closed_hold.json'
    profile_path.write_text(json.dumps(profile), encoding='utf-8')
    raw['assignments'] = [{
        'name': 'rzxa_to_40_hold', 'enabled': True, 'trigger': 'DO 3/1 Sim',
        'output': 'rzxa_voltage_demand', 'profile': str(profile_path),
    }]
    path = tmp_path / 'closed.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    backend = MemoryBackend(config)
    records = []
    with pytest.raises(ControlNotReady, match='no ClosedLoopController'):
        Dispatcher(config, backend, records.append)


def test_open_loop_profiles_and_cleanup_never_sample(tmp_path):
    config = load_dispatcher_config(EXAMPLE)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.measure = lambda name: (_ for _ in ()).throw(AssertionError(f'sample {name}'))
    backend.connect()
    dispatcher.prepare()
    with pytest.raises(InterlockError, match='Supply demand readback'):
        dispatcher.execute_profile('motor_gentle')
    dispatcher.execute_profile('rzxa_to_40_hold')
    dispatcher.execute_profile('motor_gentle')
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[SUPPLY].value == 0.5
    assert dispatcher.cleanup()
    assert dispatcher.states[SUPPLY].value == 0.5
    assert not any(r.get('event') == 'independent_feedback' for r in records)
    assert any(r.get('event') == 'open_loop_warning' for r in records)
    dispatcher.execute_profile('rzxa_down')
    assert dispatcher.states[SUPPLY].value == 0


def test_open_loop_sample_refuses_explicitly():
    config = load_dispatcher_config(EXAMPLE)
    backend = MemoryBackend(config)
    dispatcher = Dispatcher(config, backend, lambda *_: None)
    with pytest.raises(InterlockError, match='cannot satisfy'):
        dispatcher.sample('dc_bus_voltage')
    with pytest.raises(InterlockError, match='no measurements'):
        backend.measure('dc_bus_voltage')


def test_offline_preview_does_not_connect(monkeypatch, capsys):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('must not connect'))
    assert main(['dispatch-patterns', '--bindings', str(EXAMPLE)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['schema_version'] == 3
    assert plan['control_mode'] == 'open_loop'
    assert plan['connected'] is False


def test_memory_scenario_run_completes():
    config = load_dispatcher_config(EXAMPLE)
    from oxygen_engine.memory_backend import load_scenario
    events = load_scenario(SCENARIO, {}, ['DO 3/1 Sim', 'DO 3/2 Sim', 'DO 3/4 Sim'])
    clock = Clock()
    backend = MemoryBackend(config, clock=clock, events=events)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    code = dispatcher.run()
    assert code == 0
    assert dispatcher.completed >= 1
    assert any(r['event'] == 'dispatcher_summary' and r['status'] == 'simulation_complete' for r in records)


def test_verified_gain_eighty_preserved():
    config = load_dispatcher_config(HARDWARE)
    assert config.raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] == 80.0
    assert config.raw['outputs'][MOTOR]['max_aout_v'] == 0.4


def test_physical_execution_requires_confirmations(monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('no network'))
    assert main(['dispatch-patterns', '--bindings', str(HARDWARE)]) == 0
    assert main([
        'dispatch-patterns', '--bindings', str(HARDWARE), '--execute', '--live',
        '--confirm-hardware-interlocks',
        '--confirm-physical-output', 'Test Aout2',
    ]) == 1
