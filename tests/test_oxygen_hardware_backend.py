
import json
from pathlib import Path

import pytest

from oxygen_engine.adapters.mock import MockChannel, MockOxygenServer
from oxygen_engine.dispatcher_v2_schema import load_v2_config
from oxygen_engine.dual_output_dispatcher import DualOutputDispatcher, MOTOR, SUPPLY
from oxygen_engine.hardware_schema import HARDWARE_ACKNOWLEDGEMENTS
from oxygen_engine.main import main
from oxygen_engine.oxygen_hardware_backend import HardwareSample, OxygenHardwareBackend, OxygenHardwareTransport
from oxygen_engine.scpi_transport import ScpiProtocolError
from oxygen_engine.simulation_fixture import SensorSample

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'dispatcher.v2.hardware.example.json'
CONFIRMED = ['Physical supply AO 8/7', 'Controller throttle AO 2']


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def hardware_raw():
    raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    raw['hardware_authorization'] = {key: True for key in HARDWARE_ACKNOWLEDGEMENTS}
    raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] = 80.0
    raw['outputs'][SUPPLY]['channel'] = CONFIRMED[0]
    raw['outputs'][MOTOR]['channel'] = CONFIRMED[1]
    for name, spec in raw['measurement_roles'].items():
        spec['channel'] = 'Measured DC bus' if name == 'dc_bus_voltage' else 'External verified ' + name
        spec['mapping_verified'] = True
        if name != 'dc_bus_voltage':
            spec['property'] = 'DO_Value'
    for assignment in raw['assignments']:
        assignment['profile'] = str(ROOT / assignment['profile'])
    return raw


def save(tmp_path, raw, filename='hardware.local.json'):
    path = tmp_path / filename
    path.write_text(json.dumps(raw), encoding='utf-8')
    return path


def output_only_raw():
    raw = hardware_raw()
    raw['hardware_mode'] = 'output_only'
    raw['output_only_authorization'] = {
        'no_live_feedback_acknowledged': True,
        'external_bus_and_regen_protection_verified': True,
    }
    raw['measurement_roles'] = {}
    raw['interlocks']['motor_requires_measured_bus_in_range'] = False
    for assignment in raw['assignments']:
        assignment['profile'] = str(ROOT / 'profiles/hardware-output-only' / Path(assignment['profile']).name)
    return raw


@pytest.fixture
def output_only_backend(physical_backend, tmp_path):
    server, old_backend, _, records = physical_backend
    for spec in old_backend.config.raw['measurement_roles'].values():
        cid = server._by_name.pop(spec['channel'])
        del server._channels[cid]
    raw = output_only_raw()
    raw['connection'] = old_backend.config.raw['connection'].copy()
    config = load_v2_config(save(tmp_path, raw, 'output-only.local.json'))
    clock = old_backend.clock
    backend = OxygenHardwareBackend(config, records.append, confirmed_outputs=CONFIRMED, live=True,
                                    interlocks_confirmed=True, clock=clock)
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                                      timeline=boundary_timeline)
    try:
        yield server, backend, dispatcher, records
    finally:
        backend.close()


def test_output_only_profiles_work_without_any_sensor_channels(output_only_backend, monkeypatch):
    server, backend, dispatcher, records = output_only_backend
    commands = []
    server._on_command = commands.append
    monkeypatch.setattr(backend, 'measure', lambda name: pytest.fail('Output-only must never read feedback'))
    backend.connect()
    dispatcher.prepare()
    assert not any(c.startswith(':CHANNELlist:PROPerty ') for c in commands)
    assert all(key.startswith('trigger:') for key in backend.input_ids)
    assert backend.transport.numeric_name is None
    before = backend.writes_attempted
    with pytest.raises(RuntimeError, match='Supply demand readback'):
        dispatcher.execute_profile('motor_gentle')
    assert backend.writes_attempted == before
    dispatcher.execute_profile('rzxa_to_40_hold')
    dispatcher.execute_profile('motor_gentle')
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.states[SUPPLY].value == .5
    assert dispatcher.cleanup()
    assert dispatcher.states[SUPPLY].value == .5
    dispatcher.execute_profile('rzxa_down')
    assert dispatcher.states[SUPPLY].value == 0
    assert not any(c.startswith(':NUMeric:') or c == ':ACQUisition:STATe?' for c in commands)
    assert not any(r['event'] == 'independent_feedback' for r in records)
    assert any(r['event'] == 'output_only_warning' for r in records)


