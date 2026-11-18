"""Offline CES demand stepping; never creates a hardware transport."""

import json
from pathlib import Path

import pytest

from oxygen_engine.dispatcher import Dispatcher, MOTOR, SUPPLY
from oxygen_engine.errors import ConfigError, ControlError, InterlockError
from oxygen_engine.memory_backend import MemoryBackend, SimulationFinished
from oxygen_engine.schema import SUPPLY_DEMAND_GRID, load_dispatcher_config
from oxygen_engine.scpi_transport import ScpiCommandError, ScpiErrorEntry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples/ces-eight-button.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def raw_config():
    raw = json.loads(EXAMPLE.read_text())
    for assignment in raw['assignments']:
        assignment['profile'] = str((EXAMPLE.parent / assignment['profile']).resolve())
    return raw


def load(tmp_path, raw):
    path = tmp_path / 'ces.json'
    path.write_text(json.dumps(raw))
    return load_dispatcher_config(path)


def make(tmp_path, start=.475, raw=None):
    raw = raw_config() if raw is None else raw
    raw['simulation']['outputs']['supply_demand_fixture']['initial_v'] = start
    config = load(tmp_path, raw)
    clock, records = Clock(), []
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    backend.connect()
    dispatcher.prepare()
    return dispatcher, backend, clock, records


@pytest.mark.parametrize('start', SUPPLY_DEMAND_GRID)
@pytest.mark.parametrize('direction,delta', [('up', 1), ('down', -1)])
def test_every_grid_point_and_boundary(tmp_path, start, direction, delta):
    dispatcher, backend, _, records = make(tmp_path, start)
    index = SUPPLY_DEMAND_GRID.index(start)
    next_index = index + delta
    boundary = not 0 <= next_index < 9
    target = SUPPLY_DEMAND_GRID[max(0, min(8, next_index))]
    dispatcher.execute_profile('rzxa_voltage_' + direction)
    assert dispatcher.states[SUPPLY].value == target
    assert dispatcher.states[SUPPLY].last_confirmed_v == target
    assert dispatcher.states[SUPPLY].held_by == 'rzxa_voltage_' + direction
    assert backend.writes_attempted == (0 if boundary else 1)
    if boundary:
        event = next(r for r in records if r['event'] == 'relative_step_boundary_noop')
        assert event['hardware_write'] is False
    else:
        assert list(backend.commands) == [('write', SUPPLY, target)]
        assert target - start == pytest.approx(delta * .00625, abs=1e-14)
    assert records[-1]['outputs'][SUPPLY]['physical_voltage_established'] is False


def test_repeated_up_down_runtime_completion(tmp_path):
    dispatcher, backend, _, _ = make(tmp_path, .5)
    for direction in ['down'] * 8 + ['up'] * 8:
        dispatcher.execute_profile('rzxa_voltage_' + direction)
    assert dispatcher.completed == 16
    assert dispatcher.states[SUPPLY].value == .5
    assert backend.writes_attempted == 16


@pytest.mark.parametrize('start', [0, .2, .449, .451, .468, .499])
def test_off_grid_start_no_entry(tmp_path, start):
    dispatcher, backend, _, _ = make(tmp_path, start)
    with pytest.raises(InterlockError, match='grid starting point'):
        dispatcher.execute_profile('rzxa_voltage_up')
    assert backend.writes_attempted == 0


@pytest.mark.parametrize('readback', [None, float('nan'), float('inf'), 'NONE', True, -.01, .51, .48])
def test_invalid_or_unexpected_readback_blocks_and_latches(tmp_path, monkeypatch, readback):
    dispatcher, backend, _, records = make(tmp_path)
    original = backend.read_output
    monkeypatch.setattr(backend, 'read_output', lambda key: readback if key == 'supply_demand_fixture' else original(key))
    with pytest.raises(Exception):
        dispatcher.execute_profile('rzxa_voltage_up')
    assert dispatcher.states[SUPPLY].status == 'unknown'
    assert backend.writes_attempted == 0
    assert any(r['event'] == 'output_verification_failed' for r in records)
    monkeypatch.setattr(backend, 'read_output', original)
    with pytest.raises(ControlError, match='explicit restart'):
        dispatcher.execute_profile('rzxa_voltage_down')


@pytest.mark.parametrize('mode', ['-200', 'missing', 'mismatch'])
def test_failed_transition_never_retries_or_continues(tmp_path, monkeypatch, mode):
    dispatcher, backend, _, records = make(tmp_path, .4625)
    original_write = backend.write
    original_read = backend.read_output

    def write(role, value):
        original_write(role, value)
        if mode == '-200':
            raise ScpiCommandError('constant-property write', ScpiErrorEntry(-200, 'Execution error'))

    def read(key):
        if backend.writes_attempted and key == 'supply_demand_fixture':
            return None if mode == 'missing' else .46
        return original_read(key)

    monkeypatch.setattr(backend, 'write', write)
    if mode != '-200':
        monkeypatch.setattr(backend, 'read_output', read)
    with pytest.raises(Exception):
        dispatcher.execute_profile('rzxa_voltage_up')
    assert list(backend.commands) == [('write', SUPPLY, .46875)]
    state = dispatcher.states[SUPPLY]
    assert state.status == 'unknown'
    assert state.last_attempted_v == .46875
    assert state.last_confirmed_v == .4625
    failure = next(r for r in records if r['event'] == 'output_command_failed')
    assert failure['retry_attempted'] is False
    assert failure['physical_voltage_established'] is False
    assert dispatcher.completed == 0
    with pytest.raises(ControlError, match='explicit restart'):
        dispatcher.execute_profile('rzxa_voltage_down')
    assert backend.writes_attempted == 1
    json.dumps(records, allow_nan=False)


