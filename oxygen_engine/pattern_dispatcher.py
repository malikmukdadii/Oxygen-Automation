from __future__ import annotations

import json
import logging
import math
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .adapters.oxygen import OxygenAdapter
from .constant_pattern import (
    APPROVED_CHANNEL, SimulatedConstantTransport, execute_timeline, finite_number,
    run_simulated_pattern,
)
from .do_monitor import DiagnosticEdgeTracker
from .profiles import ProfileError, load_profile
from .scpi_transport import ScpiProtocolError


@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float
    start_v: float
    end_v: float
    phase: str


@dataclass(frozen=True)
class OutputPattern:
    name: str
    segments: tuple[Segment, ...]

    @property
    def duration_s(self):
        return self.segments[-1].end_s

    @property
    def boundaries(self):
        return tuple(s.end_s for s in self.segments)

    def value_at(self, elapsed_s):
        t = max(0.0, finite_number(elapsed_s, 'elapsed time'))
        for s in self.segments:
            if t < s.end_s:
                return s.start_v + (s.end_v - s.start_v) * (t - s.start_s) / (s.end_s - s.start_s)
        return 0.0

    def phase_at(self, t):
        return next((s.phase for s in self.segments if t < s.end_s), 'complete')


def require_keys(raw, allowed, label):
    if not isinstance(raw, dict) or set(raw) - set(allowed):
        raise ValueError(f'{label} must be an object with only these keys: {sorted(allowed)}')


def load_output_pattern(path, logical_output):
    profile = load_profile(path)
    if profile.recording or profile.expected_conditions:
        raise ProfileError('Dispatcher profiles cannot request recording or expected_conditions')
    if not 3 <= len(profile.steps) <= 102:
        raise ProfileError('A profile needs initial zero, 1..100 ramps/holds, and final return_neutral')
    first, *middle, final = profile.steps
    if first.type != 'set_aout' or final.type != 'return_neutral':
        raise ProfileError('Profiles must begin with set_aout zero and end with return_neutral')
    require_keys(first.params, {'channel', 'value'}, 'Initial set_aout')
    require_keys(final.params, {'channel'}, 'Final return_neutral')
    if finite_number(first.params['value'], 'initial value') != 0:
        raise ProfileError('Initial setpoint must be zero')
    elapsed, value, segments = 0.0, 0.0, []
    for index, step in enumerate(profile.steps):
        if step.type != 'hold' and step.params.get('channel') != logical_output:
            raise ProfileError(f'Every output step must target logical output {logical_output}')
        if index == 0 or index == len(profile.steps) - 1:
            continue
        if step.type not in {'ramp_aout', 'hold'}:
            raise ProfileError('Only ramps and holds are allowed between initial/final zero')
        allowed = {'duration_s'} if step.type == 'hold' else {'channel', 'target', 'duration_s', 'rate_per_s'}
        require_keys(step.params, allowed, step.type)
        duration = finite_number(step.params['duration_s'], 'step duration')
        if not .1 <= duration <= 300:
            raise ProfileError('Each ramp/hold duration must be between 0.1 and 300 seconds')
        target = value if step.type == 'hold' else finite_number(step.params['target'], 'target')
        if not 0 <= target <= 1:
            raise ProfileError('Only the approved 0..1 V simulated envelope is supported')
        if 'rate_per_s' in step.params:
            rate = finite_number(step.params['rate_per_s'], 'ramp rate')
            if not math.isclose(rate, abs(target - value) / duration, rel_tol=1e-9, abs_tol=1e-12):
                raise ProfileError('Ramp rate disagrees with target and duration; omit it to calculate automatically')
        segments.append(Segment(elapsed, elapsed + duration, value, target, f'step_{index}_{step.type}'))
        elapsed += duration
        value = target
    if value != 0 or elapsed > 300:
        raise ProfileError('Ramp back to zero before return_neutral; total duration must not exceed 300 seconds')
    if not math.isfinite(profile.timeout_s) or profile.timeout_s < elapsed:
        raise ProfileError('Profile timeout must be finite and cover the full duration')
    return OutputPattern(profile.name, tuple(segments))