def test_output_only_refuses_to_fabricate_measurements(output_only_backend):
    _, backend, dispatcher, _ = output_only_backend
    with pytest.raises(PermissionError, match='no live measurements'):
        backend.measure('dc_bus_voltage')
    with pytest.raises(RuntimeError, match='cannot satisfy'):
        dispatcher.sample('dc_bus_voltage')


def test_output_only_non_neutral_startup_never_authorizes(output_only_backend):
    server, backend, dispatcher, _ = output_only_backend
    server._channels[server._by_name[CONFIRMED[1]]].properties['TRION/SourceChannel_A_CONST/Const2'] = '.1'
    backend.connect()
    with pytest.raises(RuntimeError, match='Startup motor is not neutral'):
        dispatcher.prepare()
    assert not backend.authorized and not backend.transport.capabilities
    assert backend.writes_attempted == 0


@pytest.mark.parametrize('problem', ['output_source', 'trigger_id'])
def test_output_only_identity_changes_revoke_capabilities(output_only_backend, problem):
    server, backend, dispatcher, _ = output_only_backend
    backend.connect()
    dispatcher.prepare()
    if problem == 'output_source':
        server._channels[backend.channel_id(SUPPLY)].properties['TRION/SourceChannel_A'] = 'CONST1'
    else:
        old = next(iter(backend.input_ids.values()))[1]
        channel = server._channels.pop(old)
        channel.channel_id = '777'
        server._channels['777'] = channel
        server._by_name[channel.name] = '777'
    with pytest.raises(ScpiProtocolError):
        backend.discover()
    assert not backend.authorized and not backend.transport.capabilities


@pytest.mark.parametrize('problem', ['missing_ack', 'missing_external_protection', 'feedback_profile',
                                      'feedback_precondition', 'measurement_present', 'wrong_interlock',
                                      'unknown_mode', 'not_explicit', 'missing_physical_ack'])
def test_output_only_schema_requires_explicit_consistent_configuration(tmp_path, problem):
    raw = output_only_raw()
    if problem == 'missing_ack':
        raw['output_only_authorization']['no_live_feedback_acknowledged'] = False
    elif problem == 'missing_external_protection':
        raw['output_only_authorization']['external_bus_and_regen_protection_verified'] = False
    elif problem == 'feedback_profile':
        raw['assignments'][2]['profile'] = str(ROOT / 'profiles/hardware/motor_gentle.json')
    elif problem == 'feedback_precondition':
        profile = json.loads(Path(raw['assignments'][0]['profile']).read_text())
        profile['steps'][0]['params']['supply_output_enabled_or_verified_external'] = True
        raw['assignments'][0]['profile'] = str(save(tmp_path, profile, 'bad-profile.json'))
    elif problem == 'measurement_present':
        raw['measurement_roles'] = hardware_raw()['measurement_roles']
    elif problem == 'wrong_interlock':
        raw['interlocks']['motor_requires_measured_bus_in_range'] = True
    elif problem == 'unknown_mode':
        raw['hardware_mode'] = 'anything'
    elif problem == 'not_explicit':
        del raw['hardware_mode']
        del raw['output_only_authorization']
    else:
        raw['hardware_authorization']['external_emergency_stop_verified'] = False
    with pytest.raises(ValueError):
        load_v2_config(save(tmp_path, raw))


def test_output_only_measurement_roles_can_be_omitted(tmp_path):
    raw = output_only_raw()
    del raw['measurement_roles']
    assert load_v2_config(save(tmp_path, raw)).raw['hardware_mode'] == 'output_only'