@pytest.mark.parametrize('direction', ['up', 'down'])
def test_motor_neutral_always_required_even_if_legacy_guard_disabled(tmp_path, direction):
    raw = raw_config()
    raw['interlocks']['supply_down_requires_motor_neutral'] = False
    dispatcher, backend, _, _ = make(tmp_path, raw=raw)
    backend.outputs['motor_throttle_fixture']['value'] = .1
    with pytest.raises(ControlError, match='read back'):
        dispatcher.execute_profile('rzxa_voltage_' + direction)
    assert backend.writes_attempted == 0


@pytest.mark.parametrize('start', SUPPLY_DEMAND_GRID)
@pytest.mark.parametrize('profile', ['motor_gentle', 'regen_test', 'driving_cycle', 'efficiency_scan'])
def test_motor_profiles_authorized_only_at_verified_discrete_demands(tmp_path, start, profile):
    dispatcher, backend, _, _ = make(tmp_path, start)
    dispatcher.execute_profile(profile)
    assert dispatcher.completed == 1
    assert dispatcher.states[MOTOR].value == 0
    assert all(value <= .4 for _, role, value in backend.commands if role == MOTOR)


@pytest.mark.parametrize('start', [0, .2, .449, .451, .499])
def test_motor_rejects_outside_discrete_grid(tmp_path, start):
    dispatcher, backend, _, _ = make(tmp_path, start)
    with pytest.raises(InterlockError, match='prerequisite'):
        dispatcher.execute_profile('motor_gentle')
    assert backend.writes_attempted == 0


@pytest.mark.parametrize('demands', [[], [.45, .45], [.451], [.50625], [True], ['0.5']])
def test_invalid_commissioned_set(tmp_path, demands):
    raw = raw_config()
    raw['interlocks']['motor_supply_demands_v'] = demands
    with pytest.raises(ConfigError):
        load(tmp_path, raw)


def test_scalar_and_set_cannot_be_combined(tmp_path):
    raw = raw_config()
    raw['interlocks']['motor_supply_demand_v'] = .5
    with pytest.raises(ConfigError, match='not both'):
        load(tmp_path, raw)


def test_uncommissioned_grid_point_is_not_a_motor_prerequisite(tmp_path):
    raw = raw_config()
    raw['interlocks']['motor_supply_demands_v'] = [.45, .5]
    dispatcher, backend, _, _ = make(tmp_path, .475, raw)
    with pytest.raises(InterlockError, match='prerequisite'):
        dispatcher.execute_profile('motor_gentle')
    assert backend.writes_attempted == 0


@pytest.mark.parametrize('bad_read_number', [2, 4])
def test_start_recheck_and_runtime_completion_reject_changed_readback(tmp_path, monkeypatch, bad_read_number):
    dispatcher, backend, _, records = make(tmp_path)
    original = backend.read_output
    reads = 0

    def read(key):
        nonlocal reads
        if key == 'supply_demand_fixture':
            reads += 1
            if reads == bad_read_number:
                return .46
        return original(key)

    monkeypatch.setattr(backend, 'read_output', read)
    with pytest.raises(ControlError, match='read back'):
        dispatcher.execute_profile('rzxa_voltage_up')
    assert backend.writes_attempted == (0 if bad_read_number == 2 else 1)
    assert dispatcher.completed == 0
    assert dispatcher.states[SUPPLY].status == 'unknown'
    assert any(r['event'] == 'profile_interrupted_state' for r in records)


def test_preconditions_that_age_out_edge_do_not_write(tmp_path, monkeypatch):
    dispatcher, backend, clock, records = make(tmp_path)
    original = backend.read_output

    def read(key):
        clock.wait(.15)
        return original(key)

    monkeypatch.setattr(backend, 'read_output', read)
    dispatcher.execute_profile('rzxa_voltage_up', trigger_observed=clock())
    assert backend.writes_attempted == 0
    assert dispatcher.started == 0
    assert any(r['event'] == 'trigger_stale' for r in records)


