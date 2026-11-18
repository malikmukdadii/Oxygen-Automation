"""TCP SCPI transport with serialized access, reconnect, and error inspection."""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


class ScpiError(Exception):
    """Base SCPI transport / protocol error."""


class ScpiConnectionError(ScpiError):
    """Connection lost or unavailable."""


class ScpiTimeoutError(ScpiError):
    """Socket or response timeout."""


class ScpiProtocolError(ScpiError):
    """Unrecognized or malformed SCPI response."""


def quote_scpi(value: str) -> str:
    if any(c in value for c in '\r\n\x00'):
        raise ScpiProtocolError("Control characters are not allowed in SCPI strings")
    return '"' + value.replace('"', '""') + '"'


def strip_response_header(raw: str, command: str | None = None) -> str:
    """Accept optional headers, including OXYGEN's omitted numeric NORMal node."""
    match = re.match(r'^\s*(:[A-Za-z][A-Za-z0-9]*(?::[A-Za-z][A-Za-z0-9]*)*|[A-Za-z][A-Za-z0-9]*(?::[A-Za-z][A-Za-z0-9]*)+|\*[A-Za-z]+)\??\s+', raw)
    if not match:
        return raw.strip()
    if command:
        expected = command.split(None, 1)[0].strip(':?').upper().split(':')
        actual = match[1].strip(':?').upper().split(':')
        # OXYGEN 8.2 returns :NUM:DIMS for :NUMeric:NORMal:DIMS?.
        if (
            len(expected) >= 3
            and expected[0] in {'NUM', 'NUMERIC'}
            and expected[1] in {'NORM', 'NORMAL'}
            and actual[0] in {'NUM', 'NUMERIC'}
            and len(actual) == len(expected) - 1
        ):
            expected = [expected[0], *expected[2:]]
        if len(actual) > len(expected) or any(not (a.startswith(e) or e.startswith(a)) for a, e in zip(actual, expected)):
            raise ScpiProtocolError(f"Unexpected response header for {command!r}: {raw!r}")
    return raw[match.end():].strip()


def parse_scpi(raw: str) -> list:
    """Parse comma lists, parenthesized tuples and doubled-quote strings losslessly.

    Bare tokens remain strings, in particular full-width OXYGEN channel IDs.
    """
    text = raw.strip()
    pos = 0

    def sequence(nested: bool = False) -> list:
        nonlocal pos
        values = []
        while pos < len(text):
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos == len(text):
                break
            char = text[pos]
            if char == '(':
                pos += 1
                value = tuple(sequence(True))
            elif char in '\"\'':
                quote = char
                pos += 1
                value = ''
                while True:
                    if pos >= len(text):
                        raise ScpiProtocolError(f"Unterminated SCPI string: {raw!r}")
                    char = text[pos]
                    pos += 1
                    if char == quote:
                        if pos < len(text) and text[pos] == quote:
                            pos += 1
                        else:
                            break
                    value += char
            else:
                start = pos
                while pos < len(text) and text[pos] not in ',()\"\'':
                    pos += 1
                value = text[start:pos].strip()
                if not value:
                    raise ScpiProtocolError(f"Missing SCPI value: {raw!r}")
            values.append(value)
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if nested and pos < len(text) and text[pos] == ')':
                pos += 1
                return values
            if pos == len(text):
                break
            if text[pos] != ',':
                raise ScpiProtocolError(f"Expected SCPI separator: {raw!r}")
            pos += 1
            if not text[pos:].strip():
                raise ScpiProtocolError(f"Trailing SCPI separator: {raw!r}")
        if nested:
            raise ScpiProtocolError(f"Unterminated SCPI tuple: {raw!r}")
        return values

    return sequence()


@dataclass
class ScpiErrorEntry:
    code: int
    message: str
    raw_response: str | None = field(default=None, compare=False, repr=False)

    def __str__(self) -> str:
        return f"{self.code},{self.message}"