def test_output_only_offline_preview_does_not_connect(tmp_path, monkeypatch, capsys):
    import socket

    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('Offline preview must not connect'))
    path = save(tmp_path, output_only_raw())
    assert main(['dispatch-patterns', '--bindings', str(path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['hardware_mode'] == 'output_only'
    assert plan['bus_feedback'].startswith('NONE:')
    assert plan['physical_actuation'] is False


def boundary_timeline(pattern, setpoint, verify, emit, **kwargs):
    for segment in pattern.segments:
        setpoint(segment.end_v)
        verify(segment.end_v)
        kwargs['wait'](segment.end_s - segment.start_s)
    return {'actual_elapsed_s': pattern.duration_s}


@pytest.fixture(params=[False, True], ids=['header-off', 'header-on'])
def physical_backend(tmp_path, request):
    raw = hardware_raw()
    server = MockOxygenServer(response_headers=request.param)
    for cid, channel in list(server._channels.items()):
        if channel.channel_type == 'aout':
            del server._channels[cid]
            server._by_name.pop(channel.name, None)
    output_ids = {SUPPLY: '18446744073709551614', MOTOR: '18446744073709551613'}
    for index, role in enumerate((SUPPLY, MOTOR)):
        spec = raw['outputs'][role]
        constant = 7 if role == SUPPLY else 2
        cid = output_ids[role]
        props = {'Mode': 'ConstOutput', 'Range': '10', 'Unit': 'V',
                 'TRION/SourceChannel_A': f'CONST{constant}',
                 f'TRION/SourceChannel_A_CONST/Const{constant}': '0.0'}
        server._channels[cid] = MockChannel(cid, spec['channel'], 'aout', props)
        server._by_name[spec['channel']] = cid
    for index, (name, spec) in enumerate(raw['measurement_roles'].items()):
        cid = str(8000 + index)
        if name == 'dc_bus_voltage':
            channel = MockChannel(cid, spec['channel'], 'measurement', {'Unit': 'V'}, measurement=40.0)
        else:
            channel = MockChannel(cid, spec['channel'], 'digital_output', {'DO_Value': '0' if name == 'supply_disabled' else '1'})
        server._channels[cid] = channel
        server._by_name[spec['channel']] = cid
    for index, assignment in enumerate(raw['assignments']):
        cid = str(9000 + index)
        server._channels[cid] = MockChannel(cid, assignment['trigger'], 'digital_output', {'DO_Value': '0'})
        server._by_name[assignment['trigger']] = cid
    server.start()
    raw['connection']['port'] = server.bound_port
    raw['connection']['timeout_s'] = .2
    config = load_v2_config(save(tmp_path, raw))
    clock, records = Clock(), []
    backend = OxygenHardwareBackend(config, records.append, confirmed_outputs=CONFIRMED, live=True,
                                    interlocks_confirmed=True, clock=clock)
    dispatcher = DualOutputDispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                                      timeline=boundary_timeline)
    try:
        yield server, backend, dispatcher, records
    finally:
        backend.close()
        server.stop()


def test_hardware_schema_and_verified_33v_ceiling(tmp_path):
    raw = hardware_raw()
    raw['outputs'][MOTOR]['max_aout_v'] = 3.3
    config = load_v2_config(save(tmp_path, raw))
    assert config.raw['simulation_only'] is False
    assert config.raw['outputs'][MOTOR]['neutral_aout_v'] == 0
    raw['outputs'][MOTOR]['max_aout_v'] = 3.3001
    with pytest.raises(ValueError):
        load_v2_config(save(tmp_path, raw))


@pytest.mark.parametrize('problem', ['missing_ack', 'fake_telemetry', 'no_bus', 'unverified_bus', 'fake_permission',
                                      'nonzero_neutral', 'unknown_gain', 'gain_target_mismatch', 'simulation_profile'])
def test_hardware_schema_fails_closed(tmp_path, problem):
    raw = hardware_raw()
    if problem == 'missing_ack':
        raw['hardware_authorization']['external_emergency_stop_verified'] = False
    elif problem == 'fake_telemetry':
        raw['simulation'] = {'telemetry': {'dc_bus_voltage': 40}}
    elif problem == 'no_bus':
        raw['measurement_roles']['dc_bus_voltage']['channel'] = None
    elif problem == 'unverified_bus':
        raw['measurement_roles']['dc_bus_voltage']['mapping_verified'] = False
    elif problem == 'fake_permission':
        raw['outputs'][MOTOR]['authorization'] = {'simulation': True, 'physical': False}
    elif problem == 'nonzero_neutral':
        raw['outputs'][MOTOR]['neutral_aout_v'] = .1
    elif problem == 'unknown_gain':
        raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] = None
    elif problem == 'gain_target_mismatch':
        raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] = 1
    else:
        raw['assignments'][0]['profile'] = str(ROOT / 'profiles/rzxa_demand_ramp_40v_hold.json')
    with pytest.raises(ValueError):
        load_v2_config(save(tmp_path, raw))


