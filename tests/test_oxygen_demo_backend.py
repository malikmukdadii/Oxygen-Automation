from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from oxygen_engine.adapters.mock import MockChannel, MockOxygenServer
from oxygen_engine.dispatcher_v2_schema import OXYGEN_DEMO_OUTPUTS, load_v2_config
from oxygen_engine.dual_output_dispatcher import DualOutputDispatcher, MOTOR, SUPPLY
from oxygen_engine.main import main
from oxygen_engine.oxygen_demo_backend import OxygenDemoBackend
from oxygen_engine.scpi_transport import ScpiProtocolError

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'tests' / 'fixtures' / 'dispatcher_v2_demo_bindings.json'
CONFIRMED = [value[0] for value in OXYGEN_DEMO_OUTPUTS.values()]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


@pytest.fixture(params=[False, True], ids=['header-off', 'header-on'])
def backend(request):
    server = MockOxygenServer(response_headers=request.param)
    for role, (name, cid, prop) in OXYGEN_DEMO_OUTPUTS.items():
        source = 'CONST0' if role == SUPPLY else 'CONST1'
        channel = MockChannel(cid, name, 'aout', {'Mode': 'ConstOutput', 'Range': '10', 'Unit': 'V',
                                                'TRION/SourceChannel_A': source, prop: '0.0'})
        server._channels[cid] = channel
        server._by_name[name] = cid
    server.start()
    try:
        config = load_v2_config(CONFIG)
        config.raw['connection']['port'] = server.bound_port
        config.raw['connection']['timeout_s'] = .2
        clock, records = Clock(), []
        io = OxygenDemoBackend(config, records.append, confirmed_outputs=CONFIRMED, clock=clock)
        dispatcher = DualOutputDispatcher(config, io, records.append, clock=clock, wait=clock.wait)
        yield server, io, dispatcher, records
    finally:
        server.stop()


def test_demo_profiles_use_distinct_recorded_properties_and_preserve_supply(backend):
    server, io, dispatcher, records = backend
    commands = []
    server._on_command = commands.append
    try:
        io.connect()
        dispatcher.prepare()
        assert not any('?' not in command.split(None, 1)[0] for command in commands)
        dispatcher.execute_profile('rzxa_to_40_hold')
        supply = OXYGEN_DEMO_OUTPUTS[SUPPLY]
        motor = OXYGEN_DEMO_OUTPUTS[MOTOR]
        assert float(server._channels[supply[1]].properties[supply[2]]) == .5
        before = len(commands)
        dispatcher.execute_profile('motor_gentle')
        motor_writes = [c for c in commands[before:] if '?' not in c.split(None, 1)[0]]
        assert motor_writes and all(c.startswith(f':CHANNELlist:PROPerty "{motor[1]}","{motor[2]}"') for c in motor_writes)
        assert float(server._channels[supply[1]].properties[supply[2]]) == .5
        assert float(server._channels[motor[1]].properties[motor[2]]) == 0
        assert dispatcher.states[SUPPLY].held_by == 'rzxa_demand_ramp_40v_hold'
        assert dispatcher.cleanup()
        assert float(server._channels[supply[1]].properties[supply[2]]) == .5
        assert io.measure('dc_bus_voltage').source.startswith('FAKE ')
        dispatcher.execute_profile('rzxa_down')
        assert float(server._channels[supply[1]].properties[supply[2]]) == 0
        assert any(r['event'] == 'demo_constant_source_pinned' and r['source'] == 'CONST1' for r in records)
    finally:
        io.close()


