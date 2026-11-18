"""Electrical energy metrics from OXYGEN sync CSV (authoritative) or labeled simulated series."""

from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import ConfigError


DEFAULT_CHANNEL_MAP = {
    'time_s': 'time_s',
    'dc_voltage_v': 'dc_voltage_v',
    'dc_current_a': 'dc_current_a',
    'dc_power_w': 'dc_power_w',
    'erpm': 'erpm',
    'drive_control_v': 'drive_control_v',
}


@dataclass
class EnergyMetrics:
    e_in_j: float
    e_return_j: float
    e_net_j: float
    electrical_energy_return_ratio: float | None
    peak_neg_current_a: float
    peak_neg_power_w: float
    regen_duration_s: float
    start_erpm: float | None
    end_erpm: float | None
    mean_decel_erpm_per_s: float | None
    bus_max_v: float | None
    bus_overshoot_v: float | None
    sample_count: int
    simulated: bool
    alignment_uncertain: bool = False
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SyncSeries:
    time_s: list[float]
    dc_voltage_v: list[float] = field(default_factory=list)
    dc_current_a: list[float] = field(default_factory=list)
    dc_power_w: list[float] = field(default_factory=list)
    erpm: list[float] = field(default_factory=list)
    drive_control_v: list[float] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    simulated: bool = False
    alignment_uncertain: bool = False
    source: str = 'sync_csv'
    trial_id: str | None = None

    def __len__(self) -> int:
        return len(self.time_s)


def _finite(value, label: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f'{label} must be numeric')
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError as exc:
            raise ConfigError(f'{label} must be numeric') from exc
    if not isinstance(value, (int, float)):
        raise ConfigError(f'{label} must be numeric')
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f'{label} must be finite')
    return result


def validate_series(series: SyncSeries) -> None:
    n = len(series.time_s)
    if n < 2:
        raise ConfigError('Energy series requires at least two samples')
    for name in ('dc_voltage_v', 'dc_current_a', 'dc_power_w', 'erpm', 'drive_control_v'):
        values = getattr(series, name)
        if values and len(values) != n:
            raise ConfigError(f'{name} length mismatch')
    prior = None
    for index, stamp in enumerate(series.time_s):
        value = _finite(stamp, f'time_s[{index}]')
        if prior is not None and value < prior:
            raise ConfigError('time_s must be monotonic nondecreasing')
        prior = value
    if series.dc_power_w:
        for index, power in enumerate(series.dc_power_w):
            _finite(power, f'dc_power_w[{index}]')
    elif series.dc_voltage_v and series.dc_current_a:
        series.dc_power_w = [
            _finite(v, 'V') * _finite(i, 'I') for v, i in zip(series.dc_voltage_v, series.dc_current_a)
        ]
    else:
        raise ConfigError('Need dc_power_w or both dc_voltage_v and dc_current_a')


def trapezoidal_energy(time_s: list[float], power_w: list[float]) -> tuple[float, float, float]:
    """Positive P_DC = RZ-XA supplying VESC; negative = toward RZ-XA.

    Intervals that cross zero are split at the interpolated zero-crossing so a
    +10→−10 W / 1 s segment contributes 2.5 J in and 2.5 J return (not a cancelled avg).
    """
    e_in = 0.0
    e_return = 0.0
    for index in range(1, len(time_s)):
        dt = time_s[index] - time_s[index - 1]
        if dt <= 0:
            continue
        p0 = power_w[index - 1]
        p1 = power_w[index]
        if p0 == 0 and p1 == 0:
            continue
        # Same-sign (including touching zero on one end): one trapezoid.
        if p0 * p1 > 0 or p0 == 0 or p1 == 0:
            energy = 0.5 * (p0 + p1) * dt
            if energy >= 0:
                e_in += energy
            else:
                e_return += -energy
            continue
        # Zero-crossing inside the sample: split into positive and negative subintervals.
        frac = abs(p0) / (abs(p0) + abs(p1))
        dt_pos = dt * frac if p0 > 0 else dt * (1.0 - frac)
        dt_neg = dt - dt_pos
        if p0 > 0:
            e_in += 0.5 * p0 * dt_pos
            e_return += 0.5 * abs(p1) * dt_neg
        else:
            e_return += 0.5 * abs(p0) * dt_neg
            e_in += 0.5 * p1 * dt_pos
    return e_in, e_return, e_in - e_return


