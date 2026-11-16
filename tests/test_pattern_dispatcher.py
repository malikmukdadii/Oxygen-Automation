from __future__ import annotations

import importlib
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from oxygen_engine.adapters.mock import MockChannel, MockOxygenServer
from oxygen_engine.constant_pattern import APPROVED_CHANNEL, CONSTANT_PROPERTY, SimulatedConstantTransport, execute_timeline
from oxygen_engine.main import main
from oxygen_engine.pattern_dispatcher import (
    PatternDispatcher, StrictRotatingHandler, load_dispatcher_config, load_output_pattern,
)
from oxygen_engine.profiles import ProfileError
from oxygen_engine.scpi_transport import ScpiProtocolError

ROOT = Path(__file__).resolve().parents[1]
CID = '8898268430164033617'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def example_raw():
    raw = json.loads((ROOT / 'dispatcher.example.json').read_text())
    for assignment in raw['assignments']:
        assignment['profile'] = str(ROOT / assignment['profile'])
    return raw


def write_config(tmp_path, raw):
    path = tmp_path / 'dispatcher.json'
    path.write_text(json.dumps(raw))
    return path


def test_examples_use_relative_paths_and_expected_shapes():
    config = load_dispatcher_config(ROOT / 'dispatcher.example.json')
    assert [a.pattern.duration_s for a in config.assignments] == [10, 6, 8]
    gentle = config.assignments[1].pattern
    assert [gentle.value_at(t) for t in (0, 1, 2, 4, 5, 6)] == [0, .25, .5, .5, .25, 0]
    double = config.assignments[2].pattern
    assert [double.value_at(t) for t in range(9)] == [0, .75, .75, 0, 0, .75, .75, 0, 0]
    clock, records, writes = Clock(), [], []
    result = execute_timeline(double, lambda v: writes.append((clock.now, v)), lambda v: None,
                              records.append, interval_s=.07, clock=clock, wait=clock.wait)
    assert result['actual_elapsed_s'] == pytest.approx(8)
    assert writes[-1][1] == 0
    assert all(0 <= value <= .75 for _, value in writes)
    phases = {r['phase'] for r in records}
    assert all(segment.phase in phases for segment in double.segments)


def test_fractional_deadlines_with_large_clock_origin_finish_once(tmp_path):
    raw = json.loads((ROOT / 'profiles/sim_aout_gentle_6s.json').read_text())
    for step in raw['steps'][1:-1]:
        step['params']['duration_s'] = .1
    path = tmp_path / 'fractional.json'
    path.write_text(json.dumps(raw))
    pattern = load_output_pattern(path, 'sim_aout')
    clock, records, writes = Clock(), [], []
    clock.now = 343966.17
    calls = 0

    def wait(seconds):
        nonlocal calls
        calls += 1
        assert calls < 50, 'Reached deadlines must not repeat forever'
        clock.wait(seconds)

    result = execute_timeline(pattern, writes.append, lambda v: None, records.append,
                              clock=clock, wait=wait, interval_s=.07)
    assert result['actual_elapsed_s'] == pytest.approx(.3)
    assert writes[-1] == 0
    assert records[-1]['phase'] == 'complete'


@pytest.mark.parametrize('bad', [
    {'connection': {'port': True}},
    {'connection': {'timeout_s': float('nan')}},
    {'timing': {'interval_ms': 1}},
    {'timing': {'poll_ms': 50, 'max_trigger_gap_ms': 50}},
    {'timing': {'max_lateness_ms': float('inf')}},
    {'unknown': True},
    {'outputs': {'sim_aout': {'channel': 'AO 3/2 Sim', 'expected_id': CID}}},
    {'outputs': {'sim_aout': {'channel': APPROVED_CHANNEL, 'expected_id': int(CID)}}},
])
def test_bad_configuration_fails_offline(tmp_path, bad):
    raw = example_raw()
    raw.update(bad)
    with pytest.raises((ValueError, ScpiProtocolError)):
        load_dispatcher_config(write_config(tmp_path, raw))


@pytest.mark.parametrize('failure', ['duplicate_name', 'duplicate_trigger', 'wrong_output', 'wrong_trigger', 'enabled_string', 'all_disabled'])
def test_assignment_validation(tmp_path, failure):
    raw = example_raw()
    if failure == 'duplicate_name':
        raw['assignments'][1]['name'] = raw['assignments'][0]['name']
    elif failure == 'duplicate_trigger':
        raw['assignments'][1]['trigger'] = raw['assignments'][0]['trigger']
    elif failure == 'wrong_output':
        raw['assignments'][0]['output'] = 'another_output'
    elif failure == 'wrong_trigger':
        raw['assignments'][0]['trigger'] = 'DO 3/1 Physical'
    elif failure == 'enabled_string':
        raw['assignments'][0]['enabled'] = 'false'
    else:
        for assignment in raw['assignments']:
            assignment['enabled'] = False
    with pytest.raises(ValueError):
        load_dispatcher_config(write_config(tmp_path, raw))