@dataclass(frozen=True)
class Assignment:
    name: str
    trigger: str
    output: str
    profile_path: Path
    pattern: OutputPattern


@dataclass(frozen=True)
class DispatcherConfig:
    host: str
    port: int
    timeout_s: float
    expected_id: str
    assignments: tuple[Assignment, ...]
    poll_s: float
    max_gap_s: float
    interval_s: float
    max_lateness_s: float


def load_dispatcher_config(path):
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(raw, dict) and 'schema_version' in raw:
        from .dispatcher_v2_schema import canonical_to_legacy

        raw = canonical_to_legacy(raw)
    require_keys(raw, {'connection', 'outputs', 'assignments', 'timing'}, 'Dispatcher configuration')
    connection = raw.get('connection', {})
    require_keys(connection, {'host', 'port', 'timeout_s'}, 'connection')
    host = connection.get('host', '127.0.0.1')
    port = connection.get('port', 10001)
    timeout = finite_number(connection.get('timeout_s', .5), 'timeout')
    if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535 or not 0 < timeout <= 5:
        raise ValueError('Connection requires a host, integer TCP port and timeout in (0, 5] seconds')
    outputs = raw.get('outputs')
    if not isinstance(outputs, dict) or len(outputs) != 1:
        raise ValueError('Exactly one logical output is currently supported')
    logical_output, output = next(iter(outputs.items()))
    if not logical_output:
        raise ValueError('Logical output name cannot be empty')
    require_keys(output, {'channel', 'expected_id'}, 'output')
    if output.get('channel') != APPROVED_CHANNEL:
        raise ValueError('Only the confirmed simulated AO 3/1 Sim output is authorized')
    expected_id = OxygenAdapter._channel_id(output['expected_id'])
    timing = raw.get('timing', {})
    require_keys(timing, {'poll_ms', 'max_trigger_gap_ms', 'interval_ms', 'max_lateness_ms'}, 'timing')
    poll = finite_number(timing.get('poll_ms', 50), 'poll interval') / 1000
    gap = finite_number(timing.get('max_trigger_gap_ms', 250), 'maximum trigger gap') / 1000
    interval = finite_number(timing.get('interval_ms', 50), 'update interval') / 1000
    lateness = finite_number(timing.get('max_lateness_ms', 250), 'maximum lateness') / 1000
    if not (.02 <= poll <= .25 and poll < gap <= 1 and .02 <= interval <= .25 and 0 < lateness <= 1):
        raise ValueError('Invalid timing: intervals 20..250 ms, gap greater than poll and <=1000 ms, lateness 1..1000 ms')
    items = raw.get('assignments')
    if not isinstance(items, list) or not 1 <= len(items) <= 6:
        raise ValueError('Provide 1..6 button assignments')
    assignments, names, triggers = [], set(), set()
    for item in items:
        require_keys(item, {'name', 'enabled', 'trigger', 'output', 'profile'}, 'assignment')
        if type(item.get('enabled', True)) is not bool:
            raise ValueError('Assignment enabled must be Boolean')
        if not item.get('enabled', True):
            continue
        name, trigger = item.get('name'), item.get('trigger')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Enabled assignment names must be nonempty and unique')
        if trigger not in {f'DO 3/{i} Sim' for i in range(1, 7)} or trigger in triggers:
            raise ValueError('Each enabled assignment needs a unique DO 3/1..6 Sim trigger')
        if item.get('output') != logical_output:
            raise ValueError('Assignment output must reference the configured logical output')
        filename = item.get('profile')
        if not isinstance(filename, str) or not filename:
            raise ValueError('Assignment profile must be a file path')
        profile_path = (path.parent / filename).resolve()
        pattern = load_output_pattern(profile_path, logical_output)
        assignments.append(Assignment(name, trigger, logical_output, profile_path, pattern))
        names.add(name)
        triggers.add(trigger)
    if not assignments:
        raise ValueError('At least one assignment must be enabled')
    return DispatcherConfig(host, port, timeout, expected_id, tuple(assignments), poll, gap, interval, lateness)


