"""One-shot constant-output pattern for an explicitly confirmed simulated AO."""

from __future__ import annotations

import csv
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .adapters.oxygen import OxygenAdapter, PropertyValue
from .profiles import Profile, ProfileError, load_profile
from .scpi_transport import ScpiProtocolError, ScpiTransport, parse_scpi, quote_scpi

APPROVED_CHANNEL = 'AO 3/1 Sim'
CONSTANT_PROPERTY = 'TRION/SourceChannel_A_CONST/Const0'


def finite_number(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a finite number')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{name} must be a finite number')
    return number


@dataclass(frozen=True)
class ConstantPattern:
    peak_v: float = 1.0
    ramp_up_s: float = 3.0
    hold_s: float = 4.0
    ramp_down_s: float = 3.0

    def __post_init__(self) -> None:
        peak = finite_number(self.peak_v, 'peak')
        if not 0 < peak <= 1:
            raise ValueError('This simulated-output authorization permits only 0 to 1 V')
        if tuple(finite_number(v, 'phase duration') for v in
                 (self.ramp_up_s, self.hold_s, self.ramp_down_s)) != (3.0, 4.0, 3.0):
            raise ValueError('This first pattern must use 3 s up, 4 s hold, 3 s down')

    @property
    def duration_s(self) -> float:
        return self.ramp_up_s + self.hold_s + self.ramp_down_s

    @property
    def boundaries(self) -> tuple[float, ...]:
        return (self.ramp_up_s, self.ramp_up_s + self.hold_s, self.duration_s)

    def value_at(self, elapsed_s: float) -> float:
        t = max(0.0, finite_number(elapsed_s, 'elapsed time'))
        if t < self.ramp_up_s:
            return self.peak_v * t / self.ramp_up_s
        if t < self.ramp_up_s + self.hold_s:
            return self.peak_v
        if t < self.duration_s:
            return self.peak_v * (self.duration_s - t) / self.ramp_down_s
        return 0.0

    def phase_at(self, t: float) -> str:
        if t < self.ramp_up_s:
            return 'ramp_up'
        if t < self.ramp_up_s + self.hold_s:
            return 'hold'
        return 'ramp_down' if t < self.duration_s else 'complete'

    @classmethod
    def from_profile(cls, profile: Profile) -> ConstantPattern:
        if [s.type for s in profile.steps] != ['set_aout', 'ramp_aout', 'hold', 'ramp_aout', 'return_neutral']:
            raise ProfileError('aout-pattern accepts only set/ramp/hold/ramp/neutral, with no recording or instrument steps')
        if profile.recording:
            raise ProfileError('Recording configuration is forbidden for this isolated pattern')
        initial, up, hold, down, neutral = [s.params for s in profile.steps]
        if any(p.get('channel') != 'sim_aout' for p in (initial, up, down, neutral)):
            raise ProfileError('Every output step must target the isolated logical channel sim_aout')
        if finite_number(initial['value'], 'initial value') != 0 or finite_number(down['target'], 'final value') != 0:
            raise ProfileError('The pattern must begin and end at zero')
        pattern = cls(finite_number(up['target'], 'peak'), finite_number(up['duration_s'], 'ramp duration'),
                      finite_number(hold['duration_s'], 'hold duration'), finite_number(down['duration_s'], 'ramp duration'))
        for step, duration in ((up, pattern.ramp_up_s), (down, pattern.ramp_down_s)):
            if not math.isclose(finite_number(step['rate_per_s'], 'ramp rate'), pattern.peak_v / duration, rel_tol=1e-9):
                raise ProfileError('Ramp rate and duration disagree')
        if not math.isfinite(profile.timeout_s) or profile.timeout_s < pattern.duration_s:
            raise ProfileError('Profile timeout must cover its duration')
        return pattern


