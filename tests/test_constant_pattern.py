"""One-shot simulated constant patterns: timing, write scope and failure cleanup."""

from __future__ import annotations

import csv
import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from oxygen_engine.adapters.mock import MockChannel, MockOxygenServer
from oxygen_engine.constant_pattern import (
    APPROVED_CHANNEL, CONSTANT_PROPERTY, ConstantPattern, SimulatedConstantOutput,
    SimulatedConstantTransport, execute_timeline, run_simulated_pattern,
)
from oxygen_engine.main import main
from oxygen_engine.profiles import ProfileError, load_profile, parse_profile
from oxygen_engine.scpi_transport import ScpiProtocolError

CID = '8898268430164033617'
PROFILE = Path(__file__).resolve().parents[1] / 'profiles' / 'sim_aout_ramp_10s.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def virtual_timeline(pattern, setpoint, verify, emit, **kwargs):
    clock = Clock()
    return execute_timeline(pattern, setpoint, verify, emit, clock=clock, wait=clock.wait, **kwargs)


def test_profile_reference_shape_and_boundaries():
    pattern = ConstantPattern.from_profile(load_profile(PROFILE))
    assert pattern.duration_s == 10
    assert [pattern.value_at(t) for t in (0, 1.5, 3, 5, 7, 8.5, 10, 11)] == [0, .5, 1, 1, 1, .5, 0, 0]
    assert [pattern.phase_at(t) for t in (0, 3, 7, 10)] == ['ramp_up', 'hold', 'ramp_down', 'complete']


@pytest.mark.parametrize('bad', [dict(peak_v=2), dict(peak_v=-1), dict(peak_v=float('nan')),
                                      dict(peak_v=float('inf')), dict(hold_s=5), dict(ramp_up_s=0)])
def test_pattern_authorization_envelope(bad):
    with pytest.raises(ValueError):
        ConstantPattern(**bad)


def test_restricted_profile_rejects_other_hardware_and_recording():
    raw = json.loads(PROFILE.read_text())
    raw['steps'][2] = {'type': 'instrument_op', 'params': {'op': 'set_output', 'value': True}}
    with pytest.raises(ProfileError):
        ConstantPattern.from_profile(parse_profile(raw))
    raw = json.loads(PROFILE.read_text())
    raw['recording'] = {'start': True}
    with pytest.raises(ProfileError):
        ConstantPattern.from_profile(parse_profile(raw))
    raw = json.loads(PROFILE.read_text())
    raw['steps'][1]['params']['rate_per_s'] = 1
    with pytest.raises(ProfileError, match='disagree'):
        ConstantPattern.from_profile(parse_profile(raw))


def test_timeline_uses_elapsed_time_and_finishes_once_at_ten_seconds():
    clock, writes, records = Clock(), [], []
    result = execute_timeline(ConstantPattern(), lambda v: writes.append((clock.now, v)),
                              lambda v: None, records.append, clock=clock, wait=clock.wait)
    assert result['actual_elapsed_s'] == pytest.approx(10)
    assert writes[0] == (0, 0)
    assert writes[-1] == (10, 0)
    assert all(0 <= value <= 1 for _, value in writes)
    assert any(abs(t - 3) < 1e-8 and value == 1 for t, value in writes)
    assert not any(3.01 < t < 6.99 for t, value in writes)
    assert [r['phase'] for r in records if r['elapsed_s'] in (0, 3, 7, 10)] == ['ramp_up', 'hold', 'ramp_down', 'complete']


def test_slow_writes_skip_expired_slots_instead_of_stretching_or_bursting():
    clock, writes = Clock(), []

    def setpoint(value):
        writes.append((clock.now, value))
        clock.now += .08

    result = execute_timeline(ConstantPattern(), setpoint, lambda v: None, lambda r: None,
                              clock=clock, wait=clock.wait)
    assert result['actual_elapsed_s'] <= 10.25
    assert result['completion_lateness_ms'] <= 250
    assert result['skipped_update_slots'] > 0
    assert writes[-1][1] == 0
    assert all(b[0] - a[0] >= .079 for a, b in zip(writes, writes[1:]))
    for t, value in writes:
        assert value == pytest.approx(ConstantPattern().value_at(t))


def test_excessively_late_schedule_aborts_before_a_stale_write():
    clock, writes = Clock(), []

    def late_wait(seconds):
        clock.now += seconds + .3

    with pytest.raises(TimeoutError):
        execute_timeline(ConstantPattern(), writes.append, lambda v: None, lambda r: None,
                         clock=clock, wait=late_wait)
    assert writes == []


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


