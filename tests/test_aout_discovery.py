import copy
import json
from pathlib import Path

import pytest

from oxygen_engine.adapters.oxygen import OxygenAdapter
from oxygen_engine.aout_diagnostic import AoutDiagnosticTransport
from oxygen_engine.aout_discovery import AoutDiscovery
from oxygen_engine.dispatcher import MOTOR, SUPPLY
from oxygen_engine.errors import ConfigError
from oxygen_engine.main import main
from oxygen_engine.schema import load_dispatcher_config
from oxygen_engine.scpi_transport import ScpiProtocolError, parse_scpi, quote_scpi

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'tests' / 'fixtures' / 'dispatcher_v3_demo_bindings.json'


class ReadOnlyInstrument:
    def __init__(self, outputs):
        self.commands = []
        self.channels = []
        self.properties = {}
        for index, spec in enumerate(outputs.values()):
            cid = str(18446744073709551615 - index)
            self.channels.append((cid, spec['channel']))
            self.properties[cid] = {
                'Mode': '(STRING,"ConstOutput")',
                'TRION/SourceChannel_A': f'(STRING,"CONST{1 - index}")',
                'Unit': '(STRING,"V")', 'Range': '(RANGE,-10,"V",10,"V")',
                'TRION/SourceChannel_A_CONST/Const0': '(SCALAR,0.1,"V")',
                'TRION/SourceChannel_A_CONST/Const1': '(SCALAR,0.2,"V")',
            }

    def query(self, command):
        self.commands.append(command)
        if command == '*IDN?':
            return 'DEWETRON,OXYGEN,00000001,8.2'
        if command == ':CHANNELlist:NAMes?':
            return ','.join(f'({quote_scpi(cid)},{quote_scpi(name)})' for cid, name in self.channels)
        if command.startswith(':CHANNELlist:ITEM'):
            cid = command.split('ITEM', 1)[1].split(':')[0]
            return ','.join(quote_scpi(p) for p in self.properties[cid])
        if command.startswith(':CHANNELlist:PROPerty? '):
            cid, prop = parse_scpi(command.split(' ', 1)[1])
            return self.properties[cid][prop]
        raise AssertionError(f'Unexpected command: {command}')

    def write(self, command):
        pytest.fail(f'Discovery attempted a write: {command}')


@pytest.fixture
def discovery():
    outputs = copy.deepcopy(load_dispatcher_config(CONFIG).raw['outputs'])
    outputs[SUPPLY]['channel'] = 'RZ-XA demand AO 9/7, exact "name"'
    outputs[SUPPLY]['display_name'] = 'Supply demand'
    outputs[MOTOR]['channel'] = 'VESC throttle AO 2/4'
    for spec in outputs.values():
        spec.pop('expected_id', None)
        spec['const_property'] = 'auto'
    instrument = ReadOnlyInstrument(outputs)
    return AoutDiscovery(OxygenAdapter(instrument), outputs), instrument


def test_exact_names_full_ids_slots_and_active_constant_resolution(discovery):
    service, instrument = discovery
    reports = service.discover()
    assert reports[SUPPLY]['channel_id'] == '18446744073709551615'
    assert reports[MOTOR]['channel_id'] == '18446744073709551614'
    assert reports[SUPPLY]['display_name'] == 'Supply demand'
    assert reports[SUPPLY]['const_property'].endswith('/Const1')
    assert reports[MOTOR]['const_property'].endswith('/Const0')
    assert reports[SUPPLY]['current_setpoint'] == .2
    assert reports[SUPPLY]['range'] == [-10, 10]
    assert all('?' in c.split(' ', 1)[0] for c in instrument.commands)
    assert not any('NUMeric' in c for c in instrument.commands)