class SimulatedConstantTransport(ScpiTransport):
    """Narrow write capability; general OxygenAdapter.set_property stays blocked."""

    def __init__(self, *, host: str, port: int, timeout_s: float, channel: str,
                 expected_id: str, confirmed_simulated: bool, emit: Callable[[dict], None]) -> None:
        super().__init__(host=host, port=port, timeout_s=timeout_s)
        if channel != APPROVED_CHANNEL or not confirmed_simulated:
            raise ValueError('Explicit simulation confirmation for AO 3/1 Sim is required')
        self.expected_id = OxygenAdapter._channel_id(expected_id)
        self.emit = emit
        self.armed = False
        self.trigger_id: str | None = None
        self.trigger_ids: set[str] = set()

    def authorize(self) -> None:
        if not self.connected:
            raise ScpiProtocolError('Cannot authorize a disconnected session')
        self.armed = True

    def close(self) -> None:
        self.armed = False
        super().close()

    def reconnect(self) -> None:
        raise ScpiProtocolError('Pattern sessions never reconnect or resume writes')

    def query(self, command: str) -> str:
        allowed = command in {'*IDN?', ':CHANNELlist:NAMes?', 'SYST:ERR?'}
        allowed = allowed or command == f':CHANNELlist:ITEM{self.expected_id}:ATTR:NAMes?'
        trigger_ids = self.trigger_ids | ({self.trigger_id} if self.trigger_id is not None else set())
        allowed = allowed or any(command == f':CHANNELlist:ITEM{cid}:ATTR:NAMes?' for cid in trigger_ids)
        prefix = ':CHANNELlist:PROPerty? '
        if command.startswith(prefix):
            args = parse_scpi(command[len(prefix):])
            allowed = (len(args) == 2 and all(isinstance(a, str) for a in args) and args[0] == self.expected_id
                       and args[1] in {'Mode', 'Range', 'Unit', 'TRION/SourceChannel_A', CONSTANT_PROPERTY}
                       and command == prefix + ','.join(quote_scpi(a) for a in args))
            allowed = allowed or any(command == prefix + f'{quote_scpi(cid)},"DO_Value"' for cid in trigger_ids)
        if not allowed:
            raise ScpiProtocolError(f'Pattern query not permitted: {command!r}')
        started = time.monotonic()
        record = {'event': 'query', 'command': command}
        try:
            reply = super().query(command)
            record['raw_payload'] = reply
            return reply
        except KeyboardInterrupt:
            # An interrupted in-flight reply cannot safely be reused for cleanup.
            self.close()
            raise
        except Exception as exc:
            record['error'] = str(exc)
            raise
        finally:
            record['latency_ms'] = (time.monotonic() - started) * 1000
            self.emit(record)

    def write(self, command: str) -> None:
        prefix = ':CHANNELlist:PROPerty '
        if not self.armed or not command.startswith(prefix):
            raise ScpiProtocolError('No authorized simulated constant write capability')
        args = parse_scpi(command[len(prefix):])
        if (len(args) != 5 or args[:3] != [self.expected_id, CONSTANT_PROPERTY, 'SCALAR']
                or args[4] != 'V'):
            raise ScpiProtocolError('Only the selected Const0 scalar property may be written')
        value = finite_number(args[3], 'setpoint')
        canonical = prefix + f'{quote_scpi(self.expected_id)},{quote_scpi(CONSTANT_PROPERTY)},SCALAR,{value:.9g},"V"'
        if not 0 <= value <= 1 or command != canonical:
            raise ScpiProtocolError('Only canonical scalar writes from 0 to 1 V are permitted')
        self.emit({'event': 'write_attempt', 'command': command, 'requested_v': value})
        try:
            super().write(command)
        except KeyboardInterrupt:
            self.close()
            raise


