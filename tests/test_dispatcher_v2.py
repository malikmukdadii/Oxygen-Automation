from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxygen_engine.dispatcher_v2_schema import (
    canonical_to_legacy, load_v2_config, load_v2_profile, migrate_legacy,
)
from oxygen_engine.dual_output_dispatcher import DualOutputDispatcher, MOTOR, SUPPLY
from oxygen_engine.main import main
from oxygen_engine.pattern_dispatcher import load_dispatcher_config
from oxygen_engine.simulation_fixture import MemoryFixture, SensorSample, load_scenario

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'dispatcher.v2.simulation.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def raw_config():
    raw = json.loads(CONFIG.read_text(encoding='utf-8'))
    for assignment in raw['assignments']:
        assignment['profile'] = str(ROOT / assignment['profile'])
    return raw


def save(tmp_path, raw, filename='bindings.json'):
    path = tmp_path / filename
    path.write_text(json.dumps(raw), encoding='utf-8')
    return path


def prepared(*, config=None, timeline=None):
    config = config or load_v2_config(CONFIG)
    clock, records = Clock(), []
    backend = MemoryFixture(config, clock=clock)
    kwargs = {} if timeline is None else {'timeline': timeline}
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait, **kwargs)
    backend.connect()
    dispatcher.prepare()
    return dispatcher, backend, clock, records


def inject_bus(backend, value=40.0):
    backend.inject('dc_bus_voltage', value)


def test_all_six_proposed_profiles_load_without_real_channel_ids():
    config = load_v2_config(CONFIG)
    assert len(config.profiles) == 6
    assert all(spec['expected_id'] is None for spec in config.raw['outputs'].values())
    assert config.raw['outputs'][SUPPLY]['mapping']['verified'] is False
    assert config.profiles['rzxa_to_40_hold'].completion['mode'] == 'hold_final'


@pytest.mark.parametrize('filename', ['dispatcher.v2.simulation.json', 'tests/fixtures/dispatcher_v2_demo_bindings.json'])
def test_v2_accepts_any_exact_slot_or_controller_trigger_name(tmp_path, filename):
    source = ROOT / filename
    raw = json.loads(source.read_text(encoding='utf-8'))
    for assignment in raw['assignments']:
        assignment['profile'] = str((source.parent / assignment['profile']).resolve())
    raw['assignments'][0]['trigger'] = 'DO 9/8'
    raw['assignments'][3]['trigger'] = 'Chassis Controller DOUT "Start", rack A'
    config = load_v2_config(save(tmp_path, raw))
    assert config.raw['assignments'][0]['trigger'] == 'DO 9/8'
    assert config.raw['assignments'][3]['trigger'] == 'Chassis Controller DOUT "Start", rack A'


@pytest.mark.parametrize('trigger', [None, '', '   ', 123, [], {}, 'DO\n1', 'DO\r1', 'DO\x001'])
def test_invalid_trigger_names_are_rejected(tmp_path, trigger):
    raw = raw_config()
    raw['assignments'][0]['trigger'] = trigger
    with pytest.raises(ValueError, match='DOUT channel name'):
        load_v2_config(save(tmp_path, raw))


def test_duplicate_custom_trigger_names_are_rejected(tmp_path):
    raw = raw_config()
    raw['assignments'][0]['trigger'] = raw['assignments'][1]['trigger'] = 'Controller DOUT 1'
    with pytest.raises(ValueError, match='unique'):
        load_v2_config(save(tmp_path, raw))


def test_custom_controller_trigger_works_in_fixture_scenario(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('No network is permitted'))
    raw = raw_config()
    trigger = raw['assignments'][0]['trigger'] = 'Chassis Controller DOUT 1'
    config = load_v2_config(save(tmp_path, raw))
    scenario = save(tmp_path, {'simulation_only': True, 'events': [
        {'at_s': .5, 'buttons': {trigger: True}}, {'at_s': 10, 'stop': True},
    ]}, 'scenario.json')
    names = [a['trigger'] for a in raw['assignments']]
    events = load_scenario(scenario, raw['measurement_roles'], names)
    clock, records = Clock(), []
    backend = MemoryFixture(config, clock=clock, events=events)
    assert set(backend.buttons) == set(names)
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    assert dispatcher.run() == 0
    assert dispatcher.completed == 1
    assert backend.outputs['supply_demand_fixture']['initial_v'] == .5


