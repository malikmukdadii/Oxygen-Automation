"""Coordinated experiments: accelerated memory integration and explicit fault injection."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from oxygen_engine.errors import ConfigError, ControlError
from oxygen_engine.experiment_feedback import MemoryExperimentFeedback
from oxygen_engine.experiment_plan import MOTOR, NUMERIC_UNITS, SUPPLY, load_experiment, memory_mirror
from oxygen_engine.experiments import ExperimentRunner
from oxygen_engine.main import main
from oxygen_engine.memory_backend import MemoryBackend
from oxygen_engine.output_lease import OutputLease
from oxygen_engine.schema import load_dispatcher_config
from oxygen_engine.scpi_transport import ScpiCommandError, ScpiErrorEntry

ROOT = Path(__file__).resolve().parents[1]
GRID = ROOT / 'experiments/dc_ac_grid.example.json'
DYNAMIC = ROOT / 'experiments/dynamic_motor.example.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr('socket.socket', lambda *a, **k: pytest.fail('No live sockets in experiment tests'))
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: pytest.fail('No live connection'))


def physical_config(tmp_path, limit=.4):
    raw = json.loads((ROOT / 'examples/physical-dual.json').read_text())
    raw['outputs'][SUPPLY]['channel'] = 'Takasago Voltage Control'
    raw['outputs'][MOTOR]['channel'] = 'Drive Control'
    raw['outputs'][MOTOR]['max_aout_v'] = limit
    for a in raw['assignments']:
        a['profile'] = str((ROOT / 'examples' / a['profile']).resolve())
    path = tmp_path / 'bindings.json'
    path.write_text(json.dumps(raw))
    return load_dispatcher_config(path)


def make_runner(tmp_path, *, full=False, dynamic=False, transform=None, raw=None):
    config = physical_config(tmp_path, .55 if full else .4)
    raw = raw or json.loads((DYNAMIC if dynamic else GRID).read_text())
    if full:
        raw['commissioned_drive_limit_v'] = .55
        raw['higher_drive_limit_authorized'] = True
        for target in raw['motor_targets']:
            target.update(enabled=True, commissioned=True)
    path = tmp_path / 'experiment.json'
    path.write_text(json.dumps(raw))
    plan = load_experiment(path, config)
    mirror = memory_mirror(config)
    clock, records = Clock(), []
    backend = MemoryBackend(mirror, clock=clock)
    runner = None

    def feedback_factory(b, specs, c):
        model = MemoryExperimentFeedback(b, specs, c)
        return SimpleNamespace(read=lambda: transform(model.read(), runner) if transform else model.read())

    runner = ExperimentRunner(mirror, plan, backend, records.append, clock=clock, wait=clock.wait,
                              feedback_factory=feedback_factory)
    return runner, backend, records


def test_successful_commissioned_twelve_point_sweep(tmp_path):
    runner, backend, records = make_runner(tmp_path, full=True)
    assert runner.run() == 0
    points = [r for r in records if r['event'] == 'operating_point_result']
    assert len(points) == 12 and all(r['sample_valid'] for r in points)
    assert [(r['commanded_bus_v'], r['commanded_motor_adc_v']) for r in points] == [
        (bus, drive) for bus in (36, 38, 40) for drive in (.25, .35, .45, .55)]
    assert all(r['native_capture_confirmed'] and r['matrix_samples_after'] > r['matrix_samples_before'] for r in points)
    assert all(r['dc_to_ac_efficiency_pct'] == pytest.approx(85) for r in points)
    assert all(r['measurement_count'] >= 3 and r['settle_status'] == 'settled' for r in points)
    assert all(r['phase'] == 'return_neutral' for r in points)
    assert runner.writer.states[MOTOR].value == 0
    assert runner.writer.states[SUPPLY].value == .5
    assert records[-1]['full_grid_complete'] is True
    assert len({r['experiment_id'] for r in points}) == 1
    assert runner.clock() == pytest.approx(210)
    assert not backend.connected
    # Every supply command has a fresh neutral readback and low-speed frame in its guard.
    assert not any(r == 'vesc_brake' for _, r, _ in backend.commands)
    assert all(value <= (.5 if role == SUPPLY else .55) for _, role, value in backend.commands)


def test_initial_plan_never_writes_disabled_high_targets(tmp_path):
    runner, backend, records = make_runner(tmp_path)
    assert runner.run() == 0
    points = [r for r in records if r['event'] == 'operating_point_result']
    assert len(points) == 12
    assert sum(r['sample_valid'] for r in points) == 6
    assert sum(r['status'] == 'disabled' for r in points) == 6
    assert all(v <= .35 for _, role, v in backend.commands if role == MOTOR)
    assert records[-1]['full_grid_complete'] is False
    assert runner.clock() == pytest.approx(114)


@pytest.mark.parametrize('problem', ['uncommissioned', 'config_clamp', 'plan_clamp', 'higher_ack', 'wrong_gain', 'wrong_unit'])
def test_out_of_range_or_uncommissioned_plan_refused_before_execution(tmp_path, problem):
    config = physical_config(tmp_path, .55 if problem in {'plan_clamp', 'higher_ack'} else .4)
    raw = json.loads(GRID.read_text())
    if problem == 'wrong_gain':
        config.raw['outputs'][SUPPLY]['mapping']['gain_bus_v_per_aout_v'] = 79
    elif problem == 'wrong_unit':
        raw['feedback']['dc_current']['unit'] = 'mA'
    else:
        raw['motor_targets'][2]['enabled'] = True
        raw['motor_targets'][2]['commissioned'] = problem != 'uncommissioned'
        if problem in {'config_clamp', 'higher_ack'}:
            raw['commissioned_drive_limit_v'] = .55
            raw['higher_drive_limit_authorized'] = problem != 'higher_ack'
    path = tmp_path / 'bad-plan.json'
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError):
        load_experiment(path, config)


def test_bus_convergence_failure_never_permits_drive(tmp_path):
    def transform(frame, runner):
        if runner.phase == 'bus_convergence':
            v = frame['values']
            v['dc_bus_voltage'] = 20
            v['dc_power'] = 20 * v['dc_current']
        return frame

    runner, backend, records = make_runner(tmp_path, transform=transform)
    assert runner.run() == 1
    assert not any(role == MOTOR and value > 0 for _, role, value in backend.commands)
    assert any('convergence' in r.get('error', '') for r in records)
    assert not any(r.get('sample_valid') for r in records)


@pytest.mark.parametrize('problem', ['stale', 'nan', 'future', 'fake_source', 'can_age', 'frozen_acquisition',
                                      'physical_fault', 'source_not_ready', 'overcurrent', 'overspeed',
                                      'no_native_capture', 'native_invalid', 'unstable'])
def test_invalid_feedback_or_capture_aborts_and_neutralizes(tmp_path, problem):
    def transform(frame, runner):
        if runner.phase not in {'capture_window', 'settling'}:
            return frame
        v = frame['values']
        if problem == 'stale':
            frame['sampled_at'] -= 10
        elif problem == 'future':
            frame['sampled_at'] += 10
        elif problem == 'fake_source':
            frame['source'] = 'untrusted'
        elif problem == 'nan':
            v['dc_current'] = float('nan')
        elif problem == 'can_age':
            v['vesc_telemetry_age_s'] = 10
        elif problem == 'frozen_acquisition':
            v['acquisition_time_s'] = 0
        elif problem == 'physical_fault':
            v['physical_fault'] = True
        elif problem == 'source_not_ready':
            v['source_sink_ready'] = False
        elif problem == 'overcurrent':
            v['motor_current'] = 100
        elif problem == 'overspeed':
            v['erpm'] = 100000
        elif problem == 'no_native_capture':
            v['matrix_sample_count'] = 0
        elif problem == 'native_invalid':
            v['matrix_sample_valid'] = False
        else:
            v['erpm'] = 500 if int(runner.clock() * 4) % 2 else 100
        return frame

    runner, _, records = make_runner(tmp_path, transform=transform)
    assert runner.run() == 1
    assert runner.writer.states[MOTOR].value == 0
    assert not any(r.get('sample_valid') for r in records)
    assert records[-1]['automatic_resume'] is False
    json.dumps(records, allow_nan=False)
    with pytest.raises(ControlError, match='one-shot'):
        runner.run()


def test_neutral_command_is_not_assumed_to_mean_stopped_motor(tmp_path):
    def transform(frame, runner):
        frame['values']['erpm'] = 200
        return frame

    runner, backend, records = make_runner(tmp_path, transform=transform)
    assert runner.run() == 1
    assert not any(role == SUPPLY for _, role, _ in backend.commands)
    assert any('coast' in r.get('error', '') for r in records)


def test_scpi_200_no_retry_and_supply_remains_historical(tmp_path, monkeypatch):
    runner, backend, records = make_runner(tmp_path)
    original, failed = backend.write, []

    def write(role, value):
        if role == SUPPLY and value > .1:
            failed.append(value)
            raise ScpiCommandError('scripted constant write', ScpiErrorEntry(-200, 'Execution error'))
        original(role, value)

    monkeypatch.setattr(backend, 'write', write)
    assert runner.run() == 1
    assert len(failed) == 1
    assert runner.writer.states[MOTOR].value == 0
    assert not any(r.get('sample_valid') for r in records)
    assert any(r.get('error_type') == 'ScpiCommandError' for r in records)


def test_invalid_output_readback_preserves_supply_uncertainty(tmp_path, monkeypatch):
    runner, backend, records = make_runner(tmp_path)
    original = backend.read_output
    binding = backend.config.raw['outputs'][SUPPLY]['binding']
    failed = False

    def read_output(key):
        nonlocal failed
        if key == binding and backend.writes_attempted >= 3:
            failed = True
        return float('nan') if failed and key == binding else original(key)

    monkeypatch.setattr(backend, 'read_output', read_output)
    assert runner.run() == 1
    assert records[-1]['status'] == 'cleanup_unknown'
    assert runner.writer.states[SUPPLY].status == 'unknown'
    assert runner.writer.states[MOTOR].value == 0


def test_loss_of_connection_does_not_resume_or_claim_cleanup(tmp_path, monkeypatch):
    runner, backend, records = make_runner(tmp_path)
    original = backend.write
    failures = []

    def write(role, value):
        if role == SUPPLY and value > .1:
            failures.append(value)
            backend.connected = False
            raise ConnectionError('scripted connection loss')
        original(role, value)

    monkeypatch.setattr(backend, 'write', write)
    assert runner.run() == 1
    assert len(failures) == 1
    assert records[-1]['status'] == 'cleanup_unknown'
    assert any(r['event'] == 'motor_neutral_unknown' for r in records)


def test_cancellation_attempts_neutral_and_preserves_supply(tmp_path):
    def transform(frame, runner):
        if runner.phase == 'capture_window':
            raise KeyboardInterrupt()
        return frame

    runner, _, records = make_runner(tmp_path, transform=transform)
    assert runner.run() == 130
    assert runner.writer.states[MOTOR].value == 0
    assert runner.writer.states[SUPPLY].value == .45
    assert records[-1]['status'] == 'cancelled'
    assert not any(r.get('sample_valid') for r in records)


def test_dynamic_current_mode_sequence_is_not_a_matrix_or_regen_claim(tmp_path):
    runner, backend, records = make_runner(tmp_path, dynamic=True)
    assert runner.run() == 0
    points = [r for r in records if r['event'] == 'operating_point_result']
    assert len(points) == 3 and all(r['status'] == 'demonstration_completed' for r in points)
    assert records[-1]['completed_steps'] == 3
    assert not any(r['sample_valid'] or r['native_capture_confirmed'] for r in points)
    assert all(v <= .35 for _, role, v in backend.commands if role == MOTOR)
    assert runner.writer.states[MOTOR].value == 0
    assert runner.clock() == pytest.approx(36)


def test_native_acknowledgement_does_not_make_an_undersampled_window_valid(tmp_path, monkeypatch):
    runner, _, records = make_runner(tmp_path)

    def undersampled_hold(duration, drive):
        row = runner.last_values.copy()
        row['matrix_sample_count'] += 1
        row['matrix_sample_valid'] = True
        return [row]

    monkeypatch.setattr(runner, 'hold', undersampled_hold)
    assert runner.run() == 1
    assert not any(r.get('sample_valid') for r in records)
    assert runner.writer.states[MOTOR].value == 0


def test_endpoint_lease_excludes_other_cli_owner_and_releases():
    connection = {'host': '127.0.0.1', 'port': 61997}
    with OutputLease(connection):
        with pytest.raises(ControlError, match='Another dispatcher'):
            with OutputLease({'host': 'localhost', 'port': 61997}):
                pytest.fail('Must not acquire a second output owner')
    with OutputLease(connection):
        pass


def test_cli_preview_and_memory_rehearsal_never_connect(tmp_path, capsys):
    bindings = physical_config(tmp_path).path
    assert main(['run-experiment', '--bindings', str(bindings), '--experiment', str(GRID)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['connected'] is False and len(preview['points']) == 12
    assert main(['run-experiment', '--bindings', str(bindings), '--experiment', str(GRID),
                 '--memory', '--execute', '--confirm-simulated-output', '--log-file', str(tmp_path / 'memory.jsonl')]) == 0
    assert 'completed_enabled_points' in capsys.readouterr().out
    assert main(['run-experiment', '--bindings', str(bindings), '--experiment', str(GRID),
                 '--execute', '--live', '--confirm-output', 'Drive Control',
                 '--confirm-output', 'Takasago Voltage Control']) == 2


def test_real_feedback_batch_protocol_pins_identity_units_and_allowlist(tmp_path, monkeypatch):
    import threading
    from unittest.mock import Mock
    from oxygen_engine.adapters.oxygen import OxygenAdapter
    from oxygen_engine.experiment_feedback import OxygenExperimentFeedback
    from oxygen_engine.oxygen_backend import OxygenBackend, OxygenTransport
    from oxygen_engine.scpi_transport import ScpiProtocolError, parse_scpi, quote_scpi

    config = physical_config(tmp_path)
    specs = json.loads(GRID.read_text())['feedback']
    for role, spec in specs.items():
        spec['channel'] = 'Actual input ' + role
        if 'property' in spec:
            spec['property'] = 'DO_Value'
    ids = {role: str(8000 + i) for i, role in enumerate(specs)}
    channels = {specs[r]['channel']: cid for r, cid in ids.items()}
    output_ids = {SUPPLY: '1001', MOTOR: '1002'}
    channels.update({s['channel']: output_ids[r] for r, s in config.raw['outputs'].items()})
    commands, records = [], []
    transport = OxygenTransport(host='127.0.0.1', port=1, timeout_s=.2, emit=records.append, physical=True)
    transport._sock, transport._connected = Mock(), True
    transport._sock.sendall.side_effect = lambda payload: commands.append(payload.decode().strip())
    numeric = [r for r in specs if r in NUMERIC_UNITS]
    values = dict(dc_bus_voltage=36, dc_current=.7, dc_power=25.2, ac_power=21.42, erpm=250,
                  motor_current=1, acquisition_time_s=10, vesc_telemetry_age_s=.01, matrix_sample_count=4)

    def reply():
        command = commands[-1]
        if command == ':CHANNELlist:NAMes?':
            return ','.join(f'({quote_scpi(cid)},{quote_scpi(name)})' for name, cid in channels.items())
        if command.startswith(':CHANNELlist:ITEM'):
            return '"Unit","DO_Value"'
        if command.startswith(':CHANNELlist:PROPerty? '):
            cid, prop = parse_scpi(command.split(' ', 1)[1])
            role = next(r for r in ids if ids[r] == cid)
            if prop == 'Unit':
                return f'(STRING,{quote_scpi(specs[role]["unit"])})'
            return '(BOOL,OFF)' if role == 'physical_fault' else '(BOOL,ON)'
        if command == 'SYST:ERR?':
            return '0,"No error"'
        if command == ':ACQUisition:STATe?':
            return 'Started'
        if command == ':NUMeric:NORMal:DIMS?':
            return ','.join('1' for _ in numeric)
        if command == ':NUMeric:NORMal:VALue?':
            return ','.join(str(values[r]) for r in numeric)
        raise AssertionError(command)

    monkeypatch.setattr(transport, '_readline', reply)
    backend = SimpleNamespace(transport=transport, adapter=OxygenAdapter(transport), lock=threading.RLock(),
                              channel_ids=output_ids, do_channels={}, physical=True)
    backend.error = lambda: OxygenBackend.error(backend)
    backend._boolean = lambda cid, prop: OxygenBackend._boolean(backend, cid, prop)
    feedback = OxygenExperimentFeedback(backend, specs, Clock())
    frame = feedback.read()
    assert frame['values']['dc_power'] == 25.2 and frame['simulated'] is False
    assert frame['values']['source_sink_ready'] is True
    assert not any(c.startswith(':CHANNELlist:PROPerty ') for c in commands)
    assert sum(c.startswith(':NUMeric:NORMal:ITEMS ') for c in commands) == 1
    with pytest.raises(ScpiProtocolError):
        transport.write(':NUMeric:NORMal:ITEMS "Drive Control"')
    channels[specs['dc_bus_voltage']['channel']] = '99999'
    with pytest.raises(Exception, match='identity changed'):
        feedback.read()
    transport.close()


def test_supply_scpi_200_through_real_backend_aborts_experiment(tmp_path, monkeypatch):
    from test_scpi_ramp_faults import make_dispatcher

    _, backend, device, _ = make_dispatcher(tmp_path, monkeypatch, 'queue_none')
    raw = json.loads(GRID.read_text())
    raw['bus_targets_v'] = [40]
    raw['motor_targets'] = raw['motor_targets'][:1]
    raw['commissioning'] = {key: True for key in raw['commissioning']}
    for role, spec in raw['feedback'].items():
        spec['channel'] = 'Scripted independent ' + role
        if 'property' in spec:
            spec['property'] = 'DO_Value'
    path = tmp_path / 'physical-scripted-plan.json'
    path.write_text(json.dumps(raw))
    plan = load_experiment(path, backend.config)
    records = []

    def factory(b, specs, clock):
        model = MemoryExperimentFeedback(b, specs, clock)

        def read():
            frame = model.read()
            frame.update(source='OXYGEN scripted device feedback', simulated=False)
            return frame
        return SimpleNamespace(read=read)

    runner = ExperimentRunner(backend.config, plan, backend, records.append, clock=backend.clock,
                              wait=backend.clock.wait, feedback_factory=factory)
    assert runner.run() == 1
    assert device.triggered and device.connect_count == 1
    assert device.commands.count(device.failed_command) == 1
    assert any(r.get('error_type') == 'ScpiCommandError' for r in records)
    assert runner.writer.states[MOTOR].value == 0
    assert runner.writer.states[SUPPLY].status == 'unknown'
    assert records[-1]['status'] == 'cleanup_unknown'
    assert not any(r.get('sample_valid') for r in records)
