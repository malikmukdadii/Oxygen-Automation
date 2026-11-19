"""Staged CES current-demand waveforms; no physical RPM or regeneration model."""

import json
from pathlib import Path

import pytest

from oxygen_engine.dispatcher import BRAKE, Dispatcher, MOTOR, SUPPLY
from oxygen_engine.drive_limits import authorize_dispatcher_motor_max
from oxygen_engine.errors import ConfigError, ControlError, InterlockError
from oxygen_engine.main import main
from oxygen_engine.memory_backend import MemoryBackend, load_scenario
from oxygen_engine.schema import load_dispatcher_config
from oxygen_engine.scpi_transport import ScpiCommandError, ScpiErrorEntry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples/ces-motor-commissioning.json'
EXPECTED = {
    'motor_gentle': [(3, .6), (4, .6), (3, 0)],
    'regen_test': [(3, 1.6), (4, 1.6), (1, 0), (.5, 0), (1, 0), (4, 0), (1, 0), (2, 0)],
    'driving_cycle': [(2, .6), (2, .6), (1, 0), (1, 0), (.5, 0), (1, 0), (.5, 0),
                      (2, 1.0), (2, 1.0), (1, 0), (1, 0), (.5, 0), (1, 0), (.5, 0),
                      (2, 1.6), (2, 1.6), (1, 0), (2, 0)],
    'efficiency_scan': [(1, .4), (4, .4), (1, .8), (4, .8),
                        (1, 1.2), (4, 1.2), (1, 1.6), (4, 1.6), (2, 0)],
}
DURATIONS = {'motor_gentle': 10, 'regen_test': 16.5, 'driving_cycle': 23, 'efficiency_scan': 22}
DRIVE_CEILING = 1.6
BUTTONS = ['Ramp Enable', 'Ramp Disable', 'Motor Gentle', 'Run Regen Test',
           'Run Driving Cycle', 'Run Efficiency Scan', 'RZ-XA Step Down', 'RZ-XA Step Up']


def motor_segments(pattern):
    if hasattr(pattern, 'segments_by_role'):
        return list(pattern.segments_by_role[MOTOR])
    return list(pattern.segments)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    monkeypatch.setattr('socket.socket', lambda *a, **k: pytest.fail('CES candidate tests must never open a socket'))
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: pytest.fail('No live connection'))


def raw_config():
    raw = json.loads(EXAMPLE.read_text())
    for assignment in raw['assignments']:
        assignment['profile'] = str((EXAMPLE.parent / assignment['profile']).resolve())
    return raw


def load(tmp_path, raw):
    path = tmp_path / 'candidate.json'
    path.write_text(json.dumps(raw))
    return load_dispatcher_config(path)


def setup(tmp_path, raw=None):
    config = load(tmp_path, raw_config() if raw is None else raw)
    clock, records = Clock(), []
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    return dispatcher, backend, clock, records


@pytest.mark.parametrize('name', EXPECTED)
def test_waveform_bounds_timing_transitions_completion_and_repeatability(tmp_path, name):
    dispatcher, backend, clock, records = setup(tmp_path)
    backend.connect()
    dispatcher.prepare()
    profile = dispatcher.config.profiles[name]
    pattern, checkpoints = dispatcher.compile(profile)
    segments = motor_segments(pattern)
    assert [(s.end_s - s.start_s, s.end_v) for s in segments] == EXPECTED[name]
    assert pattern.duration_s == DURATIONS[name]
    assert segments[0].start_v == 0
    final = (pattern.values_at(pattern.duration_s)[MOTOR]
             if hasattr(pattern, 'values_at') and len(getattr(pattern, 'roles', ())) > 1
             else pattern.value_at(pattern.duration_s))
    assert final == 0
    for previous, following in zip(segments, segments[1:]):
        assert previous.end_s == following.start_s
        assert previous.end_v == following.start_v
    assert all(0 <= s.start_v <= DRIVE_CEILING and 0 <= s.end_v <= DRIVE_CEILING for s in segments)
    assert profile.completion == {'mode': 'return_neutral', 'value': 0}
    for iteration in range(2):
        began = len(records)
        dispatcher.execute_profile(name)
        assert clock() == pytest.approx((iteration + 1) * DURATIONS[name])
        assert dispatcher.states[MOTOR].value == 0
        assert dispatcher.states[MOTOR].held_by is None
        assert dispatcher.states[BRAKE].value == 0
        assert dispatcher.states[SUPPLY].value == .5
        assert [r['name'] for r in records[began:] if r['event'] == 'checkpoint'] == [c for _, c in checkpoints]
    assert all(role in {MOTOR, BRAKE} and 0 <= value <= (
        DRIVE_CEILING if role == MOTOR else .4) for _, role, value in backend.commands)
    assert dispatcher.cleanup()
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[BRAKE].value == 0
    assert not any(role == SUPPLY for _, role, _ in backend.commands)
    assert not any(r['event'] == 'independent_feedback' for r in records)