def transport_for(server, records, **kwargs):
    args = dict(host='127.0.0.1', port=server.bound_port, timeout_s=.1,
                channel=APPROVED_CHANNEL, expected_id=CID, confirmed_simulated=True, emit=records.append)
    args.update(kwargs)
    return SimulatedConstantTransport(**args)


def test_full_tcp_pattern_writes_only_the_authorized_property_and_returns_zero(backend):
    server, channel = backend
    commands, records = [], []
    server._on_command = commands.append
    transport = transport_for(server, records)
    code, summary = run_simulated_pattern(transport, ConstantPattern(), records.append, timeline=virtual_timeline)
    assert code == 0 and summary['status'] == 'ok'
    assert summary['final_state'] == 'configured_zero_verified'
    assert summary['actual_elapsed_s'] == pytest.approx(10)
    assert float(channel.properties[CONSTANT_PROPERTY]) == 0
    writes = [c for c in commands if '?' not in c.split(None, 1)[0]]
    prefix = f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}",SCALAR,'
    assert writes and all(c.startswith(prefix) for c in writes)
    assert any(c.endswith('SCALAR,1,"V"') for c in writes)
    assert writes[-1].endswith('SCALAR,0,"V"')
    assert not any(c.startswith((':STORe', ':ACQU', ':NUM', 'REC:', ':MARK')) for c in commands)
    assert not transport.connected and not transport.armed


@pytest.mark.parametrize('property_change', [
    ('Mode', 'FunctionGenerator'), ('Unit', 'A'), ('TRION/SourceChannel_A', 'OTHER'),
    (CONSTANT_PROPERTY, '0.5'),
])
def test_preflight_refusal_never_writes_or_attempts_cleanup(backend, property_change):
    server, channel = backend
    channel.properties[property_change[0]] = property_change[1]
    commands, records = [], []
    server._on_command = commands.append
    code, summary = run_simulated_pattern(transport_for(server, records), ConstantPattern(), records.append,
                                          timeline=virtual_timeline)
    assert code == 1
    assert summary['writes_attempted'] == 0
    assert summary['final_state'] == 'not_attempted_no_authorization'
    assert all('?' in c.split(None, 1)[0] for c in commands)


def test_wrong_id_missing_property_and_preexisting_errors_fail_closed(backend):
    server, channel = backend
    for failure in ('wrong_id', 'missing', 'queued_error'):
        records = []
        if failure == 'missing':
            channel.properties.pop('Unit')
        if failure == 'queued_error':
            channel.properties['Unit'] = 'V'
            server._push_error(-200, 'Existing error')
        transport = transport_for(server, records, expected_id='1' if failure == 'wrong_id' else CID)
        code, summary = run_simulated_pattern(transport, ConstantPattern(), records.append, timeline=virtual_timeline)
        assert code == 1 and summary['writes_attempted'] == 0


def test_range_too_small_fails_before_writes(backend, monkeypatch):
    server, _ = backend
    original = server._handle

    def handle(line, client):
        if line.endswith(',"Range"'):
            return '(RANGE,0,"V",0.5,"V")'
        return original(line, client)

    monkeypatch.setattr(server, '_handle', handle)
    records = []
    code, summary = run_simulated_pattern(transport_for(server, records), ConstantPattern(), records.append,
                                          timeline=virtual_timeline)
    assert code == 1 and summary['writes_attempted'] == 0


def test_cancellation_attempts_and_verifies_zero(backend):
    server, channel = backend
    records = []

    def cancel(pattern, setpoint, verify, emit, **kwargs):
        setpoint(.5)
        raise KeyboardInterrupt

    code, summary = run_simulated_pattern(transport_for(server, records), ConstantPattern(), records.append, timeline=cancel)
    assert code == 130 and summary['status'] == 'cancelled'
    assert summary['final_state'] == 'configured_zero_verified'
    assert float(channel.properties[CONSTANT_PROPERTY]) == 0


def test_disconnect_does_not_reconnect_replay_or_claim_zero(backend):
    server, channel = backend
    records = []

    def disconnect(pattern, setpoint, verify, emit, **kwargs):
        setpoint(.5)
        server.disconnect_clients()
        verify(.5)

    code, summary = run_simulated_pattern(transport_for(server, records), ConstantPattern(), records.append, timeline=disconnect)
    assert code == 1 and summary['final_state'] == 'unknown'
    assert float(channel.properties[CONSTANT_PROPERTY]) == .5
    assert summary['writes_attempted'] == 2
    assert any(r['event'] == 'neutral_failed' for r in records)


