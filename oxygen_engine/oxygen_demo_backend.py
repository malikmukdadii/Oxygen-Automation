from __future__ import annotations

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from .adapters.oxygen import OxygenAdapter
from .aout_discovery import AoutDiscovery
from .constant_pattern import finite_number
from .dispatcher_v2_schema import (
    OXYGEN_DEMO_APPROVALS, OXYGEN_DEMO_OUTPUTS, confirm_demo_outputs, number, simulation_authorized,
)
from .scpi_transport import ScpiProtocolError, ScpiTransport, parse_scpi, quote_scpi
from .simulation_fixture import MemoryFixture


class OxygenDemoTransport(ScpiTransport):
    """Only the two operator-confirmed demo AOs; DO reads never grant DO writes."""

    def __init__(self, *, host, port, timeout_s, emit):
        super().__init__(host=host, port=port, timeout_s=timeout_s)
        self.emit = emit
        self.capabilities = {}
        self.do_ids = set()
        self.output_bindings = {}
        self.read_ids = set()

    def close(self):
        self.capabilities.clear()
        self.output_bindings.clear()
        self.do_ids.clear()
        self.read_ids.clear()
        super().close()

    def reconnect(self):
        raise ScpiProtocolError('OXYGEN demo never reconnects or replays output commands')

    def bind_outputs(self, bindings, *, names=None):
        if not self.connected or self.output_bindings or self.capabilities or set(bindings) != set(OXYGEN_DEMO_OUTPUTS):
            raise ScpiProtocolError('Output bindings can only be established once per session')
        names = {role: output[0] for role, output in OXYGEN_DEMO_OUTPUTS.items()} if names is None else dict(names)
        if set(names) != set(bindings):
            raise ScpiProtocolError('Both approved simulated-channel names are required')
        assigned_ids = [cid for cid, _ in bindings.values()]
        if len(set(assigned_ids)) != len(assigned_ids) or set(assigned_ids) & self.do_ids:
            raise ScpiProtocolError('Discovered output roles collide')
        channels = OxygenAdapter(self).list_channels()
        ids = set()
        for role, (cid, prop) in bindings.items():
            OxygenAdapter._channel_id(cid)
            if cid in ids or cid in self.do_ids:
                raise ScpiProtocolError('Discovered output roles collide or have unapproved properties')
            matches = [c for c in channels if c.name == names[role]]
            approved_properties = OXYGEN_DEMO_APPROVALS[role].get(names[role])
            if approved_properties is None or len(matches) != 1 or matches[0].channel_id != cid:
                raise ScpiProtocolError('Only the approved simulated-channel names may receive write bindings')
            if prop not in approved_properties:
                raise ScpiProtocolError('Discovered output roles have unapproved properties for their simulated-channel names')
            ids.add(cid)
        self.output_bindings = dict(bindings)

    def authorize(self, role, channel_id, prop, low, high):
        maximum = .5 if role == 'rzxa_voltage_demand' else .4
        if not self.connected or self.output_bindings.get(role) != (channel_id, prop) or low != 0 or not 0 < high <= maximum:
            raise ScpiProtocolError('No approved OXYGEN simulated-channel capability')
        self.capabilities[(channel_id, prop)] = (low, high)

    def query(self, command):
        allowed = command in {'*IDN?', ':CHANNELlist:NAMes?', 'SYST:ERR?'}
        allowed = allowed or any(command == f':CHANNELlist:ITEM{cid}:ATTR:NAMes?' for cid in self.read_ids)
        prefix = ':CHANNELlist:PROPerty? '
        if command.startswith(prefix):
            args = parse_scpi(command[len(prefix):])
            if len(args) == 2 and all(isinstance(a, str) for a in args):
                cid, prop = args
                permitted = cid in self.read_ids
                allowed = permitted and command == prefix + ','.join(quote_scpi(a) for a in args)
        if not allowed:
            raise ScpiProtocolError(f'OXYGEN demo query refused: {command!r}')
        started = time.monotonic()
        record = {'event': 'query', 'command': command, 'backend': 'oxygen_demo'}
        try:
            reply = super().query(command)
            if command == ':CHANNELlist:NAMes?':
                entries = parse_scpi(reply) if reply.upper() != 'NONE' else []
                self.read_ids = {entry[0] for entry in entries if isinstance(entry, tuple) and len(entry) == 2}
            record['raw_payload'] = reply
            return reply
        except KeyboardInterrupt:
            self.close()
            raise
        except Exception as exc:
            record['error'] = str(exc)
            raise
        finally:
            record['latency_ms'] = (time.monotonic() - started) * 1000
            self.emit(record)

    def write(self, command):
        prefix = ':CHANNELlist:PROPerty '
        if not command.startswith(prefix):
            raise ScpiProtocolError('Only approved demo constant-property writes are permitted')
        args = parse_scpi(command[len(prefix):])
        if len(args) != 5 or args[2] != 'SCALAR' or args[4] != 'V':
            raise ScpiProtocolError('Expected canonical SCALAR demand in V')
        capability = self.capabilities.get((args[0], args[1]))
        if capability is None:
            raise ScpiProtocolError('Output not authorized by completed demo preflight')
        value = finite_number(args[3], 'demo setpoint')
        low, high = capability
        canonical = prefix + f'{quote_scpi(args[0])},{quote_scpi(args[1])},SCALAR,{value:.9g},"V"'
        if not low <= value <= high or command != canonical:
            raise ScpiProtocolError('Demo demand is out of bounds or command is not canonical')
        try:
            super().write(command)
        except KeyboardInterrupt:
            self.close()
            raise


