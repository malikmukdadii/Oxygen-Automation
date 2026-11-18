"""Offline regenerative deceleration experiment tests (memory + synthetic series only)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from oxygen_engine.dispatcher import MOTOR, SUPPLY, Dispatcher
from oxygen_engine.drive_limits import (
    DEFAULT_COMMISSIONED_DRIVE_LIMIT_V,
    resolve_effective_drive_max,
)
from oxygen_engine.energy_analysis import (
    compute_metrics,
    load_sync_csv,
    synthetic_regen_series,
    trapezoidal_energy,
)
from oxygen_engine.errors import AuthorizationError, ConfigError
from oxygen_engine.main import main
from oxygen_engine.memory_backend import MemoryBackend
from oxygen_engine.oxygen_backend import OxygenBackend
from oxygen_engine.regen_experiment import (
    RegenExperimentRunner,
    load_experiment_config,
    plan_matrix,
    validate_experiment_plan,
)
from oxygen_engine.schema import load_dispatcher_config

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_RELEASE = ROOT / 'experiments' / 'regen_release_comparison.example.json'
EXPERIMENT_SPEED = ROOT / 'experiments' / 'regen_speed_comparison.example.json'
EXPERIMENT_BUS = ROOT / 'experiments' / 'regen_bus_voltage_comparison.example.json'
MEMORY_REGEN = ROOT / 'examples' / 'memory-regen.json'


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def _emit_collect():
    records = []

    def emit(record):
        records.append(record)

    return records, emit


def test_example_experiments_validate_offline():
    for path in (EXPERIMENT_RELEASE, EXPERIMENT_SPEED, EXPERIMENT_BUS):
        plan = validate_experiment_plan(path, MEMORY_REGEN)
        assert plan['experiment_type'] == 'regenerative_deceleration'
        assert plan['connected'] is False
        assert plan['braking_control'] == 'unused'
        assert plan['drive_limit']['effective_max_v'] == pytest.approx(0.4)


def test_default_drive_limit_040_still_valid():
    decision = resolve_effective_drive_max(
        dispatcher_max_v=0.40,
        commissioned_drive_limit_v=0.40,
        higher_drive_limit_authorized=False,
        requested_v=0.40,
    )
    assert decision.effective_max_v == pytest.approx(0.40)


def test_uncommissioned_higher_drive_rejected():
    with pytest.raises(AuthorizationError):
        resolve_effective_drive_max(
            dispatcher_max_v=0.40,
            commissioned_drive_limit_v=0.70,
            higher_drive_limit_authorized=False,
            requested_v=0.70,
        )
    with pytest.raises(AuthorizationError):
        resolve_effective_drive_max(
            dispatcher_max_v=0.40,
            commissioned_drive_limit_v=0.40,
            higher_drive_limit_authorized=False,
            requested_v=0.55,
        )


def test_authorized_higher_works_in_memory_backend(tmp_path):
    raw = json.loads(MEMORY_REGEN.read_text(encoding='utf-8'))
    raw['outputs'][MOTOR]['max_aout_v'] = 0.70
    raw['outputs'][MOTOR]['commissioned_drive_limit_v'] = 0.70
    raw['outputs'][MOTOR]['higher_drive_limit_authorized'] = True
    raw['simulation']['outputs']['motor_throttle_fixture']['max_v'] = 0.70
    for assignment in raw['assignments']:
        profile = Path(assignment['profile'])
        if not profile.is_absolute():
            assignment['profile'] = str((MEMORY_REGEN.parent / profile).resolve())
    path = tmp_path / 'higher.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    config = load_dispatcher_config(path)
    assert config.raw['outputs'][MOTOR]['max_aout_v'] == pytest.approx(0.70)
    records, emit = _emit_collect()
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, emit, clock=clock, wait=clock.wait)
    backend.connect()
    dispatcher.prepare()
    dispatcher.command(SUPPLY, 0.5)
    dispatcher.command(MOTOR, 0.70)
    assert backend.read_output('motor_throttle_fixture') == pytest.approx(0.70)
    decision = resolve_effective_drive_max(
        dispatcher_max_v=0.70,
        commissioned_drive_limit_v=0.70,
        higher_drive_limit_authorized=True,
        requested_v=0.70,
    )
    assert decision.effective_max_v == pytest.approx(0.70)
    # Commands never exceed min(dispatcher, experiment)
    with pytest.raises(AuthorizationError):
        resolve_effective_drive_max(
            dispatcher_max_v=0.70,
            commissioned_drive_limit_v=0.55,
            higher_drive_limit_authorized=True,
            requested_v=0.70,
        )


def test_schema_rejects_sim_raise_without_authorization(tmp_path):
    raw = json.loads(MEMORY_REGEN.read_text(encoding='utf-8'))
    raw['outputs'][MOTOR]['max_aout_v'] = 0.70
    for assignment in raw['assignments']:
        profile = Path(assignment['profile'])
        if not profile.is_absolute():
            assignment['profile'] = str((MEMORY_REGEN.parent / profile).resolve())
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(ConfigError):
        load_dispatcher_config(path)


def test_effective_max_is_lowest_bound():
    decision = resolve_effective_drive_max(
        dispatcher_max_v=0.85,
        commissioned_drive_limit_v=0.55,
        higher_drive_limit_authorized=True,
        requested_v=0.50,
    )
    assert decision.effective_max_v == pytest.approx(0.55)


def test_plan_matrix_keeps_disabled_speed_visible():
    experiment = load_experiment_config(EXPERIMENT_SPEED)
    plans = plan_matrix(experiment)
    assert any(not p.get('speed_enabled', True) for p in plans)
    assert any(p.get('target_erpm') == 12000 for p in plans)


def test_release_comparison_runs_offline_memory():
    experiment = load_experiment_config(EXPERIMENT_RELEASE)
    config = load_dispatcher_config(MEMORY_REGEN)
    records, emit = _emit_collect()
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, emit, clock=clock, wait=clock.wait)
    backend.connect()
    dispatcher.prepare()
    runner = RegenExperimentRunner(experiment, dispatcher, emit)
    summary = runner.run()
    assert summary['braking_control'] == 'unused'
    assert summary['simulated'] is True
    assert summary['aggregation_valid_trials']['count'] >= 1
    assert all(t['simulated'] for t in summary['trials'] if t['valid'])
    assert any(r.get('event') == 'feedback_identities_pinned' for r in records)
    pin_events = [r for r in records if r.get('event') == 'feedback_identities_pinned']
    assert pin_events[0]['rediscover_every_poll'] is False
    assert pin_events[0]['identities']['drive_control'] == 'Drive Control'
    assert pin_events[0]['identities']['takasago_voltage_control'] == 'Takasago Voltage Control'


def test_bus_and_speed_examples_run_offline():
    for path in (EXPERIMENT_BUS, EXPERIMENT_SPEED):
        experiment = load_experiment_config(path)
        config = load_dispatcher_config(MEMORY_REGEN)
        _, emit = _emit_collect()
        clock = Clock()
        backend = MemoryBackend(config, clock=clock)
        dispatcher = Dispatcher(config, backend, emit, clock=clock, wait=clock.wait)
        backend.connect()
        dispatcher.prepare()
        summary = RegenExperimentRunner(experiment, dispatcher, emit).run()
        assert summary['aggregation_valid_trials']['count'] >= 1
        # Disabled / invalid trials remain visible.
        if path == EXPERIMENT_SPEED:
            assert summary['invalid_trials']


def test_energy_metrics_label_simulated_and_ratio():
    series = synthetic_regen_series(
        release_duration_s=0.5, start_erpm=8000, bus_v=40.0, drive_peak_v=0.4, observe_s=0.5)
    assert series.simulated is True
    metrics = compute_metrics(series, bus_target_v=40.0)
    assert metrics.simulated is True
    assert metrics.e_return_j > 0
    assert metrics.electrical_energy_return_ratio is None or metrics.electrical_energy_return_ratio >= 0
    assert 'not mechanical efficiency' in ' '.join(metrics.notes)


def test_trapezoidal_integration_and_csv_import(tmp_path):
    path = tmp_path / 'sync.csv'
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(
            handle, fieldnames=['time_s', 'dc_voltage_v', 'dc_current_a', 'erpm', 'drive_control_v'])
        writer.writeheader()
        writer.writerow({'time_s': 0.0, 'dc_voltage_v': 40, 'dc_current_a': 1.0, 'erpm': 8000, 'drive_control_v': 0.4})
        writer.writerow({'time_s': 1.0, 'dc_voltage_v': 40, 'dc_current_a': -2.0, 'erpm': 4000, 'drive_control_v': 0.0})
        writer.writerow({'time_s': 2.0, 'dc_voltage_v': 40, 'dc_current_a': -1.0, 'erpm': 1000, 'drive_control_v': 0.0})
    series = load_sync_csv(path, simulated=False)
    metrics = compute_metrics(series)
    assert metrics.simulated is False
    assert metrics.e_return_j > 0
    e_in, e_return, e_net = trapezoidal_energy(series.time_s, series.dc_power_w)
    assert e_in >= 0 and e_return >= 0
    assert e_net == pytest.approx(e_in - e_return)


def test_csv_rejects_nonmonotonic_time(tmp_path):
    path = tmp_path / 'bad.csv'
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['time_s', 'dc_power_w'])
        writer.writeheader()
        writer.writerow({'time_s': 1.0, 'dc_power_w': 1})
        writer.writerow({'time_s': 0.5, 'dc_power_w': -1})
    with pytest.raises(ConfigError):
        load_sync_csv(path)


def test_reject_invalid_release_timing():
    raw = json.loads(EXPERIMENT_RELEASE.read_text(encoding='utf-8'))
    raw['release_durations_s'] = [0.01]
    path = ROOT / 'experiments' / '_tmp_invalid_timing.json'
    try:
        path.write_text(json.dumps(raw), encoding='utf-8')
        with pytest.raises(ConfigError, match='50 ms'):
            load_experiment_config(path)
    finally:
        path.unlink(missing_ok=True)


def test_cli_run_experiment_plan_and_execute(tmp_path):
    assert main([
        'run-experiment',
        '--experiment', str(EXPERIMENT_RELEASE),
        '--bindings', str(MEMORY_REGEN),
    ]) == 0
    out = tmp_path / 'artifacts'
    assert main([
        'run-experiment',
        '--experiment', str(EXPERIMENT_RELEASE),
        '--bindings', str(MEMORY_REGEN),
        '--execute',
        '--confirm-simulated-output',
        '--output-dir', str(out),
    ]) == 0
    assert list(out.glob('*-results.json'))
    assert list(out.glob('*-comparison.csv'))


def test_cli_blocks_physical_enable(tmp_path):
    raw = json.loads(EXPERIMENT_RELEASE.read_text(encoding='utf-8'))
    raw['simulation_only'] = False
    raw['physical_enabled'] = True
    raw['illustrative_only'] = False
    path = tmp_path / 'physical.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    assert main([
        'run-experiment',
        '--experiment', str(path),
        '--bindings', str(MEMORY_REGEN),
        '--execute',
        '--confirm-simulated-output',
    ]) == 1


def test_fault_does_not_auto_resume():
    experiment = load_experiment_config(EXPERIMENT_RELEASE)
    config = load_dispatcher_config(MEMORY_REGEN)
    records, emit = _emit_collect()
    clock = Clock()
    backend = MemoryBackend(config, clock=clock)
    dispatcher = Dispatcher(config, backend, emit, clock=clock, wait=clock.wait)
    backend.connect()
    dispatcher.prepare()
    runner = RegenExperimentRunner(experiment, dispatcher, emit)

    def boom(_name):
        raise RuntimeError('injected feedback fault')

    runner.feedback_provider = boom
    # Force phase C to require measured ERPM path via provider during bus read first.
    experiment['start_condition']['require_measured_erpm'] = True
    # Make bus read raise on first trial after phase A by failing provider for bus_v.
    calls = {'n': 0}

    def flaky(name):
        calls['n'] += 1
        if name == 'bus_v' and calls['n'] > 2:
            raise RuntimeError('bus feedback lost')
        if name == 'erpm':
            return 8000.0
        return 40.0

    runner.feedback_provider = flaky
    summary = runner.run()
    assert runner.faulted is True
    assert any(r.get('event') == 'experiment_fault' for r in records)
    assert all(item.get('auto_resume', False) is False
               for item in records if item.get('event') == 'experiment_fault')
    # No auto-resume: remaining planned trials after fault are not all executed as valid.
    assert any(not t['valid'] for t in summary['trials'])


def test_feedback_pin_once_no_list_channels_on_button_poll(monkeypatch):
    """read_button must not rediscover via list_channels after connect pin."""
    # Use a lightweight stub: after connect, clearing do_channels still uses pinned input_ids
    # without calling list_channels.
    calls = {'list': 0}
    config = load_dispatcher_config(ROOT / 'examples' / 'oxygen-demo.json')

    class StubAdapter:
        def list_channels(self):
            calls['list'] += 1
            return []

    # Unit-level: OxygenBackend.read_button with pre-pinned ids.
    backend = OxygenBackend.__new__(OxygenBackend)
    backend.lock = __import__('threading').RLock()
    backend.do_channels = {}
    backend.input_ids = {'trigger:demo': ('DO Demo', '9')}
    backend.adapter = StubAdapter()

    def fake_boolean(cid, prop):
        return False

    def fake_error():
        return None

    backend._boolean = fake_boolean
    backend.error = fake_error
    assert backend.read_button('DO Demo') is False
    assert calls['list'] == 0
    assert 'DO Demo' in backend.do_channels


def test_analyze_regen_csv_cli(tmp_path):
    series = synthetic_regen_series(
        release_duration_s=0.2, start_erpm=5000, bus_v=36.0, drive_peak_v=0.4, observe_s=0.2)
    csv_path = tmp_path / 'series.csv'
    with csv_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=['time_s', 'dc_voltage_v', 'dc_current_a', 'dc_power_w', 'erpm', 'drive_control_v'],
        )
        writer.writeheader()
        for index, t in enumerate(series.time_s):
            writer.writerow({
                'time_s': t,
                'dc_voltage_v': series.dc_voltage_v[index],
                'dc_current_a': series.dc_current_a[index],
                'dc_power_w': series.dc_power_w[index],
                'erpm': series.erpm[index],
                'drive_control_v': series.drive_control_v[index],
            })
    out = tmp_path / 'analysis'
    assert main([
        'analyze-regen-csv',
        '--csv', str(csv_path),
        '--simulated',
        '--output-dir', str(out),
    ]) == 0
    assert (out / 'energy_metrics.json').exists()


def test_sim_pass_does_not_mean_physical_enable():
    experiment = load_experiment_config(EXPERIMENT_RELEASE)
    assert experiment['physical_enabled'] is False
    assert experiment['simulation_only'] is True
    assert experiment.get('illustrative_only') is True
    assert DEFAULT_COMMISSIONED_DRIVE_LIMIT_V == pytest.approx(0.40)