@pytest.fixture
def confirmed_test_backend(backend):
    server, original_io, original_dispatcher, records = backend
    config = original_io.config
    channels = {
        SUPPLY: ('Test Aout2', '1758937120874954814', 'CONST4', 'TRION/SourceChannel_A_CONST/Const4'),
        MOTOR: ('Test Aout1', '1758937120874954810', 'CONST0', 'TRION/SourceChannel_A_CONST/Const0'),
    }
    for name, _, _, _ in channels.values():
        seeded_id = server._by_name.pop(name, None)
        if seeded_id is not None:
            del server._channels[seeded_id]
    for role, (name, cid, source, prop) in channels.items():
        old_name, old_id, _ = OXYGEN_DEMO_OUTPUTS[role]
        channel = server._channels[old_id]
        server._by_name.pop(old_name)
        channel.name = config.raw['outputs'][role]['channel'] = name
        for key in list(channel.properties):
            if key.startswith('TRION/SourceChannel_A_CONST/'):
                del channel.properties[key]
        channel.properties['TRION/SourceChannel_A'] = source
        channel.properties[prop] = '0.0'
        move_channel(server, name, old_id, cid)
    buttons = ['Ramp Enable', 'Ramp Disable', 'RZXA Char', 'Motor Gentle', 'Motor Stop Go', 'Motor Cruise']
    for assignment, name in zip(config.raw['assignments'], buttons):
        cid = server._by_name.pop(assignment['trigger'])
        assignment['trigger'] = server._channels[cid].name = name
        server._by_name[name] = cid
    io = OxygenDemoBackend(config, records.append, confirmed_outputs=['Test Aout2', 'Test Aout1'], clock=original_io.clock)
    dispatcher = DualOutputDispatcher(config, io, records.append, clock=original_io.clock, wait=original_dispatcher.wait)
    try:
        yield server, io, dispatcher, records
    finally:
        io.close()


def test_confirmed_fixture_has_one_channel_per_configured_name(confirmed_test_backend):
    server, io, _, _ = confirmed_test_backend
    for spec in io.config.raw['outputs'].values():
        matches = [channel for channel in server._channels.values() if channel.name == spec['channel']]
        assert len(matches) == 1
        assert server._by_name[spec['channel']] == matches[0].channel_id


def test_confirmed_setup_still_rejects_duplicate_channel_names(confirmed_test_backend):
    server, io, _, _ = confirmed_test_backend
    cid = '18446744073709551003'
    server._channels[cid] = MockChannel(cid, 'Test Aout2', 'aout', {})
    with pytest.raises(ScpiProtocolError, match='exactly one channel'):
        io.connect()
    assert not io.authorized and not io.transport.capabilities


def test_confirmed_const4_setup_preserves_supply_and_returns_motor_neutral(confirmed_test_backend):
    server, io, dispatcher, records = confirmed_test_backend
    commands = []
    server._on_command = commands.append
    io.connect()
    dispatcher.prepare()
    assert io.constant_property(SUPPLY) == 'TRION/SourceChannel_A_CONST/Const4'
    assert io.constant_property(MOTOR) == 'TRION/SourceChannel_A_CONST/Const0'
    assert all('?' in c.split(None, 1)[0] for c in commands)
    assert io.read_button('Ramp Enable') is False
    dispatcher.execute_profile('rzxa_to_40_hold')
    before = len(commands)
    dispatcher.execute_profile('motor_gentle')
    motor_writes = [c for c in commands[before:] if '?' not in c.split(None, 1)[0]]
    assert motor_writes and all(c.startswith(
        ':CHANNELlist:PROPerty "1758937120874954810","TRION/SourceChannel_A_CONST/Const0",'
    ) for c in motor_writes)
    assert dispatcher.states[SUPPLY].value == .5
    assert dispatcher.states[MOTOR].value == 0
    assert any(r['event'] == 'oxygen_demo_connected' and
               r['operator_confirmed_simulated_outputs'] == ['Test Aout1', 'Test Aout2'] for r in records)
    assert dispatcher.cleanup()
    assert float(server._channels[io.channel_id(SUPPLY)].properties[io.constant_property(SUPPLY)]) == .5
    dispatcher.execute_profile('rzxa_down')
    assert dispatcher.states[SUPPLY].value == 0


