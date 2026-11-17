"""Observation-only DO diagnostics and replay of user-captured OXYGEN 8.2 data."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from oxygen_engine.adapters.mock import MockOxygenServer
from oxygen_engine.adapters.oxygen import OxygenAdapter
from oxygen_engine.do_diagnostic import DiagnosticTransport, DOWatchDiagnostic, WatchConsoleFilter, WatchOptions
from oxygen_engine.do_monitor import DiagnosticEdgeTracker
from oxygen_engine.main import main
from oxygen_engine.scpi_transport import ScpiProtocolError, ScpiTransport, strip_response_header


@pytest.fixture
def observed():
    path = Path(__file__).parent / 'fixtures' / 'oxygen_8_2_observed.json'
    return json.loads(path.read_text(encoding='utf-8'))


def test_observed_do_pilot_windows_replay_without_duplicates():
    path = Path(__file__).parent / 'fixtures' / 'oxygen_8_2_do_pilot.json'
    pilot = json.loads(path.read_text(encoding='utf-8'))
    for window, expected_presses in [('rising_sample_window', 1), ('falling_sample_window', 0)]:
        tracker = DiagnosticEdgeTracker(.25)
        presses = 0
        for sample in pilot[window]:
            value = OxygenAdapter.parse_property_value(sample['raw'])
            assert value.values == (sample['state'],)
            received = sample['sample_started_monotonic_s'] + sample['query_latency_ms'] / 1000
            presses += tracker.observe(value.values[0], received)['press']
        assert presses == expected_presses
    assert pilot['summary']['presses_detected'] == 1
    assert pilot['summary']['milestone_verified'] is False


def test_console_filter_shows_changes_and_periodic_status_only():
    console = WatchConsoleFilter(status_interval_s=1)

    def sample(now, state=False, **kwargs):
        return {'event': 'sample', 'monotonic_s': now, 'state': state,
                'raw': '(BOOL,ON)' if state else '(BOOL,OFF)', **kwargs}

    assert console.should_print(sample(0))
    assert not console.should_print(sample(.05))
    assert not console.should_print(sample(.95))
    assert console.should_print(sample(1.0))
    assert console.should_print(sample(1.05, True))
    assert not console.should_print(sample(1.1, True))
    assert console.should_print(sample(1.15, True, stale=True))
    assert console.should_print({'event': 'observation_error'})
    assert console.should_print({'event': 'press_detected'})
    assert console.should_print({'event': 'reconnected'})
    assert console.should_print(sample(1.2, True))
    assert not console.should_print({'event': 'query'})


def test_changes_only_console_and_full_sample_console():
    changes = WatchConsoleFilter(status_interval_s=0)
    sample = {'event': 'sample', 'monotonic_s': 0, 'state': False}
    assert changes.should_print(sample)
    assert not changes.should_print({**sample, 'monotonic_s': 100})
    assert changes.should_print({**sample, 'monotonic_s': 101, 'state': True})
    samples = WatchConsoleFilter(mode='samples')
    assert samples.should_print(sample)
    assert samples.should_print(sample)
    assert not samples.should_print({'event': 'query'})


def test_repeated_numeric_status_does_not_flood_console():
    console = WatchConsoleFilter(status_interval_s=1)
    for kind in ('comparison_mismatch', 'numeric_stale'):
        assert console.should_print({'event': kind, 'monotonic_s': 0})
        assert not console.should_print({'event': kind, 'monotonic_s': .01})
        assert console.should_print({'event': kind, 'monotonic_s': 1})


@pytest.mark.parametrize('interval', [-1, float('nan'), float('inf')])
def test_invalid_console_status_intervals(interval):
    with pytest.raises(ValueError):
        WatchConsoleFilter(status_interval_s=interval)


def test_observed_properties_replay_through_adapter(observed, monkeypatch):
    transport = ScpiTransport()
    transport._connected = True
    transport._sock = Mock()
    adapter = OxygenAdapter(transport)
    for channel in [*observed['ao_observations'], observed['do_observation']]:
        for prop, payload in channel['property_payloads'].items():
            monkeypatch.setattr(transport, '_readline', lambda payload=payload: payload)
            result = adapter.get_property_value(channel['id'], prop)
            if prop.startswith('TRION/SourceChannel_A_CONST/'):
                assert result.kind == 'SCALAR'
                assert result.values == (0.0, 'V')
            elif prop == 'DO_Value':
                assert result.kind == 'BOOL' and result.values == (False,)
            elif prop == 'Range':
                assert result.values == (-10.0, 'V', 10.0, 'V')
            else:
                assert result.kind == 'STRING'
    second = observed['ao_observations'][1]['property_payloads']
    assert 'TRION/SourceChannel_A_CONST/Const1' in second
    assert 'TRION/SourceChannel_A_CONST/Const0' not in second


def test_observed_attributes_dimensions_and_identification(observed, monkeypatch):
    do = observed['do_observation']
    transport = ScpiTransport()
    transport._connected = True
    transport._sock = Mock()
    adapter = OxygenAdapter(transport)
    monkeypatch.setattr(transport, '_readline', lambda: do['attribute_names_payload'])
    assert 'DO_Value' in adapter.list_properties(do['id'])
    assert 'Value' not in adapter.list_properties(do['id'])
    assert strip_response_header(observed['numeric_dimensions_wire_reply'],
                                 observed['numeric_dimensions_query']) == '1'
    monkeypatch.setattr(transport, '_readline', lambda: observed['identification_payload'])
    assert adapter.identify() == 'DEWETRON,OXYGEN,00000001,8.2'
    assert observed['do_observation']['ui_transition_verified'] is False


def test_observed_decoded_numeric_samples(observed, monkeypatch):
    transport = ScpiTransport()
    transport._connected = True
    transport._sock = Mock()
    adapter = OxygenAdapter(transport)
    for sample in observed['numeric_samples_decoded']:
        # Wire formatting is synthetic: only decoded values were captured live.
        replies = iter(['0,"No error"'] * 3 + [','.join('1' for _ in sample['channels']),
                                              ','.join(str(v) for v in sample['values'])])
        monkeypatch.setattr(transport, '_readline', lambda: next(replies))
        assert adapter.read_measurements(sample['channels']) == sample['values']


def test_observed_decoded_states(observed, monkeypatch):
    transport = ScpiTransport()
    transport._connected = True
    transport._sock = Mock()
    adapter = OxygenAdapter(transport)
    monkeypatch.setattr(transport, '_readline', lambda: observed['acquisition_state_decoded'])
    assert adapter.acquisition_state() == 'Started'
    monkeypatch.setattr(transport, '_readline', lambda: observed['recording_state_decoded'])
    assert adapter.recording_state() == 'Stopped'


def test_tracker_startup_high_sustained_high_and_rearm():
    tracker = DiagnosticEdgeTracker(0.25)
    observations = [tracker.observe(state, i * .01)
                    for i, state in enumerate([True, True, False, True, True, False, True])]
    assert observations[0]['edge'] == 'baseline'
    assert sum(o['press'] for o in observations) == 2
    assert observations[5]['observed_high_ms'] == pytest.approx(20)
    assert not observations[1]['press'] and not observations[4]['press']


def test_tracker_gap_error_reset_and_backwards_time_suppress_edges():
    tracker = DiagnosticEdgeTracker(.25)
    tracker.observe(False, 0)
    assert tracker.observe(True, .5)['edge'] == 'baseline'
    tracker.reset()
    assert tracker.observe(True, .51)['press'] is False
    assert tracker.observe(False, .52)['press'] is False
    assert tracker.observe(True, .53)['press'] is True
    assert tracker.observe(False, .1)['edge'] == 'baseline'


@pytest.mark.parametrize(('high_interval', 'expected'), [((.01, .03), 0), ((.01, .10), 1)])
def test_sampling_cannot_recover_pulses_between_polls(high_interval, expected):
    tracker = DiagnosticEdgeTracker(.25)
    events = [tracker.observe(high_interval[0] <= t < high_interval[1], t)
              for t in (0, .05, .10, .15)]
    assert sum(e['press'] for e in events) == expected


@pytest.mark.parametrize('bad', [
    {'interval_s': 0}, {'interval_s': float('nan')}, {'duration_s': -1},
    {'max_gap_s': 0}, {'expected_presses': 0}, {'interaction_ms': -1},
    {'event_source': 'numeric'}, {'port': 70000},
])
def test_invalid_options(bad):
    with pytest.raises(ValueError):
        WatchOptions(channel='DO 3/1 Sim', **bad)


@pytest.mark.parametrize('numeric', [False, True])
@pytest.mark.parametrize('command', [
    ':STORe:STARt', ':STORe:STOP', ':ACQUisition:STARt', ':ACQUisition:STOP',
    ':CHANNELlist:PROPerty "1","DO_Value",ON', ':MARKer:ADD "x"', '*CLS',
    ':NUMeric:NORMal:ITEMS "x";:STORe:STARt',
])
def test_transport_blocks_all_actuation(numeric, command):
    transport = DiagnosticTransport(WatchOptions(channel='x', compare_numeric=numeric), lambda r: None)
    transport._connected = True
    transport._sock = Mock()
    with pytest.raises(ScpiProtocolError):
        transport.write(command)
    transport._sock.sendall.assert_not_called()


def test_transport_blocks_compound_queries_and_default_numeric_setup():
    transport = DiagnosticTransport(WatchOptions(channel='x'), lambda r: None)
    transport._connected = True
    transport._sock = Mock()
    for command in [':CHANNELlist:PROPerty? "1","DO_Value";:STORe:STARt',
                    '*IDN?;*CLS', ':NUMeric:NORMal:VALue?']:
        with pytest.raises(ScpiProtocolError):
            transport.query(command)
    with pytest.raises(ScpiProtocolError):
        transport.write(':NUMeric:NORMal:FORMat ASCII')
    transport._sock.sendall.assert_not_called()


@pytest.fixture(params=[False, True], ids=['header-off', 'header-on'])
def server(request):
    backend = MockOxygenServer(response_headers=request.param)
    backend.start()
    try:
        yield backend
    finally:
        backend.stop()


def run_diagnostic(server, **kwargs):
    options = dict(channel='DO 3/1 Sim', port=server.bound_port, interval_s=.005,
                   max_gap_s=1, timeout_s=.1, reconnect_s=.005, duration_s=.12)
    options.update(kwargs)
    records = []
    diagnostic = DOWatchDiagnostic(WatchOptions(**options), records.append)
    status = diagnostic.run()
    return status, diagnostic, records


def test_tcp_watch_startup_high_and_one_event_per_activation(server):
    commands = []
    states = iter([True, True, False, True, True, False])

    def on_command(command):
        commands.append(command)
        if command.endswith(',"DO_Value"'):
            server.set_do('DO 3/1 Sim', next(states, False))

    server._on_command = on_command
    status, diagnostic, records = run_diagnostic(server, expected_presses=1)
    assert status == 0
    assert diagnostic.summary['presses_detected'] == 1
    assert diagnostic.summary['count_difference'] == 0
    assert diagnostic.summary['milestone_verified'] is False
    assert len([r for r in records if r['event'] == 'press_detected']) == 1
    assert all('?' in c.split(None, 1)[0] for c in commands)
    sample = next(r for r in records if r['event'] == 'sample')
    assert sample['raw'] == '(BOOL,ON)' and sample['state'] is True
    assert sample['query_latency_ms'] >= 0
    assert 'ts' in sample and 'monotonic_s' in sample
    assert any(r['event'] == 'query' and r.get('latency_ms', -1) >= 0 for r in records)


def test_tcp_numeric_comparison_configures_only_once(server):
    commands = []
    count = 0

    def on_command(command):
        nonlocal count
        commands.append(command)
        if command.endswith(',"DO_Value"'):
            count += 1
            server.set_do('DO 3/1 Sim', count >= 2)

    server._on_command = on_command
    status, diagnostic, records = run_diagnostic(server, compare_numeric=True)
    assert status == 0
    assert diagnostic.summary['property_presses'] == 1
    assert diagnostic.summary['numeric_presses'] == 1
    assert diagnostic.summary['mismatches'] == 0
    assert len([r for r in records if r['event'] == 'press_detected']) == 1
    writes = [c for c in commands if '?' not in c.split(None, 1)[0]]
    assert writes == [':NUMeric:NORMal:ITEMS "REL-TIME","DO 3/1 Sim"',
                      ':NUMeric:NORMal:FORMat ASCII', ':NUMeric:NORMal:NUMber ALL']


def test_numeric_unsupported_falls_back_to_property(server, monkeypatch):
    original = server._handle

    def handle(line, client):
        if line == ':NUMeric:NORMal:DIMS?':
            return '128'
        return original(line, client)

    monkeypatch.setattr(server, '_handle', handle)
    status, diagnostic, records = run_diagnostic(server, compare_numeric=True)
    assert status == 0
    assert diagnostic.summary['samples'] > 0
    assert any(r['event'] == 'numeric_unavailable' for r in records)
    assert all(r['numeric_state'] is None for r in records if r['event'] == 'sample')
    status, _, records = run_diagnostic(server, compare_numeric=True, event_source='numeric')
    assert status == 1
    assert any(r['event'] == 'diagnostic_error' for r in records)


def test_numeric_source_can_reveal_transition_while_property_is_static(server):
    samples = 0

    def on_command(command):
        nonlocal samples
        # Flip on numeric samples so prepare-time DO_Value reads cannot pre-arm the edge.
        # Match both long-form VALue? and abbreviated VAL? (header-on path).
        upper = command.upper()
        if ('NUM' in upper) and ('VAL' in upper.split(':')[-1]):
            samples += 1
            # Advance mock REL-TIME so consecutive fast polls stay "fresh" for edge tracking.
            server._relative_time_origin -= 0.002
            server.set_measurement('DO 3/1 Sim', float(samples >= 2))

    server._on_command = on_command
    status, diagnostic, records = run_diagnostic(
        server, compare_numeric=True, event_source='numeric', duration_s=.35, interval_s=.015)
    assert status == 0
    assert diagnostic.summary['property_presses'] == 0
    assert diagnostic.summary['numeric_presses'] == 1
    assert diagnostic.summary['mismatches'] > 0
    presses = [r for r in records if r['event'] == 'press_detected']
    assert len(presses) == 1 and presses[0]['source'] == 'numeric'


@pytest.mark.parametrize('reply', ['1,0.5', '1,NAN'])
def test_nonbinary_or_nonfinite_numeric_falls_back_without_events(server, monkeypatch, reply):
    original = server._handle

    def handle(line, client):
        if line == ':NUMeric:NORMal:VALue?':
            return reply
        return original(line, client)

    monkeypatch.setattr(server, '_handle', handle)
    status, diagnostic, records = run_diagnostic(server, compare_numeric=True)
    assert status == 0
    assert diagnostic.summary['presses_detected'] == 0
    assert any(r['event'] == 'numeric_unavailable' for r in records)
    for record in records:
        json.dumps(record, allow_nan=False)


def test_held_numeric_clock_cannot_generate_an_event(server, monkeypatch):
    original = server._handle
    calls = 0

    def handle(line, client):
        nonlocal calls
        if line == ':NUMeric:NORMal:VALue?':
            calls += 1
            return '1,0' if calls == 1 else '1,1'
        return original(line, client)

    monkeypatch.setattr(server, '_handle', handle)
    status, diagnostic, records = run_diagnostic(server, compare_numeric=True, event_source='numeric')
    assert status == 0
    assert diagnostic.summary['numeric_presses'] == 0
    assert any(r['event'] == 'numeric_stale' for r in records)


def test_stopped_acquisition_disables_numeric_events_not_property_reads(server):
    server.acquisition_state = 'Stopped'
    status, diagnostic, records = run_diagnostic(server, compare_numeric=True)
    assert status == 0 and diagnostic.summary['samples'] > 0
    assert all(r['numeric_state'] is None for r in records if r['event'] == 'sample')


def test_unsupported_numeric_header_reconnects_into_property_only_mode(server, monkeypatch):
    original = server._handle

    def handle(line, client):
        if line == ':NUMeric:NORMal:DIMS?':
            return '-113,"Undefined header"'
        return original(line, client)

    monkeypatch.setattr(server, '_handle', handle)
    status, diagnostic, records = run_diagnostic(server, compare_numeric=True, duration_s=.2)
    assert status == 0 and diagnostic.summary['samples'] > 0
    assert any(r['event'] == 'numeric_unavailable' for r in records)
    assert any(r['event'] == 'sample' and r['valid'] for r in records)


def test_reconnect_high_is_baseline_not_a_press(server):
    calls = 0

    def on_command(command):
        nonlocal calls
        if command.endswith(',"DO_Value"'):
            calls += 1
            server.set_do('DO 3/1 Sim', calls in {2, 3, 4} or calls >= 6)
            if calls == 3:
                server.disconnect_clients()

    server._on_command = on_command
    status, diagnostic, records = run_diagnostic(server, duration_s=.2)
    assert status == 0
    assert diagnostic.summary['reconnects'] == 1
    assert diagnostic.summary['presses_detected'] == 2
    assert any(r['event'] == 'reconnected' for r in records)
    baselines = [r for r in records if r['event'] == 'transition' and r['edge'] == 'baseline']
    assert baselines[-1]['state'] is True and baselines[-1]['press'] is False


def test_timeout_is_logged_and_recovery_does_not_press(server):
    calls = 0

    def on_command(command):
        nonlocal calls
        if command.endswith(',"DO_Value"'):
            calls += 1
            if calls == 2:
                server.set_do('DO 3/1 Sim', True)
                server._force_timeout = True

    server._on_command = on_command
    status, diagnostic, records = run_diagnostic(server, timeout_s=.02, duration_s=.18)
    assert status == 0
    assert diagnostic.summary['timeouts'] == 1
    assert diagnostic.summary['reconnects'] == 1
    assert diagnostic.summary['presses_detected'] == 0
    assert any(r.get('error_type') == 'ScpiTimeoutError' for r in records)


def test_malformed_property_resyncs_without_a_cross_gap_press(server, monkeypatch):
    original = server._handle
    calls = 0

    def handle(line, client):
        nonlocal calls
        if line.endswith(',"DO_Value"'):
            calls += 1
            if calls == 2:
                return '(BOOL,maybe)'
            if calls > 2:
                server.set_do('DO 3/1 Sim', True)
        return original(line, client)

    monkeypatch.setattr(server, '_handle', handle)
    status, diagnostic, records = run_diagnostic(server, duration_s=.2)
    assert status == 0
    assert diagnostic.summary['presses_detected'] == 0
    assert any(r.get('error_type') == 'ScpiProtocolError' for r in records)
    assert any(r['event'] == 'reconnected' for r in records)


def test_scpi_error_invalidates_sample_before_edge(server):
    calls = 0

    def on_command(command):
        nonlocal calls
        if command.endswith(',"DO_Value"'):
            calls += 1
            if calls == 2:
                server.set_do('DO 3/1 Sim', True)
                server._push_error(-200, 'Injected observation failure')

    server._on_command = on_command
    _, diagnostic, records = run_diagnostic(server)
    assert diagnostic.summary['presses_detected'] == 0
    assert any(r['event'] == 'scpi_error' for r in records)
    assert any(r['event'] == 'sample' and not r['valid'] for r in records)


def test_slow_reply_suppresses_state_and_resyncs(monkeypatch):
    module = importlib.import_module('oxygen_engine.do_diagnostic')
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    records = []
    diagnostic = DOWatchDiagnostic(WatchOptions(channel='x'), records.append)
    adapter = Mock()
    adapter.parse_property_value.side_effect = OxygenAdapter.parse_property_value
    diagnostic.errors = lambda transport: False
    adapter.get_property_reply.return_value = '(BOOL,OFF)'
    diagnostic.poll(adapter, '1')

    def slow(*args):
        clock.now += .5
        return '(BOOL,ON)'

    clock.now = .05
    adapter.get_property_reply.side_effect = slow
    diagnostic.poll(adapter, '1')
    adapter.get_property_reply.side_effect = None
    adapter.get_property_reply.return_value = '(BOOL,ON)'
    clock.now = .60
    diagnostic.poll(adapter, '1')
    clock.now = .65
    diagnostic.poll(adapter, '1')
    assert diagnostic.counts['property_presses'] == 0
    assert diagnostic.counts['stale_samples'] >= 1
    assert not any(r['event'] == 'press_detected' for r in records)


def test_unknown_channel_and_unlisted_property_fail_closed(server):
    for options in ({'channel': 'missing'}, {'property_name': 'invented'}):
        status, diagnostic, records = run_diagnostic(server, **options)
        assert status == 1
        assert diagnostic.counts['samples'] == 0
        assert any(r['event'] == 'diagnostic_error' for r in records)


def test_cli_bypasses_engine_and_records_jsonl(server, monkeypatch, tmp_path, capsys):
    module = importlib.import_module('oxygen_engine.main')
    monkeypatch.setattr(module, '_make_engine', lambda args: pytest.fail('Engine must not be constructed'))
    path = tmp_path / 'watch.jsonl'
    assert main(['watch-do', '--channel', 'DO 3/1 Sim', '--port', str(server.bound_port),
                 '--duration', '.04', '--interval-ms', '5', '--max-gap-ms', '1000',
                 '--log-file', str(path), '--config', 'does-not-exist.json', '--live',
                 '--console', 'events', '--status-interval', '0']) == 0
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[-1]['event'] == 'summary'
    assert any(r['event'] == 'query' for r in rows)
    assert any(r['event'] == 'sample' for r in rows)
    assert all('ts' in r and 'run_id' in r for r in rows)
    console = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert all(r['event'] != 'query' for r in console)
    assert sum(r['event'] == 'sample' for r in console) == 1
    assert sum(r['event'] == 'sample' for r in rows) > 1
