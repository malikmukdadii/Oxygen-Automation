"""Unified OXYGEN ConstOutput backend for simulated and physical channels."""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass

from .adapters.oxygen import OxygenAdapter
from .aout_discovery import AoutDiscovery
from .constant_pattern import finite_number
from .errors import AuthorizationError, ControlError, InterlockError
from .schema import ABSOLUTE_AOUT_MAX_V, number
from .scpi_transport import ScpiCommandError, ScpiProtocolError, ScpiTransport, parse_scpi, quote_scpi


@dataclass(frozen=True)
class Sample:
    value: float | bool
    sampled_at: float
    source: str
    simulated: bool = False


class OxygenTransport(ScpiTransport):
    """One SCPI path: authorized constant writes + DO reads; optional numeric bus for closed-loop."""

    def __init__(self, *, host, port, timeout_s, emit, physical: bool):
        super().__init__(host=host, port=port, timeout_s=timeout_s)
        self.emit = emit
        self.physical = physical
        self.read_ids = set()
        self.do_ids = set()
        self.bindings = {}
        self.capabilities = {}
        self.numeric_name = None
        self.experiment_numeric_names = ()
        self.invalid = False
        # When True, query/write failures from optional telemetry must not kill AOUT bindings.
        self.soft_telemetry = False
        self.last_command_id = 0
        self._command_context = None

    def close(self):
        self.capabilities.clear()
        self.bindings.clear()
        self.read_ids.clear()
        self.do_ids.clear()
        self.numeric_name = None
        self.experiment_numeric_names = ()
        self.soft_telemetry = False
        super().close()

    def reconnect(self):
        raise ScpiProtocolError('Dispatcher never reconnects or replays output commands')

    def query(self, command):
        if self.invalid:
            raise ScpiProtocolError('Transport invalidated; restart explicitly')
        allowed = command in {'*IDN?', ':CHANNELlist:NAMes?', 'SYST:ERR?'}
        if self.physical:
            allowed = allowed or command == ':ACQUisition:STATe?'
        allowed = allowed or any(command == f':CHANNELlist:ITEM{cid}:ATTR:NAMes?' for cid in self.read_ids)
        allowed = allowed or (
            (self.numeric_name is not None or self.experiment_numeric_names) and command in {':NUMeric:NORMal:DIMS?', ':NUMeric:NORMal:VALue?'})
        prefix = ':CHANNELlist:PROPerty? '
        if command.startswith(prefix):
            args = parse_scpi(command[len(prefix):])
            allowed = (len(args) == 2 and all(isinstance(a, str) for a in args) and args[0] in self.read_ids and
                       command == prefix + ','.join(quote_scpi(a) for a in args))
        if not allowed:
            raise ScpiProtocolError(f'Query refused: {command!r}')
        started = time.monotonic()
        record = {'event': 'query', 'backend': 'oxygen_scpi', 'command': command, 'physical': self.physical}
        if self._command_context is not None:
            if command == 'SYST:ERR?':
                self._command_context['command_phase'] = 'error_queue_query'
            record.update(self._command_context)
        try:
            reply = super().query(command)
            if command == ':CHANNELlist:NAMes?':
                entries = parse_scpi(reply) if reply.upper() != 'NONE' else []
                self.read_ids = {entry[0] for entry in entries if isinstance(entry, tuple) and len(entry) == 2}
            record['raw_payload'] = reply
            return reply
        except BaseException as exc:
            record.update(error_type=type(exc).__name__, error=str(exc),
                          raw_payload=getattr(exc, 'raw_response', None))
            if self.physical and not self.soft_telemetry:
                self.invalid = True
                self.capabilities.clear()
            raise
        finally:
            record['latency_ms'] = (time.monotonic() - started) * 1000
            self.emit(record)

    def command(self, command, query_error=True):
        with self._lock:
            self.last_command_id += 1
            previous = self._command_context
            context = {'scpi_command_id': self.last_command_id, 'associated_command': command,
                       'command_phase': 'dispatch', 'socket_send_completed': False,
                       'queue_association': 'next queue entry after serialized write; device causality unproven'}
            self._command_context = context
            started = time.monotonic()
            try:
                self.emit({'event': 'scpi_command_started', **context, 'physical': self.physical})
                result = super().command(command, query_error=query_error)
                self.emit({'event': 'scpi_command_completed', **context,
                           'elapsed_ms': (time.monotonic() - started) * 1000,
                           'query_error': query_error, 'output_readback_confirmed': False})
                return result
            except BaseException as exc:
                record = {'event': 'scpi_command_failed', **context,
                          'elapsed_ms': (time.monotonic() - started) * 1000,
                          'error_type': type(exc).__name__, 'error': str(exc),
                          'device_write_outcome': 'unknown', 'retry_attempted': False}
                if isinstance(exc, ScpiCommandError):
                    record.update(error_code=exc.entry.code, error_message=exc.entry.message,
                                  raw_error_reply=exc.entry.raw_response)
                self.emit(record)
                raise
            finally:
                self._command_context = previous

    def bind_outputs(self, reports, outputs):
        if not self.connected or self.bindings or self.capabilities or set(reports) != set(outputs):
            raise ScpiProtocolError('Bindings are established once per session for active roles only')
        bindings = {}
        for role, report in reports.items():
            spec = outputs[role]
            cid, prop = report['channel_id'], report['const_property']
            OxygenAdapter._channel_id(cid)
            permission = spec['authorization']
            if report['mismatches'] or report['channel'] != spec['channel'] or cid not in self.read_ids:
                raise ScpiProtocolError('Discovered output is not authorized')
            if not re.fullmatch(r'TRION/SourceChannel_A_CONST/Const[0-9]+', prop):
                raise ScpiProtocolError('Constant property must be a TRION ConstN source')
            if self.physical:
                if permission.get('simulation') is not False or permission.get('physical') is not True:
                    raise ScpiProtocolError('Physical binding requires physical authorization')
            else:
                if permission.get('simulation') is not True or permission.get('physical') is not False:
                    raise ScpiProtocolError('Simulated binding requires simulation authorization')
            # Operator-configured clamp is authoritative; discovery already verified HW coverage.
            bindings[role] = (cid, prop, spec['min_aout_v'], spec['max_aout_v'])
        if len({v[0] for v in bindings.values()}) != len(bindings):
            raise ScpiProtocolError('Output bindings collide')
        self.bindings = bindings

    def authorize(self, role):
        if self.invalid or not self.connected or role not in self.bindings:
            raise ScpiProtocolError('Output has no valid session binding')
        cid, prop, low, high = self.bindings[role]
        if low != 0 or not 0 < high <= ABSOLUTE_AOUT_MAX_V:
            raise ScpiProtocolError('Configured clamp is outside the absolute ConstOutput span')
        self.capabilities[(cid, prop)] = (low, high)

    def write(self, command):
        if self.invalid:
            raise ScpiProtocolError('Transport invalidated; restart explicitly')
        numeric = set()
        if self.numeric_name is not None:
            numeric = {
                ':NUMeric:NORMal:ITEMS ' + quote_scpi(self.numeric_name),
                ':NUMeric:NORMal:FORMat ASCII',
                ':NUMeric:NORMal:NUMber ALL',
            }
        if self.experiment_numeric_names:
            numeric |= {':NUMeric:NORMal:ITEMS ' + ','.join(quote_scpi(n) for n in self.experiment_numeric_names),
                        ':NUMeric:NORMal:FORMat ASCII', ':NUMeric:NORMal:NUMber ALL'}
        if command not in numeric:
            prefix = ':CHANNELlist:PROPerty '
            if not command.startswith(prefix):
                raise ScpiProtocolError('Only authorized constant demands are permitted')
            args = parse_scpi(command[len(prefix):])
            if len(args) != 5 or not all(isinstance(a, str) for a in args) or args[2] != 'SCALAR' or args[4] != 'V':
                raise ScpiProtocolError('Output requires canonical SCALAR demand in V')
            bounds = self.capabilities.get((args[0], args[1]))
            value = finite_number(args[3], 'demand')
            canonical = prefix + f'{quote_scpi(args[0])},{quote_scpi(args[1])},SCALAR,{value:.9g},"V"'
            if bounds is None or not bounds[0] <= value <= bounds[1] or command != canonical:
                raise ScpiProtocolError('Output is unauthorized, noncanonical or out of bounds')
        try:
            if self._command_context is not None:
                self._command_context['command_phase'] = 'socket_send'
            super().write(command)
            if self._command_context is not None:
                self._command_context['socket_send_completed'] = True
        except BaseException:
            if self.physical and not self.soft_telemetry:
                self.invalid = True
                self.capabilities.clear()
            raise