class SimulatedConstantOutput:
    def __init__(self, transport: SimulatedConstantTransport, emit: Callable[[dict], None]) -> None:
        self.transport = transport
        self.adapter = OxygenAdapter(transport)
        self.emit = emit
        self.channel_id = transport.expected_id
        self.prepared = False
        self.writes = 0

    def check_error(self) -> None:
        error = self.transport.check_error()
        if error.code:
            self.emit({'event': 'scpi_error', 'code': error.code, 'message': error.message})
            raise ScpiProtocolError(f'SCPI error: {error}')

    @staticmethod
    def text(value: PropertyValue) -> str:
        if value.kind == 'STRING' and len(value.values) == 1:
            return value.values[0]
        raise ScpiProtocolError(f'Expected an observed STRING property, got {value!r}')

    def check_context(self) -> None:
        for prop, expected in [('Mode', 'ConstOutput'), ('TRION/SourceChannel_A', 'CONST0'), ('Unit', 'V')]:
            if self.text(self.adapter.get_property_value(self.channel_id, prop)) != expected:
                raise ScpiProtocolError(f'{prop} must already be {expected}; no mode/source changes will be made')
        self.check_error()

    def readback(self, expected: float) -> float:
        value = self.adapter.get_property_value(self.channel_id, CONSTANT_PROPERTY)
        if value.kind != 'SCALAR' or len(value.values) != 2 or value.values[1] != 'V':
            raise ScpiProtocolError(f'Unexpected constant readback: {value!r}')
        actual = finite_number(value.values[0], 'constant readback')
        self.check_error()
        if not math.isclose(actual, expected, abs_tol=1e-6, rel_tol=0):
            raise ScpiProtocolError(f'Constant readback mismatch: requested {expected}, reported {actual}')
        return actual

    def prepare(self) -> None:
        identity = self.adapter.identify()
        if identity.split(',')[:2] != ['DEWETRON', 'OXYGEN']:
            raise ScpiProtocolError(f'Unexpected instrument identity: {identity!r}')
        matches = [c for c in self.adapter.list_channels() if c.name == APPROVED_CHANNEL]
        if len(matches) != 1 or matches[0].channel_id != self.channel_id:
            raise ScpiProtocolError('Channel name/expected ID mismatch; refusing all writes')
        required = {'Mode', 'Range', 'Unit', 'TRION/SourceChannel_A', CONSTANT_PROPERTY}
        if not required.issubset(self.adapter.list_properties(self.channel_id)):
            raise ScpiProtocolError('Required observed AO properties are missing; none will be guessed')
        self.check_context()
        value = self.adapter.get_property_value(self.channel_id, 'Range')
        if (value.kind != 'RANGE' or len(value.values) != 4 or value.values[1] != 'V' or value.values[3] != 'V'
                or finite_number(value.values[0], 'lower range') > 0
                or finite_number(value.values[2], 'upper range') < 1):
            raise ScpiProtocolError('Configured range must cover 0 to 1 V without changing it')
        self.readback(0.0)
        self.check_error()
        self.transport.authorize()
        self.prepared = True
        self.emit({'event': 'preflight_passed', 'identify': identity, 'channel': APPROVED_CHANNEL,
                   'channel_id': self.channel_id, 'constant_property': CONSTANT_PROPERTY,
                   'operator_confirmed_simulated': True, 'mode': 'ConstOutput', 'initial_v': 0.0})
        self.setpoint(0.0)

    def setpoint(self, value: float) -> None:
        value = finite_number(value, 'setpoint')
        command = f':CHANNELlist:PROPerty {quote_scpi(self.channel_id)},{quote_scpi(CONSTANT_PROPERTY)},SCALAR,{value:.9g},"V"'
        self.writes += 1
        self.transport.command(command)
        actual = self.readback(value)
        self.emit({'event': 'setpoint_ack', 'requested_v': value, 'readback_v': actual})

    def verify(self, value: float) -> None:
        self.check_context()
        self.readback(value)


