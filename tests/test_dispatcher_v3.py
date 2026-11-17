"""Offline tests for schema v3 open-loop dispatcher (no live OXYGEN)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from oxygen_engine.closed_loop import register_controller
from oxygen_engine.dispatcher import MOTOR, SUPPLY, Dispatcher
from oxygen_engine.errors import AuthorizationError, ControlNotReady, InterlockError
from oxygen_engine.main import main
from oxygen_engine.memory_backend import MemoryBackend
from oxygen_engine.schema import ConfigError, confirm_outputs, load_dispatcher_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'dispatcher.v3.example.json'
HARDWARE = ROOT / 'dispatcher.v3.hardware.example.json'
DEMO = ROOT / 'dispatcher.v3.oxygen-demo.example.json'
SCENARIO = ROOT / 'scenarios' / 'open_loop_fixture_demo.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def boundary_timeline(pattern, guard, verify, progress, *, interval_s, max_lateness_s, clock, wait):
    elapsed = 0.0
    for segment in pattern.segments:
        elapsed = segment.end_s
        value = pattern.value_at(elapsed)
        guard(value)
        verify(value)
        progress({'event': 'progress', 'elapsed_s': elapsed, 'phase': segment.phase, 'requested_v': value})
        wait(0)
    return {'actual_elapsed_s': elapsed, 'skipped_updates': 0, 'maximum_lateness_s': 0.0}


@pytest.fixture(autouse=True)
def _clear_controller():
    register_controller(None)
    yield
    register_controller(None)


def test_example_config_loads_open_loop():
    config = load_dispatcher_config(EXAMPLE)
    assert config.mode == 'open_loop'
    assert config.transport == 'memory'
    assert config.raw['measurements'] == {}
    assert config.raw['interlocks']['require_supply_demand_before_motor'] is True
    assert config.raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] == 80.0
    assert {'rzxa_to_40_hold', 'rzxa_down', 'motor_gentle'} <= set(config.profiles)


def test_hardware_and_demo_examples_load():
    assert load_dispatcher_config(HARDWARE).physical
    assert load_dispatcher_config(DEMO).transport == 'oxygen_scpi'
    assert not load_dispatcher_config(DEMO).physical


def _absolutize_profiles(raw):
    for assignment in raw.get('assignments', []):
        profile = Path(assignment['profile'])
        if not profile.is_absolute():
            assignment['profile'] = str((ROOT / profile).resolve())
    return raw


def test_open_loop_allows_optional_logging_measurements(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['measurements'] = {
        'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': None, 'mapping_verified': False}
    }
    path = tmp_path / 'logged.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    assert 'dc_bus_voltage' in config.raw['measurements']


def test_closed_loop_without_controller_is_not_ready(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['control'] = {'mode': 'closed_loop'}
    raw['measurements'] = {
        'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': None, 'mapping_verified': False},
        'motor_input_mapping_verified': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'motor_physical_disable_verified': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'supply_external_ready': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'regen_safe': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'supply_shutdown_permitted': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'supply_standalone_test': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
        'supply_disabled': {'kind': 'boolean', 'channel': None, 'mapping_verified': False},
    }
    raw['interlocks'].update({
        'bus_target_v': 40.0,
        'bus_tolerance_v': 2.0,
        'max_feedback_age_s': 1.0,
        'motor_requires_measured_bus_in_range': True,
        'allow_unverified_simulated_interlock_values': True,
        'allow_unverified_live_interlock_values': False,
    })
    raw['simulation']['telemetry'] = {name: (40.0 if name == 'dc_bus_voltage' else True)
                                      for name in raw['measurements']}
    profile = {
        'schema_version': 3,
        'name': 'closed_loop_hold',
        'output': 'rzxa_voltage_demand',
        'timeout_s': 30,
        'completion': {'mode': 'hold_final', 'value': 0.5, 'operator_action_required_to_stop': True},
        'requires': {'motor_neutral': True, 'supply_output_enable': 'external_or_explicitly_verified',
                     'measured_bus_feedback': 'required_for_motor_interlock'},
        'steps': [
            {'type': 'assert_preconditions', 'params': {'motor_neutral': True}},
            {'type': 'ramp_aout', 'params': {'channel': 'rzxa_voltage_demand', 'target': 0.5, 'duration_s': 5.0}},
            {'type': 'hold', 'params': {'duration_s': 3.0}},
        ],
    }
    profile_path = tmp_path / 'closed_hold.json'
    profile_path.write_text(json.dumps(profile), encoding='utf-8')
    raw['assignments'] = [{
        'name': 'rzxa_to_40_hold', 'enabled': True, 'trigger': 'DO 3/1 Sim',
        'output': 'rzxa_voltage_demand', 'profile': str(profile_path),
    }]
    path = tmp_path / 'closed.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    backend = MemoryBackend(config)
    with pytest.raises(ControlNotReady, match='no ClosedLoopController'):
        Dispatcher(config, backend, lambda *_: None)


def test_supply_ramp_from_nonzero_and_cleanup_never_samples():
    config = load_dispatcher_config(EXAMPLE)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    # Start from a mid-range demand — open-loop must accept any valid setpoint.
    backend.write(SUPPLY, 0.2)
    dispatcher.states[SUPPLY].value = 0.2
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == 0.5
    dispatcher.execute_profile('motor_gentle')
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.cleanup()
    assert dispatcher.states[SUPPLY].value == 0.5
    assert not any(r.get('event') == 'independent_feedback' for r in records)


def test_require_supply_demand_before_motor_configurable(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['interlocks']['require_supply_demand_before_motor'] = False
    path = tmp_path / 'no-prereq.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    assert dispatcher.states[SUPPLY].value == 0
    dispatcher.execute_profile('motor_gentle')
    assert dispatcher.states[MOTOR].value == 0


def test_discovery_pinned_no_repeated_full_discover():
    config = load_dispatcher_config(EXAMPLE)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    assert backend.full_discover_count == 1
    dispatcher.prepare()
    dispatcher.execute_profile('rzxa_to_40_hold')
    dispatcher.execute_profile('rzxa_down')
    assert backend.full_discover_count == 1


def test_scpi_txn_count_no_rediscover_during_ramp(monkeypatch):
    """OxygenBackend pins discovery at connect; ramp steps must not re-issue CHANNELlist:NAMes?."""
    from oxygen_engine.oxygen_backend import OxygenBackend, OxygenTransport
    from oxygen_engine.scpi_transport import ScpiErrorEntry, parse_scpi, quote_scpi

    config = load_dispatcher_config(DEMO)
    outputs = config.raw['outputs']
    channels = []
    properties = {}
    for index, spec in enumerate(outputs.values()):
        cid = str(18446744073709551615 - index)
        channels.append((cid, spec['channel']))
        slot = 1 - index
        properties[cid] = {
            'Mode': '(STRING,"ConstOutput")',
            'TRION/SourceChannel_A': f'(STRING,"CONST{slot}")',
            'Unit': '(STRING,"V")',
            'Range': '(RANGE,-10,"V",10,"V")',
            f'TRION/SourceChannel_A_CONST/Const{slot}': '(SCALAR,0,"V")',
        }
    for i, name in enumerate(['Ramp Enable', 'Ramp Disable', 'RZXA Char',
                              'Motor Gentle', 'Motor Stop Go', 'Motor Cruise'], start=1):
        cid = str(900000000000000000 + i)
        channels.append((cid, name))
        properties[cid] = {'DO_Value': '(BOOL,OFF)', 'Mode': '(STRING,"DIO")'}

    commands = []

    def instrument_query(command):
        commands.append(command)
        if command == '*IDN?':
            return 'DEWETRON,OXYGEN,00000001,8.2'
        if command == 'SYST:ERR?':
            return '0,"No error"'
        if command == ':CHANNELlist:NAMes?':
            return ','.join(f'({quote_scpi(cid)},{quote_scpi(name)})' for cid, name in channels)
        if command.startswith(':CHANNELlist:ITEM'):
            cid = command.split('ITEM', 1)[1].split(':')[0]
            return ','.join(quote_scpi(p) for p in properties[cid])
        if command.startswith(':CHANNELlist:PROPerty? '):
            cid, prop = parse_scpi(command.split(' ', 1)[1])
            return properties[cid][prop]
        raise AssertionError(f'Unexpected query: {command}')

    def instrument_write(command):
        commands.append(command)
        if not command.startswith(':CHANNELlist:PROPerty '):
            raise AssertionError(f'Unexpected write: {command}')
        cid, prop, kind, value, unit = parse_scpi(command.split(' ', 1)[1])
        assert kind == 'SCALAR' and unit == 'V'
        properties[cid][prop] = f'(SCALAR,{value},"V")'

    class Proxy(OxygenTransport):
        def connect(self):
            self._sock = object()
            self._connected = True
            self._closed = False

        def close(self):
            self.capabilities.clear()
            self.bindings.clear()
            self.read_ids.clear()
            self.do_ids.clear()
            self.numeric_name = None
            self._sock = None
            self._connected = False
            self._closed = True

        def query(self, command):
            if self.invalid:
                raise AuthorizationError('Transport invalidated; restart explicitly')
            from oxygen_engine.scpi_transport import ScpiProtocolError
            allowed = command in {'*IDN?', ':CHANNELlist:NAMes?', 'SYST:ERR?'}
            allowed = allowed or any(command == f':CHANNELlist:ITEM{cid}:ATTR:NAMes?' for cid in self.read_ids)
            prefix = ':CHANNELlist:PROPerty? '
            if command.startswith(prefix):
                args = parse_scpi(command[len(prefix):])
                allowed = (len(args) == 2 and all(isinstance(a, str) for a in args) and args[0] in self.read_ids and
                           command == prefix + ','.join(quote_scpi(a) for a in args))
            if not allowed:
                raise ScpiProtocolError(f'Query refused: {command!r}')
            reply = instrument_query(command)
            if command == ':CHANNELlist:NAMes?':
                entries = parse_scpi(reply) if reply.upper() != 'NONE' else []
                self.read_ids = {entry[0] for entry in entries if isinstance(entry, tuple) and len(entry) == 2}
            self.emit({'event': 'query', 'backend': 'oxygen_scpi', 'command': command, 'raw_payload': reply})
            return reply

        def write(self, command):
            from oxygen_engine.constant_pattern import finite_number
            from oxygen_engine.scpi_transport import ScpiProtocolError
            if self.invalid:
                raise ScpiProtocolError('Transport invalidated; restart explicitly')
            prefix = ':CHANNELlist:PROPerty '
            if not command.startswith(prefix):
                raise ScpiProtocolError('Only authorized constant demands are permitted')
            args = parse_scpi(command[len(prefix):])
            if len(args) != 5 or not all(isinstance(a, str) for a in args) or args[2] != 'SCALAR' or args[4] != 'V':
                raise ScpiProtocolError('Output requires canonical SCALAR demand in V')
            bounds = self.capabilities.get((args[0], args[1]))
            value = finite_number(args[3], 'demand')
            canonical = prefix + f'{quote_scpi(args[0])},{quote_scpi(args[1])},SCALAR,{value:.9g},"V"'
            if bounds is None or not bounds[0] <= value <= bounds[1] or command != canonical:
                raise ScpiProtocolError('Output is unauthorized, noncanonical or out of bounds')
            instrument_write(command)

        def check_error(self):
            raw = instrument_query('SYST:ERR?')
            code, message = parse_scpi(raw)
            return ScpiErrorEntry(int(code), str(message))

        def command(self, command, query_error=True):
            if command.endswith('?'):
                return self.query(command)
            self.write(command)
            if query_error:
                entry = self.check_error()
                if entry.code:
                    from oxygen_engine.scpi_transport import ScpiProtocolError
                    raise ScpiProtocolError(f'OXYGEN SCPI error: {entry}')
            return None

    monkeypatch.setattr('oxygen_engine.oxygen_backend.OxygenTransport', Proxy)
    records = []
    backend = OxygenBackend(config, records.append, confirmed_outputs=['Test Aout2', 'Test Aout1'])
    clock = Clock()
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    assert backend.full_discover_count == 1
    names_at_connect = sum(1 for c in commands if c == ':CHANNELlist:NAMes?')
    assert names_at_connect >= 1
    txn_at_connect = len(commands)
    dispatcher.prepare()
    backend.write(SUPPLY, 0.2)
    dispatcher.states[SUPPLY].value = 0.2
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert backend.full_discover_count == 1
    assert sum(1 for c in commands if c == ':CHANNELlist:NAMes?') == names_at_connect
    writes = [c for c in commands[txn_at_connect:] if c.startswith(':CHANNELlist:PROPerty ')]
    assert writes, 'ramp must issue constant property writes'
    assert not any(c.startswith(':CHANNELlist:ITEM') for c in commands[txn_at_connect:])
    assert dispatcher.states[SUPPLY].value == 0.5


def test_open_loop_logging_measurements_do_not_alter_setpoints(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['measurements'] = {
        'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': 'FAKE bus', 'mapping_verified': False}
    }
    raw['simulation']['telemetry'] = {'dc_bus_voltage': 12.0}
    path = tmp_path / 'log-meas.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    backend.write(SUPPLY, 0.1)
    dispatcher.states[SUPPLY].value = 0.1
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == 0.5
    assert any(r.get('event') == 'measurement_log' for r in records)
    assert not any(r.get('event') == 'independent_feedback' for r in records)


def test_open_loop_sample_refuses_control_path():
    config = load_dispatcher_config(EXAMPLE)
    backend = MemoryBackend(config)
    dispatcher = Dispatcher(config, backend, lambda *_: None)
    with pytest.raises(InterlockError, match='open_loop'):
        dispatcher.sample('dc_bus_voltage')


def test_confirm_outputs_has_no_name_whitelist():
    config = load_dispatcher_config(DEMO)
    # Any exact configured names work; whitelist removed.
    confirm_outputs(config, ['Test Aout2', 'Test Aout1'], live=False)
    with pytest.raises(AuthorizationError, match='Confirm BOTH'):
        confirm_outputs(config, ['Test Aout2'], live=False)
    physical = load_dispatcher_config(HARDWARE)
    with pytest.raises(AuthorizationError, match='--live'):
        confirm_outputs(physical, ['Test Aout2', 'Test Aout1'], live=False)
    confirm_outputs(physical, ['Test Aout2', 'Test Aout1'], live=True)


def test_offline_preview_does_not_connect(monkeypatch, capsys):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('must not connect'))
    assert main(['dispatch-patterns', '--bindings', str(EXAMPLE)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['schema_version'] == 3
    assert plan['control_mode'] == 'open_loop'


def test_memory_scenario_run_completes():
    config = load_dispatcher_config(EXAMPLE)
    from oxygen_engine.memory_backend import load_scenario
    events = load_scenario(SCENARIO, {}, ['DO 3/1 Sim', 'DO 3/2 Sim', 'DO 3/4 Sim'])
    clock = Clock()
    backend = MemoryBackend(config, clock=clock, events=events)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    code = dispatcher.run()
    assert code == 0
    assert dispatcher.completed >= 1


def test_physical_execution_requires_live(monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('no network'))
    assert main(['dispatch-patterns', '--bindings', str(HARDWARE)]) == 0
    assert main([
        'dispatch-patterns', '--bindings', str(HARDWARE), '--execute',
        '--confirm-output', 'Test Aout2', '--confirm-output', 'Test Aout1',
    ]) == 1