class OxygenBackend:
    def __init__(self, config, emit, *, confirmed_outputs, live=False, clock=time.monotonic):
        if config.transport != 'oxygen_scpi':
            raise AuthorizationError('OxygenBackend requires transport oxygen_scpi')
        from .schema import confirm_outputs
        confirm_outputs(config, confirmed_outputs, live=live)
        self.config, self.emit, self.clock = config, emit, clock
        self.physical = config.physical
        self.open_loop = config.open_loop
        connection = config.raw['connection']
        self.transport = OxygenTransport(
            host=connection['host'], port=connection['port'], timeout_s=connection['timeout_s'],
            emit=emit, physical=self.physical)
        self.adapter = OxygenAdapter(self.transport)
        self.discovery = AoutDiscovery(self.adapter, config.raw['outputs'])
        self.lock = threading.RLock()
        self.input_ids = {}
        self.channel_ids = {}
        self.authorized = set()
        self.failed = False
        self.writes_attempted = 0
        self.do_channels = {}
        self._session = None
        self.full_discover_count = 0

    @property
    def connected(self):
        return self.transport.connected

    def error(self):
        entry = self.transport.check_error()
        if entry.code:
            raise ScpiProtocolError(f'OXYGEN SCPI error: {entry}')

    def _pin_inputs(self, channels):
        output_ids = set(self.channel_ids.values())
        sensors = {
            name: m['channel'] for name, m in self.config.raw.get('measurements', {}).items()
            if isinstance(m, dict) and m.get('channel')
        }
        triggers = {a['name']: a['trigger'] for a in self.config.raw['assignments'] if a.get('enabled', True)}
        for key, name in [('sensor:' + k, v) for k, v in sensors.items()] + [
                ('trigger:' + k, v) for k, v in triggers.items()]:
            matches = [c for c in channels if c.name == name]
            if len(matches) != 1 or matches[0].channel_id in output_ids:
                if key.startswith('sensor:') and self.open_loop:
                    self.emit({
                        'event': 'telemetry_unavailable', 'channel': name, 'key': key,
                        'warning': True, 'logging_only': True,
                        'message': 'Optional measurement missing; open-loop AOUT continues',
                    })
                    continue
                raise ScpiProtocolError(f'{name}: independent input must have exactly one non-AOUT identity')
            cid = matches[0].channel_id
            if key in self.input_ids and self.input_ids[key] != (name, cid):
                raise ScpiProtocolError(f'{name}: input mapping changed; restart explicitly')
            self.input_ids[key] = (name, cid)
        if not self.open_loop and 'sensor:dc_bus_voltage' in self.input_ids:
            bus_id = self.input_ids['sensor:dc_bus_voltage'][1]
            if any(cid == bus_id for key, (_, cid) in self.input_ids.items() if key != 'sensor:dc_bus_voltage'):
                raise ScpiProtocolError('Measured bus channel cannot alias a trigger or Boolean interlock')

    def connect(self):
        with self.lock:
            self.transport.connect()
            if self.adapter.identify().split(',')[:2] != ['DEWETRON', 'OXYGEN']:
                raise ScpiProtocolError('Expected DEWETRON OXYGEN')
            reports = self.discovery.discover()
            self.full_discover_count += 1
            self.channel_ids = {role: r['channel_id'] for role, r in reports.items()}
            self._session = {
                self.config.raw['outputs'][role]['binding']: {
                    'name': r['channel'], 'id': r['channel_id'], 'mode': r['mode'],
                    'source': r['source'], 'property': r['const_property'], 'unit': r['unit'],
                    'min_v': r['range'][0], 'max_v': r['range'][1],
                } for role, r in reports.items()
            }
            channels = self.adapter.list_channels()
            self._pin_inputs(channels)
            measurements = self.config.raw.get('measurements', {})
            if measurements and (not self.open_loop or any(
                    isinstance(m, dict) and m.get('channel') for m in measurements.values())):
                for name, spec in measurements.items():
                    if not spec.get('channel'):
                        continue
                    key = 'sensor:' + name
                    if key not in self.input_ids:
                        continue
                    cid = self.input_ids[key][1]
                    prop = 'Unit' if name == 'dc_bus_voltage' else spec.get('property')
                    if prop and prop not in self.adapter.list_properties(cid):
                        if self.open_loop:
                            self.emit({
                                'event': 'telemetry_unavailable', 'name': name, 'property': prop,
                                'warning': True, 'logging_only': True,
                                'message': 'Optional measurement property unavailable; AOUT continues',
                            })
                            self.input_ids.pop(key, None)
                            continue
                        raise ScpiProtocolError(f'{name}: configured input property {prop!r} is unavailable')
                # Enable numeric reads for optional open-loop logging and closed-loop control.
                if 'dc_bus_voltage' in measurements and measurements['dc_bus_voltage'].get('channel'):
                    if 'sensor:dc_bus_voltage' in self.input_ids:
                        self.transport.numeric_name = measurements['dc_bus_voltage']['channel']
            for assignment in self.config.raw['assignments']:
                if assignment.get('enabled', True):
                    name = assignment['trigger']
                    cid = self.input_ids['trigger:' + assignment['name']][1]
                    if 'DO_Value' not in self.adapter.list_properties(cid):
                        raise ScpiProtocolError(f'{name}: Boolean DO_Value is unavailable')
                    self.do_channels[name] = cid
                    self.transport.do_ids.add(cid)
                    self.read_button(name)
            if not self.open_loop:
                for name in measurements:
                    self.measure(name)
            self.error()
            self.transport.bind_outputs(reports, self.config.raw['outputs'])
            self.emit({
                'event': 'oxygen_connected',
                'simulated': not self.physical,
                'physical': self.physical,
                'control_mode': self.config.mode,
                'live_feedback_enabled': not self.open_loop,
                'physical_hardware_authorized': self.physical,
                'output_enable_controlled': False,
                'active_roles': sorted(self.config.raw['outputs']),
                'endpoint': f'{self.transport.host}:{self.transport.port}',
                'outputs': reports,
                'inputs': dict(self.input_ids),
                'full_discover_count': self.full_discover_count,
            })

    def close(self):
        with self.lock:
            self.authorized.clear()
            self._session = None
            self.transport.close()

    def discover(self):
        """Return session-pinned discovery. Does not re-enumerate OXYGEN during ramps."""
        with self.lock:
            if self.failed:
                raise ScpiProtocolError('Session invalidated; close and restart')
            if self._session is None:
                raise ScpiProtocolError('Discovery is pinned at connect; session is not ready')
            return {key: dict(value) for key, value in self._session.items()}

    def constant_property(self, role):
        return self.discovery.pinned[role]['const_property']

    def expected_source(self, role):
        return self.discovery.pinned[role]['source']

    def channel_id(self, role):
        return self.channel_ids[role]

    def authorize(self, role, binding, spec):
        with self.lock:
            if spec != self.config.raw['outputs'].get(role) or binding != spec.get('binding'):
                raise AuthorizationError('Authorization does not match configured output')
            if self._session is None:
                raise AuthorizationError('Cannot authorize before session discovery is pinned')
            self.transport.authorize(role)
            self.authorized.add(role)

    def read_output(self, binding):
        with self.lock:
            roles = [role for role, spec in self.config.raw['outputs'].items() if spec['binding'] == binding]
            if len(roles) != 1:
                raise ScpiProtocolError('Unknown output binding')
            role = roles[0]
            cid, prop = self.channel_ids[role], self.constant_property(role)
            raw, value = None, None
            stage = 'property_query'
            try:
                raw = self.adapter.get_property_reply(cid, prop)
                stage = 'property_parse'
                value = self.adapter.parse_property_value(raw)
                stage = 'type_and_unit_validation'
                if value.kind != 'SCALAR' or len(value.values) != 2 or value.values[1] != 'V':
                    raise ScpiProtocolError(f'Constant readback must be SCALAR in V: raw={raw!r}, kind={value.kind}')
                stage = 'error_queue_check'
                self.error()
                stage = 'finite_value_validation'
                return finite_number(value.values[0], 'constant readback')
            except Exception as exc:
                self.emit({'event': 'output_readback_failed', 'output': role, 'stage': stage,
                           'channel': self.config.raw['outputs'][role]['channel'],
                           'channel_id': cid, 'property': prop,
                           'query_command': f':CHANNELlist:PROPerty? {quote_scpi(cid)},{quote_scpi(prop)}',
                           'raw_readback': raw if raw is not None else getattr(exc, 'raw_response', None),
                           'parsed_kind': value.kind if value is not None else None,
                           'parsed_values_repr': repr(value.values) if value is not None else None,
                           'error_type': type(exc).__name__, 'error': str(exc),
                           'last_scpi_command_id': self.transport.last_command_id,
                           'physical_voltage_established': False})
                raise

    def write(self, role, value):
        with self.lock:
            if role not in self.authorized:
                raise AuthorizationError('Output role not authorized by completed startup preflight')
            if self._session is None:
                raise AuthorizationError('Write refused: discovery session is not pinned')
            spec = self.config.raw['outputs'][role]
            value = number(value, 'demand', spec['min_aout_v'], spec['max_aout_v'])
            if (self.physical and not self.open_loop and
                    not (role == 'vesc_throttle' and value == spec['neutral_aout_v'])):
                ready = self.measure('supply_external_ready')
                age_limit = self.config.raw['interlocks']['max_feedback_age_s']
                if ready.value is not True or not 0 <= self.clock() - ready.sampled_at <= age_limit:
                    raise AuthorizationError(
                        'Physical write refused: fresh real supply_external_ready is required')
            self.writes_attempted += 1
            command = (f':CHANNELlist:PROPerty {quote_scpi(self.channel_id(role))},'
                       f'{quote_scpi(self.constant_property(role))},SCALAR,{value:.9g},"V"')
            try:
                self.transport.command(command)
            except Exception as exc:
                self.emit({'event': 'aout_write_failed', 'output': role, 'channel': spec['channel'],
                           'channel_id': self.channel_id(role), 'property': self.constant_property(role),
                           'command': command, 'requested_v': value, 'wire_requested_v': float(f'{value:.9g}'),
                           'scpi_command_id': self.transport.last_command_id,
                           'error_type': type(exc).__name__, 'error': str(exc),
                           'device_write_outcome': 'unknown', 'retry_attempted': False})
                raise

    def _boolean(self, cid, prop):
        value = self.adapter.get_property_value(cid, prop)
        if value.kind not in {'BOOL', 'BOOLEAN'} or len(value.values) != 1 or type(value.values[0]) is not bool:
            raise ScpiProtocolError(f'{cid}/{prop}: expected an actual Boolean value')
        return value.values[0]

    def read_button(self, name):
        with self.lock:
            if name in self.do_channels:
                cid = self.do_channels[name]
            else:
                self._pin_inputs(self.adapter.list_channels())
                matches = [cid for key, (channel, cid) in self.input_ids.items()
                           if key.startswith('trigger:') and channel == name]
                if len(matches) != 1:
                    raise ScpiProtocolError('Unknown or ambiguous trigger')
                cid = matches[0]
            result = self._boolean(cid, 'DO_Value')
            self.error()
            return result

    def measure(self, name):
        with self.lock:
            measurements = self.config.raw.get('measurements', {})
            if name not in measurements:
                raise InterlockError(f'cannot satisfy measurement request: {name}')
            if self.open_loop and not measurements[name].get('channel'):
                raise InterlockError(f'cannot satisfy measurement request: {name}')
            started = self.clock()
            # Use pinned sensor IDs; do not re-list the full channel tree.
            if 'sensor:' + name not in self.input_ids:
                raise InterlockError(f'cannot satisfy measurement request: {name}')
            spec = measurements[name]
            cid = self.input_ids['sensor:' + name][1]
            # Soft telemetry: faults must not invalidate AOUT bindings on a healthy session.
            if self.open_loop:
                self.transport.soft_telemetry = True
            try:
                if name == 'dc_bus_voltage':
                    unit = self.adapter.get_property_value(cid, 'Unit')
                    if unit.kind != 'STRING' or unit.values != ('V',):
                        raise InterlockError('Bus channel unit is not V')
                    if not self.open_loop:
                        if self.adapter.acquisition_state() != 'Started':
                            raise ScpiProtocolError('Real bus channel must be in V with acquisition Started')
                    if self.transport.numeric_name is None:
                        self.transport.numeric_name = spec['channel']
                    value = self.adapter.read_measurements([spec['channel']])[0]
                    if not math.isfinite(value):
                        raise InterlockError('Nonfinite measured bus voltage')
                else:
                    value = self._boolean(cid, spec['property'])
                self.error()
                return Sample(value, started, f'OXYGEN {cid}/{spec.get("property") or "numeric"}',
                              simulated=not self.physical)
            except InterlockError:
                raise
            except Exception as exc:
                if self.open_loop:
                    raise InterlockError(f'optional measurement failed: {exc}') from exc
                raise
            finally:
                if self.open_loop:
                    self.transport.soft_telemetry = False