def test_confirmed_name_does_not_authorize_another_constant_property(confirmed_test_backend):
    server, io, _, _ = confirmed_test_backend
    channel = server._channels['1758937120874954814']
    del channel.properties['TRION/SourceChannel_A_CONST/Const4']
    channel.properties['TRION/SourceChannel_A'] = 'CONST3'
    channel.properties['TRION/SourceChannel_A_CONST/Const3'] = '0.0'
    with pytest.raises(ScpiProtocolError, match='unapproved properties'):
        io.connect()
    assert not io.authorized and not io.transport.capabilities


@pytest.mark.parametrize('confirmed', [[], ['Test Aout2'], ['Test Aout1'], CONFIRMED])
def test_new_mapping_requires_both_selected_name_confirmations(confirmed):
    config = load_v2_config(CONFIG)
    config.raw['outputs'][SUPPLY]['channel'] = 'Test Aout2'
    config.raw['outputs'][MOTOR]['channel'] = 'Test Aout1'
    with pytest.raises(PermissionError, match='Confirm BOTH'):
        OxygenDemoBackend(config, lambda r: None, confirmed_outputs=confirmed)


def test_cli_accepts_confirmations_for_the_selected_approved_names(tmp_path, monkeypatch):
    import socket

    raw = load_v2_config(CONFIG).raw
    raw['outputs'][SUPPLY]['channel'] = 'Test Aout2'
    raw['outputs'][MOTOR]['channel'] = 'Test Aout1'
    for assignment in raw['assignments']:
        assignment['profile'] = str((CONFIG.parent / assignment['profile']).resolve())
    path = tmp_path / 'confirmed-demo.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('No connection is permitted'))

    def without_execution(dispatcher):
        assert dispatcher.backend.output_names == {SUPPLY: 'Test Aout2', MOTOR: 'Test Aout1'}
        assert not dispatcher.backend.connected
        return 0

    monkeypatch.setattr(DualOutputDispatcher, 'run', without_execution)
    assert main(['dispatch-patterns', '--bindings', str(path), '--execute', '--confirm-simulated-output',
                 '--confirm-demo-output', 'Test Aout2', '--confirm-demo-output', 'Test Aout1',
                 '--log-file', str(tmp_path / 'demo.jsonl')]) == 0


@pytest.mark.parametrize('names', [('Test Aout1', 'Test Aout2'), ('Other simulated AO', 'Test Aout1')])
def test_confirmations_cannot_approve_wrong_roles_or_unknown_names(names):
    config = load_v2_config(CONFIG)
    config.raw['outputs'][SUPPLY]['channel'], config.raw['outputs'][MOTOR]['channel'] = names
    with pytest.raises(PermissionError, match='explicitly approved'):
        OxygenDemoBackend(config, lambda r: None, confirmed_outputs=names)


def test_current_source_changes_are_not_silently_adopted(backend):
    server, io, dispatcher, _ = backend
    try:
        io.connect()
        dispatcher.prepare()
        server._channels[OXYGEN_DEMO_OUTPUTS[MOTOR][1]].properties['TRION/SourceChannel_A'] = 'CONST0'
        with pytest.raises(ScpiProtocolError, match='context changed'):
            dispatcher.verify(MOTOR)
        assert not io.authorized and not io.transport.capabilities
        server._channels[OXYGEN_DEMO_OUTPUTS[MOTOR][1]].properties['TRION/SourceChannel_A'] = 'CONST1'
        with pytest.raises(ScpiProtocolError, match='session invalidated'):
            io.discover()
        with pytest.raises(PermissionError):
            io.write(MOTOR, 0)
    finally:
        io.close()