@pytest.mark.parametrize('live,interlocks,names', [(False, True, CONFIRMED), (True, False, CONFIRMED),
                                                   (True, True, []), (True, True, CONFIRMED[:1])])
def test_physical_authorization_is_required_before_any_connection(tmp_path, monkeypatch, live, interlocks, names):
    import socket

    config = load_v2_config(save(tmp_path, hardware_raw()))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('Must not connect'))
    with pytest.raises(PermissionError):
        OxygenHardwareBackend(config, lambda r: None, confirmed_outputs=names, live=live, interlocks_confirmed=interlocks)


def test_hardware_profiles_use_discovered_properties_and_real_feedback(physical_backend):
    server, backend, dispatcher, records = physical_backend
    commands = []
    server._on_command = commands.append
    backend.connect()
    dispatcher.prepare()
    assert backend.constant_property(SUPPLY).endswith('/Const7')
    assert backend.constant_property(MOTOR).endswith('/Const2')
    assert not any(c.startswith(':CHANNELlist:PROPerty ') for c in commands)
    sample = backend.measure('dc_bus_voltage')
    assert sample.value == 40 and sample.simulated is False and sample.source.startswith('OXYGEN ')
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == .5
    before = len(commands)
    dispatcher.execute_profile('motor_gentle')
    writes = [c for c in commands[before:] if c.startswith(':CHANNELlist:PROPerty ')]
    assert writes and all('"18446744073709551613","TRION/SourceChannel_A_CONST/Const2"' in c for c in writes)
    assert dispatcher.states[SUPPLY].value == .5 and dispatcher.states[MOTOR].value == 0
    assert dispatcher.cleanup()
    assert dispatcher.states[SUPPLY].value == .5
    dispatcher.execute_profile('rzxa_down')
    assert dispatcher.states[SUPPLY].value == 0
    assert any(r['event'] == 'preflight_passed' and r['simulated'] is False for r in records)


def test_bus_excursion_refuses_motor_writes(physical_backend):
    server, backend, dispatcher, _ = physical_backend
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('rzxa_to_40_hold')
    bus = backend.config.raw['measurement_roles']['dc_bus_voltage']['channel']
    server._channels[server._by_name[bus]].measurement = 45
    before = backend.writes_attempted
    with pytest.raises(RuntimeError, match='outside'):
        dispatcher.execute_profile('motor_gentle')
    assert backend.writes_attempted == before
    assert dispatcher.states[SUPPLY].value == .5


@pytest.mark.parametrize('sample', [SensorSample(40, 0), HardwareSample(40, -2, 'OXYGEN 8000/numeric')])
def test_hardware_dispatcher_rejects_fake_or_stale_samples(physical_backend, monkeypatch, sample):
    _, backend, dispatcher, _ = physical_backend
    monkeypatch.setattr(backend, 'measure', lambda name: sample)
    with pytest.raises(RuntimeError, match='untrusted'):
        dispatcher.sample('dc_bus_voltage')


