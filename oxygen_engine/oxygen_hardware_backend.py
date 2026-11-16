
"""Explicitly authorized physical constant-AOUT backend with real OXYGEN feedback."""

import math
import re
import threading
import time
from dataclasses import dataclass

from .adapters.oxygen import OxygenAdapter
from .aout_discovery import AoutDiscovery
from .constant_pattern import finite_number
from .dispatcher_v2_schema import ROLES, number
from .hardware_schema import confirm_hardware_outputs, load_hardware_config
from .scpi_transport import ScpiProtocolError, ScpiTransport, parse_scpi, quote_scpi


@dataclass(frozen=True)
class HardwareSample:
    value: float | bool
    sampled_at: float
    source: str
    simulated: bool = False


class OxygenHardwareTransport(ScpiTransport):
    def __init__(self, *, host, port, timeout_s, emit):
        super().__init__(host=host, port=port, timeout_s=timeout_s)
        self.emit = emit
        self.read_ids = set()
        self.bindings = {}
        self.capabilities = {}
        self.numeric_name = None
        self.invalid = False

    def close(self):
        self.capabilities.clear()
        self.bindings.clear()
        self.read_ids.clear()
        self.numeric_name = None
        super().close()

    def reconnect(self):
        raise ScpiProtocolError('Hardware never reconnects or replays output commands')

    def query(self, command):
        if self.invalid:
            raise ScpiProtocolError('Hardware transport invalidated; restart explicitly')
        allowed = command in {'*IDN?', ':CHANNELlist:NAMes?', 'SYST:ERR?', ':ACQUisition:STATe?'}
        allowed = allowed or any(command == f':CHANNELlist:ITEM{cid}:ATTR:NAMes?' for cid in self.read_ids)
        allowed = allowed or self.numeric_name is not None and command in {':NUMeric:NORMal:DIMS?', ':NUMeric:NORMal:VALue?'}
        prefix = ':CHANNELlist:PROPerty? '
        if command.startswith(prefix):
            args = parse_scpi(command[len(prefix):])
            allowed = (len(args) == 2 and all(isinstance(a, str) for a in args) and args[0] in self.read_ids and
                       command == prefix + ','.join(quote_scpi(a) for a in args))
        if not allowed:
            raise ScpiProtocolError(f'Hardware query refused: {command!r}')
        started = time.monotonic()
        record = {'event': 'query', 'backend': 'oxygen_hardware', 'command': command}
        try:
            reply = super().query(command)
            if command == ':CHANNELlist:NAMes?':
                entries = parse_scpi(reply) if reply.upper() != 'NONE' else []
                self.read_ids = {entry[0] for entry in entries if isinstance(entry, tuple) and len(entry) == 2}
            record['raw_payload'] = reply
            return reply
        except BaseException:
            self.invalid = True
            self.capabilities.clear()
            raise
        finally:
            record['latency_ms'] = (time.monotonic() - started) * 1000
            self.emit(record)

    def bind_outputs(self, reports, outputs):
        if not self.connected or self.bindings or self.capabilities or set(reports) != set(ROLES):
            raise ScpiProtocolError('Physical bindings are established once per session')
        bindings = {}
        for role, report in reports.items():
            spec = outputs[role]
            cid, prop = report['channel_id'], report['const_property']
            OxygenAdapter._channel_id(cid)
            permission = spec['authorization']
            if (report['mismatches'] or report['channel'] != spec['channel'] or cid not in self.read_ids or
                    permission.get('simulation') is not False or permission.get('physical') is not True or
                    not re.fullmatch(r'TRION/SourceChannel_A_CONST/Const[0-9]+', prop)):
                raise ScpiProtocolError('Discovered physical output is not explicitly authorized')
            bindings[role] = (cid, prop, spec['min_aout_v'], spec['max_aout_v'])
        if len({v[0] for v in bindings.values()}) != len(bindings):
            raise ScpiProtocolError('Physical output bindings collide')
        self.bindings = bindings

    def authorize(self, role):
        if self.invalid or not self.connected or role not in self.bindings:
            raise ScpiProtocolError('Physical output has no valid session binding')
        cid, prop, low, high = self.bindings[role]
        if low != 0 or not 0 < high <= (.5 if role == 'rzxa_voltage_demand' else 3.3):
            raise ScpiProtocolError('Physical clamp exceeds the role ceiling')
        self.capabilities[(cid, prop)] = (low, high)

    def write(self, command):
        if self.invalid:
            raise ScpiProtocolError('Hardware transport invalidated; restart explicitly')
        numeric = set()
        if self.numeric_name is not None:
            numeric = {':NUMeric:NORMal:ITEMS ' + quote_scpi(self.numeric_name),
                       ':NUMeric:NORMal:FORMat ASCII', ':NUMeric:NORMal:NUMber ALL'}
        if command not in numeric:
            prefix = ':CHANNELlist:PROPerty '
            if not command.startswith(prefix):
                raise ScpiProtocolError('Only authorized constant demands and bus numeric-transfer setup are permitted')
            args = parse_scpi(command[len(prefix):])
            if len(args) != 5 or not all(isinstance(a, str) for a in args) or args[2] != 'SCALAR' or args[4] != 'V':
                raise ScpiProtocolError('Physical output requires canonical SCALAR demand in V')
            bounds = self.capabilities.get((args[0], args[1]))
            value = finite_number(args[3], 'physical demand')
            canonical = prefix + f'{quote_scpi(args[0])},{quote_scpi(args[1])},SCALAR,{value:.9g},"V"'
            if bounds is None or not bounds[0] <= value <= bounds[1] or command != canonical:
                raise ScpiProtocolError('Physical output is unauthorized, noncanonical or out of bounds')
        try:
            super().write(command)
        except BaseException:
            self.invalid = True
            self.capabilities.clear()
            raise


