"""Dynamic DC bus voltage grids for bidirectional regenerative sweeps.

Generates exact grids with Decimal / integer scaling so 0.25 V and similar steps
do not silently round. Manual relative 0.5 V RZ-XA steps remain on the existing
commissioned SUPPLY_DEMAND_GRID and are unchanged by this module.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Iterator, Sequence

from .errors import ConfigError
from .schema import VERIFIED_GAIN

ALLOWED_STEPS_V = (Decimal('1.0'), Decimal('0.5'), Decimal('0.25'),
                   Decimal('0.1'), Decimal('0.05'))
DEFAULT_START_V = Decimal('36.0')
DEFAULT_END_V = Decimal('40.0')
DEFAULT_STEP_V = Decimal('0.25')


def _as_decimal(value, label: str) -> Decimal:
    try:
        if isinstance(value, bool):
            raise InvalidOperation
        if isinstance(value, Decimal):
            result = value
        else:
            result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConfigError(f'{label} must be a finite decimal voltage') from exc
    if not result.is_finite():
        raise ConfigError(f'{label} must be a finite decimal voltage')
    return result


def validate_step_v(step_v) -> Decimal:
    step = _as_decimal(step_v, 'step_v')
    if step not in ALLOWED_STEPS_V:
        raise ConfigError(
            f'step_v must be one of {[float(s) for s in ALLOWED_STEPS_V]}; '
            'incompatible grids are rejected (no silent round)')
    return step


def generate_voltage_points(start_v=DEFAULT_START_V, end_v=DEFAULT_END_V,
                            step_v=DEFAULT_STEP_V) -> tuple[Decimal, ...]:
    """Inclusive voltage grid from min(start,end) to max(start,end)."""
    start = _as_decimal(start_v, 'start_v')
    end = _as_decimal(end_v, 'end_v')
    step = validate_step_v(step_v)
    low, high = (start, end) if start <= end else (end, start)
    if low < Decimal('1') or high > Decimal('60'):
        raise ConfigError('Voltage grid endpoints must be within [1, 60] V')
    span = high - low
    if span % step != 0:
        raise ConfigError(
            f'Voltage span {span} V is not an integer multiple of step {step} V; '
            'refusing incompatible grid (no silent round)')
    count = int(span / step) + 1
    return tuple(low + step * index for index in range(count))


def bus_to_aout(bus_v, *, gain: float = VERIFIED_GAIN) -> float:
    """Commanded AOUT for a nominal DC bus demand (not measured bus voltage)."""
    bus = float(_as_decimal(bus_v, 'bus_v'))
    if gain <= 0:
        raise ConfigError('Supply gain must be positive')
    return bus / float(gain)


def aout_to_bus(aout_v, *, gain: float = VERIFIED_GAIN) -> float:
    return float(_as_decimal(aout_v, 'aout_v')) * float(gain)


def visit_count(*, point_count: int, round_trips: int | None,
                continuous: bool) -> int | None:
    """Visit counts for finite round trips (shared endpoints not duplicated).

    One round trip on N points: 2*(N-1)+1 = 2N-1 visits.
    Additional round trips each add 2*(N-1) visits (shared seam endpoint once).
    Total = round_trips * 2 * (N-1) + 1.
    Example: N=17 → 33 visits for 1 round trip; 65 visits for 2 round trips.
    """
    if point_count < 2:
        raise ConfigError('Voltage grid needs at least two points')
    if continuous:
        return None
    if round_trips is None or type(round_trips) is not int or round_trips < 1:
        raise ConfigError(
            'Finite sweeps require integer round_trips >= 1; '
            'omitted/invalid does not mean infinite (use continuous=true)')
    return round_trips * 2 * (point_count - 1) + 1


def bidirectional_visits(
    points: Sequence[Decimal],
    *,
    initial_direction: str = 'descending',
    round_trips: int | None = 1,
    continuous: bool = False,
) -> Iterator[tuple[Decimal, str, int, int | None]]:
    """Yield (voltage, direction, visit_index, round_trip_index).

    Direction reverses at endpoints without duplicating the endpoint voltage.
    Consecutive round trips also share the seam endpoint (no duplicate).
    continuous=True yields forever (caller must cancel); finite otherwise.
    """
    if initial_direction not in {'ascending', 'descending'}:
        raise ConfigError("initial_direction must be 'ascending' or 'descending'")
    if continuous and round_trips is not None:
        raise ConfigError('continuous sweeps must omit round_trips (or set null)')
    if not continuous and (round_trips is None or type(round_trips) is not int or round_trips < 1):
        raise ConfigError(
            'Finite sweeps require integer round_trips >= 1; use continuous=true for endless')

    ordered = tuple(points)
    if len(ordered) < 2:
        raise ConfigError('Voltage grid needs at least two points')
    ascending = list(ordered)
    descending = list(reversed(ordered))

    visit_index = 0
    trip = 0
    while True:
        if not continuous and trip >= round_trips:
            return
        # One round trip = descend then ascend (or reverse), sharing endpoints once.
        if initial_direction == 'descending':
            legs = (descending, ascending)
            leg_dirs = ('descending', 'ascending')
        else:
            legs = (ascending, descending)
            leg_dirs = ('ascending', 'descending')
        for leg_index, (leg, direction) in enumerate(zip(legs, leg_dirs)):
            # Skip the first point when it was already emitted as the previous endpoint
            # (second leg of a trip, or first leg of a subsequent round trip / continuous loop).
            skip_first = not (trip == 0 and leg_index == 0)
            start = 1 if skip_first else 0
            for voltage in leg[start:]:
                yield voltage, direction, visit_index, None if continuous else trip
                visit_index += 1
        trip += 1


def dry_run_sequence(start_v=DEFAULT_START_V, end_v=DEFAULT_END_V, step_v=DEFAULT_STEP_V,
                     *, initial_direction: str = 'descending', round_trips: int | None = 1,
                     continuous: bool = False, limit: int | None = None) -> list[dict]:
    """Voltage sequence without physical outputs (planning / tests)."""
    points = generate_voltage_points(start_v, end_v, step_v)
    rows = []
    for voltage, direction, visit_index, trip in bidirectional_visits(
            points, initial_direction=initial_direction, round_trips=round_trips,
            continuous=continuous):
        rows.append({
            'visit_index': visit_index,
            'round_trip_index': trip,
            'bus_target_v': float(voltage),
            'commanded_aout_v': bus_to_aout(voltage),
            'direction': direction,
            'physical_voltage_established': False,
        })
        if limit is not None and len(rows) >= limit:
            break
    return rows


def assert_grid_examples() -> None:
    """Internal sanity for the documented 36..40 / 0.25 V bidirectional math."""
    points = generate_voltage_points(36.0, 40.0, 0.25)
    assert len(points) == 17
    assert visit_count(point_count=17, round_trips=1, continuous=False) == 33
    assert visit_count(point_count=17, round_trips=2, continuous=False) == 65
    first = list(bidirectional_visits(points, initial_direction='descending', round_trips=1))
    assert len(first) == 33
    assert first[0][0] == Decimal('40.0')
    assert first[16][0] == Decimal('36.0')
    assert first[17][0] == Decimal('36.25')  # no duplicate 36.0
    assert first[-1][0] == Decimal('40.0')