def test_scenario_rejects_an_unconfigured_custom_trigger(tmp_path):
    raw = raw_config()
    scenario = save(tmp_path, {'simulation_only': True, 'events': [
        {'at_s': 0, 'buttons': {'Not a configured DOUT': True}},
    ]}, 'scenario.json')
    with pytest.raises(ValueError, match='configured trigger names'):
        load_scenario(scenario, raw['measurement_roles'], [a['trigger'] for a in raw['assignments']])


def test_supply_hold_motor_isolation_repeat_safety_and_explicit_down():
    dispatcher, backend, clock, records = prepared()
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == pytest.approx(.5)
    assert dispatcher.states[MOTOR].value == 0
    assert {role for role, value in backend.commands} == {SUPPLY}
    assert backend.measure('dc_bus_voltage').value == 0, 'Bus measurement must not be generated from demand'
    count = len(backend.commands)
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert len(backend.commands) == count
    assert any(r['event'] == 'profile_noop_already_at_target' for r in records)
    inject_bus(backend)
    before = len(backend.commands)
    supply_owner = dispatcher.states[SUPPLY].held_by
    dispatcher.execute_profile('motor_gentle')
    assert {role for role, value in list(backend.commands)[before:]} == {MOTOR}
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[SUPPLY].value == .5
    assert dispatcher.states[SUPPLY].held_by == supply_owner
    assert dispatcher.active_profile is None
    before = len(backend.commands)
    dispatcher.execute_profile('rzxa_down')
    assert {role for role, value in list(backend.commands)[before:]} == {SUPPLY}
    assert dispatcher.states[SUPPLY].value == 0


@pytest.mark.parametrize('name', ['motor_gentle', 'motor_stop_go', 'motor_cruise'])
def test_all_motor_profiles_leave_supply_unchanged_and_finish_neutral(name):
    dispatcher, backend, _, _ = prepared()
    dispatcher.execute_profile('rzxa_to_40_hold')
    inject_bus(backend)
    before = len(backend.commands)
    dispatcher.execute_profile(name)
    assert dispatcher.states[SUPPLY].value == .5
    assert dispatcher.states[MOTOR].value == 0
    assert all(role == MOTOR for role, _ in list(backend.commands)[before:])


def test_characterization_is_supply_only_and_finishes_at_half_volt():
    dispatcher, backend, _, _ = prepared()
    dispatcher.execute_profile('rzxa_36_40_36')
    assert dispatcher.states[SUPPLY].value == .5
    assert {role for role, _ in backend.commands} == {SUPPLY}


@pytest.mark.parametrize('problem', ['missing_bus', 'stale_bus', 'bad_bus', 'future_bus', 'mapping', 'physical_disable', 'external_ready'])
def test_motor_interlocks_fail_closed_before_any_motor_write(problem):
    dispatcher, backend, clock, _ = prepared()
    dispatcher.execute_profile('rzxa_to_40_hold')
    inject_bus(backend)
    if problem == 'missing_bus':
        backend.streaming = False
        backend.samples.pop('dc_bus_voltage')
    elif problem == 'stale_bus':
        backend.streaming = False
        clock.wait(2)
    elif problem == 'future_bus':
        backend.streaming = False
        backend.samples['dc_bus_voltage'] = SensorSample(40, clock() + 10)
    elif problem == 'bad_bus':
        inject_bus(backend, 43)
    else:
        name = {'mapping': 'motor_input_mapping_verified', 'physical_disable': 'motor_physical_disable_verified',
                'external_ready': 'supply_external_ready'}[problem]
        backend.inject(name, False)
    before = list(backend.commands)
    with pytest.raises(RuntimeError):
        dispatcher.execute_profile('motor_gentle')
    assert list(backend.commands) == before
    assert dispatcher.states[SUPPLY].value == .5