def execute_timeline(pattern: ConstantPattern, setpoint: Callable[[float], None],
                     verify: Callable[[float], None], emit: Callable[[dict], None], *,
                     interval_s: float = .05, max_lateness_s: float = .25,
                     clock: Callable[[], float] = time.monotonic,
                     wait: Callable[[float], None] = time.sleep) -> dict:
    if not .02 <= finite_number(interval_s, 'interval') <= .25:
        raise ValueError('Update interval must be between 20 and 250 ms')
    if not 0 < finite_number(max_lateness_s, 'maximum lateness') <= 1:
        raise ValueError('Maximum lateness must be positive and no greater than one second')
    started = clock()
    deadline = started

    def elapsed_at(now):
        # Absolute deadlines can be reached while subtraction rounds just below a boundary.
        reached = (t for t in pattern.boundaries if now >= started + t)
        return max(now - started, max(reached, default=0.0))

    last_value = None
    last_phase = None
    last_progress = -math.inf
    skipped = 0
    maximum_lateness = 0.0
    while True:
        wait(max(0.0, deadline - clock()))
        now = clock()
        late = max(0.0, now - deadline)
        maximum_lateness = max(maximum_lateness, late)
        if late > max_lateness_s:
            raise TimeoutError(f'Pattern update exceeded the lateness limit: {late:.3f}s')
        elapsed = elapsed_at(now)
        value = pattern.value_at(elapsed)
        phase = pattern.phase_at(elapsed)
        operation_started = clock()
        progress_due = phase != last_phase or elapsed - last_progress >= .5
        if last_value is None or not math.isclose(last_value, value, abs_tol=1e-12, rel_tol=0):
            setpoint(value)
            last_value = value
        if progress_due:
            verify(value)
        operation_s = clock() - operation_started
        if operation_s > max_lateness_s:
            raise TimeoutError(f'Pattern I/O exceeded the lateness limit: {operation_s:.3f}s')
        if progress_due:
            emit({'event': 'progress', 'phase': phase, 'elapsed_s': elapsed,
                  'nominal_duration_s': pattern.duration_s, 'requested_v': value,
                  'lateness_ms': late * 1000, 'io_ms': operation_s * 1000})
            last_progress = elapsed
            last_phase = phase
        if elapsed >= pattern.duration_s:
            actual_elapsed = clock() - started
            completion_lateness = max(0.0, actual_elapsed - pattern.duration_s)
            if completion_lateness > max_lateness_s:
                raise TimeoutError(f'Final setpoint acknowledgement exceeded the completion limit: {completion_lateness:.3f}s')
            return {'nominal_duration_s': pattern.duration_s, 'actual_elapsed_s': actual_elapsed,
                    'completion_lateness_ms': completion_lateness * 1000,
                    'skipped_update_slots': skipped, 'maximum_lateness_ms': maximum_lateness * 1000}
        after = elapsed_at(clock())
        due_index = math.floor(max(0, after) / interval_s + 1e-9) + 1
        prior_index = math.floor(max(0, elapsed) / interval_s + 1e-9)
        skipped += max(0, due_index - prior_index - 1)
        # Never replay overdue samples in a burst. Include the phase boundaries.
        future = [due_index * interval_s, pattern.duration_s]
        future.extend(t for t in pattern.boundaries if t > after)
        deadline = started + min(future)


def run_simulated_pattern(transport: SimulatedConstantTransport, pattern: ConstantPattern,
                          emit: Callable[[dict], None], *, interval_s: float = .05,
                          max_lateness_s: float = .25, timeline=execute_timeline,
                          before_prepare=None) -> tuple[int, dict]:
    output = SimulatedConstantOutput(transport, emit)
    status = 'fault'
    result = {}
    cleanup = 'not_attempted_no_authorization'
    exit_code = 1
    try:
        transport.connect()
        if before_prepare is not None:
            before_prepare()
        output.prepare()
        result = timeline(pattern, output.setpoint, output.verify, emit,
                          interval_s=interval_s, max_lateness_s=max_lateness_s)
        status, exit_code = 'ok', 0
    except KeyboardInterrupt:
        status, exit_code = 'cancelled', 130
        emit({'event': 'cancelled', 'message': 'Pattern stopped; attempting zero without reconnecting'})
    except Exception as exc:
        emit({'event': 'fault', 'error_type': type(exc).__name__, 'error': str(exc)})
    finally:
        if output.prepared:
            cleanup = 'unknown'
            if transport.connected:
                try:
                    output.check_context()
                    output.setpoint(0.0)
                    cleanup = 'configured_zero_verified'
                    emit({'event': 'neutral_verified', 'value_v': 0.0})
                except (Exception, KeyboardInterrupt) as exc:
                    emit({'event': 'neutral_failed', 'error': str(exc),
                          'message': 'Final value unknown; inspect the simulated output in OXYGEN'})
            else:
                emit({'event': 'neutral_failed', 'message': 'Disconnected: no reconnect/replay; final value unknown'})
            if cleanup != 'configured_zero_verified':
                status, exit_code = 'fault', 1
        transport.close()
    summary = {'event': 'pattern_summary', 'status': status, 'final_state': cleanup,
               'writes_attempted': output.writes, 'channel': APPROVED_CHANNEL,
               'channel_id': transport.expected_id, **result,
               'note': 'Readback confirms configuration only, not physical voltage. Timing is software/best-effort.'}
    emit(summary)
    return exit_code, summary