def compute_metrics(
    series: SyncSeries,
    *,
    bus_target_v: float | None = None,
    regen_window: tuple[float, float] | None = None,
) -> EnergyMetrics:
    validate_series(series)
    times = list(series.time_s)
    powers = list(series.dc_power_w)
    if regen_window is not None:
        start, end = regen_window
        mask = [(start <= t <= end) for t in times]
        if sum(mask) < 2:
            raise ConfigError('regen_window leaves fewer than two samples')
        times = [t for t, keep in zip(times, mask) if keep]
        powers = [p for p, keep in zip(powers, mask) if keep]
        currents = [c for c, keep in zip(series.dc_current_a, mask)] if series.dc_current_a else []
        voltages = [v for v, keep in zip(series.dc_voltage_v, mask)] if series.dc_voltage_v else []
        erpms = [e for e, keep in zip(series.erpm, mask)] if series.erpm else []
    else:
        currents = list(series.dc_current_a)
        voltages = list(series.dc_voltage_v)
        erpms = list(series.erpm)

    e_in, e_return, e_net = trapezoidal_energy(times, powers)
    ratio = (e_return / e_in) if e_in > 0 else None
    peak_neg_power = min(0.0, min(powers))
    peak_neg_current = min(0.0, min(currents)) if currents else 0.0
    regen_duration = 0.0
    for index in range(1, len(powers)):
        if powers[index] < 0 or powers[index - 1] < 0:
            regen_duration += times[index] - times[index - 1]

    start_erpm = erpms[0] if erpms else None
    end_erpm = erpms[-1] if erpms else None
    mean_decel = None
    if start_erpm is not None and end_erpm is not None and times[-1] > times[0]:
        mean_decel = (start_erpm - end_erpm) / (times[-1] - times[0])

    bus_max = max(voltages) if voltages else None
    overshoot = None
    if bus_max is not None and bus_target_v is not None:
        overshoot = max(0.0, bus_max - bus_target_v)

    notes = []
    if series.simulated:
        notes.append('simulated: electrical metrics are labeled simulated, not physical recovery')
    notes.append('electrical_energy_return_ratio is not mechanical efficiency')
    if series.alignment_uncertain:
        notes.append('alignment_uncertain: marker/timebase alignment flagged')

    return EnergyMetrics(
        e_in_j=e_in,
        e_return_j=e_return,
        e_net_j=e_net,
        electrical_energy_return_ratio=ratio,
        peak_neg_current_a=peak_neg_current,
        peak_neg_power_w=peak_neg_power,
        regen_duration_s=regen_duration,
        start_erpm=start_erpm,
        end_erpm=end_erpm,
        mean_decel_erpm_per_s=mean_decel,
        bus_max_v=bus_max,
        bus_overshoot_v=overshoot,
        sample_count=len(times),
        simulated=series.simulated,
        alignment_uncertain=series.alignment_uncertain,
        notes=tuple(notes),
    )