def test_gentle_uses_modest_current_and_scan_has_four_equal_observation_windows(tmp_path):
    dispatcher, _, _, _ = setup(tmp_path)
    gentle = dispatcher.config.profiles['motor_gentle']
    assert [s['params']['target'] for s in gentle.steps if s['type'] == 'ramp_aout'] == [.6, 0]
    scan = dispatcher.config.profiles['efficiency_scan']
    assert [s['params']['target'] for s in scan.steps if s['type'] == 'ramp_aout'] == [.4, .8, 1.2, 1.6, 0]
    assert [s['params']['duration_s'] for s in scan.steps if s['type'] == 'hold'] == [4] * 4
    # Drive-only profiles keep ADC2 released; coast/brake markers belong to coordinated trials.
    for name in ('motor_gentle', 'efficiency_scan'):
        profile = dispatcher.config.profiles[name]
        assert profile.outputs == (MOTOR,)


def test_regen_has_exact_acceleration_and_deceleration_measurement_windows(tmp_path):
    dispatcher, backend, _, records = setup(tmp_path)
    backend.connect()
    dispatcher.prepare()
    _, checkpoints = dispatcher.compile(dispatcher.config.profiles['regen_test'])
    assert dict((name, at) for at, name in checkpoints) == {
        'trial_b_regen_accel_begin': 0,
        'trial_b_regen_hold_160_begin': 3,
        'trial_b_regen_drive_release_begin': 7,
        'trial_b_regen_brake_prompt_begin': 8,
        'trial_b_regen_brake_hold': 9.5,
        'trial_b_regen_complete': 14.5,
    }
    dispatcher.execute_profile('regen_test')
    names = [r['name'] for r in records if r['event'] == 'checkpoint']
    assert names.index('trial_b_regen_accel_begin') < names.index('trial_b_regen_drive_release_begin')
    assert names.index('trial_b_regen_drive_release_begin') < names.index('trial_b_regen_brake_prompt_begin')
    assert names.index('trial_b_regen_brake_prompt_begin') < names.index('trial_b_regen_complete')
    assert ('write', MOTOR, 1.6) in backend.commands
    assert ('write', BRAKE, 0.2) in backend.commands
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[BRAKE].value == 0
    assert records[-1]['outputs'][MOTOR]['physical_voltage_established'] is False


def test_coast_trial_a_selectable_on_same_regen_button(tmp_path):
    raw = raw_config()
    raw['assignments'] = [a for a in raw['assignments'] if a['name'] == 'regen_test']
    raw['assignments'][0]['profile'] = str((ROOT / 'profiles/ces/motor_coast_compare.json').resolve())
    dispatcher, backend, _, records = setup(tmp_path, raw)
    backend.connect()
    dispatcher.prepare()
    profile = dispatcher.config.profiles['regen_test']
    assert profile.outputs == (MOTOR,)
    assert [s['params']['target'] for s in profile.steps if s['type'] == 'ramp_aout'] == [1.6, 0]
    dispatcher.execute_profile('regen_test')
    assert ('write', MOTOR, 1.6) in backend.commands
    assert not any(role == BRAKE for _, role, _ in backend.commands)
    assert any(r['name'] == 'trial_a_coast_complete' for r in records if r['event'] == 'checkpoint')