@pytest.mark.parametrize('failure', ['recording', 'conditions', 'instrument', 'nonzero_start', 'nonzero_end',
                                      'overshoot', 'negative', 'nan', 'short', 'long', 'timeout',
                                      'wrong_channel', 'rate', 'extra_param', 'missing_duration'])
def test_unsafe_or_unsupported_profiles_are_rejected(tmp_path, failure):
    raw = json.loads((ROOT / 'profiles/sim_aout_gentle_6s.json').read_text())
    if failure == 'recording':
        raw['recording'] = {'start': True}
    elif failure == 'conditions':
        raw['expected_conditions'] = {'voltage': 1}
    elif failure == 'instrument':
        raw['steps'][2] = {'type': 'instrument_op', 'params': {}}
    elif failure == 'nonzero_start':
        raw['steps'][0]['params']['value'] = .5
    elif failure == 'nonzero_end':
        raw['steps'][-2]['params']['target'] = .2
    elif failure in {'overshoot', 'negative', 'nan'}:
        raw['steps'][1]['params']['target'] = {'overshoot': 2, 'negative': -1, 'nan': float('nan')}[failure]
    elif failure in {'short', 'long'}:
        raw['steps'][1]['params']['duration_s'] = .01 if failure == 'short' else 301
    elif failure == 'timeout':
        raw['timeout_s'] = 1
    elif failure == 'wrong_channel':
        raw['steps'][1]['params']['channel'] = 'rzxa'
    elif failure == 'rate':
        raw['steps'][1]['params']['rate_per_s'] = 5
    elif failure == 'extra_param':
        raw['steps'][2]['params']['start_recording'] = True
    else:
        del raw['steps'][1]['params']['duration_s']
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(raw))
    with pytest.raises((ValueError, KeyError, ProfileError)):
        load_output_pattern(path, 'sim_aout')


@pytest.fixture(params=[False, True], ids=['header-off', 'header-on'])
def backend(request):
    server = MockOxygenServer(response_headers=request.param)
    channel = MockChannel(CID, APPROVED_CHANNEL, 'aout', {
        'Mode': 'ConstOutput', 'Range': '10', 'Unit': 'V',
        'TRION/SourceChannel_A': 'CONST0', CONSTANT_PROPERTY: '0.0',
    })
    server._channels[CID] = channel
    server._by_name[APPROVED_CHANNEL] = CID
    server.start()
    try:
        yield server, channel
    finally:
        server.stop()


def dispatcher_for(server, emit, **kwargs):
    config = replace(load_dispatcher_config(ROOT / 'dispatcher.example.json'), port=server.bound_port)
    transport = SimulatedConstantTransport(host=config.host, port=config.port, timeout_s=.1,
                                           channel=APPROVED_CHANNEL, expected_id=CID,
                                           confirmed_simulated=True, emit=emit)
    return PatternDispatcher(config, transport, emit, **kwargs)


