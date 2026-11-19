"""Physical dispatcher path with scripted OXYGEN SCPI only; never opens sockets."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from oxygen_engine.dispatcher import BRAKE, MOTOR, SUPPLY, Dispatcher, configuration_summary
from oxygen_engine.errors import AuthorizationError, ConfigError
from oxygen_engine.memory_backend import MemoryBackend
from oxygen_engine.oxygen_backend import OxygenBackend
from oxygen_engine.schema import confirm_outputs, load_dispatcher_config
from oxygen_engine.scpi_transport import parse_scpi, quote_scpi

ROOT = Path(__file__).resolve().parents[1]
PHYSICAL = ROOT / 'examples/ces-physical-three-output.json'
MEMORY = ROOT / 'examples/ces-motor-commissioning.json'
IDS = {SUPPLY: '5349994873749504051', MOTOR: '5349994873749504049', BRAKE: '5349994873749504050'}
SLOTS = {SUPPLY: 2, MOTOR: 0, BRAKE: 1}
NAMES = {SUPPLY: 'Takasago Voltage Control', MOTOR: 'Drive Control', BRAKE: 'Braking Control'}
BUTTONS = ['Ramp Enable', 'Ramp Disable', 'Motor Gentle', 'Run Regen Test',
           'Run Driving Cycle', 'Run Efficiency Scan', 'RZ-XA Step Down', 'RZ-XA Step Up']


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def boundary_timeline(pattern, guard, verify, progress, *, wait, **timing):
    for elapsed in pattern.boundaries:
        value = pattern.values_at(elapsed) if hasattr(pattern, 'values_at') else pattern.value_at(elapsed)
        guard(value)
        verify(value)
        progress({'event': 'progress', 'elapsed_s': elapsed, 'phase': pattern.phase_at(elapsed)})
        wait(0)
    return {'actual_elapsed_s': pattern.duration_s, 'skipped_updates': 0, 'maximum_lateness_s': 0.0}


def raw_config(path=PHYSICAL):
    raw = json.loads(path.read_text())
    for assignment in raw['assignments']:
        assignment['profile'] = str((path.parent / assignment['profile']).resolve())
    return raw


def load(tmp_path, raw):
    path = tmp_path / 'bindings.json'
    path.write_text(json.dumps(raw))
    return load_dispatcher_config(path)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr('socket.socket', lambda *a, **k: pytest.fail('No physical sockets permitted'))
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: pytest.fail('No live connection'))


class Device:
    def __init__(self, config, *, fault=None, headers=False):
        self.commands, self.replies, self.errors, self.writes = [], [], [], []
        self.values = {SUPPLY: .5, MOTOR: 0.0, BRAKE: 0.0}
        self.names = dict(NAMES)
        self.properties = {
            IDS[role]: {
                'Mode': '(STRING,"ConstOutput")', 'Unit': '(STRING,"V")',
                'Range': '(RANGE,-10,"V",10,"V")',
                'TRION/SourceChannel_A': f'(STRING,"CONST{SLOTS[role]}")',
                self.prop(role): '(SCALAR,0,"V")',
            } for role in IDS
        }
        self.buttons = {str(9000 + i): a['trigger'] for i, a in enumerate(config.raw['assignments'])}
        self.properties.update({cid: {'DO_Value': '(BOOL,OFF)'} for cid in self.buttons})
        self.fault, self.headers = fault, headers
        self.failed_command, self.read_override = None, None
        self.connections = 0

    @staticmethod
    def prop(role):
        return f'TRION/SourceChannel_A_CONST/Const{SLOTS[role]}'

    def connect(self, transport):
        self.connections += 1
        transport._sock = Mock()
        transport._sock.sendall.side_effect = self.send
        transport._connected = True
        transport._closed = False

    def reply(self, command, payload):
        header = command.split()[0].rstrip('?') + ' ' if self.headers else ''
        self.replies.append(header + payload)

    def send(self, payload):
        command = payload.decode().strip()
        self.commands.append(command)
        if command == '*IDN?':
            self.reply(command, 'DEWETRON,OXYGEN,MOCK,8.2')
        elif command == ':CHANNELlist:NAMes?':
            channels = [(IDS[r], self.names[r]) for r in IDS] + list(self.buttons.items())
            self.reply(command, ','.join(f'({quote_scpi(cid)},{quote_scpi(name)})' for cid, name in channels))
        elif command.startswith(':CHANNELlist:ITEM'):
            cid = command.split('ITEM', 1)[1].split(':')[0]
            self.reply(command, ','.join(quote_scpi(p) for p in self.properties[cid]))
        elif command.startswith(':CHANNELlist:PROPerty? '):
            cid, prop = parse_scpi(command.split(' ', 1)[1])
            role = next((r for r in IDS if IDS[r] == cid and self.prop(r) == prop), None)
            if role == BRAKE and self.read_override is not None:
                self.reply(command, self.read_override)
            elif role is not None:
                self.reply(command, f'(SCALAR,{self.values[role]:.9g},"V")')
            else:
                self.reply(command, self.properties[cid][prop])
        elif command == 'SYST:ERR?':
            self.reply(command, self.errors.pop(0) if self.errors else '0,"No error"')
        elif command.startswith(':CHANNELlist:PROPerty '):
            cid, prop, kind, value, unit = parse_scpi(command.split(' ', 1)[1])
            role = next(r for r in IDS if IDS[r] == cid and self.prop(r) == prop)
            assert kind == 'SCALAR' and unit == 'V'
            value = float(value)
            self.values[role] = value
            self.writes.append((role, value))
            if role == BRAKE and value > 0 and self.fault and self.failed_command is None:
                self.failed_command = command
                if self.fault == '-200':
                    self.errors.append('-200,"Execution error"')
                elif self.fault == 'missing':
                    self.read_override = 'NONE'
                elif self.fault == 'mismatch':
                    self.read_override = '(SCALAR,0.07,"V")'
        else:
            raise AssertionError(f'Unexpected SCPI command: {command}')


def physical_dispatcher(tmp_path, monkeypatch, raw=None, **device_options):
    config = load(tmp_path, raw_config() if raw is None else raw)
    clock, records = Clock(), []
    backend = OxygenBackend(config, records.append, confirmed_outputs=list(NAMES.values()), live=True, clock=clock)
    device = Device(config, **device_options)
    monkeypatch.setattr(backend.transport, 'connect', lambda: device.connect(backend.transport))
    monkeypatch.setattr(backend.transport, '_readline', lambda: device.replies.pop(0))
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    return dispatcher, backend, device, records


def assert_ordered(writes):
    drive = brake = 0.0
    for role, value in writes:
        if role == MOTOR:
            drive = value
        elif role == BRAKE:
            brake = value
        assert not (drive > 0 and brake > 0)


def test_authorized_three_output_physical_config_and_confirmations(tmp_path):
    config = load(tmp_path, raw_config())
    assert config.physical and config.has_brake
    assert config.raw['outputs'][MOTOR]['max_aout_v'] == 1.6
    assert config.raw['outputs'][MOTOR]['commissioned_drive_limit_v'] == 1.6
    assert config.raw['outputs'][BRAKE]['max_aout_v'] == 3.3
    assert config.raw['outputs'][SUPPLY]['max_aout_v'] == .5
    assert config.raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] == 80
    assert [a['trigger'] for a in config.raw['assignments']] == BUTTONS
    confirm_outputs(config, list(NAMES.values()), live=True)
    with pytest.raises(AuthorizationError, match='Confirm every'):
        confirm_outputs(config, [NAMES[MOTOR], NAMES[SUPPLY]], live=True)
    with pytest.raises(AuthorizationError, match='--live'):
        confirm_outputs(config, list(NAMES.values()), live=False)
    assert configuration_summary(config)['assignments'][3]['parameters'] == {'brake_target_v': .2}


@pytest.mark.parametrize('bad', ['flag', 'release', 'ack', 'permission', 'name', 'higher_drive'])
def test_invalid_physical_authorization_or_identity_rejected(tmp_path, bad):
    raw = raw_config()
    if bad == 'flag':
        raw['live_control']['enable_physical_brake'] = False
    elif bad == 'release':
        raw['outputs'][BRAKE]['brake_release_verified'] = False
    elif bad == 'ack':
        del raw['hardware_authorization']['brake_release_verified']
    elif bad == 'permission':
        raw['outputs'][BRAKE]['authorization'] = {'simulation': True, 'physical': False}
    elif bad == 'name':
        raw['outputs'][BRAKE]['channel'] = 'Drive Control'
    else:
        raw['outputs'][MOTOR]['higher_drive_limit_authorized'] = False
    with pytest.raises(ConfigError):
        load(tmp_path, raw)


@pytest.mark.parametrize('headers', [False, True])
@pytest.mark.parametrize('name', ['regen_test', 'driving_cycle'])
def test_physical_scpi_discovery_write_readback_and_order(tmp_path, monkeypatch, headers, name):
    dispatcher, backend, device, records = physical_dispatcher(tmp_path, monkeypatch, headers=headers)
    backend.connect()
    dispatcher.prepare()
    assert backend.channel_id(BRAKE) == IDS[BRAKE]
    assert backend.constant_property(BRAKE) == Device.prop(BRAKE)
    assert backend.authorized == {SUPPLY, MOTOR, BRAKE}
    assert len(backend.do_channels) == 8
    assert all(backend.read_button(button) is False for button in BUTTONS)
    dispatcher.execute_profile(name)
    assert (MOTOR, 1.6) in device.writes
    assert (BRAKE, .2) in device.writes
    assert_ordered(device.writes)
    assert not any(role == SUPPLY for role, _ in device.writes)
    assert dispatcher.states[BRAKE].value == dispatcher.states[MOTOR].value == 0
    assert dispatcher.cleanup()
    assert device.writes[-2:] == [(MOTOR, 0), (BRAKE, 0)]
    assert backend.full_discover_count == device.connections == 1
    assert any(r['event'] == 'setpoint_ack' and r['output'] == BRAKE and r['readback_v'] == .2 for r in records)
    backend.close()


@pytest.mark.parametrize('bad', ['discovered_name', 'expected_id', 'source', 'range', 'release'])
def test_discovery_or_startup_mismatch_never_writes(tmp_path, monkeypatch, bad):
    raw = raw_config()
    if bad == 'expected_id':
        raw['outputs'][BRAKE]['expected_id'] = '99999'
    dispatcher, backend, device, _ = physical_dispatcher(tmp_path, monkeypatch, raw)
    if bad == 'discovered_name':
        device.names[BRAKE] = 'Not Braking Control'
    elif bad == 'source':
        device.properties[IDS[BRAKE]]['TRION/SourceChannel_A'] = '(STRING,"CONST7")'
    elif bad == 'range':
        device.properties[IDS[BRAKE]]['Range'] = '(RANGE,0,"V",0.4,"V")'
    elif bad == 'release':
        device.values[BRAKE] = .1
    with pytest.raises(Exception):
        backend.connect()
        dispatcher.prepare()
    assert not device.writes
    assert not backend.authorized
    backend.close()


@pytest.mark.parametrize('fault', ['-200', 'missing', 'mismatch'])
def test_physical_brake_failure_stops_without_retry_or_supply_write(tmp_path, monkeypatch, fault):
    dispatcher, _, device, records = physical_dispatcher(tmp_path, monkeypatch, fault=fault)
    monkeypatch.setattr(dispatcher, 'monitor', lambda: dispatcher.execute_profile('regen_test'))
    assert dispatcher.run() == 1
    assert dispatcher.completed == 0
    assert device.commands.count(device.failed_command) == 1
    interrupted = next(r for r in records if r['event'] == 'profile_interrupted_state')
    assert interrupted['outputs'][BRAKE]['status'] == 'unknown'
    assert not any(role == SUPPLY for role, _ in device.writes)
    assert device.connections == 1
    assert_ordered(device.writes)
    failure = next(r for r in records if r['event'] == 'output_command_failed' and r['output'] == BRAKE)
    assert failure['retry_attempted'] is False
    if fault == '-200':
        assert any(r.get('error_code') == -200 for r in records)
    json.dumps(records, allow_nan=False)


def test_physical_cancellation_zeroes_drive_then_releases_brake(tmp_path, monkeypatch):
    dispatcher, _, device, _ = physical_dispatcher(tmp_path, monkeypatch)

    def wait(seconds):
        if device.values[BRAKE] > 0:
            raise KeyboardInterrupt()

    monkeypatch.setattr(dispatcher, 'wait', wait)
    monkeypatch.setattr(dispatcher, 'monitor', lambda: dispatcher.execute_profile('driving_cycle'))
    assert dispatcher.run() == 130
    assert device.writes[-2:] == [(MOTOR, 0), (BRAKE, 0)]
    assert not any(role == SUPPLY for role, _ in device.writes)
    assert_ordered(device.writes)


def test_authorized_release_does_not_depend_on_closed_loop_readiness(tmp_path, monkeypatch):
    dispatcher, backend, device, _ = physical_dispatcher(tmp_path, monkeypatch)
    backend.connect()
    dispatcher.prepare()
    backend.open_loop = False
    backend.config.raw['interlocks']['max_feedback_age_s'] = 1.0
    monkeypatch.setattr(backend, 'measure', lambda *a: pytest.fail('Release must not query readiness'))
    backend.write(BRAKE, 0)
    assert backend.read_output(dispatcher.outputs[BRAKE]['binding']) == 0
    assert device.writes[-1] == (BRAKE, 0)
    monkeypatch.setattr(backend, 'measure', lambda *a: SimpleNamespace(value=False, sampled_at=0))
    with pytest.raises(AuthorizationError, match='supply_external_ready'):
        backend.write(BRAKE, .2)
    assert device.writes == [(BRAKE, 0)]
    backend.close()


def test_tuned_parameter_reaches_physical_scpi_without_changing_ceiling(tmp_path, monkeypatch):
    raw = raw_config()
    raw['assignments'][3]['parameters']['brake_target_v'] = .28
    dispatcher, backend, device, _ = physical_dispatcher(tmp_path, monkeypatch, raw)
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('regen_test')
    assert (BRAKE, .28) in device.writes
    assert backend.transport.bindings[BRAKE][-1] == 3.3
    assert dispatcher.states[BRAKE].value == 0
    assert_ordered(device.writes)
    backend.close()


@pytest.mark.parametrize('value', [0, .35, 1.8, 3.3])
def test_parameter_tuning_uses_configured_range_not_old_brake_intensity(tmp_path, value):
    raw = raw_config(MEMORY)
    raw['assignments'][3]['parameters']['brake_target_v'] = value
    config = load(tmp_path, raw)
    backend = MemoryBackend(config)
    clock = Clock()
    dispatcher = Dispatcher(config, backend, lambda r: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('regen_test')
    assert any(role == BRAKE and v == value for _, role, v in backend.commands)
    assert dispatcher.states[BRAKE].value == 0
    assert all(role != SUPPLY for _, role, _ in backend.commands)


def test_editing_only_parameters_resolves_independently_without_profile_changes(tmp_path):
    raw = raw_config()
    before = (ROOT / 'profiles/ces/motor_regen_trial_b.json').read_bytes()
    raw['assignments'][3]['parameters']['brake_target_v'] = .27
    raw['assignments'][4]['parameters'] = {'brake_light_v': .18, 'brake_heavy_v': .31}
    config = load(tmp_path, raw)
    targets = lambda name: [s['params']['target'] for s in config.profiles[name].steps
                            if s['type'] == 'ramp_aout' and s['params']['channel'] == BRAKE]
    assert targets('regen_test') == [.27, 0]
    assert targets('driving_cycle') == [.18, 0, .31, 0]
    assert (ROOT / 'profiles/ces/motor_regen_trial_b.json').read_bytes() == before
    raw['assignments'][5].update(profile=raw['assignments'][3]['profile'], parameters={'brake_target_v': .12})
    config = load(tmp_path, raw)
    assert config.profiles['regen_test'].steps != config.profiles['efficiency_scan'].steps


@pytest.mark.parametrize('value', ['0.2', True, None, [], {}, float('nan'), float('inf'), -float('inf'), -.01, 3.31])
def test_invalid_or_out_of_range_parameter_is_explicit_error(tmp_path, value):
    raw = raw_config()
    raw['assignments'][3]['parameters']['brake_target_v'] = value
    with pytest.raises(ConfigError, match='[Pp]arameter.*brake_target_v'):
        load(tmp_path, raw)


@pytest.mark.parametrize('value', [None, [], .2, 'brake_target_v'])
def test_parameters_must_be_an_object(tmp_path, value):
    raw = raw_config()
    raw['assignments'][3]['parameters'] = value
    with pytest.raises(ConfigError, match='parameters must be an object'):
        load(tmp_path, raw)


def test_missing_parameter_and_legacy_numeric_target(tmp_path):
    raw = raw_config()
    del raw['assignments'][3]['parameters']
    with pytest.raises(ConfigError, match="missing assignment parameter 'brake_target_v'"):
        load(tmp_path, raw)
    profile = json.loads((ROOT / 'profiles/ces/motor_regen_trial_b.json').read_text())
    for step in profile['steps']:
        if isinstance(step.get('params', {}).get('target'), dict):
            step['params']['target'] = .21
    path = tmp_path / 'numeric.json'
    path.write_text(json.dumps(profile))
    raw['assignments'][3]['profile'] = str(path)
    config = load(tmp_path, raw)
    assert any(s.get('params', {}).get('target') == .21 for s in config.profiles['regen_test'].steps)


@pytest.mark.parametrize('reference', [{}, {'parameter': 5}, {'parameter': ''}, {'parameter': 'brake_target_v', 'extra': 1}])
def test_malformed_reference_rejected(tmp_path, reference):
    raw = raw_config()
    profile = json.loads((ROOT / 'profiles/ces/motor_regen_trial_b.json').read_text())
    for step in profile['steps']:
        if isinstance(step.get('params', {}).get('target'), dict):
            step['params']['target'] = reference
    path = tmp_path / 'malformed.json'
    path.write_text(json.dumps(profile))
    raw['assignments'][3]['profile'] = str(path)
    with pytest.raises(ConfigError, match='parameter'):
        load(tmp_path, raw)


def test_parameter_resolution_precedes_mutual_exclusion_validation(tmp_path):
    raw = raw_config()
    profile = json.loads((ROOT / 'profiles/ces/motor_regen_trial_b.json').read_text())
    profile['steps'].insert(5, {
        'type': 'ramp_aout',
        'params': {'channel': BRAKE, 'target': {'parameter': 'brake_target_v'}, 'duration_s': 1},
    })
    path = tmp_path / 'overlap.json'
    path.write_text(json.dumps(profile))
    raw['assignments'][3]['profile'] = str(path)
    with pytest.raises(ConfigError, match='Zero drive before brake'):
        load(tmp_path, raw)