@pytest.mark.parametrize('name', EXPECTED)
@pytest.mark.parametrize('failure', ['cancel', 'fault'])
def test_interrupt_attempts_zero_demand_cleanup_not_physical_fault_authorization(tmp_path, monkeypatch, name, failure):
    dispatcher, backend, clock, records = setup(tmp_path)
    original_wait = clock.wait
    fired = False

    def wait(seconds):
        nonlocal fired
        original_wait(seconds)
        if clock() >= .5 and not fired:
            fired = True
            if failure == 'cancel':
                raise KeyboardInterrupt()
            raise RuntimeError('injected profile fault')

    monkeypatch.setattr(dispatcher, 'wait', wait)
    monkeypatch.setattr(dispatcher, 'monitor', lambda: dispatcher.execute_profile(name))
    assert dispatcher.run() == (130 if failure == 'cancel' else 1)
    assert dispatcher.completed == 0
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[BRAKE].value == 0
    assert dispatcher.states[MOTOR].status == 'readback_verified'
    assert dispatcher.states[SUPPLY].value == .5
    assert not any(role == SUPPLY for _, role, _ in backend.commands)
    assert records[-1]['physical_actuation'] is False


@pytest.mark.parametrize('name,at_s', [('regen_test', 8.5), ('driving_cycle', 20.5)])
def test_cancel_during_deceleration_observation_keeps_zero_demand(tmp_path, monkeypatch, name, at_s):
    dispatcher, backend, clock, _ = setup(tmp_path)

    def wait(seconds):
        clock.wait(seconds)
        if clock() >= at_s:
            raise KeyboardInterrupt()

    monkeypatch.setattr(dispatcher, 'wait', wait)
    monkeypatch.setattr(dispatcher, 'monitor', lambda: dispatcher.execute_profile(name))
    assert dispatcher.run() == 130
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[BRAKE].value == 0
    assert any(value > .4 for _, role, value in backend.commands if role == MOTOR)
    assert dispatcher.completed == 0


def test_communication_loss_is_unknown_no_reconnect_or_guessed_stop_write(tmp_path, monkeypatch):
    dispatcher, backend, clock, records = setup(tmp_path)
    original_connect = backend.connect
    connections = []

    def connect():
        connections.append(clock())
        original_connect()

    def wait(seconds):
        clock.wait(seconds)
        if clock() >= .5:
            backend.close()
            raise ConnectionError('injected connection loss')

    monkeypatch.setattr(backend, 'connect', connect)
    monkeypatch.setattr(dispatcher, 'wait', wait)
    monkeypatch.setattr(dispatcher, 'monitor', lambda: dispatcher.execute_profile('motor_gentle'))
    assert dispatcher.run() == 1
    assert len(connections) == 1
    assert dispatcher.states[MOTOR].status == 'unknown'
    assert records[-1]['status'] == 'cleanup_unknown'
    assert any(r['event'] == 'motor_neutral_unknown' for r in records)
    assert backend.commands[-1][2] > 0


def test_positive_point_three_demand_is_not_a_stopped_startup_condition(tmp_path):
    raw = raw_config()
    raw['simulation']['outputs']['motor_throttle_fixture']['initial_v'] = .3
    dispatcher, backend, _, _ = setup(tmp_path, raw)
    backend.connect()
    with pytest.raises(ControlError, match='not neutral'):
        dispatcher.prepare()
    assert backend.writes_attempted == 0
    assert dispatcher.cleanup()
    assert backend.writes_attempted == 0


@pytest.mark.parametrize('supply', [0, .451])
def test_existing_supply_prerequisite_still_applies(tmp_path, supply):
    raw = raw_config()
    raw['simulation']['outputs']['supply_demand_fixture']['initial_v'] = supply
    dispatcher, backend, _, _ = setup(tmp_path, raw)
    backend.connect()
    dispatcher.prepare()
    with pytest.raises(InterlockError, match='prerequisite'):
        dispatcher.execute_profile('motor_gentle')
    assert backend.writes_attempted == 0


def test_supply_steps_require_zero_demand_without_proving_stopped_motion(tmp_path):
    dispatcher, backend, _, _ = setup(tmp_path)
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('rzxa_voltage_down')
    assert backend.commands[-1] == ('write', SUPPLY, .49375)
    assert dispatcher.states[MOTOR].value == 0
    backend.outputs['motor_throttle_fixture']['value'] = .3
    before = len(backend.commands)
    with pytest.raises(ControlError):
        dispatcher.execute_profile('rzxa_voltage_down')
    assert len(backend.commands) == before


@pytest.mark.parametrize('name', EXPECTED)
def test_candidate_rejected_above_unchanged_physical_point_four_limit(tmp_path, name):
    raw = json.loads((ROOT / 'examples/physical-dual.json').read_text())
    assignment = next(a for a in raw_config()['assignments'] if a['name'] == name)
    raw['assignments'] = [assignment]
    with pytest.raises(ConfigError, match='target|vesc_brake|brake|configured logical roles'):
        load(tmp_path, raw)