def test_timeout_after_write_reports_unknown_without_retry(backend):
    server, channel = backend
    records = []

    def timeout(pattern, setpoint, verify, emit, **kwargs):
        server._force_timeout = True
        setpoint(.5)

    code, summary = run_simulated_pattern(transport_for(server, records, timeout_s=.02), ConstantPattern(),
                                          records.append, timeline=timeout)
    assert code == 1 and summary['final_state'] == 'unknown'
    assert float(channel.properties[CONSTANT_PROPERTY]) == .5
    assert summary['writes_attempted'] == 2


def test_readback_mismatch_aborts_and_zeroes(backend, monkeypatch):
    server, channel = backend
    records = []
    original = server._handle

    def handle(line, client):
        if line.startswith(':CHANNELlist:PROPerty?') and line.endswith(f',"{CONSTANT_PROPERTY}"'):
            if float(channel.properties[CONSTANT_PROPERTY]) > 0:
                return '(SCALAR,0.2,"V")'
        return original(line, client)

    monkeypatch.setattr(server, '_handle', handle)

    def mismatch(pattern, setpoint, verify, emit, **kwargs):
        setpoint(.5)

    code, summary = run_simulated_pattern(transport_for(server, records), ConstantPattern(), records.append, timeline=mismatch)
    assert code == 1 and summary['status'] == 'fault'
    assert summary['final_state'] == 'configured_zero_verified'
    assert float(channel.properties[CONSTANT_PROPERTY]) == 0


def test_failed_final_zero_readback_cannot_report_success(backend, monkeypatch):
    server, channel = backend
    original = server._handle
    corrupt_zero = False
    records = []

    def handle(line, client):
        if corrupt_zero and line.endswith(f',"{CONSTANT_PROPERTY}"'):
            return '(SCALAR,0.2,"V")'
        return original(line, client)

    monkeypatch.setattr(server, '_handle', handle)

    def finish_with_bad_cleanup(pattern, setpoint, verify, emit, **kwargs):
        nonlocal corrupt_zero
        setpoint(.5)
        corrupt_zero = True
        return {}

    code, summary = run_simulated_pattern(transport_for(server, records), ConstantPattern(), records.append,
                                          timeline=finish_with_bad_cleanup)
    assert code == 1 and summary['status'] == 'fault'
    assert summary['final_state'] == 'unknown'
    assert any(r['event'] == 'neutral_failed' for r in records)


@pytest.mark.parametrize('command', [
    ':STORe:STARt', ':ACQUisition:STOP', '*CLS',
    f':CHANNELlist:PROPerty "{CID}","Mode","ConstOutput"',
    f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}",SCALAR,2,"V"',
    f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}",SCALAR,-1,"V"',
    f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}",SCALAR,NAN,"V"',
    f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}",SCALAR,1,"A"',
    f':CHANNELlist:PROPerty "1","{CONSTANT_PROPERTY}",SCALAR,1,"V"',
    f':CHANNELlist:PROPerty "{CID}","DO_Value",SCALAR,1,"V"',
    f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}",SCALAR,1,"V";:STORe:STARt',
])
def test_write_capability_cannot_escape_target_or_envelope(command):
    transport = SimulatedConstantTransport(host='127.0.0.1', port=10001, timeout_s=.5,
                                           channel=APPROVED_CHANNEL, expected_id=CID,
                                           confirmed_simulated=True, emit=lambda r: None)
    transport._connected = True
    transport._sock = Mock()
    transport.authorize()
    with pytest.raises((ScpiProtocolError, ValueError)):
        transport.write(command)
    transport._sock.sendall.assert_not_called()
    with pytest.raises(ScpiProtocolError):
        transport.reconnect()


def test_explicit_confirmation_and_preflight_are_both_required():
    with pytest.raises(ValueError):
        SimulatedConstantTransport(host='127.0.0.1', port=10001, timeout_s=.5,
                                   channel=APPROVED_CHANNEL, expected_id=CID,
                                   confirmed_simulated=False, emit=lambda r: None)
    transport = SimulatedConstantTransport(host='127.0.0.1', port=10001, timeout_s=.5,
                                           channel=APPROVED_CHANNEL, expected_id=CID,
                                           confirmed_simulated=True, emit=lambda r: None)
    with pytest.raises(ScpiProtocolError):
        transport.write(f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}",SCALAR,0,"V"')


