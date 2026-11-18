"""Scripted SCPI faults through the V3 physical code path; no sockets are opened."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from oxygen_engine.dispatcher import Dispatcher, MOTOR, SUPPLY
from oxygen_engine.oxygen_backend import OxygenBackend, OxygenTransport
from oxygen_engine.schema import load_dispatcher_config
from oxygen_engine.scpi_transport import ScpiCommandError, ScpiProtocolError, parse_scpi, quote_scpi

ROOT = Path(__file__).resolve().parents[1]
IDS = {SUPPLY: '5349994873749504051', MOTOR: '5349994873749504049'}
PROPS = {SUPPLY: 'TRION/SourceChannel_A_CONST/Const2', MOTOR: 'TRION/SourceChannel_A_CONST/Const0'}
NAMES = {SUPPLY: 'Takasago Voltage Control', MOTOR: 'Drive Control'}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


class Device:
    def __init__(self, clock, mode, headers):
        self.clock, self.mode, self.headers = clock, mode, headers
        self.commands, self.replies, self.errors = [], [], []
        self.triggered = False
        self.failed_command = None
        self.values = {SUPPLY: .45, MOTOR: 0.0}
        self.properties = {
            IDS[role]: {'Mode': '(STRING,"ConstOutput")', 'Unit': '(STRING,"V")',
                        'Range': '(RANGE,-10,"V",10,"V")',
                        'TRION/SourceChannel_A': '(STRING,"CONST2")' if role == SUPPLY else '(STRING,"CONST0")',
                        PROPS[role]: '(SCALAR,0,"V")'}
            for role in (SUPPLY, MOTOR)
        }
        self.properties['9001'] = {'DO_Value': '(BOOL,OFF)'}
        self.read_override = None
        self.connect_count = 0

    def connect(self, transport):
        self.connect_count += 1
        transport._sock = Mock()
        transport._sock.sendall.side_effect = self.send
        transport._connected = True
        transport._closed = False

    def reply(self, command, payload):
        self.replies.append((command.split()[0].rstrip('?') + ' ' if self.headers else '') + payload)

    def send(self, payload):
        command = payload.decode().strip()
        self.commands.append(command)
        if command == '*IDN?':
            self.reply(command, 'DEWETRON,OXYGEN,MOCK,8.2')
        elif command == ':CHANNELlist:NAMes?':
            self.reply(command, ','.join(f'({quote_scpi(IDS[r])},{quote_scpi(NAMES[r])})' for r in IDS)
                       + ',("9001","RZXA Char")')
        elif command.startswith(':CHANNELlist:ITEM'):
            cid = command.split('ITEM', 1)[1].split(':')[0]
            self.reply(command, ','.join(quote_scpi(p) for p in self.properties[cid]))
        elif command.startswith(':CHANNELlist:PROPerty? '):
            cid, prop = parse_scpi(command.split(' ', 1)[1])
            role = next((r for r in IDS if IDS[r] == cid and PROPS[r] == prop), None)
            if role == SUPPLY and self.read_override is not None:
                self.reply(command, self.read_override)
            elif role is not None:
                self.reply(command, f'(SCALAR,{self.values[role]:.9g},"V")')
            else:
                self.reply(command, self.properties[cid].get(prop, 'NONE'))
        elif command == 'SYST:ERR?':
            if self.errors:
                self.clock.wait(.103)
                self.reply(command, self.errors.pop(0))
            else:
                self.reply(command, '0,"No error"')
        elif command.startswith(':CHANNELlist:PROPerty '):
            cid, prop, kind, value, unit = parse_scpi(command.split(' ', 1)[1])
            assert kind == 'SCALAR' and unit == 'V'
            role = next(r for r in IDS if IDS[r] == cid and PROPS[r] == prop)
            value = float(value)
            if role == SUPPLY and value >= .46875 and not self.triggered:
                self.triggered, self.failed_command = True, command
                if self.mode != 'queue_rejected':
                    self.values[role] = value
                if self.mode.startswith('queue_'):
                    self.errors.append('-200,"Execution error"')
                self.read_override = {
                    'queue_none': 'NONE', 'none': 'NONE', 'wrong_type': '(STRING,"updating")',
                    'wrong_unit': '(SCALAR,0.46875,"mV")', 'malformed': '(SCALAR,broken,"V")',
                    'nonfinite': '(SCALAR,nan,"V")', 'mismatch': '(SCALAR,0.46,"V")',
                    'out_of_range': '(SCALAR,0.6,"V")', 'inline_error': '-200,"Execution error"',
                }.get(self.mode)
            else:
                self.values[role] = value
        else:
            raise AssertionError(f'Unexpected scripted command: {command}')


@pytest.fixture(autouse=True)
def prohibit_live_network(monkeypatch):
    monkeypatch.setattr('socket.socket', lambda *a, **k: pytest.fail('No real sockets permitted'))
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: pytest.fail('No real connection permitted'))


def make_dispatcher(tmp_path, monkeypatch, mode, headers=False, relative=False):
    raw = json.loads((ROOT / 'examples/physical-dual.json').read_text())
    for role, name in NAMES.items():
        raw['outputs'][role]['channel'] = name
    filename = 'rzxa_voltage_up.json' if relative else 'rzxa_36_38_40_ladder.json'
    if relative:
        raw['interlocks']['supply_demand_grid_commissioned'] = True
    raw['assignments'] = [{'name': 'ladder', 'enabled': True, 'trigger': 'RZXA Char', 'output': SUPPLY,
                           'profile': str(ROOT / 'profiles' / filename)}]
    path = tmp_path / 'scripted.json'
    path.write_text(json.dumps(raw))
    config = load_dispatcher_config(path)
    clock, records = Clock(), []
    device = Device(clock, mode, headers)
    if relative:
        device.values[SUPPLY] = .4625
    backend = OxygenBackend(config, records.append, confirmed_outputs=list(NAMES.values()), live=True, clock=clock)
    monkeypatch.setattr(backend.transport, 'connect', lambda: device.connect(backend.transport))
    monkeypatch.setattr(backend.transport, '_readline', lambda: device.replies.pop(0))
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait)
    monkeypatch.setattr(dispatcher, 'monitor', lambda: dispatcher.execute_profile('ladder'))
    return dispatcher, backend, device, records


@pytest.mark.parametrize('headers', [False, True])
@pytest.mark.parametrize('mode', ['queue_none', 'queue_rejected', 'none', 'inline_error', 'mismatch'])
def test_relative_step_at_37_5_demand_retains_scpi_fault_evidence(tmp_path, monkeypatch, mode, headers):
    dispatcher, _, device, records = make_dispatcher(tmp_path, monkeypatch, mode, headers, relative=True)
    assert dispatcher.run() == 1
    assert device.triggered and device.connect_count == 1
    assert device.commands.count(device.failed_command) == 1
    writes = [c for c in device.commands if c.startswith(':CHANNELlist:PROPerty ')]
    supply_writes = [c for c in writes if f'"{IDS[SUPPLY]}"' in c]
    assert supply_writes == [device.failed_command]
    assert 'SCALAR,0.46875,"V"' in device.failed_command
    assert dispatcher.completed == 0
    state = next(r for r in records if r['event'] == 'profile_interrupted_state')['outputs'][SUPPLY]
    assert state['last_confirmed_v'] == .4625
    assert state['last_attempted_v'] == .46875
    assert state['status'] == 'unknown'
    if mode.startswith('queue_'):
        transaction = next(r for r in records if r['event'] == 'scpi_command_failed')
        assert transaction['error_code'] == -200
        assert transaction['raw_error_reply'] == '-200,"Execution error"'
        wire = next(r for r in records if r['event'] == 'aout_write_failed')
        assert wire['scpi_command_id'] == transaction['scpi_command_id']
    if mode == 'none':
        read = next(r for r in records if r['event'] == 'output_readback_failed')
        assert read['raw_readback'].endswith('NONE')
    json.dumps(records, allow_nan=False)


@pytest.mark.parametrize('headers', [False, True], ids=['header-off', 'header-on'])
@pytest.mark.parametrize('mode', ['queue_none', 'none', 'wrong_type', 'wrong_unit', 'malformed',
                                  'nonfinite', 'mismatch', 'out_of_range', 'queue_applied'])
def test_mid_ramp_failure_preserves_evidence_and_neutral_cleanup(tmp_path, monkeypatch, headers, mode):
    dispatcher, backend, device, records = make_dispatcher(tmp_path, monkeypatch, mode, headers)
    assert dispatcher.run() == 1
    assert device.triggered and device.connect_count == 1
    failed_at = device.commands.index(device.failed_command)
    later_writes = [c for c in device.commands[failed_at + 1:] if c.startswith(':CHANNELlist:PROPerty ')]
    assert later_writes == [f':CHANNELlist:PROPerty "{IDS[MOTOR]}","{PROPS[MOTOR]}",SCALAR,0,"V"']
    assert device.commands.count(device.failed_command) == 1
    assert dispatcher.completed == 0
    interrupted = next(r for r in records if r['event'] == 'profile_interrupted_state')['outputs'][SUPPLY]
    assert interrupted['status'] == 'unknown'
    assert interrupted['last_attempted_v'] == pytest.approx(.46875)
    assert interrupted['last_confirmed_v'] == pytest.approx(.466666667)
    assert interrupted['physical_voltage_established'] is False
    assert interrupted['value'] < interrupted['last_attempted_v']
    failure = next(r for r in records if r['event'] == 'output_command_failed')
    assert failure['stage'] == ('backend_write_and_error_queue' if mode.startswith('queue_') else 'readback_verification')
    timeline = next(r for r in records if r['event'] == 'timeline_io_failed')
    assert timeline['elapsed_s'] == pytest.approx(10.25)
    assert timeline['interval_ms'] == 250
    if mode.startswith('queue_'):
        assert timeline['io_ms'] == pytest.approx(103)
        wire = next(r for r in records if r['event'] == 'aout_write_failed')
        transaction = next(r for r in records if r['event'] == 'scpi_command_failed')
        queue = next(r for r in records if r.get('raw_payload') == '-200,"Execution error"')
        assert wire['scpi_command_id'] == transaction['scpi_command_id'] == queue['scpi_command_id']
        assert transaction['error_code'] == -200
        assert transaction['raw_error_reply'] == '-200,"Execution error"'
        assert transaction['socket_send_completed'] is True
        assert transaction['device_write_outcome'] == 'unknown'
    if device.read_override is not None:
        read = next(r for r in records if r['event'] == 'output_readback_failed') if mode not in {'mismatch', 'out_of_range'} else None
        if read:
            assert read['raw_readback'] == device.read_override
            assert read['channel_id'] == IDS[SUPPLY] and read['property'] == PROPS[SUPPLY]
    assert any(r['event'] == 'motor_neutral_verified' for r in records)
    summary = records[-1]
    assert summary['event'] == 'dispatcher_summary' and summary['status'] == 'cleanup_unknown'
    assert summary['output_states'][SUPPLY]['status'] == 'unknown'
    assert summary['output_states'][SUPPLY]['last_confirmed_v'] == pytest.approx(.466666667)
    assert summary['output_states'][MOTOR]['status'] == 'readback_verified'
    termination = next(r for r in records if r['event'] == 'termination_output_state')
    assert termination['dispatcher_state'] == 'stopped_unknown'
    assert next(r for r in records if r['event'] == 'profile_interrupted_state')['dispatcher_state'] == 'interrupted'
    json.dumps(records, allow_nan=False)
    assert not backend.connected


def test_rejected_write_with_healthy_readback_can_reconfirm_old_setpoint(tmp_path, monkeypatch):
    dispatcher, _, device, records = make_dispatcher(tmp_path, monkeypatch, 'queue_rejected')
    assert dispatcher.run() == 1
    assert any(r['event'] == 'scpi_command_failed' for r in records)
    assert records[-1]['status'] == 'fault'
    supply = records[-1]['output_states'][SUPPLY]
    assert supply['status'] == 'readback_verified'
    assert supply['last_attempted_v'] == pytest.approx(.46875)
    assert supply['last_confirmed_v'] == pytest.approx(.466666667)
    assert device.commands.count(device.failed_command) == 1


def test_inline_property_error_preserves_wire_reply_and_does_not_reconnect(tmp_path, monkeypatch):
    dispatcher, backend, device, records = make_dispatcher(tmp_path, monkeypatch, 'inline_error', headers=True)
    assert dispatcher.run() == 1
    assert device.connect_count == 1
    query_failure = next(r for r in records if r['event'] == 'query' and r.get('error_type'))
    assert '-200,"Execution error"' in query_failure['raw_payload']
    read_failure = next(r for r in records if r['event'] == 'output_readback_failed')
    assert '-200,"Execution error"' in read_failure['raw_readback']
    assert backend.transport.invalid
    assert any(r['event'] == 'motor_neutral_unknown' for r in records)
    assert records[-1]['status'] == 'cleanup_unknown'
    assert device.commands.count(device.failed_command) == 1


def test_error_queue_association_does_not_prove_command_was_rejected(monkeypatch):
    records, replies = [], ['-200,"Earlier asynchronous error"']
    transport = OxygenTransport(host='127.0.0.1', port=1, timeout_s=.2, emit=records.append, physical=True)
    transport._sock, transport._connected = Mock(), True
    transport.capabilities[(IDS[SUPPLY], PROPS[SUPPLY])] = (0, .5)
    monkeypatch.setattr(transport, '_readline', lambda: replies.pop(0))
    command = f':CHANNELlist:PROPerty "{IDS[SUPPLY]}","{PROPS[SUPPLY]}",SCALAR,0.45,"V"'
    with pytest.raises(ScpiCommandError) as caught:
        transport.command(command)
    assert caught.value.command == command and caught.value.entry.code == -200
    assert transport._sock.sendall.call_count == 2  # write + one queue query, not a drain/retry
    failure = records[-1]
    assert 'causality unproven' in failure['queue_association']
    assert failure['device_write_outcome'] == 'unknown'
    assert not transport.invalid  # A device queue error alone does not prevent neutral cleanup.
    transport.close()


def test_send_failure_is_not_mislabeled_with_a_stale_error_queue_entry(monkeypatch):
    records = []
    transport = OxygenTransport(host='127.0.0.1', port=1, timeout_s=.2, emit=records.append, physical=True)
    transport._sock, transport._connected = Mock(), True
    transport.capabilities[(IDS[SUPPLY], PROPS[SUPPLY])] = (0, .5)
    transport._last_error = transport._parse_error('-200,"old error"')
    transport._sock.sendall.side_effect = OSError('link lost')
    command = f':CHANNELlist:PROPerty "{IDS[SUPPLY]}","{PROPS[SUPPLY]}",SCALAR,0.45,"V"'
    with pytest.raises(Exception, match='Write failed'):
        transport.command(command)
    failure = records[-1]
    assert failure['command_phase'] == 'socket_send' and failure['socket_send_completed'] is False
    assert 'error_code' not in failure and 'raw_error_reply' not in failure
    assert not transport.connected and transport.invalid
    assert not transport.capabilities


def test_unavailable_prewrite_readback_prevents_the_next_write(tmp_path, monkeypatch):
    dispatcher, _, device, records = make_dispatcher(tmp_path, monkeypatch, 'prewrite_none')
    original_send = device.send
    unavailable_at = []

    def send(payload):
        command = payload.decode().strip()
        if (command == f':CHANNELlist:PROPerty? "{IDS[SUPPLY]}","{PROPS[SUPPLY]}"'
                and dispatcher.clock() >= 10.25 and not device.triggered):
            device.triggered = True
            device.read_override = 'NONE'
            unavailable_at.append(len(device.commands))
        original_send(payload)

    monkeypatch.setattr(device, 'send', send)
    assert dispatcher.run() == 1
    assert unavailable_at
    assert not any(c.startswith(f':CHANNELlist:PROPerty "{IDS[SUPPLY]}"')
                   for c in device.commands[unavailable_at[0]:])
    supply = records[-1]['output_states'][SUPPLY]
    assert supply['status'] == 'unknown'
    assert supply['last_attempted_v'] == pytest.approx(.466666667)
    assert supply['last_confirmed_v'] == pytest.approx(.466666667)
    assert any(r['event'] == 'motor_neutral_verified' for r in records)
    assert not any(r['event'] == 'output_command_failed' and r['output'] == SUPPLY for r in records)
    assert next(r for r in records if r['event'] == 'timeline_io_failed')['requested_v'] == pytest.approx(.46875)


def test_output_bounds_are_not_relaxed_by_diagnostic_changes(tmp_path, monkeypatch):
    dispatcher, backend, device, _ = make_dispatcher(tmp_path, monkeypatch, 'none')
    backend.connect()
    dispatcher.prepare()
    before = len(device.commands)
    with pytest.raises(ValueError, match='setpoint must be in'):
        dispatcher.command(SUPPLY, .5001)
    assert len(device.commands) == before
    assert dispatcher.states[SUPPLY].last_attempted_v is None
    backend.close()