def load_sync_csv(
    path,
    *,
    channel_map: dict | None = None,
    simulated: bool = False,
    alignment_uncertain: bool = False,
    markers: dict | None = None,
    polarity: str = 'rzxa_supply_positive',
    time_unit: str = 's',
    marker_column: str | None = 'marker',
) -> SyncSeries:
    """Import OXYGEN sync acquisition CSV with configurable channel mapping."""
    path = Path(path)
    mapping = {**DEFAULT_CHANNEL_MAP, **(channel_map or {})}
    if polarity not in {'rzxa_supply_positive', 'invert'}:
        raise ConfigError("polarity must be 'rzxa_supply_positive' or 'invert'")
    if time_unit not in {'s', 'ms'}:
        raise ConfigError("time_unit must be 's' or 'ms'")
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ConfigError('CSV has no header')
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    if not rows:
        raise ConfigError('CSV has no data rows')

    def column(logical: str) -> list[float]:
        name = mapping.get(logical)
        if not name or name not in rows[0]:
            return []
        return [_finite(row[name], name) for row in rows]

    times = column('time_s')
    if not times:
        raise ConfigError('CSV mapping must include time_s')
    if time_unit == 'ms':
        times = [t / 1000.0 for t in times]

    currents = column('dc_current_a')
    powers = column('dc_power_w')
    if polarity == 'invert':
        currents = [-c for c in currents]
        powers = [-p for p in powers]

    marker_values: list[str] = []
    marker_key = mapping.get('marker', marker_column)
    if marker_key and marker_key in fieldnames:
        marker_values = [(row.get(marker_key) or '').strip() for row in rows]

    series = SyncSeries(
        time_s=times,
        dc_voltage_v=column('dc_voltage_v'),
        dc_current_a=currents,
        dc_power_w=powers,
        erpm=column('erpm'),
        drive_control_v=column('drive_control_v'),
        markers=marker_values,
        simulated=simulated,
        alignment_uncertain=alignment_uncertain,
        source=str(path),
    )
    if markers:
        missing = [key for key, value in markers.items() if value is None]
        if missing:
            series.alignment_uncertain = True
    validate_series(series)
    return series


def _slice_series(series: SyncSeries, mask: list[bool], *, trial_id: str | None = None) -> SyncSeries:
    if sum(1 for keep in mask if keep) < 2:
        raise ConfigError('Trial window/markers leave fewer than two samples')

    def take(values: list):
        return [value for value, keep in zip(values, mask) if keep] if values else []

    return SyncSeries(
        time_s=take(series.time_s),
        dc_voltage_v=take(series.dc_voltage_v),
        dc_current_a=take(series.dc_current_a),
        dc_power_w=take(series.dc_power_w),
        erpm=take(series.erpm),
        drive_control_v=take(series.drive_control_v),
        markers=take(series.markers),
        simulated=series.simulated,
        alignment_uncertain=series.alignment_uncertain,
        source=series.source,
        trial_id=trial_id,
    )


def extract_trial_series(
    series: SyncSeries,
    *,
    trial_id: str | None = None,
    window: tuple[float, float] | None = None,
    marker_start: str | None = None,
    marker_end: str | None = None,
    allow_whole_series: bool = False,
) -> SyncSeries:
    """Associate sync data with one trial via window and/or markers — never reuse whole CSV by default."""
    if window is None and not (marker_start and marker_end):
        if allow_whole_series:
            out = SyncSeries(**{**asdict(series), 'trial_id': trial_id})
            validate_series(out)
            return out
        raise ConfigError(
            'Refusing to analyze whole sync CSV for a trial; provide window and/or start/end markers '
            '(or allow_whole_series only for a single-trial fixture)')

    start_t = None
    end_t = None
    if marker_start and marker_end:
        if not series.markers:
            raise ConfigError('Marker column required to bind trial by markers')
        try:
            start_idx = next(i for i, text in enumerate(series.markers) if text == marker_start)
            end_idx = next(i for i, text in enumerate(series.markers) if text == marker_end and i >= start_idx)
        except StopIteration as exc:
            raise ConfigError(
                f'Trial markers not found for trial_id={trial_id!r}: '
                f'{marker_start!r} .. {marker_end!r}') from exc
        start_t = series.time_s[start_idx]
        end_t = series.time_s[end_idx]
    if window is not None:
        w0, w1 = window
        if w1 <= w0:
            raise ConfigError('Trial window end must be greater than start')
        if start_t is None:
            start_t, end_t = w0, w1
        else:
            # Markers define the primary association; window must agree.
            if abs(start_t - w0) > 1e-6 or abs(end_t - w1) > 1e-6:
                series.alignment_uncertain = True
            start_t, end_t = w0, w1

    mask = [start_t <= t <= end_t for t in series.time_s]
    return _slice_series(series, mask, trial_id=trial_id)


