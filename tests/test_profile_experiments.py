"""V3 experiment library and operator diagnostics; memory transport only."""

import json
from pathlib import Path

import pytest

from oxygen_engine.dispatcher import Dispatcher, MOTOR, SUPPLY, configuration_summary
from oxygen_engine.errors import ControlError, InterlockError
from oxygen_engine.memory_backend import MemoryBackend, SimulationFinished
from oxygen_engine.schema import load_dispatcher_config

ROOT = Path(__file__).resolve().parents[1]
PROFILES = sorted(p for p in (ROOT / 'profiles').glob('*.json')
                  if json.loads(p.read_text(encoding='utf-8-sig')).get('schema_version') == 3)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: pytest.fail('Memory tests must not connect'))


def memory_raw():
    raw = json.loads((ROOT / 'dispatcher.example.json').read_text(encoding='utf-8-sig'))
    for assignment in raw['assignments']:
        assignment['profile'] = str(ROOT / assignment['profile'])
    return raw


def setup_dispatcher(tmp_path, raw, *, events=()):
    path = tmp_path / 'memory.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock, records = Clock(), []
    backend = MemoryBackend(config, clock=clock, events=events)
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    return dispatcher, backend, clock, records


@pytest.mark.parametrize('profile_path', PROFILES, ids=lambda p: p.stem)
@pytest.mark.parametrize('initial_supply', [0.0, 0.2, 0.5])
def test_v3_library_real_timeline_repeatable_in_memory(tmp_path, profile_path, initial_supply):
    raw = memory_raw()
    profile = json.loads(profile_path.read_text())
    role = profile['output']
    initial = .5 if role == MOTOR else initial_supply
    raw['simulation']['outputs'][raw['outputs'][SUPPLY]['binding']]['initial_v'] = initial
    raw['assignments'] = [{'name': 'experiment', 'enabled': True, 'trigger': 'Memory experiment',
                           'output': role, 'profile': str(profile_path)}]
    dispatcher, backend, clock, records = setup_dispatcher(tmp_path, raw)
    backend.connect()
    dispatcher.prepare()
    other = MOTOR if role == SUPPLY else SUPPLY
    other_before = dispatcher.states[other].value
    expected_checkpoints = [s['params']['name'] for s in profile['steps'] if s['type'] == 'checkpoint']
    for _ in range(2):
        before = len(records)
        dispatcher.execute_profile('experiment')
        assert dispatcher.states[role].value == profile['completion']['value']
        assert dispatcher.states[other].value == other_before
        assert [r['name'] for r in records[before:] if r['event'] == 'checkpoint'] == expected_checkpoints
        assert clock.now > 0
    spec = raw['outputs'][role]
    assert backend.commands
    assert all(r == role and spec['min_aout_v'] <= value <= spec['max_aout_v']
               for _, r, value in backend.commands)
    assert dispatcher.completed == 2
    assert dispatcher.cleanup()
    assert dispatcher.states[MOTOR].value == 0
    assert not any(r['event'] == 'independent_feedback' for r in records)
    backend.close()


def test_disabled_assignments_are_reported_but_not_polled(tmp_path):
    raw = memory_raw()
    disabled = {'rzxa_steps', 'motor_stop_go', 'motor_cruise'}
    for assignment in raw['assignments']:
        assignment['enabled'] = assignment['name'] not in disabled
    dispatcher, backend, _, records = setup_dispatcher(tmp_path, raw)
    summary = configuration_summary(dispatcher.config)
    assert {a['name'] for a in summary['disabled_assignments']} == disabled
    assert all('not polled' in a['reason'] for a in summary['disabled_assignments'])
    assert {a['name'] for a in summary['assignments']} == {'rzxa_to_40_hold', 'rzxa_down', 'motor_gentle'}
    assert len(backend.buttons) == 3
    assert summary['output_mappings'][MOTOR]['max_aout_v'] == .4
    assert summary['output_mappings'][SUPPLY]['max_aout_v'] == .5
    backend.connect()
    dispatcher.prepare()
    startup = next(r for r in records if r['event'] == 'dispatcher_startup_summary')
    assert startup['dispatcher_state'] == 'ready'
    assert set(startup['resolved_outputs']) == {SUPPLY, MOTOR}
    backend.close()


