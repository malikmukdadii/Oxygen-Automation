"""Isolated OXYGEN DO observation. No engine lifecycle or command dispatch."""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .adapters.oxygen import OxygenAdapter
from .channel_manager import ChannelError
from .do_monitor import DiagnosticEdgeTracker, DOMonitor
from .scpi_transport import (
    ScpiError, ScpiProtocolError, ScpiTimeoutError, ScpiTransport,
    parse_scpi, quote_scpi,
)


@dataclass
class WatchOptions:
    channel: str
    host: str = '127.0.0.1'
    port: int = 10001
    property_name: str = 'DO_Value'
    interval_s: float = 0.05
    timeout_s: float = 0.5
    max_gap_s: float = 0.25
    reconnect_s: float = 1.0
    duration_s: float = 0.0
    compare_numeric: bool = False
    event_source: str = 'property'
    expected_presses: int | None = None
    interaction_ms: float | None = None

    def __post_init__(self) -> None:
        if not self.channel or not self.property_name or not 1 <= self.port <= 65535:
            raise ValueError('A channel, property, and valid TCP port are required')
        for name in ('interval_s', 'timeout_s', 'max_gap_s', 'reconnect_s'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if not math.isfinite(self.duration_s) or self.duration_s < 0:
            raise ValueError('duration must be finite and nonnegative')
        if self.event_source not in {'property', 'numeric'}:
            raise ValueError('event source must be property or numeric')
        if self.event_source == 'numeric' and not self.compare_numeric:
            raise ValueError('numeric event source requires --compare-numeric')
        if self.expected_presses is not None and self.expected_presses <= 0:
            raise ValueError('expected presses must be positive')
        if self.interaction_ms is not None and (not math.isfinite(self.interaction_ms) or self.interaction_ms <= 0):
            raise ValueError('interaction duration must be finite and positive')


class DiagnosticTransport(ScpiTransport):
    """Allow only discovery reads and explicitly opted-in numeric transfer setup."""

    def __init__(self, options: WatchOptions, emit: Callable[[dict], None]) -> None:
        super().__init__(host=options.host, port=options.port, timeout_s=options.timeout_s)
        self.allow_numeric = options.compare_numeric
        self.emit = emit

    def write(self, command: str) -> None:
        allowed = self.allow_numeric and command in {
            ':NUMeric:NORMal:FORMat ASCII', ':NUMeric:NORMal:NUMber ALL',
        }
        prefix = ':NUMeric:NORMal:ITEMS '
        if self.allow_numeric and command.startswith(prefix):
            names = parse_scpi(command[len(prefix):])
            allowed = bool(names) and all(isinstance(n, str) for n in names)
            if allowed:
                allowed = command == prefix + ','.join(quote_scpi(n) for n in names)
        if not allowed:
            raise ScpiProtocolError(f'watch-do forbids writes: {command!r}')
        super().write(command)

    def query(self, command: str) -> str:
        header = command.split(None, 1)[0]
        permitted = command in {
            '*IDN?', 'SYST:ERR?', ':CHANNELlist:NAMes?',
            ':ACQUisition:STATe?',
        } or re.fullmatch(r':CHANNELlist:ITEM[0-9]+:ATTR:NAMes\?', command)
        if header == ':CHANNELlist:PROPerty?' and command.startswith(header + ' '):
            args = parse_scpi(command[len(header) + 1:])
            permitted = len(args) == 2 and all(isinstance(a, str) for a in args)
            if permitted:
                permitted = command == header + ' ' + ','.join(quote_scpi(a) for a in args)
        if self.allow_numeric and command in {':NUMeric:NORMal:DIMS?', ':NUMeric:NORMal:VALue?'}:
            permitted = True
        if not permitted:
            raise ScpiProtocolError(f'watch-do forbids query: {command!r}')
        started = time.monotonic()
        record = {'event': 'query', 'command': command}
        try:
            reply = super().query(command)
            record['raw_payload'] = reply
            return reply
        except ScpiError as exc:
            record['error'] = str(exc)
            record['error_type'] = type(exc).__name__
            raise
        finally:
            record['latency_ms'] = (time.monotonic() - started) * 1000
            self.emit(record)


def _distribution(values) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {'count': 0, 'p50_ms': None, 'p95_ms': None, 'max_ms': None}
    return {
        'count': len(ordered),
        'p50_ms': ordered[(len(ordered) - 1) // 2],
        'p95_ms': ordered[math.ceil(len(ordered) * 0.95) - 1],
        'max_ms': ordered[-1],
    }


class DOWatchDiagnostic:
    def __init__(self, options: WatchOptions, emit: Callable[[dict], None], *,
                 stop: threading.Event | None = None,
                 transport_factory=DiagnosticTransport) -> None:
        self.options = options
        self.sink = emit
        self.stop = stop or threading.Event()
        self.transport_factory = transport_factory
        self.run_id = uuid.uuid4().hex
        self.trackers = {s: DiagnosticEdgeTracker(options.max_gap_s) for s in ('property', 'numeric')}
        self.history = deque(maxlen=20)
        self.query_latencies = deque(maxlen=10000)
        self.poll_gaps = deque(maxlen=10000)
        self.counts = dict(samples=0, valid_samples=0, property_presses=0, numeric_presses=0,
                           stale_samples=0, mismatches=0, errors=0, timeouts=0, reconnects=0)
        self.numeric_enabled = False
        self.numeric_disabled = False
        self.numeric_time: float | None = None
        self.last_poll: float | None = None
        self.ever_connected = False
        self.summary: dict = {}

    def emit(self, record: dict) -> None:
        record = dict(record)
        record.setdefault('ts', datetime.now(timezone.utc).isoformat())
        record.setdefault('monotonic_s', time.monotonic())
        record['run_id'] = self.run_id
        if record.get('event') == 'query':
            self.query_latencies.append(record['latency_ms'])
        self.sink(record)

    def reset(self) -> None:
        for tracker in self.trackers.values():
            tracker.reset()
        self.numeric_time: float | None = None

    def errors(self, transport: ScpiTransport) -> bool:
        found = False
        for _ in range(50):
            entry = transport.check_error()
            if entry.code == 0:
                return found
            found = True
            self.counts['errors'] += 1
            self.emit({'event': 'scpi_error', 'code': entry.code, 'message': entry.message})
        raise ScpiProtocolError('Error queue did not terminate after 50 entries')

    def prepare(self, transport: DiagnosticTransport) -> tuple[OxygenAdapter, str]:
        adapter = OxygenAdapter(transport)
        identity = adapter.identify()
        matches = [c for c in adapter.list_channels() if c.name == self.options.channel]
        if len(matches) != 1:
            raise ChannelError(f'Expected exactly one channel named {self.options.channel!r}')
        channel = matches[0]
        attributes = adapter.list_properties(channel.channel_id)
        if self.options.property_name not in attributes:
            self.emit({'event': 'attributes', 'names': attributes})
            raise ChannelError(f'Property {self.options.property_name!r} is not listed; no property was guessed')
        metadata = {p: adapter.get_property_reply(channel.channel_id, p)
                    for p in ('Mode', 'Direction') if p in attributes}
        self.emit({'event': 'discovery', 'identification': identity, 'channel': channel.name,
                   'channel_id': channel.channel_id, 'property': self.options.property_name,
                   'attributes': attributes, 'metadata': metadata,
                   'acquisition_state': adapter.acquisition_state()})
        if self.errors(transport):
            raise ScpiProtocolError('SCPI errors during discovery; session not armed')
        self.numeric_enabled = False
        if self.options.compare_numeric and not self.numeric_disabled:
            try:
                adapter.configure_numeric_measurements(['REL-TIME', channel.name])
                if self.errors(transport):
                    raise ScpiProtocolError('Numeric transfer setup reported SCPI errors')
                self.numeric_enabled = True
            except ScpiProtocolError as exc:
                self.numeric_disabled = True
                self.emit({'event': 'numeric_unavailable', 'error': str(exc)})
                if self.options.event_source == 'numeric':
                    raise ChannelError('Numeric event source unavailable; select property or investigate') from exc
                if not transport.connected:
                    raise
                self.errors(transport)
        return adapter, channel.channel_id

    @staticmethod
    def property_state(adapter: OxygenAdapter, raw: str) -> bool:
        value = adapter.parse_property_value(raw)
        if value.kind in {'BOOL', 'BOOLEAN'} and len(value.values) == 1:
            return value.values[0]
        if value.kind in {'STRING', 'NUMBER'} and len(value.values) == 1:
            return DOMonitor._parse_bool(str(value.values[0]))
        raise ScpiProtocolError(f'Not a supported Boolean state property: {raw!r}')

    def poll(self, adapter: OxygenAdapter, cid: str) -> None:
        started = time.monotonic()
        gap = None if self.last_poll is None else started - self.last_poll
        self.last_poll = started
        if gap is not None:
            self.poll_gaps.append(gap * 1000)
        raw = adapter.get_property_reply(cid, self.options.property_name)
        property_finished = time.monotonic()
        state = self.property_state(adapter, raw)
        numeric = None
        numeric_state = None
        relative_time = None
        numeric_fresh = False
        numeric_acquisition = None
        if self.numeric_enabled:
            try:
                numeric_acquisition = adapter.acquisition_state()
                if numeric_acquisition == 'Started':
                    relative_time, numeric = adapter.read_selected_measurements(2)
                    if not math.isfinite(numeric) or numeric not in {0.0, 1.0}:
                        raise ScpiProtocolError(f'Numeric DO value is not binary: {numeric!r}')
                    if not math.isfinite(relative_time) or relative_time < 0:
                        raise ScpiProtocolError(f'Invalid numeric REL-TIME: {relative_time!r}')
                    numeric_fresh = self.numeric_time is None or 0 < relative_time - self.numeric_time <= self.options.max_gap_s
                    self.numeric_time = relative_time
                    if numeric_fresh:
                        numeric_state = bool(numeric)
                    else:
                        self.trackers['numeric'].reset()
                        self.emit({'event': 'numeric_stale', 'relative_time_s': relative_time})
                else:
                    self.trackers['numeric'].reset()
                    self.numeric_time = None
            except ScpiProtocolError as exc:
                self.emit({'event': 'numeric_unavailable', 'error': str(exc)})
                self.trackers['numeric'].reset()
                self.numeric_disabled = True
                self.numeric_enabled = False
                numeric = None
                relative_time = None
                if self.options.event_source == 'numeric':
                    raise ChannelError('Numeric event source no longer available') from exc
                if not adapter.transport.connected:
                    raise
        numeric_finished = time.monotonic()
        errors = self.errors(adapter.transport)
        finished = time.monotonic()
        stale = finished - started > self.options.max_gap_s or (gap is not None and gap > self.options.max_gap_s)
        record = {'event': 'sample', 'channel_id': cid, 'raw': raw, 'state': state,
                  'numeric_value': numeric, 'numeric_state': numeric_state,
                  'numeric_relative_time_s': relative_time, 'numeric_fresh': numeric_fresh,
                  'numeric_acquisition_state': numeric_acquisition,
                  'property_latency_ms': (property_finished - started) * 1000,
                  'query_latency_ms': (finished - started) * 1000,
                  'sample_started_monotonic_s': started,
                  'property_received_monotonic_s': property_finished,
                  'comparison_skew_ms': (numeric_finished - property_finished) * 1000 if numeric is not None else None,
                  'poll_gap_ms': gap * 1000 if gap is not None else None,
                  'stale': stale, 'valid': not errors and not stale}
        self.counts['samples'] += 1
        if errors or stale:
            self.reset()
            if stale:
                self.counts['stale_samples'] += 1
            record['edge'] = 'suppressed'
            self.emit(record)
            return
        self.counts['valid_samples'] += 1
        self.emit(record)
        if numeric_state is not None and state != numeric_state:
            self.counts['mismatches'] += 1
            self.emit({'event': 'comparison_mismatch', 'property_state': state, 'numeric_state': numeric_state,
                       'note': 'Sequential samples; skew/aggregation may explain a mismatch'})
        for source, level in (('property', state), ('numeric', numeric_state)):
            if level is None:
                continue
            edge = self.trackers[source].observe(level, finished)
            if edge['edge'] == 'steady':
                continue
            if edge['press']:
                self.counts[source + '_presses'] += 1
            entry = {'event': 'transition', 'source': source, **edge,
                     'press_count': self.counts[source + '_presses']}
            self.emit(entry)
            self.history.append({'ts': datetime.now(timezone.utc).isoformat(), **entry})
            if edge['press'] and source == self.options.event_source:
                self.emit({'event': 'press_detected', 'source': source,
                           'press_count': self.counts[source + '_presses'], 'diagnostic_only': True})

    def run(self) -> int:
        started = time.monotonic()
        deadline = started + self.options.duration_s if self.options.duration_s else math.inf
        exit_code = 0
        self.emit({'event': 'watch_started', 'options': self.options.__dict__,
                   'read_only_outputs': True, 'numeric_transfer_configuration': self.options.compare_numeric,
                   'note': 'No engine, output writes, lifecycle commands or automatic verification'})
        try:
            while not self.stop.is_set() and time.monotonic() < deadline:
                transport = self.transport_factory(self.options, self.emit)
                self.reset()
                try:
                    self.emit({'event': 'connecting', 'endpoint': f'{self.options.host}:{self.options.port}'})
                    transport.connect()
                    adapter, cid = self.prepare(transport)
                    if self.ever_connected:
                        self.counts['reconnects'] += 1
                    self.emit({'event': 'reconnected' if self.ever_connected else 'connected',
                               'note': 'Next fresh sample is baseline only; no replay'})
                    self.ever_connected = True
                    while not self.stop.is_set() and time.monotonic() < deadline:
                        tick = time.monotonic()
                        self.poll(adapter, cid)
                        self.stop.wait(max(0, min(self.options.interval_s - (time.monotonic() - tick),
                                                  deadline - time.monotonic())))
                except (ScpiError, ValueError) as exc:
                    self.counts['errors'] += 1
                    if isinstance(exc, ScpiTimeoutError):
                        self.counts['timeouts'] += 1
                    self.reset()
                    self.emit({'event': 'observation_error', 'error_type': type(exc).__name__, 'error': str(exc)})
                    self.emit({'event': 'disconnected', 'note': 'History invalidated; no cross-gap event'})
                except ChannelError as exc:
                    self.emit({'event': 'diagnostic_error', 'error': str(exc)})
                    exit_code = 1
                    break
                finally:
                    transport.close()
                self.stop.wait(max(0, min(self.options.reconnect_s, deadline - time.monotonic())))
        except KeyboardInterrupt:
            self.emit({'event': 'operator_stop'})
        finally:
            presses = self.counts[self.options.event_source + '_presses']
            expected = self.options.expected_presses
            self.summary = {
                'event': 'summary', **self.counts, 'event_source': self.options.event_source,
                'presses_detected': presses, 'operator_expected_presses': expected,
                'count_difference': presses - expected if expected is not None else None,
                'count_ratio': presses / expected if expected is not None else None,
                'interaction_ms_operator_label': self.options.interaction_ms,
                'elapsed_s': time.monotonic() - started,
                'poll_gaps': _distribution(self.poll_gaps),
                'query_latencies': _distribution(self.query_latencies),
                'statistics_window': 'last 10000 observations',
                'transition_history_tail': list(self.history),
                'milestone_verified': False,
                'limitations': 'Counts alone cannot prove no missed/duplicate presses. Pulses between polls are invisible; compare timestamped operator ground truth. Live verification remains pending.',
            }
            self.emit(self.summary)
        return exit_code if self.ever_connected and self.counts['valid_samples'] else 1


class WatchConsoleFilter:
    """Reduce console noise only; never filter the JSONL log or edge tracker."""

    def __init__(self, mode: str = 'events', status_interval_s: float = 1.0) -> None:
        if mode not in {'events', 'samples'}:
            raise ValueError('console mode must be events or samples')
        if not math.isfinite(status_interval_s) or status_interval_s < 0:
            raise ValueError('status interval must be finite and nonnegative')
        self.mode = mode
        self.status_interval_s = status_interval_s
        self.last: dict[str, tuple[tuple, float]] = {}

    def should_print(self, record: dict) -> bool:
        event = record['event']
        if event == 'query':
            return False
        if event in {'connected', 'reconnected'}:
            self.last.clear()
        if self.mode == 'samples' or event not in {'sample', 'comparison_mismatch', 'numeric_stale'}:
            return True
        signature = tuple(record.get(key) for key in (
            'raw', 'state', 'numeric_state', 'valid', 'stale',
            'numeric_acquisition_state', 'property_state',
        ))
        now = record['monotonic_s']
        previous = self.last.get(event)
        changed = previous is None or previous[0] != signature
        due = previous is not None and self.status_interval_s > 0 and now - previous[1] >= self.status_interval_s
        if changed or due:
            self.last[event] = (signature, now)
            return True
        return False


def run_watch_do(args) -> int:
    console = WatchConsoleFilter(args.console, args.status_interval)
    options = WatchOptions(
        channel=args.channel, host=args.host, port=args.port, property_name=args.property,
        interval_s=args.interval_ms / 1000, timeout_s=args.timeout,
        max_gap_s=args.max_gap_ms / 1000, reconnect_s=args.reconnect_delay,
        duration_s=args.duration, compare_numeric=args.compare_numeric,
        event_source=args.event_source, expected_presses=args.expected_presses,
        interaction_ms=args.interaction_ms,
    )
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = Path(args.log_file or f'logs/watch-do-{stamp}-{uuid.uuid4().hex[:8]}.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as log:
        def emit(record):
            line = json.dumps(record, allow_nan=False)
            log.write(line + '\n')
            log.flush()
            if console.should_print(record):
                print(line, flush=True)

        diagnostic = DOWatchDiagnostic(options, emit)
        diagnostic.emit({'event': 'log_opened', 'path': str(path.resolve()),
                         'console_mode': args.console, 'status_interval_s': args.status_interval,
                         'note': 'Every query/sample remains in the log; console filtering does not change polling'})
        return diagnostic.run()
