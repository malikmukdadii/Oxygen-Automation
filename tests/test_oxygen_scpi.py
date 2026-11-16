"""Documented OXYGEN wire formats and read-only discovery safety."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from oxygen_engine.adapters.mock import MockOxygenServer
from oxygen_engine.adapters.oxygen import OxygenAdapter, PropertyValue
from oxygen_engine.main import main
from oxygen_engine.scpi_transport import (
    ScpiConnectionError, ScpiProtocolError, ScpiTimeoutError, ScpiTransport,
    parse_scpi, quote_scpi,
)


@pytest.fixture
def scripted(monkeypatch):
    transport = ScpiTransport()
    transport._sock = Mock()
    transport._connected = True
    replies = []
    monkeypatch.setattr(transport, '_readline', lambda: replies.pop(0))
    return transport, OxygenAdapter(transport), replies


@pytest.fixture(params=[False, True], ids=['header-off', 'header-on'])
def oxygen(request):
    server = MockOxygenServer(response_headers=request.param)
    server.start()
    transport = ScpiTransport(port=server.bound_port, timeout_s=0.25)
    transport.connect()
    try:
        yield server, transport, OxygenAdapter(transport)
    finally:
        transport.close()
        server.stop()


@pytest.mark.parametrize('header', ['', ':CHANNEL:NAME ', ':CHANNELlist:NAMes '])
def test_documented_channel_tuples(scripted, header):
    transport, adapter, replies = scripted
    replies.append(header + '("18446744073709551615","AI, \"\"quoted\"\""),("1","other")')
    channels = adapter.list_channels()
    assert channels[0].channel_id == '18446744073709551615'
    assert channels[0].name == 'AI, "quoted"'
    assert channels[0].channel_type == 'unknown'
    transport._sock.sendall.assert_called_once_with(b':CHANNELlist:NAMes?\n')


@pytest.mark.parametrize('raw', [
    '', 'garbage', '[]', '("1")', '("1","a","extra")',
    '("1","a"', '("1","a"),', '("1","a") junk',
    '("18446744073709551616","a")', '("1.1","a")',
    '("1","a"),("1","b")', '("1",("nested"))',
])
def test_malformed_channel_replies(scripted, raw):
    _, adapter, replies = scripted
    replies.append(raw)
    with pytest.raises(ScpiProtocolError):
        adapter.list_channels()


@pytest.mark.parametrize(('raw', 'expected'), [
    ('ON', PropertyValue('BOOLEAN', (True,))),
    ('FALSE', PropertyValue('BOOLEAN', (False,))),
    ('(BOOL,ON)', PropertyValue('BOOL', (True,))),
    ('(BOOL,OFF)', PropertyValue('BOOL', (False,))),
    ('"a, \"\"quoted\"\" string"', PropertyValue('STRING', ('a, "quoted" string',))),
    ('(STRING,"Voltage")', PropertyValue('STRING', ('Voltage',))),
    ('(FLOAT,1.0E-1)', PropertyValue('FLOAT', (0.1,))),
    ('-2.5E3', PropertyValue('NUMBER', (-2500.0,))),
    ('(ENUM,"ChannelStored","Auto")', PropertyValue('ENUM', ('ChannelStored', 'Auto'))),
    ('(SCALAR,100.0,"ms")', PropertyValue('SCALAR', (100.0, 'ms'))),
    ('(RANGE,-10.0,"V",10.0,"V")', PropertyValue('RANGE', (-10.0, 'V', 10.0, 'V'))),
    ('(RATIO,1,3,"V")', PropertyValue('RATIO', (1.0, 3.0, 'V'))),
    ('(CHANNEL_ID,"18446744073709551615")', PropertyValue('CHANNEL_ID', ('18446744073709551615',))),
    ('(CHANNEL_ID_VECTOR,"18446744073709551615","1")', PropertyValue('CHANNEL_ID_VECTOR', ('18446744073709551615', '1'))),
    ('NONE', PropertyValue('NONE', ())),
])
def test_documented_property_formats(scripted, raw, expected):
    transport, adapter, replies = scripted
    replies.append(':CHANNEL:PROP ' + raw)
    assert adapter.get_property_value('18446744073709551615', 'a,b') == expected
    transport._sock.sendall.assert_called_once_with(
        b':CHANNELlist:PROPerty? "18446744073709551615","a,b"\n')


@pytest.mark.parametrize('raw', ['', 'garbage', '(BOOL,maybe)', '(FLOAT,no)',
                                      '(SCALAR,1)', '(RANGE,1,"V")', '(BOGUS,1)',
                                      '(STRING,("nested"))', '"unterminated', 'ON,OFF'])
def test_malformed_properties(scripted, raw):
    _, adapter, replies = scripted
    replies.append(raw)
    with pytest.raises(ScpiProtocolError):
        adapter.get_property_value('1', 'explicit-property')


@pytest.mark.parametrize('state', ['Armed', 'Paused', 'Started', 'Stopped', 'Stopping'])
def test_recording_text_states(scripted, state):
    _, adapter, replies = scripted
    replies.extend([':STOR:STAT ' + state, state])
    assert adapter.recording_state() == state
    assert adapter.recording_status() is (state != 'Stopped')


@pytest.mark.parametrize('raw', ['0', '1', 'bad Started', 'ON', '', 'Started,Stopped'])
def test_malformed_recording_states(scripted, raw):
    _, adapter, replies = scripted
    replies.append(raw)
    with pytest.raises(ScpiProtocolError):
        adapter.recording_status()


def test_error_queue_quoting_and_headers(scripted):
    _, _, replies = scripted
    transport = scripted[0]
    replies.append(':SYSTEM:ERROR -102,"Syntax, \"\"quoted\"\" error"')
    entry = transport.check_error()
    assert (entry.code, entry.message) == (-102, 'Syntax, "quoted" error')


@pytest.mark.parametrize('raw', ['', '0', 'x,"bad"', '0,"unclosed', '0,"ok",extra'])
def test_malformed_errors(scripted, raw):
    transport, _, replies = scripted
    replies.append(raw)
    with pytest.raises(ScpiProtocolError):
        transport.check_error()


def test_inline_error_is_not_a_measurement(scripted):
    transport, _, replies = scripted
    replies.append(':NUM:NORM:VAL -200,"Execution error"')
    with pytest.raises(ScpiProtocolError, match='Execution error'):
        transport.query(':NUMeric:NORMal:VALue?')


def test_unrelated_response_header_rejected(scripted):
    transport, _, replies = scripted
    replies.append(':CHANNEL:PROP Started')
    with pytest.raises(ScpiProtocolError, match='Unexpected response header'):
        transport.query(':STORe:STATe?')
    assert not transport.connected


def test_discovery_and_numeric_wire_protocol(oxygen):
    server, transport, adapter = oxygen
    commands = []
    server._on_command = commands.append
    assert adapter.identify().startswith('DEWETRON,OXYGEN,')
    channels = adapter.list_channels()
    cid = adapter.resolve_channel_id('DC Power')
    assert cid == '800000000000000001'
    assert cid in adapter.list_channel_ids()
    assert adapter.resolve_channel_id('not a channel') is None
    aout = next(c for c in channels if c.name == 'Test Aout1')
    assert 'Range' in adapter.list_properties(aout.channel_id)
    assert adapter.get_property_value(aout.channel_id, 'Range').kind == 'RANGE'
    assert adapter.acquisition_state() == 'Started'
    assert adapter.recording_state() == 'Stopped'
    assert adapter.read_measurements(['DC Power', 'RPM']) == [120.0, 1500.0]
    assert adapter.read_measurement(cid) == 120.0
    assert ':NUMeric:NORMal:ITEMS "DC Power","RPM"' in commands
    assert ':NUMeric:NORMal:VALue?' in commands
    assert not any(c.startswith(('CHAN:', 'REC:')) for c in commands)
    with pytest.raises(ScpiProtocolError, match='Unknown channel ID'):
        adapter.read_measurement('18446744073709551615')
    with pytest.raises(ScpiProtocolError, match='Unknown numeric channel'):
        adapter.read_measurements(['not a channel'])
    assert adapter.list_properties('18446744073709551615') == []
    assert transport.check_error().code != 0


def test_recording_and_marker_mapping_mock_only(oxygen):
    server, _, adapter = oxygen
    adapter.start_recording()
    assert adapter.recording_state() == 'Started'
    text = 'label, with "quotes"'
    adapter.add_marker(text)
    assert server._markers[-1] == text
    adapter.stop_recording()
    assert adapter.recording_state() == 'Stopped'


def test_unknown_query_and_legacy_dialect_rejected(oxygen):
    _, transport, _ = oxygen
    with pytest.raises(ScpiProtocolError, match='Undefined header'):
        transport.query('CHAN:LIST?')


def test_timeout_disconnects_and_cannot_reuse_stale_reply(oxygen):
    _, transport, _ = oxygen
    transport.write('MOCK:TIMEOUT')
    with pytest.raises(ScpiTimeoutError):
        transport.query(':CHANNELlist:IDs? "DC Power"')
    assert not transport.connected
    with pytest.raises(ScpiConnectionError):
        transport.query('*IDN?')


@pytest.mark.parametrize('raw', ['garbage', '1,', '1,2', '(1)', ''])
def test_malformed_numeric_values(scripted, raw):
    _, adapter, replies = scripted
    replies.extend(['0,"No error"'] * 3 + ['1', raw])
    with pytest.raises(ScpiProtocolError):
        adapter.read_measurements(['DC Power'])


@pytest.mark.parametrize(('dimensions', 'values'), [
    (':NUM:DIMS 1', ':NUM:VAL 2.5'),
    (':NUM:NORM:DIMS 1', ':NUM:NORM:VAL 2.5'),
    (':NUMERIC:DIMS 1', ':NUMERIC:VALUE 2.5'),
    ('1', '2.5'),
])
def test_numeric_default_node_response_headers(scripted, dimensions, values):
    _, adapter, replies = scripted
    replies.extend(['0,"No error"'] * 3 + [dimensions, values])

    assert adapter.read_measurements(['AI 1/1 Sim']) == [2.5]


@pytest.mark.parametrize('reply', [
    ':NUM:VAL 1',
    ':NUM:FAST:DIMS 1',
    ':CHANNEL:DIMS 1',
])
def test_numeric_unrelated_headers_still_rejected(scripted, reply):
    transport, _, replies = scripted
    replies.append(reply)

    with pytest.raises(ScpiProtocolError, match='Unexpected response header'):
        transport.query(':NUMeric:NORMal:DIMS?')


def test_array_rejected_for_scalar_read(scripted):
    _, adapter, replies = scripted
    replies.extend(['0,"No error"'] * 3 + ['128'])
    with pytest.raises(ScpiProtocolError, match='must be scalar'):
        adapter.read_measurements(['array'])


def test_fragmented_crlf_reply():
    transport = ScpiTransport()
    transport._connected = True
    transport._sock = Mock()
    transport._sock.recv.side_effect = [b':CHANNEL:ID "184467', b'44073709551615"\r', b'\n']
    assert transport.query(':CHANNELlist:IDs? "x"') == '"18446744073709551615"'


def test_scpi_smoke_sends_only_read_queries(oxygen, capsys):
    server, _, _ = oxygen
    commands = []
    server._on_command = commands.append
    assert main(['scpi-smoke', '--host', '127.0.0.1', '--port', str(server.bound_port),
                 '--aout', 'Test Aout1']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['identification'].startswith('DEWETRON,OXYGEN,')
    assert report['acquisition_state'] == 'Started'
    assert report['recording_state'] == 'Stopped'
    assert report['aout_attributes'][0]['attributes']
    assert report['scpi_errors'] == []
    assert commands == ['*IDN?', ':CHANNELlist:NAMes?',
                        ':CHANNELlist:ITEM4152600322822373457:ATTR:NAMes?',
                        ':ACQUisition:STATe?', ':STORe:STATe?', 'SYST:ERR?']


def test_smoke_unknown_channel_and_errors(oxygen, capsys):
    server, _, _ = oxygen
    server._push_error(-200, 'Existing error')
    assert main(['scpi-smoke', '--port', str(server.bound_port), '--aout', 'missing']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['failures']
    assert report['scpi_errors'] == [{'code': -200, 'message': 'Existing error'}]


def test_physical_property_writes_always_disabled(scripted):
    transport, adapter, _ = scripted
    with pytest.raises(ScpiProtocolError, match='writes disabled'):
        adapter.set_property('1', 'any-output-property', '1')
    transport._sock.sendall.assert_not_called()


def test_live_aout_snapshot_never_guesses_properties(example_config_dict):
    from oxygen_engine.aout_controller import AoutController
    from oxygen_engine.config import parse_config

    example_config_dict['connection']['use_mock'] = False
    config = parse_config(example_config_dict)
    adapter, channels = Mock(), Mock()
    channels.get.return_value.channel_id = '1'
    controller = AoutController(adapter, channels, config, simulation=True)
    snapshot = controller.read_snapshot(config.aout_roles[0].logical_name)
    assert snapshot.const_value is None
    assert snapshot.waveform_lookups == {}
    adapter.get_property.assert_not_called()


def test_live_do_requires_explicit_verified_property(example_config_dict):
    from oxygen_engine.channel_manager import ChannelError
    from oxygen_engine.config import parse_config
    from oxygen_engine.do_monitor import DOMonitor

    example_config_dict['connection']['use_mock'] = False
    config = parse_config(example_config_dict)
    adapter = Mock()
    monitor = DOMonitor(adapter, Mock(), config)
    with pytest.raises(ChannelError, match='unverified'):
        monitor._resolve_state_property(config.do_commands[0], '1')
    config.live_control.do_property_verified = True
    config.do_commands[0].state_property = None
    with pytest.raises(ChannelError, match='explicitly configured'):
        monitor._resolve_state_property(config.do_commands[0], '1')
    adapter.list_properties.assert_not_called()


def test_smoke_connection_failure(monkeypatch, capsys):
    def fail_connect(self):
        raise ScpiConnectionError('Connection refused')

    monkeypatch.setattr(ScpiTransport, 'connect', fail_connect)
    assert main(['scpi-smoke']) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['failures'] == [{'operation': 'connect', 'error': 'Connection refused'}]


@pytest.mark.parametrize('value', ['x\n:STOR:STAR', 'x\r:STOR:STOP', 'x\x00'])
def test_control_character_injection_rejected(scripted, value):
    transport, adapter, _ = scripted
    with pytest.raises(ScpiProtocolError):
        quote_scpi(value)
    with pytest.raises(ScpiProtocolError):
        adapter.add_marker(value)
    with pytest.raises(ScpiProtocolError):
        transport.write(value)
    transport._sock.sendall.assert_not_called()


@pytest.mark.parametrize('raw', ['(', ')', '("a",)', '"a"junk', 'a,,b', 'a,', '(a,(b)'])
def test_malformed_scpi_grammar(raw):
    with pytest.raises(ScpiProtocolError):
        parse_scpi(raw)
