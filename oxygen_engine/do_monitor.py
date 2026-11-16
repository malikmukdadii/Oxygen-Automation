"""Digital-output polling with edge detection, debounce, and startup sync."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .adapters.oxygen import OxygenAdapter
from .channel_manager import ChannelError, ChannelManager
from .config import DOCommandAssignment, EngineConfig
from .scpi_transport import ScpiConnectionError, ScpiError

logger = logging.getLogger(__name__)


@dataclass
class DOEdgeEvent:
    logical_name: str
    oxygen_name: str
    command: str
    edge: str
    timestamp: float


class DiagnosticEdgeTracker:
    """Observation-only edges; never dispatch an engine command or bridge gaps."""

    def __init__(self, max_gap_s: float) -> None:
        self.max_gap_s = max_gap_s
        self.reset()

    def reset(self) -> None:
        self.state: bool | None = None
        self.last_sample: float | None = None
        self.high_since: float | None = None

    def observe(self, state: bool, now: float) -> dict:
        gap = None if self.last_sample is None else now - self.last_sample
        if gap is not None and (gap > self.max_gap_s or gap < 0):
            self.reset()
        previous = self.state
        self.state = state
        self.last_sample = now
        if previous is None:
            return {'edge': 'baseline', 'state': state, 'press': False, 'gap_s': gap}
        edge = 'steady' if previous == state else ('rising' if state else 'falling')
        duration = None
        if edge == 'rising':
            self.high_since = now
        elif edge == 'falling':
            if self.high_since is not None:
                duration = (now - self.high_since) * 1000
            self.high_since = None
        return {'edge': edge, 'state': state, 'press': edge == 'rising',
                'gap_s': gap, 'observed_high_ms': duration}


@dataclass
class DOChannelState:
    assignment: DOCommandAssignment
    channel_id: str | None = None
    state_property: str | None = None
    last_value: bool | None = None
    last_change_ts: float = 0.0
    armed_for_edges: bool = False  # False until startup sync completes
    high_action_latched: bool = False


class DOMonitor:
    """
    Poll DO control states and emit one command per valid edge.

    The exact OXYGEN property for High/Low is unverified on hardware.
    Mock mode uses 'Value' (also mirrored to 'State'). Use probe_do() against
    real OXYGEN to discover the property before enabling live DO control.
    """

    def __init__(
        self,
        adapter: OxygenAdapter,
        channels: ChannelManager,
        config: EngineConfig,
        on_event: Callable[[DOEdgeEvent], None] | None = None,
    ) -> None:
        self.adapter = adapter
        self.channels = channels
        self.config = config
        self.on_event = on_event
        self._states: dict[str, DOChannelState] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._latest: dict[str, bool] = {}
        self._running = False

    @property
    def latest_values(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._latest)

    def initialize(self) -> None:
        """Resolve channels and sync startup levels without generating events."""
        with self._lock:
            self._states.clear()
            for assignment in self.config.do_commands:
                state = DOChannelState(assignment=assignment)
                try:
                    resolved = self.channels.get_by_oxygen_name(assignment.oxygen_name)
                    state.channel_id = resolved.channel_id
                except ChannelError:
                    try:
                        resolved = self.channels.get(assignment.logical_name)
                        state.channel_id = resolved.channel_id
                    except ChannelError:
                        logger.warning("DO channel not resolved: %s", assignment.oxygen_name)
                        self._states[assignment.logical_name] = state
                        continue

                # Startup sync: read current level, do NOT fire events.
                try:
                    state.state_property = self._resolve_state_property(assignment, state.channel_id)
                    level = self._read_level(state)
                    state.last_value = level
                    state.high_action_latched = level  # if already High, latch so no rising action
                    self._latest[assignment.logical_name] = level
                    logger.info(
                        "DO startup sync %s=%s (no action)",
                        assignment.oxygen_name,
                        "High" if level else "Low",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("DO startup read failed for %s: %s", assignment.oxygen_name, exc)
                    state.last_value = None
                state.armed_for_edges = True
                self._states[assignment.logical_name] = state

    def _resolve_state_property(self, assignment: DOCommandAssignment, channel_id: str) -> str:
        if not self.config.connection.use_mock and not self.config.live_control.do_property_verified:
            raise ChannelError('Live DO-state property is unverified; use read-only discovery first')
        if assignment.state_property:
            return assignment.state_property
        if not self.config.connection.use_mock:
            raise ChannelError('Live DO-state property must be explicitly configured and verified')
        # Mock / simulation default — not claimed verified on hardware.
        props = self.adapter.list_properties(channel_id)
        for candidate in self.config.do_state_property_candidates:
            if candidate in props:
                return candidate
        if props:
            raise ChannelError(
                f"No DO state property candidate found on {assignment.oxygen_name}. "
                f"Available: {props}. Run probe-do to discover."
            )
        return "Value"

    def _read_level(self, state: DOChannelState) -> bool:
        if not state.channel_id or not state.state_property:
            raise ChannelError(f"DO channel not ready: {state.assignment.logical_name}")
        raw = self.adapter.get_property(state.channel_id, state.state_property)
        return self._parse_bool(raw)

    @staticmethod
    def _parse_bool(raw: str) -> bool:
        normalized = raw.strip().lower()
        if normalized in {"1", "high", "true", "on", "yes"}:
            return True
        if normalized in {"0", "low", "false", "off", "no"}:
            return False
        raise ValueError(f"Unrecognized DO state value: {raw!r}")

    def poll_once(self) -> list[DOEdgeEvent]:
        events: list[DOEdgeEvent] = []
        with self._lock:
            states = list(self._states.values())
        for state in states:
            if not state.armed_for_edges or not state.channel_id:
                continue
            try:
                level = self._read_level(state)
            except ScpiConnectionError:
                raise
            except (ScpiError, ChannelError, ValueError) as exc:
                logger.error("DO poll error %s: %s", state.assignment.oxygen_name, exc)
                continue

            with self._lock:
                self._latest[state.assignment.logical_name] = level
                prev = state.last_value
                now = time.time()
                debounce_s = self.config.debounce_ms / 1000.0

                if prev is None:
                    state.last_value = level
                    state.high_action_latched = level
                    continue

                if level == prev:
                    # Sustained High: do not repeat actions.
                    continue

                if (now - state.last_change_ts) < debounce_s and state.last_change_ts > 0:
                    continue

                state.last_change_ts = now
                state.last_value = level
                edge = "rising" if level and not prev else "falling" if (not level and prev) else None
                if edge is None:
                    continue

                if edge == "rising":
                    if state.high_action_latched:
                        continue
                    state.high_action_latched = True
                else:
                    # Falling: clear latch so next rising can fire once.
                    state.high_action_latched = False

                wanted = state.assignment.edge
                if edge != wanted:
                    continue

                event = DOEdgeEvent(
                    logical_name=state.assignment.logical_name,
                    oxygen_name=state.assignment.oxygen_name,
                    command=state.assignment.command,
                    edge=edge,
                    timestamp=now,
                )
                events.append(event)

        for event in events:
            logger.info(
                "DO edge %s on %s -> command %s",
                event.edge,
                event.oxygen_name,
                event.command,
            )
            if self.on_event:
                self.on_event(event)
        return events

    def reinitialize_after_reconnect(self) -> None:
        """After communication loss: re-sync levels without replaying stale commands."""
        logger.info("DO monitor reinitializing after reconnect (no command replay)")
        self.initialize()

    def start(self) -> None:
        if self._running:
            return
        self.initialize()
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="do-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        interval = self.config.polling_interval_ms / 1000.0
        while not self._stop.is_set():
            try:
                self.poll_once()
            except ScpiConnectionError as exc:
                logger.error("DO monitor connection lost: %s", exc)
                # Engine is responsible for fault transition; pause until restarted.
                break
            except Exception:  # noqa: BLE001
                logger.exception("DO monitor unexpected error")
            self._stop.wait(interval)

    def probe_channel(self, oxygen_name: str, settle_s: float = 0.0) -> dict:
        """
        Capture properties before/after a manual button press.

        Caller should: capture before, ask operator to press, capture after.
        If settle_s > 0, waits then captures a second snapshot automatically
        (useful in mock tests that flip the DO between snapshots).
        """
        resolved = self.channels.get_by_oxygen_name(oxygen_name)
        before = self.adapter.capture_properties_snapshot(resolved.channel_id)
        result = {
            "channel_id": resolved.channel_id,
            "oxygen_name": oxygen_name,
            "before": before,
            "after": None,
            "diff": None,
            "note": (
                "DO High/Low property is unverified on hardware. "
                "Compare before/after after a manual OXYGEN button press."
            ),
        }
        if settle_s > 0:
            time.sleep(settle_s)
            after = self.adapter.capture_properties_snapshot(resolved.channel_id)
            result["after"] = after
            result["diff"] = self.adapter.diff_properties(before, after)
        return result

    def complete_probe(self, oxygen_name: str, before: dict[str, str]) -> dict:
        resolved = self.channels.get_by_oxygen_name(oxygen_name)
        after = self.adapter.capture_properties_snapshot(resolved.channel_id)
        return {
            "channel_id": resolved.channel_id,
            "oxygen_name": oxygen_name,
            "before": before,
            "after": after,
            "diff": self.adapter.diff_properties(before, after),
        }