@pytest.mark.parametrize('prop,payload,match', [
    ('Mode', '(STRING,"FunctionGenerator")', 'Active mode'),
    ('Mode', 'NONE', 'unambiguous STRING'),
    ('TRION/SourceChannel_A', '(STRING,"CHANNEL")', 'active constant source'),
    ('TRION/SourceChannel_A', '(STRING,"CONST9")', 'active constant source'),
    ('TRION/SourceChannel_A', 'NONE', 'unambiguous STRING'),
    ('Range', '(RANGE,0,"V",0.1,"V")', 'range does not cover'),
    ('Range', '(RANGE,10,"V",-10,"V")', 'range does not cover'),
    ('Range', '(RANGE,-10,"A",10,"A")', 'RANGE in V'),
    ('Unit', '(STRING,"A")', 'unit must be V'),
    ('TRION/SourceChannel_A_CONST/Const1', '(SCALAR,nan,"V")', 'finite SCALAR'),
    ('TRION/SourceChannel_A_CONST/Const1', '(SCALAR,0,"A")', 'finite SCALAR'),
    ('TRION/SourceChannel_A_CONST/Const1', '(SCALAR,0.6,"V")', 'configured output limits'),
    ('TRION/SourceChannel_A_CONST/Const1', '(SCALAR,11,"V")', 'outside the discovered range'),
])
def test_invalid_properties_fail_closed_with_discovered_values(discovery, prop, payload, match):
    service, instrument = discovery
    instrument.properties[instrument.channels[0][0]][prop] = payload
    report = service.inspect()[SUPPLY]
    assert report['discovered_values'][prop]
    assert any(match in error for error in report['mismatches'])
    with pytest.raises(ScpiProtocolError, match=match):
        service.discover()
    assert not service.pinned


@pytest.mark.parametrize('prop', ['Mode', 'TRION/SourceChannel_A', 'TRION/SourceChannel_A_CONST/Const1'])
def test_missing_required_property_is_not_guessed(discovery, prop):
    service, instrument = discovery
    del instrument.properties[instrument.channels[0][0]][prop]
    with pytest.raises(ScpiProtocolError):
        service.discover()
    assert not service.pinned


def test_ambiguous_constant_property_is_not_guessed(discovery, monkeypatch):
    service, instrument = discovery
    query = instrument.query

    def duplicate_attributes(command):
        reply = query(command)
        if ':ATTR:NAMes?' in command:
            reply += ',"TRION/SourceChannel_A_CONST/Const1"'
        return reply

    monkeypatch.setattr(instrument, 'query', duplicate_attributes)
    with pytest.raises(ScpiProtocolError, match='candidates'):
        service.discover()


def test_manual_property_must_match_selected_source(discovery):
    service, _ = discovery
    service.outputs[SUPPLY]['const_property'] = 'TRION/SourceChannel_A_CONST/Const0'
    with pytest.raises(ScpiProtocolError, match='differs from active'):
        service.discover()
    service.outputs[SUPPLY]['const_property'] = 'TRION/SourceChannel_A_CONST/Const1'
    assert service.discover()[SUPPLY]['const_property'].endswith('/Const1')


@pytest.mark.parametrize('problem', ['missing', 'ambiguous', 'duplicate_assignment', 'expected_id'])
def test_name_and_identity_failures(discovery, problem):
    service, instrument = discovery
    cid, name = instrument.channels[0]
    if problem == 'missing':
        instrument.channels.pop(0)
    elif problem == 'ambiguous':
        instrument.channels.append(('111', name))
    elif problem == 'duplicate_assignment':
        service.outputs[MOTOR]['channel'] = name
    else:
        service.outputs[SUPPLY]['expected_id'] = '123'
    with pytest.raises(ScpiProtocolError):
        service.discover()
    assert not service.pinned


@pytest.mark.parametrize('change', ['id', 'source', 'range', 'unit', 'mode', 'property'])
def test_session_mapping_and_context_are_pinned(discovery, change):
    service, instrument = discovery
    service.discover()
    pinned = copy.deepcopy(service.pinned)
    cid, name = instrument.channels[0]
    props = instrument.properties[cid]
    if change == 'id':
        instrument.channels[0] = ('123', name)
        instrument.properties['123'] = props
    elif change == 'source':
        props['TRION/SourceChannel_A'] = '(STRING,"CONST0")'
    elif change == 'range':
        props['Range'] = '(RANGE,-5,"V",5,"V")'
    elif change == 'unit':
        props['Unit'] = '(STRING,"A")'
    elif change == 'mode':
        props['Mode'] = '(STRING,"Other")'
    else:
        del props['TRION/SourceChannel_A_CONST/Const1']
    with pytest.raises(ScpiProtocolError, match='Session mapping changed'):
        service.discover()
    assert service.pinned == pinned
    with pytest.raises(ScpiProtocolError, match='session invalidated'):
        service.discover()