class ScpiCommandError(ScpiProtocolError):
    """Error-queue entry observed after a write, not proof of device-side causality."""

    def __init__(self, command: str, entry: ScpiErrorEntry):
        super().__init__(f"SCPI error after '{command}': {entry}")
        self.command = command
        self.entry = entry


class ScpiTransport:
    """
    Thread-safe TCP SCPI client.

    - One command at a time (lock)
    - Partial-read / newline handling
    - Bounded reconnect backoff
    - Does NOT reset OXYGEN or reload setup on connect
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 10001,
        timeout_s: float = 2.0,
        reconnect_min_s: float = 0.5,
        reconnect_max_s: float = 8.0,
        reconnect_max_attempts: int = 10,
        line_ending: bytes = b"\n",
        on_disconnect: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.reconnect_min_s = reconnect_min_s
        self.reconnect_max_s = reconnect_max_s
        self.reconnect_max_attempts = reconnect_max_attempts
        self.line_ending = line_ending
        self.on_disconnect = on_disconnect
        self.on_reconnect = on_reconnect

        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self._buffer = bytearray()
        self._connected = False
        self._last_error: ScpiErrorEntry | None = None
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._connected and self._sock is not None

    @property
    def last_error(self) -> ScpiErrorEntry | None:
        return self._last_error

    def connect(self) -> None:
        with self._lock:
            if self._closed:
                raise ScpiConnectionError("Transport is closed")
            self._open_socket()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_socket(notify=False)

    def _open_socket(self) -> None:
        self._close_socket(notify=False)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout_s)
        try:
            sock.connect((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise ScpiConnectionError(f"Failed to connect to {self.host}:{self.port}: {exc}") from exc
        self._sock = sock
        self._buffer.clear()
        was_connected = self._connected
        self._connected = True
        logger.info("SCPI connected to %s:%s", self.host, self.port)
        if was_connected is False and self.on_reconnect and was_connected:
            pass  # first connect is not a reconnect
        # Note: first connect does not invoke on_reconnect

    def _close_socket(self, notify: bool = True) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        was = self._connected
        self._connected = False
        self._buffer.clear()
        if notify and was and self.on_disconnect:
            try:
                self.on_disconnect()
            except Exception:  # noqa: BLE001 — callback must not break close
                logger.exception("on_disconnect callback failed")

    def reconnect(self) -> None:
        """Reconnect with bounded exponential backoff."""
        with self._lock:
            if self._closed:
                raise ScpiConnectionError("Transport is closed")
            delay = self.reconnect_min_s
            last_exc: Exception | None = None
            for attempt in range(1, self.reconnect_max_attempts + 1):
                try:
                    self._open_socket()
                    if self.on_reconnect:
                        try:
                            self.on_reconnect()
                        except Exception:  # noqa: BLE001
                            logger.exception("on_reconnect callback failed")
                    logger.info("SCPI reconnected on attempt %s", attempt)
                    return
                except ScpiConnectionError as exc:
                    last_exc = exc
                    logger.warning(
                        "SCPI reconnect attempt %s/%s failed: %s",
                        attempt,
                        self.reconnect_max_attempts,
                        exc,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, self.reconnect_max_s)
            raise ScpiConnectionError(
                f"Reconnect failed after {self.reconnect_max_attempts} attempts: {last_exc}"
            )

    def write(self, command: str) -> None:
        """Send a command without expecting a response."""
        with self._lock:
            self._validate_command(command)
            self._ensure_connected()
            payload = command.encode("utf-8")
            if not payload.endswith(self.line_ending):
                payload += self.line_ending
            try:
                assert self._sock is not None
                self._sock.sendall(payload)
            except OSError as exc:
                self._handle_loss(exc)
                raise ScpiConnectionError(f"Write failed: {exc}") from exc

    def query(self, command: str) -> str:
        """Send a query and return the response line (without terminator)."""
        with self._lock:
            self._validate_command(command)
            self._ensure_connected()
            payload = command.encode("utf-8")
            if not payload.endswith(self.line_ending):
                payload += self.line_ending
            wire_reply = None
            try:
                assert self._sock is not None
                self._sock.sendall(payload)
                wire_reply = self._readline()
                raw = strip_response_header(wire_reply, command)
                if re.match(r'^-[0-9]+\s*,', raw) and not re.match(r'^:?(SYSTem|SYST):(ERRor|ERR)', command, re.I):
                    raise ScpiProtocolError(f"SCPI error response: {raw}")
                return raw
            except (ScpiTimeoutError, ScpiProtocolError) as exc:
                exc.raw_response = wire_reply
                exc.query_command = command
                # Never let a late/partial reply become the next query's response.
                self._close_socket(notify=True)
                raise
            except OSError as exc:
                self._handle_loss(exc)
                raise ScpiConnectionError(f"Query failed: {exc}") from exc

    def command(self, command: str, query_error: bool = True) -> str | None:
        """
        Send a command. If command ends with '?', return the response.
        Optionally query SYST:ERR? afterward for non-query commands.
        This consumes one queue entry, not a per-command device acknowledgement.
        """
        with self._lock:
            stripped = command.strip()
            if '?' in stripped.split(None, 1)[0]:
                return self.query(stripped)
            self.write(stripped)
            if query_error:
                err = self.check_error()
                if err and err.code != 0:
                    raise ScpiCommandError(stripped, err)
            return None

    def check_error(self) -> ScpiErrorEntry:
        """Query the instrument error queue (SYST:ERR?)."""
        raw = self.query("SYST:ERR?")
        entry = self._parse_error(raw)
        entry.raw_response = raw
        self._last_error = entry
        if entry.code != 0:
            logger.error("SCPI error: %s", entry)
        return entry

    def drain_errors(self) -> list[ScpiErrorEntry]:
        """Drain the error queue until code 0 or empty."""
        errors: list[ScpiErrorEntry] = []
        for _ in range(50):
            entry = self.check_error()
            if entry.code == 0:
                break
            errors.append(entry)
        return errors

    def _ensure_connected(self) -> None:
        if self._closed:
            raise ScpiConnectionError("Transport is closed")
        if not self.connected:
            raise ScpiConnectionError("Not connected")

    @staticmethod
    def _validate_command(command: str) -> None:
        if any(c in command for c in '\r\n\x00'):
            raise ScpiProtocolError("SCPI messages must be a single line")

    def _handle_loss(self, exc: Exception) -> None:
        logger.error("SCPI connection loss: %s", exc)
        self._close_socket(notify=True)

    def _readline(self) -> str:
        assert self._sock is not None
        deadline = time.monotonic() + self.timeout_s
        while True:
            idx = self._buffer.find(self.line_ending)
            if idx >= 0:
                line = bytes(self._buffer[:idx])
                del self._buffer[: idx + len(self.line_ending)]
                try:
                    return line.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    raise ScpiProtocolError(f"Non-UTF8 SCPI response: {exc}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScpiTimeoutError(f"Timed out waiting for SCPI response ({self.timeout_s}s)")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout as exc:
                raise ScpiTimeoutError(f"Timed out waiting for SCPI response ({self.timeout_s}s)") from exc
            except OSError as exc:
                self._handle_loss(exc)
                raise ScpiConnectionError(f"Read failed: {exc}") from exc
            if not chunk:
                self._handle_loss(ConnectionError("peer closed"))
                raise ScpiConnectionError("Connection closed by peer while reading")
            self._buffer.extend(chunk)

    @staticmethod
    def _parse_error(raw: str) -> ScpiErrorEntry:
        parts = parse_scpi(strip_response_header(raw))
        if len(parts) != 2 or not all(isinstance(p, str) for p in parts):
            raise ScpiProtocolError(f"Unrecognized SYST:ERR? response: {raw!r}")
        try:
            code = int(parts[0].strip())
        except ValueError as exc:
            raise ScpiProtocolError(f"Unrecognized SYST:ERR? code in: {raw!r}") from exc
        message = parts[1]
        return ScpiErrorEntry(code=code, message=message)