class OxygenDemoBackend:
    def __init__(self, config, emit, *, confirmed_outputs, clock=time.monotonic):
        if config.raw.get('execution_backend') != 'oxygen_demo':
            raise PermissionError('Explicit oxygen_demo configuration required')
        confirm_demo_outputs(config.raw['outputs'], confirmed_outputs)
        self.output_names = {role: spec['channel'] for role, spec in config.raw['outputs'].items()}
        self.config, self.emit, self.clock = config, emit, clock
        connection = config.raw['connection']
        self.transport = OxygenDemoTransport(host=connection['host'], port=connection['port'],
                                              timeout_s=connection['timeout_s'], emit=emit)
        self.adapter = OxygenAdapter(self.transport)
        self.discovery = AoutDiscovery(self.adapter, config.raw['outputs'])
        self.lock = threading.RLock()
        self.do_channels = {}
        self.channel_ids = {}
        self.pinned_sources = {}
        self.authorized = set()
        sensor_config = SimpleNamespace(raw={'execution_backend': 'memory_fixture', 'simulation_only': True,
                                             'simulation': {**config.raw['simulation'], 'outputs': {}}})
        self.sensors = MemoryFixture(sensor_config, clock=clock)

    @property
    def connected(self):
        return self.transport.connected

    def error(self):
        entry = self.transport.check_error()
        if entry.code:
            raise ScpiProtocolError(f'OXYGEN demo SCPI error: {entry}')

    def connect(self):
        with self.lock:
            self.transport.connect()
            if self.adapter.identify().split(',')[:2] != ['DEWETRON', 'OXYGEN']:
                raise ScpiProtocolError('Expected DEWETRON OXYGEN')
            channels = self.adapter.list_channels()
            reports = self.discovery.discover()
            bindings = {role: (r['channel_id'], r['const_property']) for role, r in reports.items()}
            self.transport.bind_outputs(bindings, names=self.output_names)
            self.channel_ids = {role: binding[0] for role, binding in bindings.items()}
            for role, report in reports.items():
                cid = self.channel_ids[role]
                self.pinned_sources[role] = report['source']
                self.emit({'event': 'demo_channel_resolved', 'output': role, 'channel': report['channel'],
                           'channel_id': cid, 'property': report['const_property'], 'id_scope': 'current_session'})
                self.emit({'event': 'demo_constant_source_pinned', 'output': role, 'source': report['source'],
                           'property': report['const_property'], 'source_changed': False})
            for assignment in self.config.raw['assignments']:
                if not assignment.get('enabled', True):
                    continue
                name = assignment['trigger']
                matches = [c for c in channels if c.name == name]
                if len(matches) != 1 or matches[0].channel_id in set(self.channel_ids.values()):
                    raise ScpiProtocolError(f'Cannot uniquely discover DOUT trigger: {name}')
                cid = matches[0].channel_id
                self.transport.do_ids.add(cid)
                if 'DO_Value' not in self.adapter.list_properties(cid):
                    raise ScpiProtocolError(f'{name}: DO_Value missing')
                self.do_channels[name] = cid
                self.read_button(name)
                self.emit({'event': 'do_channel_resolved', 'assignment': assignment['name'],
                           'channel': name, 'channel_id': cid, 'property': 'DO_Value', 'access': 'read_only'})
            self.error()
            self.sensors.connect()
            self.emit({'event': 'oxygen_demo_connected', 'endpoint': f'{self.transport.host}:{self.transport.port}',
                       'operator_confirmed_simulated_outputs': sorted(self.output_names.values()),
                       'telemetry': 'FAKE independent injected sensor; NOT measured DC bus',
                       'physical_hardware_authorized': False})

    def close(self):
        with self.lock:
            self.authorized.clear()
            self.transport.close()
            self.channel_ids.clear()
            self.pinned_sources.clear()
            self.discovery.pinned.clear()
            self.discovery.failed = False
            self.do_channels.clear()
            self.sensors.close()

    def text(self, cid, prop):
        value = self.adapter.get_property_value(cid, prop)
        if value.kind != 'STRING' or len(value.values) != 1:
            raise ScpiProtocolError(f'Expected STRING for {prop}: {value!r}')
        return value.values[0]

    def discover(self):
        with self.lock:
            try:
                reports = self.discovery.discover()
            except Exception:
                self.authorized.clear()
                self.transport.capabilities.clear()
                raise
            result = {}
            for role, spec in self.config.raw['outputs'].items():
                report = reports[role]
                result[spec['simulation_fixture']] = {'name': report['channel'], 'id': report['channel_id'],
                    'mode': report['mode'], 'source': report['source'], 'property': report['const_property'],
                    'unit': report['unit'], 'min_v': report['range'][0], 'max_v': report['range'][1]}
            self.error()
            return result

    def constant_property(self, role):
        return self.discovery.pinned[role]['const_property']

    def expected_source(self, role):
        return self.pinned_sources[role]

    def channel_id(self, role):
        return self.channel_ids[role]

    def authorize(self, role, fixture_key, spec):
        with self.lock:
            if not simulation_authorized(spec['authorization']) or spec['simulation_fixture'] != fixture_key:
                raise PermissionError('Missing per-role simulation-only authorization')
            self.discover()
            self.transport.authorize(role, self.channel_ids[role], self.constant_property(role), spec['min_aout_v'], spec['max_aout_v'])
            self.authorized.add(role)

    def read_output(self, fixture_key):
        with self.lock:
            matches = [(role, spec) for role, spec in self.config.raw['outputs'].items() if spec['simulation_fixture'] == fixture_key]
            if len(matches) != 1:
                raise ScpiProtocolError('Unknown or aliased output')
            role, spec = matches[0]
            self.discover()
            value = self.adapter.get_property_value(self.channel_ids[role], self.constant_property(role))
            if value.kind != 'SCALAR' or len(value.values) != 2 or value.values[1] != 'V':
                raise ScpiProtocolError(f'Expected SCALAR constant readback in V: {value!r}')
            self.error()
            return finite_number(value.values[0], 'constant readback')

    def write(self, role, value):
        with self.lock:
            if role not in self.authorized:
                raise PermissionError('Output role not authorized by demo preflight')
            self.discover()
            spec = self.config.raw['outputs'][role]
            value = number(value, 'demo demand', spec['min_aout_v'], spec['max_aout_v'])
            command = f':CHANNELlist:PROPerty {quote_scpi(self.channel_ids[role])},{quote_scpi(self.constant_property(role))},SCALAR,{value:.9g},"V"'
            self.transport.command(command)

    def read_button(self, name):
        with self.lock:
            value = self.adapter.get_property_value(self.do_channels[name], 'DO_Value')
            self.error()
            if value.kind not in {'BOOL', 'BOOLEAN'} or len(value.values) != 1 or type(value.values[0]) is not bool:
                raise ScpiProtocolError('Invalid DO_Value Boolean')
            return value.values[0]

    def measure(self, name):
        return self.sensors.measure(name)