def test_point_three_cannot_be_configured_as_normal_stop_for_any_transport(tmp_path):
    for example in ['physical-dual.json', 'oxygen-demo.json', 'ces-motor-commissioning.json']:
        raw = json.loads((ROOT / 'examples' / example).read_text())
        raw['outputs'][MOTOR]['neutral_aout_v'] = .3
        with pytest.raises(ConfigError, match='motor neutral'):
            load(tmp_path, raw)


@pytest.mark.parametrize('physical', [False, True])
@pytest.mark.parametrize('ceiling', [.6, .8, 1.0, 1.2, 1.4, 1.6])
def test_higher_ceiling_always_requires_output_level_commissioning(physical, ceiling):
    spec = {'max_aout_v': ceiling}
    with pytest.raises(ConfigError, match='explicitly authorized'):
        authorize_dispatcher_motor_max(spec, physical=physical)
    spec.update(commissioned_drive_limit_v=ceiling, higher_drive_limit_authorized=False)
    with pytest.raises(ConfigError, match='higher_drive_limit_authorized'):
        authorize_dispatcher_motor_max(spec, physical=physical)
    spec['higher_drive_limit_authorized'] = True
    assert authorize_dispatcher_motor_max(spec, physical=physical) == ceiling
    spec['commissioned_drive_limit_v'] = .4
    with pytest.raises(ConfigError, match='cannot exceed'):
        authorize_dispatcher_motor_max(spec, physical=physical)


def test_lower_commissioning_stage_rejects_scan_before_connection(tmp_path):
    raw = raw_config()
    raw['outputs'][MOTOR].update(max_aout_v=.8, commissioned_drive_limit_v=.8)
    raw['simulation']['outputs']['motor_throttle_fixture']['max_v'] = .8
    with pytest.raises(ConfigError, match='target'):
        load(tmp_path, raw)


@pytest.mark.parametrize('authorized', [False, True])
def test_one_point_six_stage_requires_explicit_authorization(tmp_path, authorized):
    raw = raw_config()
    raw['outputs'][MOTOR].update(max_aout_v=1.6, commissioned_drive_limit_v=1.6,
                                 higher_drive_limit_authorized=authorized)
    raw['simulation']['outputs']['motor_throttle_fixture']['max_v'] = 1.6
    if not authorized:
        with pytest.raises(ConfigError, match='higher_drive_limit_authorized'):
            load(tmp_path, raw)
        return
    dispatcher, backend, _, _ = setup(tmp_path, raw)
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('efficiency_scan')
    assert ('write', MOTOR, 1.6) in backend.commands
    assert dispatcher.states[MOTOR].value == 0
    assert not dispatcher.physical
    assert not any(role == SUPPLY for _, role, _ in backend.commands)


def test_higher_physical_stage_does_not_replace_required_stop_acknowledgement(tmp_path):
    raw = json.loads((ROOT / 'examples/physical-dual.json').read_text())
    raw['assignments'] = [next(a for a in raw_config()['assignments'] if a['name'] == 'motor_gentle')]
    raw['outputs'][MOTOR].update(max_aout_v=.6, commissioned_drive_limit_v=.6,
                                 higher_drive_limit_authorized=False)
    with pytest.raises(ConfigError, match='higher_drive_limit_authorized'):
        load(tmp_path, raw)
    raw['outputs'][MOTOR]['higher_drive_limit_authorized'] = True
    assert load(tmp_path, raw).profiles['motor_gentle'].completion['value'] == 0
    raw['outputs'][MOTOR]['neutral_verified'] = False
    with pytest.raises(ConfigError, match='verified 0 V neutral'):
        load(tmp_path, raw)