def test_digital_reads_work_but_digital_writes_and_mode_changes_are_forbidden(backend):
    server, io, dispatcher, _ = backend
    try:
        io.connect()
        dispatcher.prepare()
        server._channels[server._by_name['DO 3/4 Sim']].properties['DO_Value'] = '1'
        assert io.read_button('DO 3/4 Sim') is True
        for command in [':ACQUisition:STARt', ':STORe:STARt', '*RST',
                        ':CHANNELlist:PROPerty "8898268430164033618","Mode",STRING,"ConstOutput"']:
            with pytest.raises(ScpiProtocolError):
                io.transport.write(command)
        with pytest.raises(ScpiProtocolError):
            io.transport.write(':CHANNELlist:PROPerty "8898268430164033618","TRION/SourceChannel_A_CONST/Const1",SCALAR,0.5,"V"')
        with pytest.raises(ScpiProtocolError):
            io.transport.reconnect()
    finally:
        io.close()


def test_missing_exact_channel_name_refuses_before_writes(backend):
    server, io, _, _ = backend
    commands = []
    server._on_command = commands.append
    server._channels[OXYGEN_DEMO_OUTPUTS[MOTOR][1]].name = 'Different name'
    try:
        with pytest.raises(ScpiProtocolError, match='exactly one channel'):
            io.connect()
        assert all('?' in c.split(None, 1)[0] or c == '*IDN?' for c in commands)
    finally:
        io.close()


@pytest.mark.parametrize('name', ['DO 9/8', 'Chassis Controller DOUT 1', 'Start "Run", rack A'])
def test_arbitrary_dout_names_are_discovered_and_read_only(backend, name):
    server, io, dispatcher, records = backend
    commands = []
    server._on_command = commands.append
    assignment = io.config.raw['assignments'][0]
    cid = server._by_name.pop(assignment['trigger'])
    assignment['trigger'] = name
    server._channels[cid].name = name
    server._by_name[name] = cid
    try:
        io.connect()
        dispatcher.prepare()
        assert io.do_channels[name] == cid
        server._channels[cid].properties['DO_Value'] = '0'
        assert io.read_button(name) is False
        server._channels[cid].properties['DO_Value'] = '1'
        assert io.read_button(name) is True
        assert any(r['event'] == 'do_channel_resolved' and r['channel'] == name and
                   r['channel_id'] == cid and r['access'] == 'read_only' for r in records)
        assert all('?' in c.split(None, 1)[0] for c in commands)
        with pytest.raises(ScpiProtocolError):
            io.transport.write(f':CHANNELlist:PROPerty "{cid}","DO_Value",BOOL,ON')
        assert (cid, 'DO_Value') not in io.transport.capabilities
    finally:
        io.close()


@pytest.mark.parametrize('problem', ['missing', 'ambiguous', 'missing_property', 'not_boolean', 'aout_alias'])
def test_custom_dout_discovery_fails_closed(backend, monkeypatch, problem):
    from oxygen_engine.adapters.oxygen import PropertyValue

    server, io, _, _ = backend
    commands = []
    server._on_command = commands.append
    assignment = io.config.raw['assignments'][0]
    cid = server._by_name.pop(assignment['trigger'])
    name = assignment['trigger'] = 'Chassis Controller DOUT 1'
    server._channels[cid].name = name
    server._by_name[name] = cid
    if problem == 'missing':
        server._channels[cid].name = 'Another channel'
    elif problem == 'ambiguous':
        other_id = '18446744073709551500'
        server._channels[other_id] = MockChannel(other_id, name, 'do', {'DO_Value': '0'})
    elif problem == 'missing_property':
        del server._channels[cid].properties['DO_Value']
    elif problem == 'not_boolean':
        get_value = io.adapter.get_property_value

        def non_boolean(channel_id, prop):
            if channel_id == cid and prop == 'DO_Value':
                return PropertyValue('STRING', ('ON',))
            return get_value(channel_id, prop)

        monkeypatch.setattr(io.adapter, 'get_property_value', non_boolean)
    else:
        assignment['trigger'] = OXYGEN_DEMO_OUTPUTS[SUPPLY][0]
    try:
        with pytest.raises(ScpiProtocolError):
            io.connect()
        assert not io.authorized and not io.transport.capabilities
        assert all('?' in c.split(None, 1)[0] for c in commands)
    finally:
        io.close()