@pytest.mark.parametrize('problem', ['motor_not_neutral', 'regen', 'permission'])
def test_supply_down_requires_neutral_motor_and_independent_regen_permission(problem):
    dispatcher, backend, _, _ = prepared()
    dispatcher.execute_profile('rzxa_to_40_hold')
    if problem == 'motor_not_neutral':
        backend.outputs['motor_throttle_fixture']['initial_v'] = .1
    else:
        backend.inject('regen_safe' if problem == 'regen' else 'supply_shutdown_permitted', False)
    before = list(backend.commands)
    with pytest.raises(RuntimeError):
        dispatcher.execute_profile('rzxa_down')
    assert list(backend.commands) == before
    assert backend.outputs['supply_demand_fixture']['initial_v'] == .5


def test_nonzero_unrecognized_supply_start_never_jumps_to_zero():
    dispatcher, backend, _, _ = prepared()
    backend.outputs['supply_demand_fixture']['initial_v'] = .3
    dispatcher.states[SUPPLY].value = .3
    with pytest.raises(RuntimeError, match='zero or already-at-target'):
        dispatcher.execute_profile('rzxa_to_40_hold')
    assert not backend.commands
    assert backend.outputs['supply_demand_fixture']['initial_v'] == .3


@pytest.mark.parametrize('collision', ['fixture_id', 'fixture_name', 'fixture_key', 'actual_id', 'actual_name'])
def test_role_aliases_are_rejected_offline(tmp_path, collision):
    raw = raw_config()
    if collision == 'fixture_id':
        raw['simulation']['outputs']['motor_throttle_fixture']['id'] = '101'
    elif collision == 'fixture_name':
        raw['simulation']['outputs']['motor_throttle_fixture']['name'] = 'FAKE supply analog demand'
    elif collision == 'fixture_key':
        raw['outputs'][MOTOR]['simulation_fixture'] = raw['outputs'][SUPPLY]['simulation_fixture']
    else:
        field = 'expected_id' if collision == 'actual_id' else 'channel'
        raw['outputs'][MOTOR][field] = raw['outputs'][SUPPLY][field] = '999' if field == 'expected_id' else 'same actual channel'
    with pytest.raises(ValueError):
        load_v2_config(save(tmp_path, raw))


def test_discovered_alias_or_wrong_mode_refuses_preflight_without_writes():
    config = load_v2_config(CONFIG)
    clock, records = Clock(), []
    backend = MemoryFixture(config, clock=clock)
    backend.outputs['motor_throttle_fixture']['id'] = '101'
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    backend.connect()
    with pytest.raises(RuntimeError):
        dispatcher.prepare()
    assert not backend.commands and not dispatcher.prepared


@pytest.mark.parametrize('problem', ['unknown_role', 'unknown_step', 'cross_role_write', 'overshoot', 'negative', 'nan',
                                      'bad_rate', 'bad_timeout', 'motor_hold_final', 'extra_field', 'zero_duration'])