def test_failed_profile_command_is_not_retried_before_zero_demand_cleanup(tmp_path, monkeypatch):
    dispatcher, backend, _, records = setup(tmp_path)
    original_write = backend.write
    attempts, failed = [], []

    def write(role, value):
        attempts.append((role, value))
        if role == MOTOR and value >= .4 and not failed:
            failed.append((role, value))
            raise ScpiCommandError('simulated current-demand write', ScpiErrorEntry(-200, 'Execution error'))
        original_write(role, value)

    monkeypatch.setattr(backend, 'write', write)
    monkeypatch.setattr(dispatcher, 'monitor', lambda: dispatcher.execute_profile('regen_test'))
    assert dispatcher.run() == 1
    assert attempts.count(failed[0]) == 1
    assert (MOTOR, 0) in attempts
    assert dispatcher.states[MOTOR].value == 0 or dispatcher.states[MOTOR].status == 'unknown'
    assert dispatcher.completed == 0
    interrupted = next(r for r in records if r['event'] == 'profile_interrupted_state')
    assert interrupted['outputs'][MOTOR]['status'] == 'unknown'
    failure = next(r for r in records if r['event'] == 'output_command_failed')
    assert failure['retry_attempted'] is False
    assert '-200' in failure['error']
    assert not any(role == SUPPLY for role, _ in attempts)


def test_wrong_completion_and_non_neutral_initial_setpoint_rejected(tmp_path):
    for field in ['completion', 'initial']:
        raw = raw_config()
        profile = json.loads((ROOT / 'profiles/ces/motor_gentle.json').read_text())
        if field == 'completion':
            profile['completion']['value'] = .3
        else:
            profile['steps'][1]['params']['value'] = .4
        path = tmp_path / 'bad-profile.json'
        path.write_text(json.dumps(profile))
        raw['assignments'][2]['profile'] = str(path)
        with pytest.raises(ConfigError):
            load(tmp_path, raw)


def test_eight_operator_names_and_global_owner_are_preserved(tmp_path, monkeypatch):
    dispatcher, backend, _, _ = setup(tmp_path)
    assert [a['trigger'] for a in dispatcher.config.raw['assignments']] == BUTTONS
    assert len(dispatcher.config.profiles) == 8
    assert not dispatcher.physical
    assert dispatcher.config.raw['live_control']['enable_physical_aout'] is False
    assert dispatcher.config.raw['outputs'][MOTOR]['neutral_verified'] is False
    assert dispatcher.config.raw['outputs'][MOTOR]['mapping_verified'] is False
    backend.connect()
    dispatcher.prepare()
    original_write = backend.write

    def write(role, value):
        with pytest.raises(ControlError, match='owns an output'):
            dispatcher.execute_profile('rzxa_voltage_down')
        original_write(role, value)

    monkeypatch.setattr(backend, 'write', write)
    dispatcher.execute_profile('motor_gentle')
    assert all(role == MOTOR for _, role, _ in backend.commands)


def test_existing_profiles_are_explicitly_legacy_not_silently_reinterpreted():
    for filename in ['motor_gentle.json', 'motor_coast_compare.json', 'motor_stop_go.json', 'motor_three_level.json']:
        profile = json.loads((ROOT / 'profiles' / filename).read_text())
        description = profile['description']
        assert (
            'LEGACY ZERO-DEMAND MAPPING ONLY' in description
            or 'provisional' in description.lower()
            or 'offline' in description.lower()
        )
        assert profile['completion']['value'] == 0
        assert (ROOT / 'profiles/ces' / filename).is_file()
    trial_b = json.loads((ROOT / 'profiles/ces/motor_regen_trial_b.json').read_text())
    assert trial_b['outputs'] == ['vesc_throttle', 'vesc_brake']
    assert any(s.get('params', {}).get('target') == 1.6 for s in trial_b['steps'] if s['type'] == 'ramp_aout')


def test_button_rehearsal_completes_four_profiles_without_supply_writes(tmp_path):
    dispatcher, backend, _, records = setup(tmp_path)
    triggers = [a['trigger'] for a in dispatcher.config.raw['assignments']]
    backend.events = load_scenario(ROOT / 'scenarios/ces_motor_candidates.json', {}, triggers)
    assert dispatcher.run() == 0
    completed = [r for r in records if r['event'] == 'pattern_cycle_complete']
    assert [r['assignment'] for r in completed] == list(EXPECTED)
    assert dispatcher.completed == 4
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[BRAKE].value == 0
    assert not any(role == SUPPLY for _, role, _ in backend.commands)


def test_disputed_physical_neutral_acknowledgement_blocks_cli_before_connection(tmp_path, capsys):
    raw = json.loads((ROOT / 'examples/physical-dual.json').read_text())
    raw['outputs'][MOTOR]['neutral_verified'] = False
    path = tmp_path / 'blocked-physical.json'
    path.write_text(json.dumps(raw))
    assert main(['dispatch-patterns', '--bindings', str(path)]) == 2
    assert 'verified 0 V neutral' in capsys.readouterr().err
