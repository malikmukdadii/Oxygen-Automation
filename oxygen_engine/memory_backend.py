"""In-process dual-AOUT fixture backend (no network / SCPI)."""

from __future__ import annotations

import copy
import threading
import time
from collections import deque
from dataclasses import dataclass

from .errors import AuthorizationError, ConfigError, ControlError, InterlockError
from .schema import ROLES, number, read_json, keys


class SimulationFinished(Exception):
    pass


@dataclass(frozen=True)
class Sample:
    value: float | bool
    sampled_at: float  # host monotonic when the sample was captured/returned
    source: str = 'FAKE independent injected sensor'
    simulated: bool = True
    # Optional plant/acquisition domain stamp. None ⇒ freshness is host-SCPI only.
    acquisition_time_s: float | None = None
    sequence: int | None = None
    freshness: str = 'host_scpi'  # host_scpi | acquisition | sequence
    pinned: bool = False  # True when inject() pinned an explicit sampled_at (stale tests)


def load_scenario(path, measurements, button_names=None):
    raw = read_json(path)
    allowed_buttons = set(button_names) if button_names is not None else set()
    keys(raw, {'simulation_only', 'description', 'events'}, 'scenario')
    if raw.get('simulation_only') is not True or not isinstance(raw.get('events'), list):
        raise ConfigError('Scenario must explicitly be simulation_only with an event list')
    prior = -1.0
    for event in raw['events']:
        keys(event, {'at_s', 'telemetry', 'buttons', 'streaming', 'stop'}, 'scenario event')
        at = number(event['at_s'], 'event time', 0, 86400)
        if at < prior:
            raise ConfigError('Scenario events must be ordered')
        prior = at
        if 'streaming' in event and type(event['streaming']) is not bool:
            raise ConfigError('streaming must be Boolean')
        if 'stop' in event and type(event['stop']) is not bool:
            raise ConfigError('stop must be Boolean')
        buttons = event.get('buttons', {})
        if not isinstance(buttons, dict) or any(
                name not in allowed_buttons or type(value) is not bool for name, value in buttons.items()):
            raise ConfigError('Scenario buttons must be configured trigger names and Boolean values')
        telemetry = event.get('telemetry', {})
        if not isinstance(telemetry, dict):
            raise ConfigError('Scenario telemetry must be an object')
        for name, value in telemetry.items():
            if name not in measurements:
                raise ConfigError(f'Unknown sensor fixture: {name}')
            if measurements[name]['kind'] == 'boolean':
                if type(value) is not bool:
                    raise ConfigError('Interlock samples must be Boolean')
            else:
                number(value, 'fake sensor value', -1000, 1000)
    return raw['events']


