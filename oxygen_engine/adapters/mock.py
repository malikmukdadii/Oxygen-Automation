"""Mock OXYGEN SCPI server and mock RZ-XA adapter for simulation."""

from __future__ import annotations

import logging
import math
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..scpi_transport import parse_scpi, quote_scpi

logger = logging.getLogger(__name__)


KNOWN_AOUT_PROPS = [
    "Mode",
    "Range",
    "TRION/SourceChannel_A_CONST/Const0",
    "TRION/FGenProperties/AoWaveformLookup0",
    "TRION/FGenProperties/AoWaveformLookup1",
    "TRION/FGenProperties/AoWaveformLookup2",
    "TRION/FGenProperties/AoWaveformLookup3",
]

DO_PROPS = ["Value", "State", "Name", "Type"]


@dataclass
class MockChannel:
    channel_id: str
    name: str
    channel_type: str  # aout | digital_output | measurement
    properties: dict[str, str] = field(default_factory=dict)
    measurement: float = 0.0


class MockOxygenServer:
    """
    Fake OXYGEN SCPI backend (TCP).

    Emulates documented OXYGEN channel, store, marker and numeric syntax.
    MOCK:* commands are fault-injection helpers only. Seeded AOUT and DO
    property names are simulation fixtures, NOT verified hardware mappings.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, response_headers: bool = False) -> None:
        self.host = host
        self.port = port
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []
        self._lock = threading.RLock()
        self._error_queue: list[tuple[int, str]] = []
        self._recording = False
        self.recording_state = "Stopped"
        self.acquisition_state = "Started"
        self.response_headers = response_headers
        self._numeric_items: list[str] = []
        self._relative_time_origin = time.monotonic()
        self._markers: list[str] = []
        self._force_timeout = False
        self._force_bad_response = False
        self._channels: dict[str, MockChannel] = {}
        self._by_name: dict[str, str] = {}
        self._on_command: Callable[[str], None] | None = None
        self._seed_default_channels()

    @property
    def bound_port(self) -> int:
        return self.port

    def _seed_default_channels(self) -> None:
        aouts = [
            ("4152600322822373457", "Test Aout1"),
            ("4152600322822373458", "Test Aout2"),
        ]
        for index, (cid, name) in enumerate(aouts):
            props = {
                "Mode": "ConstOutput",
                "Range": "10",
                f"TRION/SourceChannel_A_CONST/Const{index}": "0.0",
                "TRION/FGenProperties/AoWaveformLookup0": "",
                "TRION/FGenProperties/AoWaveformLookup1": "",
                "TRION/FGenProperties/AoWaveformLookup2": "",
                "TRION/FGenProperties/AoWaveformLookup3": "",
                "Name": name,
                "Type": "aout",
            }
            ch = MockChannel(cid, name, "aout", props)
            self._channels[cid] = ch
            self._by_name[name] = cid

        for i in range(1, 7):
            name = f"DO 3/{i} Sim"
            cid = f"90000000000000000{i}"
            props = {
                "Value": "0",
                "State": "0",
                "DO_Value": "0",
                "Mode": "DIO",
                "Direction": "output",
                "Name": name,
                "Type": "digital_output",
            }
            ch = MockChannel(cid, name, "digital_output", props)
            self._channels[cid] = ch
            self._by_name[name] = cid

        for cid, name, value in [
            ("800000000000000001", "DC Power", 120.0),
            ("800000000000000002", "AC Power", 95.0),
            ("800000000000000003", "RPM", 1500.0),
            ("800000000000000004", "Energy", 12.5),
        ]:
            props = {"Name": name, "Type": "measurement", "Unit": "mixed"}
            ch = MockChannel(cid, name, "measurement", props, measurement=value)
            self._channels[cid] = ch
            self._by_name[name] = cid

    def set_do(self, name: str, high: bool) -> None:
        with self._lock:
            cid = self._by_name.get(name)
            if cid is None:
                raise KeyError(f"Unknown DO channel: {name}")
            val = "1" if high else "0"
            self._channels[cid].properties["Value"] = val
            self._channels[cid].properties["State"] = val
            self._channels[cid].properties["DO_Value"] = val
            self._channels[cid].measurement = float(high)

    def get_do(self, name: str) -> bool:
        with self._lock:
            cid = self._by_name[name]
            return self._channels[cid].properties.get("Value", "0") in {"1", "High", "true", "True"}

    def set_measurement(self, name: str, value: float) -> None:
        with self._lock:
            cid = self._by_name[name]
            self._channels[cid].measurement = value

    def get_aout_const(self, name: str) -> float:
        with self._lock:
            cid = self._by_name[name]
            props = self._channels[cid].properties
            key = next(p for p in props if p.startswith('TRION/SourceChannel_A_CONST/Const'))
            return float(props[key])

    def start(self) -> int:
        if self._server is not None:
            return self.port
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        self.port = server.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, name="mock-oxygen", daemon=True)
        self._thread.start()
        logger.info("Mock OXYGEN SCPI server listening on %s:%s", self.host, self.port)
        return self.port

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for client in list(self._clients):
                try:
                    client.close()
                except OSError:
                    pass
            self._clients.clear()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def disconnect_clients(self) -> None:
        with self._lock:
            for client in list(self._clients):
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    client.close()
                except OSError:
                    pass
            self._clients.clear()

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            logger.debug("Mock client connected from %s", addr)
            with self._lock:
                self._clients.append(client)
            t = threading.Thread(
                target=self._client_loop, args=(client,), name=f"mock-client-{addr}", daemon=True
            )
            t.start()

    def _client_loop(self, client: socket.socket) -> None:
        client.settimeout(1.0)
        buf = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    idx = buf.find(b"\n")
                    if idx < 0:
                        break
                    line = bytes(buf[:idx]).decode("utf-8", errors="replace").strip()
                    del buf[: idx + 1]
                    if not line:
                        continue
                    if self._on_command:
                        self._on_command(line)
                    try:
                        response = self._handle(line, client)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Mock handler error")
                        self._push_error(-100, str(exc))
                        response = '-100,"Command error"' if '?' in line.split(None, 1)[0] else None
                    if response is not None:
                        try:
                            client.sendall((response + "\n").encode("utf-8"))
                        except OSError:
                            return
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            try:
                client.close()
            except OSError:
                pass

    def _push_error(self, code: int, message: str) -> None:
        with self._lock:
            self._error_queue.append((code, message))

    def _handle(self, line: str, client: socket.socket) -> str | None:
        command, _, params = line.strip().partition(' ')
        upper = command.lstrip(':').upper()
        aliases = {'CHANNELLIST': 'CHANNEL', 'NAMES': 'NAME', 'NAM': 'NAME',
                   'IDS': 'ID', 'PROPERTY': 'PROP', 'STORE': 'STOR',
                   'STATE': 'STAT', 'START': 'STAR', 'MARKER': 'MARK',
                   'SYSTEM': 'SYST', 'ERROR': 'ERR', 'NUMERIC': 'NUM',
                   'NORMAL': 'NORM', 'VALUE': 'VAL', 'NUMBER': 'NUMB',
                   'FORMAT': 'FORM', 'ACQUISITION': 'ACQU',
                   'COMMUNICATE': 'COMM', 'HEADER': 'HEAD'}
        is_query = upper.endswith('?')
        header = ':'.join(aliases.get(p, p) for p in upper.rstrip('?').split(':'))
        upper = header + ('?' if is_query else '')

        if self._force_timeout and is_query:
            self._force_timeout = False
            self._stop.wait(10)
            return None
        if self._force_bad_response and is_query:
            self._force_bad_response = False
            return "!!!NOT_A_VALID_SCPI_RESPONSE!!!"

        def reply(value: str) -> str:
            response_header = 'NUM:DIMS' if header == 'NUM:NORM:DIMS' else header
            return f':{response_header} {value}' if self.response_headers and not header.startswith('*') else value

        if upper == '*IDN?':
            return 'DEWETRON,OXYGEN,SIM001,7.0.0'
        if upper == 'COMM:HEAD':
            self.response_headers = params.upper() in {'ON', '1'}
            return None
        if upper == 'COMM:HEAD?':
            return reply('1' if self.response_headers else '0')
        if upper == 'SYST:ERR?':
            with self._lock:
                if self._error_queue:
                    code, msg = self._error_queue.pop(0)
                    return reply(f'{code},{quote_scpi(msg)}')
            return reply('0,"No error"')
        if upper == 'CHANNEL:NAME?':
            with self._lock:
                return reply(','.join(f'({quote_scpi(c.channel_id)},{quote_scpi(c.name)})'
                                      for c in self._channels.values()) or 'NONE')
        if upper == 'CHANNEL:ID?':
            names = parse_scpi(params) if params else list(self._by_name)
            with self._lock:
                ids = [self._by_name[n] for n in names if n in self._by_name]
            return reply(','.join(quote_scpi(cid) for cid in ids) or 'NONE')
        match = re.fullmatch(r'CHANNEL:ITEM([0-9]+):ATTR:NAME\?', upper)
        if match:
            with self._lock:
                ch = self._channels.get(match[1])
            if ch is None:
                self._push_error(-200, 'Unknown channel ID')
                return reply('NONE')
            return reply(','.join(quote_scpi(p) for p in sorted(ch.properties)))
        if upper == 'CHANNEL:PROP?':
            cid, prop = parse_scpi(params)
            with self._lock:
                ch = self._channels.get(cid)
                if ch is None or prop not in ch.properties:
                    self._push_error(-200, 'Unknown channel or property')
                    return reply('NONE')
                value = ch.properties[prop]
            if prop in {'Value', 'State'}:
                return reply('ON' if value == '1' else 'OFF')
            if prop == 'DO_Value':
                return reply('(BOOL,ON)' if value == '1' else '(BOOL,OFF)')
            if prop == 'Range':
                return reply('(RANGE,-10.0,"V",10.0,"V")')
            if prop.startswith('TRION/SourceChannel_A_CONST/Const'):
                return reply(f'(SCALAR,{value},"V")')
            return reply(quote_scpi(value))
        if upper == 'CHANNEL:PROP':
            args = parse_scpi(params)
            if len(args) != 5 or not all(isinstance(a, str) for a in args):
                self._push_error(-104, 'Expected channel,property,SCALAR,value,unit')
                return None
            cid, prop, kind, value, unit = args
            with self._lock:
                ch = self._channels.get(cid)
                if (ch is None or ch.channel_type != 'aout' or prop not in ch.properties
                        or not prop.startswith('TRION/SourceChannel_A_CONST/Const')
                        or kind.upper() != 'SCALAR' or unit != 'V'):
                    self._push_error(-200, 'Unsupported simulated property write')
                    return None
                try:
                    number = float(value)
                except ValueError:
                    number = math.nan
                if not math.isfinite(number) or not -10 <= number <= 10:
                    self._push_error(-222, 'Data out of range')
                    return None
                ch.properties[prop] = str(number)
                ch.measurement = number
            return None
        if upper == 'NUM:NORM:ITEMS':
            names = parse_scpi(params)
            if not names or any(n not in self._by_name and n != 'REL-TIME' for n in names):
                self._push_error(-200, 'Unknown numeric channel name')
            else:
                self._numeric_items = names
            return None
        if upper == 'NUM:NORM:ITEMS?':
            return reply(','.join(quote_scpi(n) for n in self._numeric_items) or '0')
        if upper in {'NUM:NORM:FORM', 'NUM:NORM:NUMB'}:
            if params.upper() != ('ASCII' if upper.endswith('FORM') else 'ALL'):
                self._push_error(-104, 'Unsupported parameter')
            return None
        if upper == 'NUM:NORM:DIMS?':
            return reply(','.join('1' for _ in self._numeric_items))
        if upper == 'NUM:NORM:VAL?':
            names = self._numeric_items
            if params:
                names = [names[int(params) - 1]]
            values = [time.monotonic() - self._relative_time_origin if n == 'REL-TIME'
                      else self._channels[self._by_name[n]].measurement for n in names]
            return reply(','.join(f'{value:.12E}' for value in values))
        if upper == 'ACQU:STAT?':
            return reply(self.acquisition_state)
        if upper == 'STOR:STAT?':
            return reply(self.recording_state)
        if upper == 'STOR:STAR':
            self._recording = True
            self.recording_state = 'Started'
            return None
        if upper == 'STOR:STOP':
            self._recording = False
            self.recording_state = 'Stopped'
            return None
        if upper == 'MARK:ADD':
            self._markers.append(parse_scpi(params)[0])
            return None

        if upper == "MOCK:DISCONNECT":
            # Close this client after acknowledging via peer shutdown
            threading.Thread(target=self._delayed_close, args=(client,), daemon=True).start()
            return None

        if upper == "MOCK:TIMEOUT":
            self._force_timeout = True
            return None

        if upper == "MOCK:BADRESP":
            self._force_bad_response = True
            return None

        if upper.startswith("MOCK:DO"):
            args = params
            parts = [p.strip() for p in args.split(",", 1)]
            if len(parts) != 2:
                self._push_error(-300, "MOCK:DO requires name,0|1")
                return None
            name, val = parts
            self.set_do(name.strip('"'), val.strip() in {"1", "High", "true", "True"})
            return None

        self._push_error(-113, f"Undefined header: {command}")
        if is_query:
            return reply('-113,"Undefined header"')
        return None

    def _delayed_close(self, client: socket.socket) -> None:
        time.sleep(0.05)
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass


class MockRZxaAdapter:
    """Functioning mock RZ-XA — no real SCPI sent."""

    def __init__(
        self,
        voltage_min: float = 0.0,
        voltage_max: float = 80.0,
        current_limit_default: float = 10.0,
    ) -> None:
        self.voltage_min = voltage_min
        self.voltage_max = voltage_max
        self._voltage = 0.0
        self._current_limit = current_limit_default
        self._output_on = False
        self._history: list[dict[str, Any]] = []

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def set_voltage(self, volts: float) -> None:
        if volts < self.voltage_min or volts > self.voltage_max:
            raise ValueError(
                f"Voltage {volts} outside [{self.voltage_min}, {self.voltage_max}]"
            )
        self._voltage = float(volts)
        self._history.append({"op": "set_voltage", "value": self._voltage, "t": time.time()})

    def set_output(self, enabled: bool) -> None:
        self._output_on = bool(enabled)
        self._history.append({"op": "set_output", "value": self._output_on, "t": time.time()})

    def set_current_limit(self, amps: float) -> None:
        if amps < 0:
            raise ValueError("Current limit must be >= 0")
        self._current_limit = float(amps)
        self._history.append({"op": "set_current_limit", "value": self._current_limit, "t": time.time()})

    def query_voltage(self) -> float:
        return self._voltage

    def query_output(self) -> bool:
        return self._output_on

    def query_current_limit(self) -> float:
        return self._current_limit

    def identify(self) -> str:
        return "TAKASAGO,RZ-XA-MOCK,SIM,0.1"