@pytest.mark.parametrize('confirmed', [[], ['AO 3/1 Sim'], ['AO 3/2 Sim'], ['AO 3/1 Sim', 'Unverified output']])
def test_both_per_channel_confirmations_are_required(confirmed):
    config = load_v2_config(CONFIG)
    with pytest.raises(PermissionError):
        OxygenDemoBackend(config, lambda r: None, confirmed_outputs=confirmed)


def test_demo_preview_is_offline_and_scenario_cannot_reach_oxygen(monkeypatch, capsys):
    module = importlib.import_module('oxygen_engine.oxygen_demo_backend')
    monkeypatch.setattr(module, 'OxygenDemoBackend', lambda *a, **k: pytest.fail('No connection authorized'))
    assert main(['dispatch-patterns', '--bindings', str(CONFIG)]) == 0
    assert 'oxygen_demo' in capsys.readouterr().out
    assert main(['dispatch-patterns', '--bindings', str(CONFIG), '--execute', '--confirm-simulated-output']) == 1
    assert main(['dispatch-patterns', '--bindings', str(CONFIG), '--scenario', 'not-used.json']) == 1
    assert main(['dispatch-patterns', '--bindings', str(CONFIG), '--live']) == 1


def move_channel(server, name, old_id, new_id):
    channel = server._channels.pop(old_id)
    channel.channel_id = new_id
    server._channels[new_id] = channel
    server._by_name[name] = new_id


def test_different_instance_ids_are_discovered_pinned_and_used_for_writes(backend):
    server, io, dispatcher, records = backend
    commands = []
    server._on_command = commands.append
    new_ids = {SUPPLY: '18446744073709551001', MOTOR: '18446744073709551002'}
    for role, (name, old_id, _) in OXYGEN_DEMO_OUTPUTS.items():
        move_channel(server, name, old_id, new_ids[role])
    try:
        io.connect()
        dispatcher.prepare()
        assert io.channel_ids == new_ids
        assert {r['channel_id'] for r in records if r['event'] == 'demo_channel_resolved'} == set(new_ids.values())
        dispatcher.execute_profile('rzxa_to_40_hold')
        before = len(commands)
        dispatcher.execute_profile('motor_gentle')
        motor_writes = [c for c in commands[before:] if '?' not in c.split(None, 1)[0]]
        assert motor_writes and all(c.startswith(f':CHANNELlist:PROPerty "{new_ids[MOTOR]}",') for c in motor_writes)
        assert dispatcher.states[SUPPLY].value == .5
        assert dispatcher.states[MOTOR].value == 0
        old_id = OXYGEN_DEMO_OUTPUTS[MOTOR][1]
        prop = OXYGEN_DEMO_OUTPUTS[MOTOR][2]
        with pytest.raises(ScpiProtocolError):
            io.transport.write(f':CHANNELlist:PROPerty "{old_id}","{prop}",SCALAR,0.1,"V"')
        with pytest.raises(ScpiProtocolError):
            io.transport.query(f':CHANNELlist:PROPerty? "{old_id}","{prop}"')
    finally:
        io.close()


def test_duplicate_name_refuses_all_writes(backend):
    server, io, _, _ = backend
    server._channels['18446744073709551003'] = MockChannel('18446744073709551003', 'AO 3/1 Sim', 'aout', {})
    try:
        with pytest.raises(ScpiProtocolError, match='exactly one channel'):
            io.connect()
        assert not io.authorized and not io.transport.capabilities
    finally:
        io.close()