def synthetic_regen_series(
    *,
    release_duration_s: float,
    start_erpm: float,
    bus_v: float,
    drive_peak_v: float,
    sample_hz: float = 200.0,
    observe_s: float = 2.0,
) -> SyncSeries:
    """Deterministic offline series for memory tests; always labeled simulated."""
    if sample_hz <= 0 or release_duration_s < 0:
        raise ConfigError('Invalid synthetic series timing')
    dt = 1.0 / sample_hz
    times: list[float] = []
    powers: list[float] = []
    currents: list[float] = []
    voltages: list[float] = []
    erpms: list[float] = []
    drives: list[float] = []
    total = release_duration_s + observe_s
    t = 0.0
    while t <= total + 1e-12:
        if t <= release_duration_s and release_duration_s > 0:
            frac = t / release_duration_s
            drive = drive_peak_v * (1.0 - frac)
            # Negative power during release models electrical return toward RZ-XA.
            power = -abs(drive_peak_v) * bus_v * 2.0 * (1.0 - frac)
            erpm = start_erpm * (1.0 - 0.6 * frac)
        else:
            drive = 0.0
            coast = t - release_duration_s
            decay = math.exp(-coast / max(observe_s, 1e-6))
            power = -abs(drive_peak_v) * bus_v * 0.15 * decay
            erpm = start_erpm * 0.4 * decay
        current = power / bus_v if bus_v else 0.0
        times.append(t)
        powers.append(power)
        currents.append(current)
        voltages.append(bus_v * (1.0 + 0.002 if power < 0 else 1.0))
        erpms.append(erpm)
        drives.append(drive)
        t += dt
    return SyncSeries(
        time_s=times,
        dc_voltage_v=voltages,
        dc_current_a=currents,
        dc_power_w=powers,
        erpm=erpms,
        drive_control_v=drives,
        simulated=True,
        alignment_uncertain=False,
        source='synthetic_simulated',
    )


def aggregate_trials(metrics_list: list[EnergyMetrics]) -> dict:
    """Mean/std aggregation; callers keep invalid trials visible separately."""
    if not metrics_list:
        return {
            'count': 0,
            'mean_e_return_j': None,
            'std_e_return_j': None,
            'mean_peak_neg_power_w': None,
            'mean_regen_duration_s': None,
            'mean_start_erpm': None,
            'std_start_erpm': None,
            'mean_bus_max_v': None,
            'std_bus_max_v': None,
        }

    def mean(values):
        return sum(values) / len(values)

    def std(values):
        if len(values) < 2:
            return 0.0
        m = mean(values)
        return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))

    e_returns = [m.e_return_j for m in metrics_list]
    peaks = [m.peak_neg_power_w for m in metrics_list]
    durations = [m.regen_duration_s for m in metrics_list]
    starts = [m.start_erpm for m in metrics_list if m.start_erpm is not None]
    buses = [m.bus_max_v for m in metrics_list if m.bus_max_v is not None]
    return {
        'count': len(metrics_list),
        'mean_e_return_j': mean(e_returns),
        'std_e_return_j': std(e_returns),
        'mean_peak_neg_power_w': mean(peaks),
        'mean_regen_duration_s': mean(durations),
        'mean_start_erpm': mean(starts) if starts else None,
        'std_start_erpm': std(starts) if starts else None,
        'mean_bus_max_v': mean(buses) if buses else None,
        'std_bus_max_v': std(buses) if buses else None,
        'all_simulated': all(m.simulated for m in metrics_list),
    }


def write_comparison_csv(path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text('', encoding='utf-8')
        return
    fieldnames = list(rows[0].keys())
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