class OxygenHardwareBackend:
    def __init__(self, config, emit, *, confirmed_outputs, live=False, interlocks_confirmed=False, clock=time.monotonic):
        load_hardware_config(config.path, config.raw)
        confirm_hardware_outputs(config, confirmed_outputs, live=live, interlocks_confirmed=interlocks_confirmed)
        self.config, self.emit, self.clock = config, emit, clock
        connection = config.raw['connection']
        self.transport = OxygenHardwareTransport(**{k: connection[k] for k in ('host', 'port', 'timeout_s')}, emit=emit)
        self.adapter = OxygenAdapter(self.transport)
        self.discovery = AoutDiscovery(self.adapter, config.raw['outputs'])
        self.lock = threading.RLock()
        self.input_ids = {}
        self.channel_ids = {}
        self.authorized = set()
        self.failed = False
        self.writes_attempted = 0

    @property
    def connected(self):
        return self.transport.connected

    def error(self):
        entry = self.transport.check_error()
        if entry.code:
            raise ScpiProtocolError(f'Physical OXYGEN SCPI error: {entry}')

    def _pin_inputs(self, channels):
        output_ids = set(self.channel_ids.values())
        sensors = {name: m['channel'] for name, m in self.config.raw['measurement_roles'].items()}
        triggers = {a['name']: a['trigger'] for a in self.config.raw['assignments'] if a.get('enabled', True)}
        for key, name in [('sensor:' + k, v) for k, v in sensors.items()] + [('trigger:' + k, v) for k, v in triggers.items()]:
            matches = [c for c in channels if c.name == name]
            if len(matches) != 1 or matches[0].channel_id in output_ids:
                raise ScpiProtocolError(f'{name}: independent hardware input must have exactly one non-AOUT identity')
            cid = matches[0].channel_id
            if key in self.input_ids and self.input_ids[key] != (name, cid):
                raise ScpiProtocolError(f'{name}: hardware input mapping changed; restart explicitly')
            self.input_ids[key] = (name, cid)
        bus_id = self.input_ids['sensor:dc_bus_voltage'][1]
        if any(cid == bus_id for key, (_, cid) in self.input_ids.items() if key != 'sensor:dc_bus_voltage'):
            raise ScpiProtocolError('Measured bus channel cannot alias a trigger or Boolean interlock')

    def connect(self):
        with self.lock:
            self.transport.connect()
            if self.adapter.identify().split(',')[:2] != ['DEWETRON', 'OXYGEN']:
                raise ScpiProtocolError('Expected DEWETRON OXYGEN')
            reports = self.discovery.discover()
            self.channel_ids = {role: r['channel_id'] for role, r in reports.items()}
            self._pin_inputs(self.adapter.list_channels())
            for name, spec in self.config.raw['measurement_roles'].items():
                cid = self.input_ids['sensor:' + name][1]
                prop = 'Unit' if name == 'dc_bus_voltage' else spec['property']
                if prop not in self.adapter.list_properties(cid):
                    raise ScpiProtocolError(f'{name}: configured hardware input property {prop!r} is unavailable')
            for assignment in self.config.raw['assignments']:
                if assignment.get('enabled', True):
                    cid = self.input_ids['trigger:' + assignment['name']][1]
                    if 'DO_Value' not in self.adapter.list_properties(cid):
                        raise ScpiProtocolError(f'{assignment["trigger"]}: Boolean DO_Value is unavailable')
                    self.read_button(assignment['trigger'])
            self.transport.numeric_name = self.config.raw['measurement_roles']['dc_bus_voltage']['channel']
            for name in self.config.raw['measurement_roles']:
                self.measure(name)
            self.error()
            self.transport.bind_outputs(reports, self.config.raw['outputs'])
            self.emit({'event': 'oxygen_hardware_connected', 'simulated': False,
                       'physical_hardware_authorized': True, 'output_enable_controlled': False,
                       'endpoint': f'{self.transport.host}:{self.transport.port}',
                       'outputs': reports, 'inputs': self.input_ids})

    def close(self):
        with self.lock:
            self.authorized.clear()
            self.transport.close()

    def discover(self):
        with self.lock:
            if self.failed:
                raise ScpiProtocolError('Hardware session invalidated; close and restart')
            try:
                reports = self.discovery.discover()
                self._pin_inputs(self.adapter.list_channels())
                self.error()
                return {self.config.raw['outputs'][role]['binding']: {
                    'name': r['channel'], 'id': r['channel_id'], 'mode': r['mode'], 'source': r['source'],
                    'property': r['const_property'], 'unit': r['unit'],
                    'min_v': r['range'][0], 'max_v': r['range'][1]} for role, r in reports.items()}
            except BaseException:
                self.failed = True
                self.authorized.clear()
                self.transport.capabilities.clear()
                raise

    def constant_property(self, role):
        return self.discovery.pinned[role]['const_property']

    def expected_source(self, role):
        return self.discovery.pinned[role]['source']

    def channel_id(self, role):
        return self.channel_ids[role]

    def authorize(self, role, binding, spec):
        with self.lock:
            if spec != self.config.raw['outputs'].get(role) or binding != spec.get('binding'):
                raise PermissionError('Physical authorization does not match configured output')
            self.discover()
            self.transport.authorize(role)
            self.authorized.add(role)

    def read_output(self, binding):
        with self.lock:
            roles = [role for role, spec in self.config.raw['outputs'].items() if spec['binding'] == binding]
            if len(roles) != 1:
                raise ScpiProtocolError('Unknown physical output binding')
            role = roles[0]
            value = self.adapter.get_property_value(self.channel_ids[role], self.constant_property(role))
            if value.kind != 'SCALAR' or len(value.values) != 2 or value.values[1] != 'V':
                raise ScpiProtocolError('Physical constant readback must be SCALAR in V')
            self.error()
            return finite_number(value.values[0], 'physical constant readback')

    def write(self, role, value):
        with self.lock:
            if role not in self.authorized:
                raise PermissionError('Physical role not authorized by completed startup preflight')
            self.discover()
            spec = self.config.raw['outputs'][role]
            value = number(value, 'physical demand', spec['min_aout_v'], spec['max_aout_v'])
            if not (role == 'vesc_throttle' and value == spec['neutral_aout_v']):
                ready = self.measure('supply_external_ready')
                if ready.value is not True or not 0 <= self.clock() - ready.sampled_at <= self.config.raw['interlocks']['max_feedback_age_s']:
                    raise PermissionError('Physical write refused: fresh real supply_external_ready is required')
            self.writes_attempted += 1
            self.transport.command(f':CHANNELlist:PROPerty {quote_scpi(self.channel_id(role))},'
                                   f'{quote_scpi(self.constant_property(role))},SCALAR,{value:.9g},"V"')

    def _boolean(self, cid, prop):
        value = self.adapter.get_property_value(cid, prop)
        if value.kind not in {'BOOL', 'BOOLEAN'} or len(value.values) != 1 or type(value.values[0]) is not bool:
            raise ScpiProtocolError(f'{cid}/{prop}: expected an actual Boolean hardware value')
        return value.values[0]

    def read_button(self, name):
        with self.lock:
            self._pin_inputs(self.adapter.list_channels())
            matches = [cid for key, (channel, cid) in self.input_ids.items() if key.startswith('trigger:') and channel == name]
            if len(matches) != 1:
                raise ScpiProtocolError('Unknown or ambiguous hardware trigger')
            result = self._boolean(matches[0], 'DO_Value')
            self.error()
            return result

    def measure(self, name):
        with self.lock:
            started = self.clock()
            self._pin_inputs(self.adapter.list_channels())
            spec = self.config.raw['measurement_roles'][name]
            cid = self.input_ids['sensor:' + name][1]
            if name == 'dc_bus_voltage':
                unit = self.adapter.get_property_value(cid, 'Unit')
                if unit.kind != 'STRING' or unit.values != ('V',) or self.adapter.acquisition_state() != 'Started':
                    raise ScpiProtocolError('Real bus channel must be in V with acquisition Started')
                value = self.adapter.read_measurements([spec['channel']])[0]
                if not math.isfinite(value):
                    raise ScpiProtocolError('Nonfinite measured hardware bus voltage')
            else:
                value = self._boolean(cid, spec['property'])
            self.error()
            return HardwareSample(value, started, f'OXYGEN {cid}/{spec.get("property") or "numeric"}')