def test_invalid_profiles_are_refused(tmp_path, problem):
    config = load_v2_config(CONFIG)
    raw = json.loads((ROOT / 'profiles/motor_gentle_ramp.json').read_text(encoding='utf-8'))
    if problem == 'unknown_role':
        raw['output'] = 'unknown'
    elif problem == 'unknown_step':
        raw['steps'][2]['type'] = 'instrument_op'
    elif problem == 'cross_role_write':
        raw['steps'][2]['params']['channel'] = SUPPLY
    elif problem in {'overshoot', 'negative', 'nan'}:
        raw['steps'][2]['params']['target'] = {'overshoot': .41, 'negative': -.1, 'nan': float('nan')}[problem]
    elif problem == 'bad_rate':
        raw['steps'][2]['params']['rate_per_s'] = 5
    elif problem == 'bad_timeout':
        raw['timeout_s'] = 1
    elif problem == 'motor_hold_final':
        raw['completion'] = {'mode': 'hold_final', 'value': 0, 'operator_action_required_to_stop': True}
    elif problem == 'extra_field':
        raw['steps'][3]['params']['start_recording'] = True
    else:
        raw['steps'][2]['params']['duration_s'] = 0
    with pytest.raises((ValueError, KeyError)):
        load_v2_profile(save(tmp_path, raw, 'bad-profile.json'), MOTOR, config.raw['outputs'][MOTOR])


@pytest.mark.parametrize('flag', ['enable_physical_aout', 'enable_rzxa_output_toggle', 'enable_real_rzxa_scpi'])
def test_live_gates_cannot_be_enabled(tmp_path, flag):
    raw = raw_config()
    raw['live_control'][flag] = True
    with pytest.raises(ValueError):
        load_v2_config(save(tmp_path, raw))


def test_unknown_assignment_role_is_rejected(tmp_path):
    raw = raw_config()
    raw['assignments'][0]['output'] = 'unknown'
    with pytest.raises(ValueError, match='Unknown logical output'):
        load_v2_config(save(tmp_path, raw))


@pytest.mark.parametrize('role', [SUPPLY, MOTOR])
@pytest.mark.parametrize('cancel', [True, False])
def test_cancel_or_fault_attempts_motor_neutral_but_preserves_supply(role, cancel):
    config = load_v2_config(CONFIG)
    clock, records = Clock(), []
    backend = MemoryFixture(config, clock=clock)
    if role == MOTOR:
        backend.outputs['supply_demand_fixture']['initial_v'] = .5
        inject_bus(backend)

    def interrupted(pattern, setpoint, verify, emit, **kwargs):
        setpoint(pattern.value_at(1))
        if cancel:
            raise KeyboardInterrupt
        raise TimeoutError('Injected I/O fault')

    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait, timeline=interrupted)
    dispatcher.monitor = lambda: dispatcher.execute_profile('rzxa_to_40_hold' if role == SUPPLY else 'motor_gentle')
    assert dispatcher.run() == (130 if cancel else 1)
    assert backend.outputs['motor_throttle_fixture']['initial_v'] == 0
    assert backend.outputs['supply_demand_fixture']['initial_v'] == pytest.approx(.1 if role == SUPPLY else .5)
    assert dispatcher.completed == 0
    assert any(r['event'] == 'profile_interrupted_state' for r in records)
    assert not backend.connected


def test_disconnect_does_not_reconnect_or_claim_verified_cleanup():
    config = load_v2_config(CONFIG)
    clock, records = Clock(), []
    backend = MemoryFixture(config, clock=clock)

    def disconnected(pattern, setpoint, verify, emit, **kwargs):
        setpoint(.1)
        backend.close()
        raise ConnectionError('Injected disconnect')

    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait, timeline=disconnected)
    dispatcher.monitor = lambda: dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.run() == 1
    assert backend.outputs['supply_demand_fixture']['initial_v'] == .1
    assert records[-1]['status'] == 'cleanup_unknown'
    assert any(r['event'] == 'supply_demand_unknown' for r in records)


def test_restart_adopts_only_readback_and_rejects_stale_independent_feedback():
    first, backend, clock, records = prepared()
    first.execute_profile('rzxa_to_40_hold')
    first.states[SUPPLY].value = .123  # A stale application state must not be restored.
    backend.close()
    backend.streaming = False
    inject_bus(backend)
    clock.wait(2)
    second = DualOutputDispatcher(first.config, backend, records.append, clock=clock, wait=clock.wait)
    backend.connect()
    second.prepare()
    assert second.states[SUPPLY].value == .5
    before = list(backend.commands)
    with pytest.raises(RuntimeError):
        second.execute_profile('motor_gentle')
    assert list(backend.commands) == before