def test_trigger_startup_high_hold_and_fresh_low_rearming():
    from oxygen_engine.triggered_pattern import triggered_timeline

    clock, records, cycles = Clock(), [], []
    states = iter([True, True, False, True, True, True, False, True, True])

    def read():
        try:
            return next(states)
        except StopIteration:
            raise KeyboardInterrupt

    def cycle(*args, **kwargs):
        cycles.append(clock.now)
        clock.now += 10
        return {'actual_elapsed_s': 10}

    with pytest.raises(KeyboardInterrupt):
        triggered_timeline(ConstantPattern(), lambda v: None, lambda v: None, records.append,
                           read_state=read, clock=clock, wait=clock.wait, timeline=cycle)
    assert len(cycles) == 2
    assert [r['cycle'] for r in records if r['event'] == 'pattern_cycle_complete'] == [1, 2]
    assert cycles[1] - cycles[0] > 10


@pytest.mark.parametrize('slow_sample', [False, True])
def test_trigger_never_bridges_stale_observations(slow_sample):
    from oxygen_engine.triggered_pattern import triggered_timeline

    clock, records = Clock(), []
    count = 0

    def read():
        nonlocal count
        count += 1
        if count == 1:
            return False
        if count == 2:
            if slow_sample:
                clock.now += .3
            return True
        raise KeyboardInterrupt

    def wait(seconds):
        clock.now += .3 if not slow_sample else seconds

    with pytest.raises(KeyboardInterrupt):
        triggered_timeline(ConstantPattern(), lambda v: None, lambda v: None, records.append,
                           read_state=read, clock=clock, wait=wait,
                           timeline=lambda *a, **k: pytest.fail('Stale edge must not run'))
    assert not any(r['event'] == 'pattern_cycle_started' for r in records)


def test_triggered_tcp_repeats_without_do_writes_or_queued_runs(backend):
    from oxygen_engine.triggered_pattern import run_triggered_pattern

    server, channel = backend
    trigger = server._channels[server._by_name['DO 3/1 Sim']]
    commands, records, cycles = [], [], []
    server._on_command = commands.append
    clock = Clock()

    def emit(record):
        records.append(record)
        if record['event'] == 'trigger_sample':
            trigger.properties['DO_Value'] = '1'

    def cycle(*args, **kwargs):
        result = virtual_timeline(*args, **kwargs)
        cycles.append(result)
        trigger.properties['DO_Value'] = '0'
        if len(cycles) == 2:
            raise KeyboardInterrupt
        return result

    code, summary = run_triggered_pattern(transport_for(server, records), ConstantPattern(), emit,
                                          timeline=cycle, clock=clock, wait=clock.wait)
    assert code == 130 and summary['final_state'] == 'configured_zero_verified'
    assert len(cycles) == 2 and float(channel.properties[CONSTANT_PROPERTY]) == 0
    writes = [c for c in commands if '?' not in c.split(None, 1)[0]]
    assert all(c.startswith(f':CHANNELlist:PROPerty "{CID}","{CONSTANT_PROPERTY}"') for c in writes)


@pytest.mark.parametrize('failure', ['missing_channel', 'missing_property', 'invalid_value'])
def test_trigger_preflight_failure_never_authorizes_output(backend, failure, monkeypatch):
    from oxygen_engine.triggered_pattern import run_triggered_pattern

    server, _ = backend
    trigger_id = server._by_name['DO 3/1 Sim']
    if failure == 'missing_channel':
        del server._channels[trigger_id]
    elif failure == 'missing_property':
        del server._channels[trigger_id].properties['DO_Value']
    else:
        original = server._handle

        def handle(line, client):
            if line.endswith(',"DO_Value"'):
                return '(STRING,"invalid")'
            return original(line, client)

        monkeypatch.setattr(server, '_handle', handle)
    records = []
    code, summary = run_triggered_pattern(transport_for(server, records), ConstantPattern(), records.append)
    assert code == 1 and summary['writes_attempted'] == 0


def test_trigger_fault_aborts_session_and_verifies_zero(backend):
    from oxygen_engine.triggered_pattern import run_triggered_pattern

    server, channel = backend
    trigger = server._channels[server._by_name['DO 3/1 Sim']]
    records = []

    def emit(record):
        records.append(record)
        if record['event'] == 'trigger_sample':
            trigger.properties['DO_Value'] = '1'

    def fault(pattern, setpoint, *args, **kwargs):
        setpoint(.5)
        raise TimeoutError('No resume')

    clock = Clock()
    code, summary = run_triggered_pattern(transport_for(server, records), ConstantPattern(), emit,
                                          timeline=fault, clock=clock, wait=clock.wait)
    assert code == 1 and summary['final_state'] == 'configured_zero_verified'
    assert float(channel.properties[CONSTANT_PROPERTY]) == 0
    assert len([r for r in records if r['event'] == 'pattern_cycle_started']) == 1