class PatternDispatcher:
    def __init__(self, config, transport, emit, *, clock=time.monotonic, wait=time.sleep,
                 timeline=execute_timeline):
        self.config, self.transport, self.emit = config, transport, emit
        self.clock, self.wait, self.timeline = clock, wait, timeline
        self.trigger_ids = {}
        self.completed = 0
        self.started = 0

    def read(self, channel_id):
        value = OxygenAdapter(self.transport).get_property_value(channel_id, 'DO_Value')
        error = self.transport.check_error()
        if error.code:
            raise ScpiProtocolError(f'Trigger SCPI error: {error}')
        if value.kind not in {'BOOL', 'BOOLEAN'} or len(value.values) != 1 or type(value.values[0]) is not bool:
            raise ScpiProtocolError(f'Invalid DO_Value: {value!r}')
        return value.values[0]

    def prepare(self):
        adapter = OxygenAdapter(self.transport)
        channels = adapter.list_channels()
        for assignment in self.config.assignments:
            matches = [c for c in channels if c.name == assignment.trigger]
            if len(matches) != 1 or matches[0].channel_id == self.transport.expected_id:
                raise ScpiProtocolError(f'Expected exactly one distinct trigger: {assignment.trigger}')
            cid = matches[0].channel_id
            self.transport.trigger_ids.add(cid)
            if 'DO_Value' not in adapter.list_properties(cid):
                raise ScpiProtocolError(f'{assignment.trigger} has no DO_Value property')
            self.read(cid)
            self.trigger_ids[assignment.name] = cid
            self.emit({'event': 'assignment_preflight_passed', 'assignment': assignment.name,
                       'trigger': assignment.trigger, 'trigger_id': cid, 'profile': assignment.pattern.name})

    def monitor(self, unused_pattern, setpoint, verify, emit, **timing):
        trackers = {a.name: DiagnosticEdgeTracker(self.config.max_gap_s) for a in self.config.assignments}
        last_verify = self.clock()
        emit({'event': 'dispatcher_ready', 'message': 'LOW then HIGH starts a pattern; Ctrl+C stops',
              'assignments': list(trackers)})
        while True:
            ready = []
            for assignment in self.config.assignments:
                started = self.clock()
                state = self.read(self.trigger_ids[assignment.name])
                now = self.clock()
                tracker = trackers[assignment.name]
                if now - started > self.config.max_gap_s:
                    tracker.reset()
                    emit({'event': 'trigger_stale', 'assignment': assignment.name, 'latency_ms': (now - started) * 1000})
                    continue
                observation = tracker.observe(state, now)
                emit({'event': 'trigger_sample', 'assignment': assignment.name, **observation})
                if observation['edge'] != 'steady':
                    emit({'event': 'trigger_state', 'assignment': assignment.name, **observation})
                if observation['press']:
                    ready.append((assignment, now))
            if ready:
                assignment, observed = ready[0]
                if self.clock() - observed > self.config.max_gap_s:
                    for tracker in trackers.values():
                        tracker.reset()
                    emit({'event': 'trigger_stale', 'assignment': assignment.name, 'message': 'Scan delayed dispatch; no run'})
                    continue
                for ignored, _ in ready[1:]:
                    emit({'event': 'trigger_ignored', 'assignment': ignored.name, 'reason': 'simultaneous press; config order wins'})
                verify(0.0)
                if self.clock() - observed > self.config.max_gap_s:
                    for tracker in trackers.values():
                        tracker.reset()
                    emit({'event': 'trigger_stale', 'assignment': assignment.name,
                          'message': 'Context verification delayed dispatch; no run'})
                    continue
                self.started += 1
                emit({'event': 'pattern_cycle_started', 'cycle': self.started, 'assignment': assignment.name,
                      'profile': assignment.pattern.name, 'output': assignment.output})
                result = self.timeline(assignment.pattern, setpoint, verify, emit, **timing)
                verify(0.0)
                self.completed += 1
                emit({'event': 'pattern_cycle_complete', 'cycle': self.started, 'assignment': assignment.name,
                      'profile': assignment.pattern.name, 'final_state': 'configured_zero_verified', **result})
                for tracker in trackers.values():
                    tracker.reset()
                last_verify = self.clock()
                emit({'event': 'dispatcher_ready', 'message': 'Waiting for fresh LOW then HIGH',
                      'completed_cycles': self.completed})
            elif self.clock() - last_verify >= .5:
                verify(0.0)
                last_verify = self.clock()
            self.wait(self.config.poll_s)

    def run(self):
        code, summary = run_simulated_pattern(self.transport, self.config.assignments[0].pattern, self.emit,
                                              interval_s=self.config.interval_s,
                                              max_lateness_s=self.config.max_lateness_s,
                                              timeline=self.monitor, before_prepare=self.prepare)
        self.emit({'event': 'dispatcher_summary', 'started_cycles': self.started,
                   'completed_cycles': self.completed, 'status': summary['status'],
                   'final_state': summary['final_state']})
        return code


class StrictRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        raise OSError('Dispatcher log write failed')


def run_dispatcher(args):
    raw = json.loads(Path(args.bindings).read_text(encoding='utf-8'))
    if isinstance(raw, dict) and raw.get('schema_version') == 2 and raw.get('execution_backend') in {'memory_fixture', 'oxygen_demo'}:
        from .dual_output_dispatcher import run_v2_cli

        return run_v2_cli(args)
    if getattr(args, 'scenario', None):
        raise ValueError('--scenario is restricted to v2 memory_fixture; never applied to OXYGEN')
    config = load_dispatcher_config(args.bindings)
    if args.live:
        raise ValueError('Do not use --live; only explicitly confirmed simulated execution is supported')
    if args.execute and not args.confirm_simulated_output:
        raise ValueError('Execution requires --confirm-simulated-output for AO 3/1 Sim')
    plan = {'event': 'dispatcher_plan', 'execute': args.execute,
            'endpoint': f'{config.host}:{config.port}', 'output': APPROVED_CHANNEL,
            'expected_id': config.expected_id,
            'assignments': [{'name': a.name, 'trigger': a.trigger, 'output': a.output,
                             'profile': str(a.profile_path), 'duration_s': a.pattern.duration_s,
                             'peak_v': max(s.end_v for s in a.pattern.segments)} for a in config.assignments]}
    if not args.execute:
        print(json.dumps({**plan, 'connected': False, 'writes_attempted': 0}, allow_nan=False, indent=2))
        return 0
    path = Path(args.log_file or 'logs/pattern-dispatcher.jsonl').resolve()
    if path == Path(args.bindings).resolve() or path in {a.profile_path for a in config.assignments}:
        raise ValueError('Log file must not overwrite configuration or profiles')
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = StrictRotatingHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    run_id = uuid.uuid4().hex
    logging_failed = False
    try:
        def emit(record):
            nonlocal logging_failed
            if logging_failed:
                return
            record = {'ts': datetime.now(timezone.utc).isoformat(), 'monotonic_s': time.monotonic(),
                      'run_id': run_id, **record}
            line = json.dumps(record, allow_nan=False)
            try:
                handler.emit(logging.LogRecord('pattern-dispatcher', logging.INFO, '', 0, line, (), None))
                if record['event'] not in {'query', 'write_attempt', 'setpoint_ack', 'trigger_sample'}:
                    print(line, flush=True)
            except OSError:
                # Fail once to stop dispatch; disable the failed sink so cleanup can still run.
                logging_failed = True
                raise

        emit({**plan, 'log': str(path), 'log_max_bytes': handler.maxBytes, 'log_backups': handler.backupCount})
        transport = SimulatedConstantTransport(host=config.host, port=config.port, timeout_s=config.timeout_s,
                                               channel=APPROVED_CHANNEL, expected_id=config.expected_id,
                                               confirmed_simulated=True, emit=emit)
        code = PatternDispatcher(config, transport, emit).run()
        if logging_failed:
            print('Dispatcher logging failed. Execution stopped; inspect the output and final zero state.', file=sys.stderr)
            return 1
        return code
    finally:
        handler.close()