def test_non_neutral_motor_on_restart_refuses_all_writes():
    config = load_v2_config(CONFIG)
    backend = MemoryFixture(config)
    backend.outputs['motor_throttle_fixture']['initial_v'] = .1
    backend.connect()
    dispatcher = DualOutputDispatcher(config, backend, lambda r: None)
    with pytest.raises(RuntimeError, match='Startup motor'):
        dispatcher.prepare()
    assert not backend.commands


def test_demo_scenario_is_network_free_and_runs_hold_motor_down(monkeypatch):
    import socket

    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('No network is permitted'))
    config = load_v2_config(CONFIG)
    clock, records = Clock(), []
    events = load_scenario(ROOT / 'scenarios/dual_aout_fixture_demo.json', config.raw['measurement_roles'])
    backend = MemoryFixture(config, clock=clock, events=events)
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    assert dispatcher.run() == 0
    assert dispatcher.completed == 3
    assert [r['assignment'] for r in records if r['event'] == 'pattern_cycle_complete'] == ['rzxa_to_40_hold', 'motor_gentle', 'rzxa_down']
    assert backend.outputs['supply_demand_fixture']['initial_v'] == 0
    assert backend.outputs['motor_throttle_fixture']['initial_v'] == 0
    assert records[-1]['physical_actuation'] is False


def test_startup_high_busy_pulses_and_held_high_do_not_replay():
    config = load_v2_config(CONFIG)
    clock, records = Clock(), []
    events = [
        {'at_s': 0, 'buttons': {'DO 3/1 Sim': True}},
        {'at_s': .3, 'buttons': {'DO 3/1 Sim': False}},
        {'at_s': .5, 'buttons': {'DO 3/1 Sim': True}},
        {'at_s': 2, 'buttons': {'DO 3/4 Sim': True}},
        {'at_s': 3, 'buttons': {'DO 3/4 Sim': False}},
        {'at_s': 9, 'telemetry': {'dc_bus_voltage': 40}},
        {'at_s': 10, 'buttons': {'DO 3/4 Sim': True}},
        {'at_s': 20, 'stop': True},
    ]
    backend = MemoryFixture(config, clock=clock, events=events)
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    assert dispatcher.run() == 0
    assert dispatcher.started == dispatcher.completed == 2
    assert backend.outputs['supply_demand_fixture']['initial_v'] == .5


def test_simultaneous_buttons_use_config_order_without_queue():
    config = load_v2_config(CONFIG)
    clock, records = Clock(), []
    events = [{'at_s': .5, 'buttons': {'DO 3/1 Sim': True, 'DO 3/3 Sim': True}}, {'at_s': 10, 'stop': True}]
    backend = MemoryFixture(config, clock=clock, events=events)
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    assert dispatcher.run() == 0 and dispatcher.completed == 1
    assert any(r['event'] == 'trigger_ignored' and r['assignment'] == 'rzxa_36_40_36' for r in records)


def test_preview_and_missing_confirmation_cannot_construct_backend(monkeypatch, capsys):
    import oxygen_engine.dual_output_dispatcher as module

    monkeypatch.setattr(module, 'MemoryFixture', lambda *a, **k: pytest.fail('No execution authorized'))
    assert main(['dispatch-patterns', '--bindings', str(CONFIG)]) == 0
    assert json.loads(capsys.readouterr().out)['connected'] is False
    assert main(['dispatch-patterns', '--bindings', str(CONFIG), '--execute']) == 1
    assert main(['dispatch-patterns', '--bindings', str(CONFIG), '--live']) == 1