@pytest.mark.parametrize('name,template,filename', [
    ('rzxa_steps', 'rzxa_steps', 'rzxa_steps.json'),
    ('motor_stop_go', 'motor_stop_go', 'motor_stop_go.json'),
    ('motor_cruise', 'motor_cruise', 'motor_cruise.json'),
    ('rzxa_char_ladder', 'rzxa_steps', 'rzxa_36_38_40_ladder.json'),
    ('motor_coastdown_comparison', 'motor_stop_go', 'motor_coastdown_comparison.json'),
    ('motor_three_level', 'motor_cruise', 'motor_three_level.json'),
])
def test_enabled_characterization_assignment_responds(tmp_path, name, template, filename):
    raw = memory_raw()
    assignment = next(a for a in raw['assignments'] if a['name'] == template)
    assignment.update(name=name, profile=str(ROOT / 'profiles' / filename))
    raw['assignments'] = [assignment]
    raw['simulation']['outputs'][raw['outputs'][SUPPLY]['binding']]['initial_v'] = .5
    duration = sum(s.get('params', {}).get('duration_s', 0)
                   for s in json.loads(Path(assignment['profile']).read_text())['steps'])
    events = [{'at_s': .1, 'buttons': {assignment['trigger']: True}},
              {'at_s': .2, 'buttons': {assignment['trigger']: False}},
              {'at_s': duration + 1, 'stop': True}]
    dispatcher, _, _, records = setup_dispatcher(tmp_path, raw, events=events)
    assert dispatcher.run() == 0
    assert dispatcher.completed == 1
    assert any(r['event'] == 'profile_started' and r['assignment'] == name for r in records)


def test_failed_precondition_names_assignment_and_never_starts(tmp_path):
    raw = memory_raw()
    dispatcher, backend, _, records = setup_dispatcher(tmp_path, raw)
    backend.connect()
    dispatcher.prepare()
    with pytest.raises(InterlockError, match='Supply demand readback'):
        dispatcher.execute_profile('motor_gentle')
    failure = next(r for r in records if r['event'] == 'profile_precondition_failed')
    assert failure['assignment'] == 'motor_gentle'
    assert failure['profile_started'] is False
    assert dispatcher.started == 0 and not backend.commands
    backend.close()


def test_non_neutral_startup_still_refuses_authorization(tmp_path):
    raw = memory_raw()
    raw['simulation']['outputs'][raw['outputs'][MOTOR]['binding']]['initial_v'] = .1
    dispatcher, backend, _, _ = setup_dispatcher(tmp_path, raw)
    backend.connect()
    with pytest.raises(ControlError, match='Startup motor is not neutral'):
        dispatcher.prepare()
    assert not backend.capabilities and not backend.commands
    backend.close()


@pytest.mark.parametrize('kind', ['slow_read', 'sample_gap', 'baseline_high'])
def test_resync_and_high_baseline_have_clear_diagnostics(tmp_path, kind):
    raw = memory_raw()
    raw['assignments'] = raw['assignments'][:1]
    dispatcher, backend, clock, records = setup_dispatcher(tmp_path, raw)
    backend.connect()
    dispatcher.prepare()
    sequence = iter({'slow_read': [(False, .3), (True, 0)],
                     'sample_gap': [(False, 0), (True, .24)],
                     'baseline_high': [(True, 0), (True, 0)]}[kind])

    def read_button(name):
        try:
            value, delay = next(sequence)
        except StopIteration:
            raise SimulationFinished()
        clock.wait(delay)
        return value

    backend.read_button = read_button
    with pytest.raises(SimulationFinished):
        dispatcher.monitor()
    assert dispatcher.started == 0
    expected = 'trigger_not_armed' if kind == 'baseline_high' else 'trigger_observation_discarded'
    assert any(r['event'] == expected for r in records)
    backend.close()


def test_later_do_read_can_age_out_an_edge_with_diagnostic(tmp_path):
    raw = memory_raw()
    raw['assignments'] = raw['assignments'][:2]
    dispatcher, backend, clock, records = setup_dispatcher(tmp_path, raw)
    backend.connect()
    dispatcher.prepare()
    sequence = iter([(False, 0), (False, 0), (True, 0), (False, .3)])

    def read_button(name):
        try:
            value, delay = next(sequence)
        except StopIteration:
            raise SimulationFinished()
        clock.wait(delay)
        return value

    backend.read_button = read_button
    with pytest.raises(SimulationFinished):
        dispatcher.monitor()
    assert any(r['event'] == 'trigger_stale' and r['assignment'] == 'rzxa_to_40_hold' for r in records)
    assert dispatcher.started == 0
    backend.close()


def test_busy_pulse_is_unobservable_and_not_queued(tmp_path):
    raw = memory_raw()
    raw['assignments'] = [raw['assignments'][0], raw['assignments'][3]]
    events = [{'at_s': .1, 'buttons': {'DO 3/1 Sim': True}},
              {'at_s': .2, 'buttons': {'DO 3/1 Sim': False}},
              {'at_s': .5, 'buttons': {'DO 3/4 Sim': True}},
              {'at_s': .8, 'buttons': {'DO 3/4 Sim': False}},
              {'at_s': 9.5, 'stop': True}]
    dispatcher, _, _, records = setup_dispatcher(tmp_path, raw, events=events)
    assert dispatcher.run() == 0
    assert dispatcher.completed == 1
    assert [r['assignment'] for r in records if r['event'] == 'profile_started'] == ['rzxa_to_40_hold']
    assert any(r['event'] == 'profile_started' and r['button_polling'] == 'paused' for r in records)
    assert not any(r['event'] == 'trigger_state' and r['assignment'] == 'motor_gentle'
                   and r['edge'] == 'rising' for r in records)