@pytest.mark.parametrize('problem', ['source', 'id', 'input_id'])
def test_session_mapping_changes_revoke_physical_capabilities(physical_backend, problem):
    server, backend, dispatcher, _ = physical_backend
    backend.connect()
    dispatcher.prepare()
    if problem == 'source':
        server._channels[backend.channel_id(SUPPLY)].properties['TRION/SourceChannel_A'] = 'CONST1'
    else:
        if problem == 'id':
            old = backend.channel_id(SUPPLY)
        else:
            old = backend.input_ids['sensor:dc_bus_voltage'][1]
        channel = server._channels.pop(old)
        channel.channel_id = '777'
        server._channels['777'] = channel
        server._by_name[channel.name] = '777'
    with pytest.raises(ScpiProtocolError):
        backend.discover()
    assert not backend.authorized and not backend.transport.capabilities
    with pytest.raises(PermissionError):
        backend.write(MOTOR, .1)


def test_unknown_bus_channel_refuses_startup_without_aout_writes(physical_backend):
    server, backend, _, _ = physical_backend
    bus = backend.config.raw['measurement_roles']['dc_bus_voltage']['channel']
    server._channels[server._by_name[bus]].name = 'Renamed bus'
    commands = []
    server._on_command = commands.append
    with pytest.raises(ScpiProtocolError):
        backend.connect()
    assert not backend.transport.capabilities
    assert not any(c.startswith(':CHANNELlist:PROPerty ') for c in commands)


def test_non_neutral_hardware_startup_never_authorizes_outputs(physical_backend):
    server, backend, dispatcher, _ = physical_backend
    name = backend.config.raw['outputs'][MOTOR]['channel']
    server._channels[server._by_name[name]].properties['TRION/SourceChannel_A_CONST/Const2'] = '.1'
    commands = []
    server._on_command = commands.append
    backend.connect()
    with pytest.raises(RuntimeError, match='Startup motor is not neutral'):
        dispatcher.prepare()
    assert not backend.authorized and not backend.transport.capabilities
    assert not any(c.startswith(':CHANNELlist:PROPerty ') for c in commands)


def test_lost_supply_readiness_blocks_demand_but_not_motor_neutral(physical_backend):
    server, backend, dispatcher, _ = physical_backend
    backend.connect()
    dispatcher.prepare()
    ready = backend.config.raw['measurement_roles']['supply_external_ready']['channel']
    server._channels[server._by_name[ready]].properties['DO_Value'] = '0'
    before = backend.writes_attempted
    with pytest.raises(PermissionError, match='supply_external_ready'):
        backend.write(SUPPLY, .1)
    assert backend.writes_attempted == before
    backend.write(MOTOR, 0)
    assert backend.writes_attempted == before + 1


def test_hardware_transport_rejects_unrelated_or_unauthorized_writes():
    transport = OxygenHardwareTransport(host='127.0.0.1', port=10001, timeout_s=.1, emit=lambda r: None)
    for command in ['*RST', ':STORe:STARt', ':ACQUisition:STARt',
                    ':CHANNELlist:PROPerty "123","Mode",STRING,"ConstOutput"',
                    ':CHANNELlist:PROPerty "123","DO_Value",BOOL,ON',
                    ':CHANNELlist:PROPerty "123","TRION/SourceChannel_A_CONST/Const4",SCALAR,0.1,"V"']:
        with pytest.raises(ScpiProtocolError):
            transport.write(command)
    with pytest.raises(ScpiProtocolError):
        transport.reconnect()


def test_hardware_cli_preview_is_offline_and_execution_requires_hardware_flags(tmp_path, monkeypatch, capsys):
    import socket

    path = save(tmp_path, hardware_raw())
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('No network is permitted'))
    assert main(['dispatch-patterns', '--bindings', str(path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['physical_actuation'] is False and plan['physical_actuation_requested'] is False
    assert plan['bus_feedback'] == 'real independent OXYGEN measurement'
    assert main(['dispatch-patterns', '--bindings', str(path), '--execute']) == 1
    assert main(['dispatch-patterns', '--bindings', str(path), '--execute', '--confirm-simulated-output']) == 1
    assert main(['dispatch-patterns', '--bindings', str(path), '--scenario', 'not-used.json']) == 1


def test_hardware_template_cannot_execute_without_commissioning(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    with pytest.raises(ValueError, match='Explicitly verify'):
        load_v2_config(save(tmp_path, raw))
