"""Offline analysis / visualization helpers for regenerative deceleration trials."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .energy_analysis import (
    SyncSeries,
    compute_metrics,
    extract_trial_series,
    load_sync_csv,
)


def series_to_trace_rows(series: SyncSeries) -> list[dict]:
    rows = []
    for index, t in enumerate(series.time_s):
        row = {
            'time_s': t,
            'P_DC_W': series.dc_power_w[index] if series.dc_power_w else None,
            'ERPM': series.erpm[index] if series.erpm else None,
            'Drive_Control_V': series.drive_control_v[index] if series.drive_control_v else None,
            'marker': series.markers[index] if series.markers and index < len(series.markers) else '',
            'trial_id': series.trial_id,
            'simulated': series.simulated,
        }
        rows.append(row)
    return rows


def write_time_series_csv(path, series: SyncSeries) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = series_to_trace_rows(series)
    if not rows:
        path.write_text('', encoding='utf-8')
        return path
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def comparison_scatter_rows(results_json) -> list[dict]:
    data = json.loads(Path(results_json).read_text(encoding='utf-8'))
    rows = []
    for trial in data.get('trials', []):
        metrics = trial.get('metrics') or {}
        rows.append({
            'trial_id': trial.get('trial_id'),
            'valid': trial.get('valid'),
            'release_duration_s': trial.get('release_duration_s'),
            'start_erpm': trial.get('target_erpm') or metrics.get('start_erpm'),
            'bus_voltage_v': trial.get('bus_voltage_v'),
            'E_return_J': metrics.get('e_return_j'),
            'electrical_energy_return_ratio': metrics.get('electrical_energy_return_ratio'),
            'peak_neg_power_w': metrics.get('peak_neg_power_w'),
            'simulated': trial.get('simulated', metrics.get('simulated')),
            'invalid_reason': trial.get('invalid_reason'),
            'csv_window_start_s': trial.get('csv_window_start_s'),
            'csv_window_end_s': trial.get('csv_window_end_s'),
        })
    return rows


def write_report_markdown(path, results_json, *, series: SyncSeries | None = None) -> Path:
    """Generate a concise markdown report; plots are CSV traces (no GUI automation)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(results_json).read_text(encoding='utf-8'))
    agg = data.get('aggregation_valid_trials', {})
    lines = [
        f"# Regenerative deceleration report: {data.get('experiment')}",
        '',
        f"- Comparison axis: `{data.get('comparison')}`",
        f"- Simulated run: `{data.get('simulated')}`",
        f"- Physical enabled: `{data.get('physical_enabled')}`",
        f"- Braking Control: unused",
        '',
        '## Aggregation (valid trials only)',
        '',
        f"- count: {agg.get('count')}",
        f"- mean E_return (J): {agg.get('mean_e_return_j')}",
        f"- std E_return (J): {agg.get('std_e_return_j')}",
        f"- mean peak negative power (W): {agg.get('mean_peak_neg_power_w')}",
        f"- mean regen duration (s): {agg.get('mean_regen_duration_s')}",
        '',
        'Electrical energy-return ratio is **not** mechanical efficiency.',
        'Simulated energy is labeled simulated.',
        '',
        '## Invalid trials (kept visible)',
        '',
    ]
    invalid = data.get('invalid_trials') or []
    if not invalid:
        lines.append('- none')
    else:
        for item in invalid:
            lines.append(f"- {item.get('trial_id')}: {item.get('reason')} (phase={item.get('phase')})")
    if series is not None:
        metrics = compute_metrics(series)
        lines.extend([
            '',
            '## Attached series metrics',
            '',
            f"- trial_id: {series.trial_id}",
            f"- simulated: {metrics.simulated}",
            f"- E_in (J): {metrics.e_in_j}",
            f"- E_return (J): {metrics.e_return_j}",
            f"- electrical energy-return ratio: {metrics.electrical_energy_return_ratio}",
        ])
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def analyze_csv_cli(
    csv_path,
    *,
    channel_map=None,
    simulated=False,
    output_dir='logs/regen-analysis',
    trial_id=None,
    window=None,
    marker_start=None,
    marker_end=None,
    marker_column='marker',
    polarity='rzxa_supply_positive',
    time_unit='s',
) -> dict:
    series = load_sync_csv(
        csv_path,
        channel_map=channel_map,
        simulated=simulated,
        polarity=polarity,
        time_unit=time_unit,
        marker_column=marker_column,
    )
    # Whole-file analysis only when no trial association was requested.
    allow_whole = trial_id is None and window is None and not marker_start and not marker_end
    trial_series = extract_trial_series(
        series,
        trial_id=trial_id,
        window=window,
        marker_start=marker_start,
        marker_end=marker_end,
        allow_whole_series=allow_whole,
    )
    metrics = compute_metrics(trial_series)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    suffix = trial_id or 'whole'
    trace = write_time_series_csv(out / f'p_dc_erpm_drive_trace_{suffix}.csv', trial_series)
    metrics_path = out / f'energy_metrics_{suffix}.json'
    metrics_path.write_text(json.dumps(metrics.as_dict(), indent=2), encoding='utf-8')
    return {
        'metrics': metrics.as_dict(),
        'trace_csv': str(trace),
        'metrics_json': str(metrics_path),
        'trial_id': trial_id,
        'window': list(window) if window else None,
        'channel_map': channel_map or {},
        'polarity': polarity,
        'time_unit': time_unit,
        'label': 'simulated' if trial_series.simulated else 'sync_acquisition',
    }
