from __future__ import annotations

import copy
import threading
import time
from collections import deque
from dataclasses import dataclass

from .dispatcher_v2_schema import ROLES, keys, number, read_json, simulation_authorized


class SimulationFinished(Exception):
    pass


@dataclass(frozen=True)
class SensorSample:
    value: float | bool
    sampled_at: float
    source: str = 'FAKE independent injected sensor'
    simulated: bool = True


def load_scenario(path, measurement_roles, button_names=None):
    raw = read_json(path)
    allowed_buttons = set(button_names) if button_names is not None else {f'DO 3/{i} Sim' for i in range(1, 7)}
    keys(raw, {'simulation_only', 'description', 'events'}, 'scenario')
    if raw.get('simulation_only') is not True or not isinstance(raw.get('events'), list):
        raise ValueError('Scenario must explicitly be simulation_only with an event list')
    prior = -1.0
    for event in raw['events']:
        keys(event, {'at_s', 'telemetry', 'buttons', 'streaming', 'stop'}, 'scenario event')
        at = number(event['at_s'], 'event time', 0, 86400)
        if at < prior:
            raise ValueError('Scenario events must be ordered')
        prior = at
        if 'streaming' in event and type(event['streaming']) is not bool:
            raise ValueError('streaming must be Boolean')
        if 'stop' in event and type(event['stop']) is not bool:
            raise ValueError('stop must be Boolean')
        buttons = event.get('buttons', {})
        if not isinstance(buttons, dict) or any(name not in allowed_buttons or type(value) is not bool for name, value in buttons.items()):
            raise ValueError('Scenario buttons must be configured trigger names and Boolean values')
        telemetry = event.get('telemetry', {})
        if not isinstance(telemetry, dict):
            raise ValueError('Scenario telemetry must be an object')
        for name, value in telemetry.items():
            if name not in measurement_roles:
                raise ValueError(f'Unknown sensor fixture: {name}')
            if measurement_roles[name]['kind'] == 'boolean':
                if type(value) is not bool:
                    raise ValueError('Interlock samples must be Boolean')
            else:
                number(value, 'fake sensor value', -1000, 1000)
    return raw['events']


class MemoryFixture:
    """In-process fixtures only. There is deliberately no SCPI/network fallback."""

    def __init__(self, config, *, clock=time.monotonic, events=()):
        if config.raw.get('execution_backend') != 'memory_fixture' or config.raw.get('simulation_only') is not True:
            raise PermissionError('MemoryFixture accepts only explicit fixture-only configurations')
        self.clock = clock
        self.outputs = copy.deepcopy(config.raw['simulation']['outputs'])
        self.sensor_values = copy.deepcopy(config.raw['simulation']['telemetry'])
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

    def connect(self):
        with self.lock:
            self.connected = True

    def close(self):
        with self.lock:
            self.connected = False
            self.capabilities.clear()

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
        if self.streaming and now >= self.next_sample:
            for name, value in self.sensor_values.items():
                self.samples[name] = SensorSample(value, now)
            self.next_sample = now + self.sample_interval

    def inject(self, name, value, *, sampled_at=None):
        with self.lock:
            if name not in self.sensor_values:
                raise ValueError('Unknown independent sensor fixture')
            self.sensor_values[name] = value
            self.samples[name] = SensorSample(value, self.clock() if sampled_at is None else sampled_at)

    def discover(self):
        with self.lock:
            self.pump()
            return copy.deepcopy(self.outputs)

    def authorize(self, role, fixture_key, spec):
        with self.lock:
            self._connected()
            if role not in ROLES or fixture_key not in self.outputs or not simulation_authorized(spec['authorization']):
                raise PermissionError('Missing explicit fixture-only capability')
            self.capabilities[role] = (fixture_key, spec['min_aout_v'], spec['max_aout_v'])

    def read_output(self, fixture_key):
        with self.lock:
            self.pump()
            return self.outputs[fixture_key]['initial_v']

    def write(self, role, value):
        with self.lock:
            self.pump()
            if role not in self.capabilities:
                raise PermissionError('Output has no fixture capability')
            key, low, high = self.capabilities[role]
            value = number(value, 'fixture demand', low, high)
            self.outputs[key]['initial_v'] = value
            self.commands.append((role, value))

    def read_button(self, name):
        with self.lock:
            self.pump()
            return self.buttons[name]

    def measure(self, name):
        with self.lock:
            self.pump()
            if name not in self.samples:
                raise RuntimeError(f'Missing independent sensor sample: {name}')
            return self.samples[name]