def write_reference_csv(path: Path, pattern: ConstantPattern, interval_s: float) -> int:
    count = math.ceil(pattern.duration_s / interval_s)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['nominal_time_s', 'requested_v', 'phase'])
        for index in range(count + 1):
            t = min(index * interval_s, pattern.duration_s)
            writer.writerow([f'{t:.6f}', f'{pattern.value_at(t):.9g}', pattern.phase_at(t)])
    return count + 1


def run_constant_pattern(args) -> int:
    pattern = ConstantPattern.from_profile(load_profile(args.profile))
    interval_s = finite_number(args.interval_ms, 'interval') / 1000
    lateness_s = finite_number(args.max_lateness_ms, 'maximum lateness') / 1000
    if not .02 <= interval_s <= .25 or not 0 < lateness_s <= 1:
        raise ValueError('Use an interval of 20..250 ms and maximum lateness of 1..1000 ms')
    if not 1 <= args.port <= 65535 or finite_number(args.timeout, 'timeout') <= 0:
        raise ValueError('A valid port and positive timeout are required')
    if args.channel != APPROVED_CHANNEL:
        raise ValueError('Only the explicitly confirmed AO 3/1 Sim is in scope')
    if args.live:
        raise ValueError('Do not use --live; use the explicit simulated-output execution flags')
    if args.execute and (not args.confirm_simulated_output or not args.expected_id):
        raise ValueError('Execution requires --confirm-simulated-output and --expected-id')
    if args.expected_id:
        OxygenAdapter._channel_id(args.expected_id)
    trigger_do = getattr(args, 'trigger_do', None)
    if trigger_do:
        if not .02 <= finite_number(args.poll_ms, 'DO poll interval') / 1000 <= .25:
            raise ValueError('DO poll interval must be between 20 and 250 ms')
        if not args.poll_ms < finite_number(args.max_trigger_gap_ms, 'DO maximum gap') <= 1000:
            raise ValueError('DO maximum gap must exceed the poll interval and be at most 1000 ms')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run_id = uuid.uuid4().hex
    path = Path(args.log_file or f'logs/constant-pattern-{stamp}-{run_id[:8]}.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    csv_path = Path(args.csv_file) if args.csv_file else path.with_suffix('.csv')
    if csv_path.resolve() == path.resolve() or csv_path.resolve() == Path(args.profile).resolve() or path.resolve() == Path(args.profile).resolve():
        raise ValueError('Profile, reference CSV and log must be distinct files')
    reference_points = write_reference_csv(csv_path, pattern, interval_s)
    with path.open('a', encoding='utf-8') as stream:
        def emit(record):
            record = {'ts': datetime.now(timezone.utc).isoformat(), 'monotonic_s': time.monotonic(),
                      'run_id': run_id, **record}
            line = json.dumps(record, allow_nan=False)
            stream.write(line + '\n')
            stream.flush()
            if record['event'] not in {'query', 'write_attempt', 'setpoint_ack', 'trigger_sample'}:
                print(line, flush=True)

        emit({'event': 'pattern_plan', 'execute': args.execute, 'channel': args.channel,
              'trigger_do': trigger_do,
              'expected_id': args.expected_id, 'peak_v': pattern.peak_v,
              'ramp_up_s': 3, 'hold_s': 4, 'ramp_down_s': 3,
              'nominal_duration_s': 10, 'interval_ms': args.interval_ms,
              'reference_csv': str(csv_path.resolve()), 'reference_points': reference_points,
              'log': str(path.resolve()), 'note': 'CSV is a nominal reference, not an uploaded/native waveform'})
        if not args.execute:
            emit({'event': 'preview_complete', 'connected': False, 'writes_attempted': 0})
            return 0
        transport = SimulatedConstantTransport(host=args.host, port=args.port, timeout_s=args.timeout,
                                               channel=args.channel, expected_id=args.expected_id,
                                               confirmed_simulated=args.confirm_simulated_output, emit=emit)
        if trigger_do:
            from .triggered_pattern import run_triggered_pattern

            code, _ = run_triggered_pattern(transport, pattern, emit,
                                            poll_s=args.poll_ms / 1000,
                                            max_gap_s=args.max_trigger_gap_ms / 1000,
                                            interval_s=interval_s, max_lateness_s=lateness_s)
            return code
        code, _ = run_simulated_pattern(transport, pattern, emit, interval_s=interval_s, max_lateness_s=lateness_s)
        return code