def test_session_channel_replacement_is_refused_not_rebound(backend):
    server, io, dispatcher, _ = backend
    try:
        io.connect()
        dispatcher.prepare()
        name, old_id, prop = OXYGEN_DEMO_OUTPUTS[MOTOR]
        new_id = '18446744073709551004'
        move_channel(server, name, old_id, new_id)
        with pytest.raises(ScpiProtocolError, match='Session mapping changed'):
            dispatcher.verify(MOTOR)
        assert io.channel_id(MOTOR) == old_id
        with pytest.raises(ScpiProtocolError):
            io.transport.authorize(MOTOR, new_id, prop, 0, .4)
    finally:
        io.close()


def test_discovered_role_id_collision_cannot_be_bound(backend):
    _, io, _, _ = backend
    try:
        io.transport.connect()
        with pytest.raises(ScpiProtocolError, match='collide'):
            io.transport.bind_outputs({role: ('777', value[2]) for role, value in OXYGEN_DEMO_OUTPUTS.items()})
        assert not io.transport.capabilities and not io.transport.output_bindings
    finally:
        io.close()


def test_duplicate_expected_ids_are_rejected_by_demo_schema(tmp_path):
    raw = json.loads(CONFIG.read_text(encoding='utf-8'))
    for spec in raw['outputs'].values():
        spec['expected_id'] = '123'
    for assignment in raw['assignments']:
        assignment['profile'] = str((CONFIG.parent / assignment['profile']).resolve())
    path = tmp_path / 'old-demo-config.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ValueError, match='cannot alias'):
        load_v2_config(path)


def test_mock_bindings_do_not_read_operator_configuration(monkeypatch):
    read_text = Path.read_text
    working_config = ROOT / 'dispatcher.v2.oxygen-demo.json'

    def isolated_read(path, *args, **kwargs):
        if path.resolve() == working_config.resolve():
            pytest.fail('Mock tests must not load operator bindings')
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', isolated_read)
    config = load_v2_config(CONFIG)
    assert {role: spec['channel'] for role, spec in config.raw['outputs'].items()} == {
        role: output[0] for role, output in OXYGEN_DEMO_OUTPUTS.items()
    }
    assert [a['trigger'] for a in config.raw['assignments']] == [f'DO 3/{i} Sim' for i in range(1, 7)]


def test_expected_id_is_checked_against_discovery(backend):
    _, io, _, _ = backend
    io.config.raw['outputs'][MOTOR]['expected_id'] = '123'
    try:
        with pytest.raises(ScpiProtocolError, match='expected_id'):
            io.connect()
        assert not io.authorized and not io.transport.capabilities
    finally:
        io.close()


def test_auto_properties_follow_sources_not_role_or_channel_number(backend):
    server, io, dispatcher, _ = backend
    for role, (_, cid, old_prop) in OXYGEN_DEMO_OUTPUTS.items():
        index = 1 if role == SUPPLY else 0
        channel = server._channels[cid]
        channel.properties.pop(old_prop)
        channel.properties['TRION/SourceChannel_A'] = f'CONST{index}'
        channel.properties[f'TRION/SourceChannel_A_CONST/Const{index}'] = '0.0'
        io.config.raw['outputs'][role]['const_property'] = 'auto'
    try:
        io.connect()
        dispatcher.prepare()
        dispatcher.execute_profile('rzxa_to_40_hold')
        dispatcher.execute_profile('motor_gentle')
        assert io.constant_property(SUPPLY).endswith('/Const1')
        assert io.constant_property(MOTOR).endswith('/Const0')
        assert dispatcher.states[SUPPLY].value == .5
        assert dispatcher.states[MOTOR].value == 0
    finally:
        io.close()


def test_configured_physical_name_cannot_obtain_demo_write_binding(backend):
    server, io, _, _ = backend
    name = 'AO 8/7 Physical voltage demand'
    io.config.raw['outputs'][SUPPLY]['channel'] = name
    server._channels[OXYGEN_DEMO_OUTPUTS[SUPPLY][1]].name = name
    try:
        with pytest.raises(ScpiProtocolError, match='approved simulated-channel names'):
            io.connect()
        assert not io.authorized and not io.transport.capabilities
    finally:
        io.close()
