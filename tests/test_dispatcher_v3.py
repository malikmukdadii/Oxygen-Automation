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
EXAMPLE = ROOT / 'dispatcher.example.json'
HARDWARE = ROOT / 'examples' / 'physical-dual.json'
DEMO = ROOT / 'examples' / 'oxygen-demo.json'
SUPPLY_ONLY = ROOT / 'examples' / 'supply-only.json'
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
    raw['timing']['telemetry_poll_ms'] = 0
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
    # AOUT write path must not sync-query telemetry.
    assert not any(r.get('event') == 'measurement_log' for r in records)
    dispatcher.log_measurements()
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
    with pytest.raises(AuthorizationError, match='Confirm every configured'):
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


@pytest.mark.parametrize('operator_present', [False, True])
def test_cli_defaults_to_dispatcher_json_or_example(tmp_path, monkeypatch, capsys, operator_present):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('must not connect'))
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text()))
    (tmp_path / 'dispatcher.example.json').write_text(json.dumps(raw))
    if operator_present:
        raw['assignments'][0]['trigger'] = 'Operator exact button'
        (tmp_path / 'dispatcher.json').write_text(json.dumps(raw))
    monkeypatch.setattr('oxygen_engine.main._project_root', lambda: tmp_path)
    assert main(['dispatch-patterns']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['schema_version'] == 3
    assert plan['control_mode'] == 'open_loop'
    assert plan['assignments'][0]['trigger'] == ('Operator exact button' if operator_present else 'DO 3/1 Sim')


def test_legacy_global_config_flag_is_retired():
    with pytest.raises(SystemExit, match='--config is retired'):
        main(['--config', 'examples/legacy-engine-config.json', 'list-profiles'])


def _supply_only_profile(tmp_path):
    profile = {
        'schema_version': 3,
        'name': 'supply_only_hold',
        'output': 'rzxa_voltage_demand',
        'timeout_s': 30,
        'completion': {'mode': 'hold_final', 'value': 0.5, 'operator_action_required_to_stop': True},
        'requires': {},
        'steps': [
            {'type': 'ramp_aout', 'params': {'channel': 'rzxa_voltage_demand', 'target': 0.5, 'duration_s': 2.0}},
            {'type': 'hold', 'params': {'duration_s': 1.0}},
        ],
    }
    path = tmp_path / 'supply_only_hold.json'
    path.write_text(json.dumps(profile), encoding='utf-8')
    return path


def test_missing_optional_measurement_warns_not_aborts(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['measurements'] = {
        'dc_bus_voltage': {
            'kind': 'number', 'unit': 'V', 'channel': 'Missing Bus Sensor', 'mapping_verified': False,
        }
    }
    # No simulation.telemetry entry — measure fails softly.
    raw['timing']['telemetry_poll_ms'] = 0
    path = tmp_path / 'missing-meas.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == 0.5
    dispatcher.log_measurements()
    assert any(r.get('event') == 'measurement_log_failed' for r in records)


def test_measurement_timeout_during_ramp_does_not_block_aout(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['measurements'] = {
        'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': 'FAKE bus', 'mapping_verified': False}
    }
    raw['timing']['telemetry_poll_ms'] = 0
    raw['simulation']['telemetry'] = {'dc_bus_voltage': 40.0}
    path = tmp_path / 'timeout-meas.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)

    def flaky_measure(name):
        raise TimeoutError('measurement communication timeout')

    backend.connect()
    dispatcher.prepare()
    backend.measure = flaky_measure
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == 0.5
    dispatcher.log_measurements()
    assert any(r.get('event') == 'measurement_log_failed' and 'timeout' in r.get('error', '').lower()
               for r in records)
    assert any(r.get('aout_session_intact') for r in records if r.get('event') == 'measurement_log_failed')


def test_open_loop_telemetry_poll_is_independent_of_writes(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['measurements'] = {
        'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': 'FAKE bus', 'mapping_verified': False}
    }
    raw['timing']['telemetry_poll_ms'] = 100
    raw['simulation']['telemetry'] = {'dc_bus_voltage': 11.0}
    path = tmp_path / 'poll-meas.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    assert config.raw['timing']['telemetry_poll_ms'] == 100
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    dispatcher.maybe_poll_telemetry()
    assert any(r.get('event') == 'measurement_log' for r in records)
    before = len([r for r in records if r.get('event') == 'measurement_log'])
    dispatcher.command(SUPPLY, 0.25)
    after_write = len([r for r in records if r.get('event') == 'measurement_log'])
    assert after_write == before
    clock.now += 0.2
    dispatcher.maybe_poll_telemetry()
    assert len([r for r in records if r.get('event') == 'measurement_log']) > before


def test_supply_only_initialization_and_execution(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    profile = _supply_only_profile(tmp_path)
    supply = raw['outputs'][SUPPLY]
    raw['outputs'] = {SUPPLY: supply}
    raw['interlocks'] = {
        'one_active_profile_global': True,
        'external_emergency_stop_required_for_live': True,
    }
    raw['assignments'] = [{
        'name': 'supply_only', 'enabled': True, 'trigger': 'DO 3/1 Sim',
        'output': SUPPLY, 'profile': str(profile),
    }]
    raw['simulation']['outputs'] = {
        supply['binding']: raw['simulation']['outputs'][supply['binding']],
    }
    path = tmp_path / 'supply-only.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    assert config.active_roles == frozenset({SUPPLY})
    assert not config.has_motor
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    assert set(dispatcher.states) == {SUPPLY}
    dispatcher.execute_profile('supply_only')
    assert dispatcher.states[SUPPLY].value == 0.5
    assert dispatcher.cleanup()
    assert dispatcher.states[SUPPLY].value == 0.5


def test_motor_only_initialization_and_execution(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    motor = raw['outputs'][MOTOR]
    raw['outputs'] = {MOTOR: motor}
    raw['interlocks'] = {
        'one_active_profile_global': True,
        'external_emergency_stop_required_for_live': True,
        'require_supply_demand_before_motor': False,
    }
    gentle = str((ROOT / 'profiles' / 'motor_gentle.json').resolve())
    raw['assignments'] = [{
        'name': 'motor_only', 'enabled': True, 'trigger': 'DO 3/4 Sim',
        'output': MOTOR, 'profile': gentle,
    }]
    raw['simulation']['outputs'] = {
        motor['binding']: raw['simulation']['outputs'][motor['binding']],
    }
    path = tmp_path / 'motor-only.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    assert config.active_roles == frozenset({MOTOR})
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    assert set(dispatcher.states) == {MOTOR}
    dispatcher.execute_profile('motor_only')
    assert dispatcher.states[MOTOR].value == 0
    assert dispatcher.cleanup()


def test_dual_output_still_works():
    config = load_dispatcher_config(EXAMPLE)
    assert config.active_roles == frozenset({SUPPLY, MOTOR})
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('rzxa_to_40_hold')
    dispatcher.execute_profile('motor_gentle')
    assert dispatcher.states[SUPPLY].value == 0.5
    assert dispatcher.states[MOTOR].value == 0


def test_operator_configured_voltage_limit_authoritative(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['outputs'][SUPPLY]['max_aout_v'] = 0.75
    raw['simulation']['outputs'][raw['outputs'][SUPPLY]['binding']]['max_v'] = 0.75
    # Keep motor interlock demand inside new supply clamp.
    raw['interlocks']['motor_supply_demand_v'] = 0.5
    profile = {
        'schema_version': 3,
        'name': 'to_075',
        'output': SUPPLY,
        'timeout_s': 20,
        'completion': {'mode': 'hold_final', 'value': 0.75, 'operator_action_required_to_stop': True},
        'requires': {'motor_neutral': True},
        'steps': [
            {'type': 'assert_preconditions', 'params': {'motor_neutral': True}},
            {'type': 'ramp_aout', 'params': {'channel': SUPPLY, 'target': 0.75, 'duration_s': 2.0}},
            {'type': 'hold', 'params': {'duration_s': 1.0}},
        ],
    }
    profile_path = tmp_path / 'to_075.json'
    profile_path.write_text(json.dumps(profile), encoding='utf-8')
    raw['assignments'] = [{
        'name': 'to_075', 'enabled': True, 'trigger': 'DO 3/1 Sim',
        'output': SUPPLY, 'profile': str(profile_path),
    }, {
        'name': 'motor_gentle', 'enabled': True, 'trigger': 'DO 3/4 Sim',
        'output': MOTOR, 'profile': str((ROOT / 'profiles' / 'motor_gentle.json').resolve()),
    }]
    path = tmp_path / 'limit.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    assert config.raw['outputs'][SUPPLY]['max_aout_v'] == 0.75
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('to_075')
    assert dispatcher.states[SUPPLY].value == 0.75


def test_example_supply_limit_remains_half_volt():
    config = load_dispatcher_config(EXAMPLE)
    assert config.raw['outputs'][SUPPLY]['max_aout_v'] == 0.5
    assert config.raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] == 80.0


def test_supply_only_rejects_explicit_motor_neutral_prerequisite(tmp_path):
    raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    supply = raw['outputs'][SUPPLY]
    raw['outputs'] = {SUPPLY: supply}
    raw['interlocks'] = {
        'one_active_profile_global': True,
        'external_emergency_stop_required_for_live': True,
    }
    profile = {
        'schema_version': 3,
        'name': 'needs_motor',
        'output': SUPPLY,
        'timeout_s': 20,
        'completion': {'mode': 'hold_final', 'value': 0.5, 'operator_action_required_to_stop': True},
        'requires': {'motor_neutral': True},
        'steps': [
            {'type': 'assert_preconditions', 'params': {'motor_neutral': True}},
            {'type': 'ramp_aout', 'params': {'channel': SUPPLY, 'target': 0.5, 'duration_s': 1.0}},
        ],
    }
    profile_path = tmp_path / 'needs_motor.json'
    profile_path.write_text(json.dumps(profile), encoding='utf-8')
    raw['assignments'] = [{
        'name': 'bad', 'enabled': True, 'trigger': 'DO 3/1 Sim',
        'output': SUPPLY, 'profile': str(profile_path),
    }]
    raw['simulation']['outputs'] = {
        supply['binding']: raw['simulation']['outputs'][supply['binding']],
    }
    path = tmp_path / 'bad-supply.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ConfigError, match='motor_neutral'):
        load_dispatcher_config(path)


def test_stock_rzxa_profiles_load_and_run_supply_only():
    config = load_dispatcher_config(SUPPLY_ONLY)
    assert config.active_roles == frozenset({SUPPLY})
    assert {'rzxa_to_40_hold', 'rzxa_down', 'rzxa_steps'} <= set(config.profiles)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == 0.5
    dispatcher.execute_profile('rzxa_steps')
    assert dispatcher.states[SUPPLY].value == 0.5
    dispatcher.execute_profile('rzxa_down')
    assert dispatcher.states[SUPPLY].value == 0.0
    assert dispatcher.cleanup()
    assert dispatcher.states[SUPPLY].value == 0.0


def test_stock_rzxa_profiles_run_dual_with_motor_neutral_interlock():
    config = load_dispatcher_config(EXAMPLE)
    assert config.raw['interlocks']['supply_down_requires_motor_neutral'] is True
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    # Motor starts neutral; dual interlock allows stock supply profiles to run.
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == 0.5
    dispatcher.execute_profile('rzxa_down')
    assert dispatcher.states[SUPPLY].value == 0.0
    dispatcher.execute_profile('rzxa_steps')
    assert dispatcher.states[SUPPLY].value == 0.5


def test_dual_can_disable_motor_neutral_sequencing(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['interlocks']['supply_down_requires_motor_neutral'] = False
    path = tmp_path / 'dual-no-mn.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, lambda *_: None, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    backend.connect()
    dispatcher.prepare()
    # Non-neutral motor would still be refused at startup; after prepare, leave motor at 0
    # and confirm supply runs without an explicit profile motor_neutral requirement.
    dispatcher.execute_profile('rzxa_to_40_hold')
    assert dispatcher.states[SUPPLY].value == 0.5


def test_telemetry_budget_does_not_block_trigger_path(tmp_path):
    raw = _absolutize_profiles(json.loads(EXAMPLE.read_text(encoding='utf-8')))
    raw['measurements'] = {
        'dc_bus_voltage': {'kind': 'number', 'unit': 'V', 'channel': 'FAKE bus', 'mapping_verified': False},
        'extra_flag': {'kind': 'boolean', 'channel': 'FAKE flag', 'property': 'DO_Value',
                       'mapping_verified': False},
    }
    raw['timing']['telemetry_poll_ms'] = 10
    raw['timing']['poll_ms'] = 50
    raw['simulation']['telemetry'] = {'dc_bus_voltage': 40.0, 'extra_flag': True}
    path = tmp_path / 'tele-budget.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    records = []
    dispatcher = Dispatcher(config, backend, records.append, clock=clock, wait=clock.wait,
                            timeline=boundary_timeline)
    calls = {'n': 0}
    backend.connect()
    dispatcher.prepare()
    original_measure = backend.measure

    def slow_measure(name):
        calls['n'] += 1
        clock.now += 0.04  # each sample burns most of a 25 ms budget
        return original_measure(name)

    backend.measure = slow_measure
    before = clock.now
    dispatcher.maybe_poll_telemetry(budget_s=0.025)
    assert clock.now - before <= 0.05
    assert any(r.get('event') == 'telemetry_poll_deferred' for r in records) or calls['n'] >= 1
    # AOUT path still independent of telemetry.
    dispatcher.command(SUPPLY, 0.2)
    assert dispatcher.states[SUPPLY].value == 0.2
    assert not any(r.get('event') == 'independent_feedback' for r in records)