def test_expected_id_is_additional_check_and_setpoint_is_not_pinned(discovery):
    service, instrument = discovery
    service.outputs[SUPPLY]['expected_id'] = instrument.channels[0][0]
    service.discover()
    instrument.properties[instrument.channels[0][0]]['TRION/SourceChannel_A_CONST/Const1'] = '(SCALAR,0.3,"V")'
    assert service.discover()[SUPPLY]['current_setpoint'] == .3


def save_config(tmp_path, raw):
    for assignment in raw['assignments']:
        profile = Path(assignment['profile'])
        if not profile.is_absolute():
            assignment['profile'] = str((CONFIG.parent / profile).resolve())
    path = tmp_path / 'bindings.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    return path


def test_schema_accepts_arbitrary_names_auto_and_optional_ids(tmp_path):
    raw = load_dispatcher_config(CONFIG).raw
    for role, spec in raw['outputs'].items():
        spec['channel'] = f'Custom slot 7 {role}'
        spec['display_name'] = role
        spec['const_property'] = 'auto'
        spec['expected_id'] = None
    assert load_dispatcher_config(save_config(tmp_path, raw)).raw['outputs'] == raw['outputs']


@pytest.mark.parametrize('field,value', [('expected_id', 123), ('expected_id', 'not-an-id'),
                                        ('channel', ''), ('display_name', 123)])
def test_schema_rejects_invalid_identity_fields(tmp_path, field, value):
    raw = load_dispatcher_config(CONFIG).raw
    raw['outputs'][SUPPLY][field] = value
    with pytest.raises((ConfigError, ScpiProtocolError, TypeError, ValueError)):
        load_dispatcher_config(save_config(tmp_path, raw))


@pytest.mark.parametrize('bad_mode', [False, True])
def test_diagnostic_reports_both_outputs_without_writes(tmp_path, monkeypatch, capsys, bad_mode):
    import oxygen_engine.aout_diagnostic as diagnostic

    raw = load_dispatcher_config(CONFIG).raw
    for role, spec in raw['outputs'].items():
        spec['channel'] = f'Physical output slot 8 {role}'
        spec['const_property'] = 'auto'
    instrument = ReadOnlyInstrument(raw['outputs'])
    if bad_mode:
        instrument.properties[instrument.channels[0][0]]['Mode'] = '(STRING,"Other")'
    instrument.connect = lambda: None
    instrument.close = lambda: None
    monkeypatch.setattr(diagnostic, 'AoutDiagnosticTransport', lambda **kwargs: instrument)
    assert main(['discover-aout', '--bindings', str(save_config(tmp_path, raw))]) == int(bad_mode)
    report = json.loads(capsys.readouterr().out)
    assert report['read_only'] and not report['physical_hardware_authorized']
    assert set(report['outputs']) == {SUPPLY, MOTOR}
    assert report['outputs'][MOTOR]['current_setpoint'] == .1
    assert all('?' in c.split(' ', 1)[0] for c in instrument.commands)


def test_diagnostic_transport_rejects_all_settings_commands():
    transport = AoutDiagnosticTransport(host='127.0.0.1', port=10001, timeout_s=.1)
    for command in ['*RST', ':ACQUisition:STARt', ':NUMeric:NORMal:ITEMS "AO"',
                    ':CHANNELlist:PROPerty "123","Mode",STRING,"ConstOutput"']:
        with pytest.raises(ScpiProtocolError):
            transport.write(command)
        with pytest.raises(ScpiProtocolError):
            transport.query(command)