class MemoryBackend:
    """In-process fixtures only. There is deliberately no SCPI/network fallback."""

    def __init__(self, config, *, clock=time.monotonic, events=()):
        if config.transport != 'memory':
            raise AuthorizationError('MemoryBackend accepts only transport memory')
        self.config = config
        self.clock = clock
        self.outputs = copy.deepcopy(config.raw['simulation']['outputs'])
        self.sensor_values = copy.deepcopy(config.raw['simulation'].get('telemetry', {}))
        self.buttons = {a['trigger']: False for a in config.raw.get('assignments', []) if a.get('enabled', True)}
        self.streaming = config.raw['simulation']['streaming']
        self.sample_interval = config.raw['simulation']['sample_interval_s']
        self.samples = {}
        self.capabilities = {}
        self.commands = deque(maxlen=10000)
        self.connected = False
        self.lock = threading.RLock()
        self.events = copy.deepcopy(list(events))
        self.event_index = 0
        self.started_at = clock()
        self.next_sample = self.started_at
        self.writes_attempted = 0
        self.open_loop = config.open_loop
        self.full_discover_count = 0
        self._session = None

    def connect(self):
        with self.lock:
            self.connected = True
            self.full_discover_count += 1
            self._session = copy.deepcopy(self.outputs)

    def close(self):
        with self.lock:
            self.connected = False
            self.capabilities.clear()
            self._session = None

    def _connected(self):
        if not self.connected:
            raise ConnectionError('Fixture disconnected; no automatic reconnect')

    def pump(self):
        self._connected()
        now = self.clock()
        while self.event_index < len(self.events) and self.events[self.event_index]['at_s'] <= now - self.started_at:
            event = self.events[self.event_index]
            self.event_index += 1
            self.buttons.update(event.get('buttons', {}))
            for name, value in event.get('telemetry', {}).items():
                self.inject(name, value, sampled_at=self.started_at + event['at_s'])
            self.streaming = event.get('streaming', self.streaming)
            if event.get('stop'):
                raise SimulationFinished('End of explicit simulation scenario')
        if self.streaming and now >= self.next_sample and self.sensor_values:
            for name, value in self.sensor_values.items():
                prior = self.samples.get(name)
                seq = 0 if prior is None or prior.sequence is None else prior.sequence + 1
                self.samples[name] = Sample(
                    value, now, acquisition_time_s=now, sequence=seq,
                    freshness='acquisition', pinned=False)
            self.next_sample = now + self.sample_interval

    def inject(self, name, value, *, sampled_at=None, acquisition_time_s=None, sequence=None):
        with self.lock:
            if name not in self.sensor_values and name not in self.config.raw.get('measurements', {}):
                raise ConfigError('Unknown independent sensor fixture')
            self.sensor_values[name] = value
            prior = self.samples.get(name)
            pinned = sampled_at is not None
            stamp = self.clock() if sampled_at is None else sampled_at
            if sequence is None:
                sequence = 0 if prior is None or prior.sequence is None else prior.sequence + 1
            acq = stamp if acquisition_time_s is None else acquisition_time_s
            self.samples[name] = Sample(
                value, stamp, acquisition_time_s=acq, sequence=sequence,
                freshness='acquisition' if not pinned else 'host_scpi', pinned=pinned)

    def discover(self):
        with self.lock:
            self.pump()
            if self._session is None:
                raise ControlError('Discovery is pinned at connect; session is not ready')
            # Return the session pin; fixture values may update but identity fields stay pinned.
            pinned = copy.deepcopy(self._session)
            for key, fixture in pinned.items():
                live = self.outputs.get(key, {})
                if 'value' in live:
                    fixture['value'] = live['value']
                if 'initial_v' in live:
                    fixture['initial_v'] = live.get('initial_v', fixture.get('initial_v'))
            return pinned

    def constant_property(self, role):
        binding = self.config.raw['outputs'][role]['binding']
        return self.outputs[binding]['property']

    def expected_source(self, role):
        binding = self.config.raw['outputs'][role]['binding']
        return self.outputs[binding]['source']

    def channel_id(self, role):
        binding = self.config.raw['outputs'][role]['binding']
        return str(self.outputs[binding]['id'])

    def authorize(self, role, binding, spec):
        with self.lock:
            self.pump()
            if binding != spec.get('binding') or binding not in self.outputs:
                raise AuthorizationError('Fixture authorization does not match configured binding')
            fixture = self.outputs[binding]
            self.capabilities[binding] = (spec['min_aout_v'], spec['max_aout_v'], fixture)

    def read_output(self, binding):
        with self.lock:
            self.pump()
            if binding not in self.outputs:
                raise ControlError('Unknown fixture output binding')
            return float(self.outputs[binding].get('value', self.outputs[binding]['initial_v']))

    def write(self, role, value):
        with self.lock:
            self.pump()
            binding = self.config.raw['outputs'][role]['binding']
            if binding not in self.capabilities:
                raise AuthorizationError('Output role not authorized by preflight')
            low, high, fixture = self.capabilities[binding]
            value = number(value, 'fixture demand', low, high)
            self.writes_attempted += 1
            self.commands.append(('write', role, value))
            fixture['value'] = value
            self.outputs[binding] = fixture

    def read_button(self, name):
        with self.lock:
            self.pump()
            if name not in self.buttons:
                raise ControlError('Unknown fixture trigger')
            return self.buttons[name]

    def measure(self, name):
        with self.lock:
            self.pump()
            if name not in self.sensor_values and name not in self.config.raw.get('measurements', {}):
                raise InterlockError(f'cannot satisfy measurement request: {name}')
            if name not in self.sensor_values:
                raise InterlockError(f'cannot satisfy measurement request: {name}')
            sample = self.samples.get(name)
            if sample is not None and sample.pinned:
                # Explicit inject(..., sampled_at=...) stays stale until re-injected.
                return sample
            now = self.clock()
            prior = sample
            seq = 0 if prior is None or prior.sequence is None else prior.sequence + 1
            # Unpinned / streaming: each measure is a fresh acquisition sample.
            sample = Sample(
                self.sensor_values[name], now, acquisition_time_s=now, sequence=seq,
                freshness='acquisition', pinned=False)
            self.samples[name] = sample
            return sample