def test_tcp_dispatches_profiles_serially_discards_busy_and_simultaneous_presses(backend):
    server, channel = backend
    records, commands, durations = [], [], []
    server._on_command = commands.append
    clock = Clock()
    buttons = [server._channels[server._by_name[f'DO 3/{i} Sim']] for i in range(1, 4)]
    waits = 0

    def wait(seconds):
        nonlocal waits
        clock.wait(seconds)
        waits += 1
        if waits == 1:
            buttons[0].properties['DO_Value'] = '1'
        elif waits == 3 or waits == 5:
            for button in buttons:
                button.properties['DO_Value'] = '0'
        elif waits == 4:
            buttons[1].properties['DO_Value'] = buttons[2].properties['DO_Value'] = '1'
        elif waits == 6:
            buttons[2].properties['DO_Value'] = '1'
        elif waits == 7:
            raise KeyboardInterrupt

    def emit(record):
        records.append(record)
        if record['event'] == 'pattern_cycle_complete' and record['cycle'] == 1:
            buttons[1].properties['DO_Value'] = '1'

    def timeline(pattern, *args, **kwargs):
        durations.append(pattern.duration_s)
        return execute_timeline(pattern, *args, clock=clock, wait=clock.wait, **kwargs)

    dispatcher = dispatcher_for(server, emit, clock=clock, wait=wait, timeline=timeline)
    code = dispatcher.run()
    assert code == 130
    assert durations == [10, 6, 8]
    assert dispatcher.completed == 3
    assert [r['assignment'] for r in records if r['event'] == 'pattern_cycle_started'] == ['ramp_10s', 'gentle_6s', 'double_pulse_8s']
    assert any(r['event'] == 'trigger_ignored' and r['assignment'] == 'double_pulse_8s' for r in records)
    assert records[-1]['final_state'] == 'configured_zero_verified'
    assert float(channel.properties[CONSTANT_PROPERTY]) == 0
    writes = [c for c in commands if '?' not in c.split(None, 1)[0]]
    assert writes and all(c.startswith(f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}"') for c in writes)
    assert not dispatcher.transport.connected


@pytest.mark.parametrize('failure', ['missing_do', 'missing_property', 'wrong_output_id'])
def test_tcp_preflight_failure_writes_nothing(backend, failure):
    server, _ = backend
    if failure == 'missing_do':
        del server._channels[server._by_name['DO 3/3 Sim']]
    elif failure == 'missing_property':
        del server._channels[server._by_name['DO 3/2 Sim']].properties['DO_Value']
    records = []
    dispatcher = dispatcher_for(server, records.append)
    if failure == 'wrong_output_id':
        dispatcher.transport.expected_id = '1'
    assert dispatcher.run() == 1
    summary = next(r for r in records if r['event'] == 'pattern_summary')
    assert summary['writes_attempted'] == 0
    assert summary['final_state'] == 'not_attempted_no_authorization'


@pytest.mark.parametrize('disconnect', [False, True])
def test_tcp_fault_stops_without_resume_and_reports_cleanup_honestly(backend, disconnect):
    server, channel = backend
    records = []
    clock = Clock()
    button = server._channels[server._by_name['DO 3/1 Sim']]

    def wait(seconds):
        clock.wait(seconds)
        button.properties['DO_Value'] = '1'

    def timeline(pattern, setpoint, *args, **kwargs):
        setpoint(.5)
        if disconnect:
            dispatcher.transport.close()
        raise TimeoutError('No automatic resume')

    dispatcher = dispatcher_for(server, records.append, clock=clock, wait=wait, timeline=timeline)
    assert dispatcher.run() == 1
    assert dispatcher.started == 1 and dispatcher.completed == 0
    assert records[-1]['final_state'] == ('unknown' if disconnect else 'configured_zero_verified')
    assert float(channel.properties[CONSTANT_PROPERTY]) == (.5 if disconnect else 0)


def test_preview_and_partial_execution_never_create_transport(monkeypatch, tmp_path, capsys):
    module = importlib.import_module('oxygen_engine.pattern_dispatcher')
    main_module = importlib.import_module('oxygen_engine.main')
    monkeypatch.setattr(module, 'SimulatedConstantTransport', lambda **kw: pytest.fail('Must stay offline'))
    monkeypatch.setattr(main_module, '_make_engine', lambda args: pytest.fail('No legacy engine'))
    assert main(['dispatch-patterns']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['connected'] is False and len(plan['assignments']) == 3
    assert main(['dispatch-patterns', '--execute']) == 1
    assert 'requires' in capsys.readouterr().err
    assert main(['dispatch-patterns', '--live']) == 1


def test_rotating_logs_are_bounded_and_valid_json(tmp_path):
    path = tmp_path / 'events.jsonl'
    handler = StrictRotatingHandler(path, maxBytes=150, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    try:
        for index in range(30):
            handler.emit(logging.LogRecord('test', logging.INFO, '', 0, json.dumps({'index': index}), (), None))
    finally:
        handler.close()
    files = list(tmp_path.iterdir())
    assert len(files) == 3
    for file in files:
        assert file.stat().st_size <= 150
        assert all(isinstance(json.loads(line)['index'], int) for line in file.read_text().splitlines())


def test_slow_context_verification_does_not_dispatch_an_expired_edge():
    config = load_dispatcher_config(ROOT / 'dispatcher.example.json')
    clock, records = Clock(), []
    state = False
    waits = 0

    def wait(seconds):
        nonlocal state, waits
        clock.wait(seconds)
        waits += 1
        state = True
        if waits == 2:
            raise KeyboardInterrupt

    dispatcher = PatternDispatcher(config, None, records.append, clock=clock, wait=wait,
                                    timeline=lambda *a, **k: pytest.fail('Expired trigger must not dispatch'))
    dispatcher.trigger_ids = {a.name: a.name for a in config.assignments}
    dispatcher.read = lambda cid: state
    with pytest.raises(KeyboardInterrupt):
        dispatcher.monitor(None, lambda v: None, lambda v: clock.wait(.3), records.append)
    assert dispatcher.started == 0
    assert any(r['event'] == 'trigger_stale' for r in records)


def test_log_failure_stops_execution_without_preventing_zero_cleanup(backend, monkeypatch, tmp_path, capsys):
    server, channel = backend
    raw = example_raw()
    raw['connection']['port'] = server.bound_port
    path = write_config(tmp_path, raw)
    original = StrictRotatingHandler.emit

    def broken_emit(self, record):
        if json.loads(record.msg)['event'] == 'injected_log_failure':
            raise OSError('Disk full')
        original(self, record)

    def monitor(self, pattern, setpoint, verify, emit, **kwargs):
        setpoint(.5)
        emit({'event': 'injected_log_failure'})
        pytest.fail('Log failure must abort')

    monkeypatch.setattr(StrictRotatingHandler, 'emit', broken_emit)
    monkeypatch.setattr(PatternDispatcher, 'monitor', monitor)
    assert main(['dispatch-patterns', '--bindings', str(path), '--execute', '--confirm-simulated-output',
                 '--log-file', str(tmp_path / 'events.jsonl')]) == 1
    assert float(channel.properties[CONSTANT_PROPERTY]) == 0
    assert 'logging failed' in capsys.readouterr().err