def test_migration_backs_up_preserves_source_and_retains_three_profiles(tmp_path):
    raw = json.loads((ROOT / 'dispatcher.example.json').read_text(encoding='utf-8'))
    for assignment in raw['assignments']:
        assignment['profile'] = str(ROOT / assignment['profile'])
    source = save(tmp_path, raw, 'dispatcher.json')
    original = source.read_bytes()
    destination = tmp_path / 'migrated' / 'dispatcher.v2.compat.json'
    result = migrate_legacy(source, destination)
    assert source.read_bytes() == original
    assert Path(result['backup']).read_bytes() == original
    migrated = load_dispatcher_config(destination)
    assert [a.pattern.duration_s for a in migrated.assignments] == [10, 6, 8]
    converted = canonical_to_legacy(json.loads(destination.read_text(encoding='utf-8')))
    assert converted['outputs'] == raw['outputs']
    with pytest.raises(ValueError):
        migrate_legacy(source, destination)
    assert source.read_bytes() == original


def test_legacy_scenarios_are_never_sent_to_oxygen(capsys):
    assert main(['dispatch-patterns', '--bindings', str(ROOT / 'dispatcher.example.json'),
                 '--scenario', str(ROOT / 'scenarios/dual_aout_fixture_demo.json')]) == 1
    assert 'never applied to OXYGEN' in capsys.readouterr().err


def test_stale_do_gap_cannot_start_a_profile():
    config = load_v2_config(CONFIG)
    clock, records = Clock(), []
    events = [{'at_s': .1, 'buttons': {'DO 3/1 Sim': True}}, {'at_s': 1, 'stop': True}]
    backend = MemoryFixture(config, clock=clock, events=events)
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock,
                                      wait=lambda seconds: clock.wait(.5))
    assert dispatcher.run() == 0
    assert dispatcher.started == 0


def test_slow_preconditions_discard_the_edge_without_actuation():
    dispatcher, backend, clock, records = prepared()
    original = dispatcher.assert_preconditions

    def slow(profile):
        original(profile)
        clock.wait(.3)

    dispatcher.assert_preconditions = slow
    dispatcher.execute_profile('rzxa_to_40_hold', trigger_observed=clock())
    assert not backend.commands and dispatcher.started == 0
    assert any(r['event'] == 'trigger_stale' for r in records)


def test_failed_final_neutral_readback_reports_unknown_not_success(monkeypatch):
    config = load_v2_config(CONFIG)
    clock, records = Clock(), []
    backend = MemoryFixture(config, clock=clock)
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    original = backend.read_output
    corrupt = False

    def read(key):
        if corrupt and key == 'motor_throttle_fixture':
            return .1
        return original(key)

    def stop():
        nonlocal corrupt
        corrupt = True
        raise KeyboardInterrupt

    monkeypatch.setattr(backend, 'read_output', read)
    dispatcher.monitor = stop
    assert dispatcher.run() == 1
    assert any(r['event'] == 'motor_neutral_unknown' for r in records)
    assert records[-1]['status'] == 'cleanup_unknown'


def test_migration_rejects_old_engine_config_without_backup_or_overwrite(tmp_path):
    raw = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
    source = save(tmp_path, raw, 'old-engine.json')
    destination = tmp_path / 'new.json'
    original = source.read_bytes()
    with pytest.raises(ValueError):
        migrate_legacy(source, destination)
    assert source.read_bytes() == original
    assert not destination.exists()
    assert not list(tmp_path.glob('*.backup-*'))


def test_bus_excursion_during_motor_run_faults_to_motor_neutral_without_supply_change():
    dispatcher, backend, _, records = prepared()
    dispatcher.execute_profile('rzxa_to_40_hold')
    inject_bus(backend)
    backend.events = [{'at_s': 10, 'telemetry': {'dc_bus_voltage': 45}}]
    dispatcher.monitor = lambda: dispatcher.execute_profile('motor_gentle')
    assert dispatcher.run() == 1
    assert backend.outputs['motor_throttle_fixture']['initial_v'] == 0
    assert backend.outputs['supply_demand_fixture']['initial_v'] == .5
    assert any(r['event'] == 'fault' for r in records)
