"""OXYGEN instrument adapter — recording, measurements, validated AOUT properties."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any

from ..scpi_transport import ScpiProtocolError, ScpiTransport, parse_scpi, quote_scpi

logger = logging.getLogger(__name__)


@dataclass
class ChannelInfo:
    channel_id: str
    name: str
    channel_type: str = "unknown"


@dataclass(frozen=True)
class PropertyValue:
    kind: str
    values: tuple[Any, ...]


class OxygenAdapter:
    """Documented OXYGEN SCPI operations; AOUT/DO property mappings unverified."""

    def __init__(self, transport: ScpiTransport) -> None:
        self.transport = transport
        self._numeric_lock = threading.RLock()

    def identify(self) -> str:
        raw = self.transport.query("*IDN?")
        if len(parse_scpi(raw)) != 4:
            raise ScpiProtocolError(f"Malformed *IDN? response: {raw!r}")
        return raw

    @staticmethod
    def _channel_id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value) or int(value) > 2**64 - 1:
            raise ScpiProtocolError(f"Invalid OXYGEN channel ID: {value!r}")
        return value

    @staticmethod
    def _strings(raw: str) -> list[str]:
        if raw.upper() == 'NONE':
            return []
        values = parse_scpi(raw)
        if len(values) == 1 and isinstance(values[0], tuple):
            values = list(values[0])
        if not values or not all(isinstance(v, str) for v in values):
            raise ScpiProtocolError(f"Expected SCPI string list: {raw!r}")
        return values

    def list_channels(self) -> list[ChannelInfo]:
        raw = self.transport.query(":CHANNELlist:NAMes?")
        if raw.upper() == 'NONE':
            return []
        data = parse_scpi(raw)
        if not data:
            raise ScpiProtocolError("Empty channel list response")
        channels: list[ChannelInfo] = []
        for item in data:
            if not isinstance(item, tuple) or len(item) != 2 or not all(isinstance(v, str) for v in item):
                raise ScpiProtocolError(f"Malformed channel entry: {item!r}")
            channels.append(ChannelInfo(self._channel_id(item[0]), item[1]))
        if len({c.channel_id for c in channels}) != len(channels):
            raise ScpiProtocolError("Duplicate channel IDs in channel list")
        return channels

    def list_channel_ids(self, names: list[str] | None = None) -> list[str]:
        suffix = ' ' + ','.join(quote_scpi(n) for n in names) if names else ''
        raw = self.transport.query(":CHANNELlist:IDs?" + suffix)
        return [self._channel_id(v) for v in self._strings(raw)]

    def resolve_channel_id(self, name: str) -> str | None:
        ids = self.list_channel_ids([name])
        if len(ids) > 1:
            raise ScpiProtocolError(f"Ambiguous OXYGEN channel name: {name!r}")
        return ids[0] if ids else None

    def list_properties(self, channel_id: str) -> list[str]:
        cid = self._channel_id(channel_id)
        raw = self.transport.query(f":CHANNELlist:ITEM{cid}:ATTR:NAMes?")
        return self._strings(raw)

    def get_property(self, channel_id: str, prop: str) -> str:
        value = self.get_property_value(channel_id, prop)
        if len(value.values) == 1:
            return str(value.values[0])
        return str((value.kind, *value.values))

    def get_property_value(self, channel_id: str, prop: str) -> PropertyValue:
        return self.parse_property_value(self.get_property_reply(channel_id, prop))

    def get_property_reply(self, channel_id: str, prop: str) -> str:
        """Return the unparsed property payload (optional header removed)."""
        cid = quote_scpi(self._channel_id(channel_id))
        return self.transport.query(f":CHANNELlist:PROPerty? {cid},{quote_scpi(prop)}")

    @staticmethod
    def parse_property_value(raw: str) -> PropertyValue:
        values = parse_scpi(raw)
        if len(values) != 1:
            raise ScpiProtocolError(f"Malformed property value: {raw!r}")
        value = values[0]
        if isinstance(value, str):
            if raw.upper() == 'NONE':
                return PropertyValue('NONE', ())
            if not raw.startswith(('"', "'")):
                if value.upper() in {'ON', 'TRUE', 'OFF', 'FALSE'}:
                    return PropertyValue('BOOLEAN', (value.upper() in {'ON', 'TRUE'},))
                try:
                    number = float(value)
                except ValueError as exc:
                    raise ScpiProtocolError(f"Malformed property value: {raw!r}") from exc
                return PropertyValue('NUMBER', (number,))
            return PropertyValue('STRING', (value,))
        if not value or not isinstance(value[0], str):
            raise ScpiProtocolError(f"Malformed typed property: {raw!r}")
        kind, *args = value
        kind = kind.upper()
        numeric_indices = {'SCALAR': (0,), 'RANGE': (0, 2), 'RATIO': (0, 1),
                           'FLOAT': (0,), 'INTEGER': (0,), 'DOUBLE': (0,)}
        arities = {'ENUM': 2, 'SCALAR': 2, 'RANGE': 4, 'RATIO': 3,
                   'CHANNEL_ID': 1, 'STRING': 1, 'FLOAT': 1, 'INTEGER': 1, 'DOUBLE': 1, 'BOOL': 1}
        if kind != 'CHANNEL_ID_VECTOR' and (kind not in arities or len(args) != arities[kind]):
            raise ScpiProtocolError(f"Unrecognized typed property: {raw!r}")
        if not all(isinstance(v, str) for v in args):
            raise ScpiProtocolError(f"Nested property values: {raw!r}")
        if kind == 'BOOL':
            if args[0].upper() not in {'ON', 'TRUE', 'OFF', 'FALSE'}:
                raise ScpiProtocolError(f"Malformed BOOL property: {raw!r}")
            return PropertyValue(kind, (args[0].upper() in {'ON', 'TRUE'},))
        try:
            for index in numeric_indices.get(kind, ()):
                args[index] = float(args[index])
            if kind in {'CHANNEL_ID', 'CHANNEL_ID_VECTOR'}:
                args = [OxygenAdapter._channel_id(v) for v in args]
        except ValueError as exc:
            raise ScpiProtocolError(f"Malformed typed property: {raw!r}") from exc
        return PropertyValue(kind, tuple(args))

    def set_property(self, channel_id: str, prop: str, value: str) -> None:
        raise ScpiProtocolError("Physical property writes disabled; AOUT/DO mappings are unverified")

    def read_measurement(self, channel_id: str) -> float:
        cid = self._channel_id(channel_id)
        channel = next((c for c in self.list_channels() if c.channel_id == cid), None)
        if channel is None:
            raise ScpiProtocolError(f"Unknown channel ID: {cid}")
        return self.read_measurements([channel.name])[0]

    def read_measurements(self, names: list[str]) -> list[float]:
        with self._numeric_lock:
            self.configure_numeric_measurements(names)
            return self.read_selected_measurements(len(names))

    def configure_numeric_measurements(self, names: list[str]) -> None:
        """Select scalar transfers without changing acquisition or output state."""
        if not names or len(set(names)) != len(names):
            raise ScpiProtocolError("Select one or more distinct scalar channel names")
        with self._numeric_lock:
            self.transport.command(":NUMeric:NORMal:ITEMS " + ','.join(quote_scpi(n) for n in names))
            self.transport.command(":NUMeric:NORMal:FORMat ASCII")
            self.transport.command(":NUMeric:NORMal:NUMber ALL")
            dims = parse_scpi(self.transport.query(":NUMeric:NORMal:DIMS?"))
            if dims != ['1'] * len(names):
                raise ScpiProtocolError(f"Selected measurements must be scalar: {dims!r}")

    def read_selected_measurements(self, count: int) -> list[float]:
        """Read an already selected transfer list, avoiding per-poll reconfiguration."""
        if count < 1:
            raise ScpiProtocolError("Expected measurement count must be positive")
        with self._numeric_lock:
            raw = self.transport.query(":NUMeric:NORMal:VALue?")
            values = parse_scpi(raw)
            if len(values) != count:
                raise ScpiProtocolError(f"Wrong numeric value count: {raw!r}")
            try:
                return [float(v) for v in values]
            except (TypeError, ValueError) as exc:
                raise ScpiProtocolError(f"Malformed numeric measurement: {raw!r}") from exc

    def recording_state(self) -> str:
        raw = self.transport.query(":STORe:STATe?")
        return self._state(raw, {'Armed', 'Paused', 'Started', 'Stopped', 'Stopping'})

    @staticmethod
    def _state(raw: str, allowed: set[str]) -> str:
        values = parse_scpi(raw)
        for state in allowed:
            if len(values) == 1 and isinstance(values[0], str) and values[0].lower() == state.lower():
                return state
        raise ScpiProtocolError(f"Unrecognized OXYGEN state response: {raw!r}")

    def acquisition_state(self) -> str:
        return self._state(self.transport.query(":ACQUisition:STATe?"), {'Stopped', 'Waiting_for_sync', 'Started'})

    def recording_status(self) -> bool:
        # Armed/paused/stopping sessions must not be mistaken for fully stopped.
        return self.recording_state() != 'Stopped'

    def start_recording(self) -> None:
        self.transport.command(":STORe:STARt")

    def stop_recording(self) -> None:
        self.transport.command(":STORe:STOP")

    def add_marker(self, text: str) -> None:
        self.transport.command(f':MARKer:ADD {quote_scpi(text)}')

    def capture_properties_snapshot(self, channel_id: str) -> dict[str, str]:
        """Capture all listed properties for DO probe / discovery."""
        props = self.list_properties(channel_id)
        snapshot: dict[str, str] = {}
        for prop in props:
            try:
                snapshot[prop] = self.get_property(channel_id, prop)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to read property %s: %s", prop, exc)
                snapshot[prop] = f"<error:{exc}>"
        return snapshot

    def diff_properties(
        self, before: dict[str, str], after: dict[str, str]
    ) -> dict[str, dict[str, str]]:
        keys = set(before) | set(after)
        diffs: dict[str, dict[str, str]] = {}
        for key in sorted(keys):
            b = before.get(key, "<missing>")
            a = after.get(key, "<missing>")
            if b != a:
                diffs[key] = {"before": b, "after": a}
        return diffs