@pytest.mark.parametrize('mutation', ['mixed', 'motor', 'completion', 'direction', 'gain', 'range', 'neutral', 'timeout'])
def test_strict_relative_profile(tmp_path, mutation):
    raw = raw_config()
    profile = json.loads((ROOT / 'profiles/rzxa_voltage_up.json').read_text())
    if mutation == 'mixed':
        profile['steps'].append({'type': 'hold', 'params': {'duration_s': 1}})
    elif mutation == 'motor':
        profile['output'] = MOTOR
        raw['assignments'][-1]['output'] = MOTOR
    elif mutation == 'completion':
        profile['completion']['value'] = .5
    elif mutation == 'direction':
        profile['steps'][0]['params']['direction'] = 'sideways'
    elif mutation == 'gain':
        raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] = 81
    elif mutation == 'range':
        raw['outputs'][SUPPLY]['max_aout_v'] = .6
    elif mutation == 'timeout':
        profile['timeout_s'] = 0
    else:
        profile['requires'] = {}
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(profile))
    raw['assignments'][-1]['profile'] = str(path)
    with pytest.raises(ConfigError):
        load(tmp_path, raw)


@pytest.mark.parametrize('feature', ['relative', 'discrete'])
def test_physical_extension_requires_separate_commissioning_gate(tmp_path, feature):
    raw = json.loads((ROOT / 'examples/physical-dual.json').read_text())
    for assignment in raw['assignments']:
        assignment['profile'] = str((ROOT / 'examples' / assignment['profile']).resolve())
    if feature == 'relative':
        raw['assignments'][-1] = raw_config()['assignments'][-1]
    else:
        del raw['interlocks']['motor_supply_demand_v']
        raw['interlocks']['motor_supply_demands_v'] = list(SUPPLY_DEMAND_GRID)
    with pytest.raises(ConfigError, match='supply_demand_grid_commissioned'):
        load(tmp_path, raw)
    raw['interlocks']['supply_demand_grid_commissioned'] = True
    assert load(tmp_path, raw).physical


@pytest.mark.parametrize('active,requested', [('motor_gentle', 'rzxa_voltage_up'), ('rzxa_voltage_down', 'motor_gentle')])
def test_global_ownership_no_concurrent_motor_supply(tmp_path, monkeypatch, active, requested):
    dispatcher, backend, _, _ = make(tmp_path)
    original = backend.write
    reentries = []

    def write(role, value):
        with pytest.raises(ControlError, match='owns an output'):
            dispatcher.execute_profile(requested)
        reentries.append(requested)
        original(role, value)

    monkeypatch.setattr(backend, 'write', write)
    dispatcher.execute_profile(active)
    assert reentries
    assert dispatcher.completed == 1


@pytest.mark.parametrize('button', range(8))
def test_eight_buttons_with_15ms_scpi_read_latency_rise_hold_and_rearm(tmp_path, monkeypatch, button):
    dispatcher, backend, clock, records = make(tmp_path)
    assignments = dispatcher.config.raw['assignments']
    calls = []
    original_read = backend.read_output

    def read_output(key):
        clock.wait(.015)
        return original_read(key)

    def read_button(name):
        cycle, offset = divmod(len(calls), 8)
        if cycle == 6:
            raise SimulationFinished()
        assert name == assignments[offset]['trigger']
        calls.append((name, clock()))
        clock.wait(.015)
        return offset == button and cycle in (1, 2, 3, 5)

    monkeypatch.setattr(backend, 'read_output', read_output)
    monkeypatch.setattr(backend, 'read_button', read_button)
    with pytest.raises(SimulationFinished):
        dispatcher.monitor()
    assert dispatcher.completed == 2
    completed = [r for r in records if r['event'] == 'pattern_cycle_complete']
    assert all(r['assignment'] == assignments[button]['name'] for r in completed)
    assert not any(r['event'] in ('trigger_stale', 'trigger_observation_discarded') for r in records)
    assert len(calls) == 48


@pytest.mark.parametrize('latency', [.03, .3])
def test_eight_button_excess_latency_discards_edges_without_relaxing_timeout(tmp_path, monkeypatch, latency):
    dispatcher, backend, clock, records = make(tmp_path)
    calls = []

    def read(name):
        cycle = len(calls) // 8
        if cycle == 4:
            raise SimulationFinished()
        calls.append(name)
        clock.wait(latency)
        return cycle > 0

    monkeypatch.setattr(backend, 'read_button', read)
    with pytest.raises(SimulationFinished):
        dispatcher.monitor()
    assert dispatcher.completed == 0
    assert backend.writes_attempted == 0
    assert any(r['event'] == 'trigger_observation_discarded' for r in records)
    assert dispatcher.config.raw['timing']['max_trigger_gap_ms'] == 250


def test_ninth_assignment_rejected_and_existing_six_preserved(tmp_path):
    raw = raw_config()
    raw['assignments'].append(dict(raw['assignments'][0], name='ninth', trigger='Operator ninth'))
    with pytest.raises(ConfigError, match='1..8'):
        load(tmp_path, raw)
    legacy = load_dispatcher_config(ROOT / 'dispatcher.example.json')
    assert len(legacy.profiles) == 6
    assert legacy.raw['interlocks']['motor_supply_demand_v'] == .5
