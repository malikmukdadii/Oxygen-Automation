"""Automation engine: state machine, command dispatch, profile worker."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .adapters.mock import MockOxygenServer
from .adapters.oxygen import OxygenAdapter
from .adapters.rzxa import RZxaAdapter, create_rzxa_adapter
from .aout_controller import AoutController, AoutError
from .channel_manager import ChannelManager
from .config import EngineConfig
from .do_monitor import DOEdgeEvent, DOMonitor
from .logging_setup import Dashboard, JsonlEventLogger
from .profiles import Profile, ProfileRunner, list_profiles, load_profile
from .scpi_transport import ScpiConnectionError, ScpiTransport

logger = logging.getLogger(__name__)


class EngineState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    IDLE = "IDLE"
    READY = "READY"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAULT = "FAULT"


# Valid transitions
_TRANSITIONS: dict[EngineState, set[EngineState]] = {
    EngineState.DISCONNECTED: {EngineState.IDLE, EngineState.FAULT},
    EngineState.IDLE: {EngineState.READY, EngineState.DISCONNECTED, EngineState.FAULT},
    EngineState.READY: {
        EngineState.RUNNING,
        EngineState.IDLE,
        EngineState.DISCONNECTED,
        EngineState.FAULT,
    },
    EngineState.RUNNING: {
        EngineState.STOPPING,
        EngineState.READY,
        EngineState.IDLE,
        EngineState.FAULT,
        EngineState.DISCONNECTED,
    },
    EngineState.STOPPING: {
        EngineState.READY,
        EngineState.IDLE,
        EngineState.FAULT,
        EngineState.DISCONNECTED,
    },
    EngineState.FAULT: {EngineState.DISCONNECTED, EngineState.IDLE, EngineState.READY},
}


@dataclass
class CommandResult:
    status: str  # ok | rejected | fault | cancelled
    timestamp: str
    message: str
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def make(
        status: str,
        message: str,
        *,
        error: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> "CommandResult":
        return CommandResult(
            status=status,
            timestamp=datetime.now(timezone.utc).isoformat(),
            message=message,
            error=error,
            data=data or {},
        )


class AutomationEngine:
    """Persistent command dispatcher with enforced state transitions."""

    PROFILE_COMMANDS = {
        "start_city": "city_drive.json",
        "start_regen": "regenerative_braking.json",
        "start_voltage_sweep": "voltage_sweep.json",
    }

    def __init__(
        self,
        config: EngineConfig,
        *,
        dry_run: bool = True,
        event_logger: JsonlEventLogger | None = None,
        dashboard: Dashboard | None = None,
        mock_server: MockOxygenServer | None = None,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        self.events = event_logger or JsonlEventLogger(config.logging.jsonl_dir)
        self.dashboard = dashboard or Dashboard(enabled=config.logging.console_dashboard)
        self._mock_server = mock_server
        self._owns_mock = False

        self.state = EngineState.DISCONNECTED
        self.software_armed = config.software_armed_default
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._runner: ProfileRunner | None = None
        self._active_profile: str | None = None
        self._current_step: str | None = None
        self._last_command: str | None = None
        self._last_result: CommandResult | None = None
        self._stop_monitor = threading.Event()

        self.transport: ScpiTransport | None = None
        self.oxygen: OxygenAdapter | None = None
        self.channels: ChannelManager | None = None
        self.do_monitor: DOMonitor | None = None
        self.aout: AoutController | None = None
        self.rzxa: RZxaAdapter | None = None

    def _emit(self, event: dict[str, Any]) -> None:
        self.events.log(event)
        if self.config.logging.diagnostic_poll_logging or event.get("event") != "poll":
            logger.debug("event %s", event)

    def _set_state(self, new_state: EngineState, reason: str = "") -> None:
        with self._lock:
            old = self.state
            if new_state == old:
                return
            allowed = _TRANSITIONS.get(old, set())
            if new_state not in allowed:
                raise RuntimeError(f"Invalid transition {old} -> {new_state}")
            self.state = new_state
        self._emit(
            {
                "event": "state_transition",
                "from": old.value,
                "to": new_state.value,
                "reason": reason,
            }
        )
        self.dashboard.update(engine_state=new_state.value)
        logger.info("State %s -> %s (%s)", old.value, new_state.value, reason)

    def start(self) -> CommandResult:
        """Connect (mock or real), discover channels, enter IDLE/READY."""
        try:
            self._ensure_transport()
            assert self.transport is not None
            self.transport.connect()
            self.oxygen = OxygenAdapter(self.transport)
            self.channels = ChannelManager(self.oxygen, self.config)
            self.channels.discover()
            simulation = self.config.connection.use_mock or self.dry_run
            self.aout = AoutController(
                self.oxygen,
                self.channels,
                self.config,
                simulation=simulation or not (
                    self.config.live_control.enable_live_aout
                    and self.config.live_control.aout_commands_verified
                ),
            )
            rz = self.config.instruments.rzxa
            self.rzxa = create_rzxa_adapter(
                use_mock=bool(rz.get("use_mock", True)),
                enable_real=self.config.live_control.enable_real_rzxa,
                commands_verified=self.config.live_control.rzxa_commands_verified,
                host=str(rz.get("host", "127.0.0.1")),
                port=int(rz.get("port", 5025)),
                voltage_min=float(rz.get("voltage_min", 0.0)),
                voltage_max=float(rz.get("voltage_max", 80.0)),
                current_limit_default=float(rz.get("current_limit_default", 10.0)),
            )
            self.do_monitor = DOMonitor(
                self.oxygen,
                self.channels,
                self.config,
                on_event=self._on_do_event,
            )
            self.do_monitor.initialize()
            if self.state == EngineState.DISCONNECTED:
                self._set_state(EngineState.IDLE, "connected")
            elif self.state == EngineState.FAULT:
                self._set_state(EngineState.IDLE, "recovered")
            self.dashboard.update(
                connection="CONNECTED",
                software_armed=self.software_armed,
                do_values=self.do_monitor.latest_values,
                aout_values=self.aout.latest_values(),
            )
            if self.software_armed and self.state == EngineState.IDLE:
                self._set_state(EngineState.READY, "software armed")
            self._emit({"event": "engine_started", "dry_run": self.dry_run})
            return CommandResult.make("ok", "Engine started", data={"state": self.state.value})
        except Exception as exc:
            logger.exception("Engine start failed")
            try:
                if self.state != EngineState.FAULT and self.state != EngineState.DISCONNECTED:
                    self._set_state(EngineState.FAULT, str(exc))
                elif self.state == EngineState.DISCONNECTED:
                    pass
            except RuntimeError:
                pass
            self.dashboard.update(connection="FAULT", last_scpi_error=str(exc))
            return CommandResult.make("fault", "Engine start failed", error=str(exc))

    def _ensure_transport(self) -> None:
        if self.transport is not None:
            return
        host = self.config.connection.host
        port = self.config.connection.port
        if self.config.connection.use_mock:
            if self._mock_server is None:
                self._mock_server = MockOxygenServer(host=host, port=self.config.connection.mock_port)
                self._mock_server.start()
                self._owns_mock = True
            port = self._mock_server.bound_port
        self.transport = ScpiTransport(
            host=host,
            port=port,
            timeout_s=self.config.connection.timeout_s,
            reconnect_min_s=self.config.connection.reconnect_min_s,
            reconnect_max_s=self.config.connection.reconnect_max_s,
            reconnect_max_attempts=self.config.connection.reconnect_max_attempts,
            on_disconnect=self._on_disconnect,
            on_reconnect=self._on_reconnect,
        )

    def _on_disconnect(self) -> None:
        self.dashboard.update(connection="DISCONNECTED")
        self._emit({"event": "scpi_disconnect"})
        with self._lock:
            current = self.state
        try:
            if current == EngineState.RUNNING:
                self.cancel("SCPI disconnection")
            if self.state not in {EngineState.DISCONNECTED, EngineState.FAULT}:
                # Prefer FAULT then allow DISCONNECTED via stop paths
                if EngineState.FAULT in _TRANSITIONS.get(self.state, set()):
                    self._set_state(EngineState.FAULT, "SCPI disconnection")
                if EngineState.DISCONNECTED in _TRANSITIONS.get(self.state, set()):
                    self._set_state(EngineState.DISCONNECTED, "SCPI disconnection")
            elif self.state == EngineState.FAULT:
                if EngineState.DISCONNECTED in _TRANSITIONS[EngineState.FAULT]:
                    self._set_state(EngineState.DISCONNECTED, "SCPI disconnection")
        except RuntimeError as exc:
            logger.error("State update on disconnect failed: %s", exc)

    def _on_reconnect(self) -> None:
        self.dashboard.update(connection="CONNECTED")
        self._emit({"event": "scpi_reconnect"})
        # Must NOT resume a previous sequence automatically.
        if self.do_monitor:
            self.do_monitor.reinitialize_after_reconnect()
        if self.channels:
            self.channels.discover()
        with self._lock:
            if self.state in {EngineState.DISCONNECTED, EngineState.FAULT}:
                self._set_state(EngineState.IDLE, "reconnected (sequence not resumed)")
                if self.software_armed:
                    self._set_state(EngineState.READY, "armed after reconnect")

    def stop(self) -> CommandResult:
        self._stop_monitor.set()
        if self.do_monitor:
            self.do_monitor.stop()
        if self._runner:
            self._runner.request_cancel()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5.0)
        if self.transport:
            self.transport.close()
        if self._owns_mock and self._mock_server:
            self._mock_server.stop()
            self._mock_server = None
            self._owns_mock = False
        with self._lock:
            if self.state != EngineState.DISCONNECTED:
                # Force path to DISCONNECTED through allowed hops
                try:
                    if self.state == EngineState.RUNNING:
                        self._set_state(EngineState.STOPPING, "stop")
                    if self.state in {EngineState.STOPPING, EngineState.READY, EngineState.IDLE, EngineState.FAULT}:
                        if self.state == EngineState.STOPPING:
                            self._set_state(EngineState.IDLE, "stopped")
                        if self.state != EngineState.DISCONNECTED:
                            self._set_state(EngineState.DISCONNECTED, "stopped")
                except RuntimeError:
                    self.state = EngineState.DISCONNECTED
        self.dashboard.update(connection="DISCONNECTED", engine_state=self.state.value)
        return CommandResult.make("ok", "Engine stopped")

    def start_monitoring(self) -> None:
        if not self.do_monitor:
            raise RuntimeError("Engine not started")
        self._stop_monitor.clear()
        self.do_monitor.start()

    def poll_once(self) -> list[DOEdgeEvent]:
        if not self.do_monitor:
            return []
        events = self.do_monitor.poll_once()
        self.dashboard.update(
            do_values=self.do_monitor.latest_values,
            aout_values=self.aout.latest_values() if self.aout else {},
            software_armed=self.software_armed,
            recording=self._recording_status(),
        )
        return events

    def _recording_status(self) -> bool:
        try:
            if self.oxygen:
                return self.oxygen.recording_status()
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_do_event(self, event: DOEdgeEvent) -> None:
        self.dispatch(event.command, source=f"do:{event.oxygen_name}")

    def dispatch(self, command: str, *, source: str = "api") -> CommandResult:
        self._last_command = command
        self.dashboard.update(last_command=f"{command} ({source})")
        self._emit({"event": "command", "command": command, "source": source})

        if command == "toggle_arm":
            return self._toggle_arm()
        if command == "cancel":
            return self.cancel(f"cancel from {source}")
        if command == "toggle_recording":
            return self._toggle_recording()

        if command in self.PROFILE_COMMANDS:
            if not self.software_armed and not self.dry_run:
                # In dry-run/mock we still allow for testing, but log permission.
                pass
            if not self.software_armed:
                # Operational permission — still allow dry-run simulation profiles
                # only when explicitly forced via dry_run engine flag.
                if not self.dry_run:
                    result = CommandResult.make(
                        "rejected",
                        "Software disarmed — arm via DO 3/6 before starting profiles",
                    )
                    self._last_result = result
                    return result
            return self.start_profile_command(command)

        result = CommandResult.make("rejected", f"Unknown command: {command}")
        self._last_result = result
        return result

    def _toggle_arm(self) -> CommandResult:
        self.software_armed = not self.software_armed
        self.dashboard.update(software_armed=self.software_armed)
        self._emit({"event": "arm_toggle", "armed": self.software_armed})
        with self._lock:
            state = self.state
        if self.software_armed and state == EngineState.IDLE:
            self._set_state(EngineState.READY, "armed")
        elif not self.software_armed and state == EngineState.READY:
            self._set_state(EngineState.IDLE, "disarmed")
        result = CommandResult.make(
            "ok",
            f"Software {'armed' if self.software_armed else 'disarmed'} "
            "(not a physical safety interlock)",
            data={"armed": self.software_armed},
        )
        self._last_result = result
        return result

    def _toggle_recording(self) -> CommandResult:
        if not self.oxygen:
            return CommandResult.make("fault", "No OXYGEN adapter")
        try:
            if self.oxygen.recording_status():
                if not self.dry_run:
                    self.oxygen.stop_recording()
                msg = "Recording stopped"
                active = False
            else:
                if not self.dry_run:
                    self.oxygen.start_recording()
                msg = "Recording started"
                active = True
            # In dry_run still flip mock recording for visibility when connected mock
            if self.dry_run and self.config.connection.use_mock:
                if active:
                    self.oxygen.start_recording()
                else:
                    self.oxygen.stop_recording()
            self.dashboard.update(recording=self._recording_status())
            result = CommandResult.make("ok", msg, data={"recording": self._recording_status()})
            self._last_result = result
            return result
        except Exception as exc:
            return CommandResult.make("fault", "Recording toggle failed", error=str(exc))

    def start_profile_command(self, command: str) -> CommandResult:
        filename = self.PROFILE_COMMANDS[command]
        path = Path(self.config.profiles_dir) / filename
        if not path.exists():
            # Also try relative to package project root
            alt = Path(__file__).resolve().parent.parent / "profiles" / filename
            path = alt if alt.exists() else path
        if not path.exists():
            return CommandResult.make("fault", f"Profile file not found: {filename}")
        profile = load_profile(path)
        return self.start_profile(profile)

    def start_profile(self, profile: Profile) -> CommandResult:
        with self._lock:
            if self.state == EngineState.RUNNING:
                result = CommandResult.make(
                    "rejected",
                    "Another automation sequence is already running",
                    data={"active_profile": self._active_profile},
                )
                self._last_result = result
                return result
            if self.state not in {EngineState.READY, EngineState.IDLE}:
                # Allow IDLE+dry_run for unattended tests
                if not (self.dry_run and self.state == EngineState.IDLE):
                    result = CommandResult.make(
                        "rejected",
                        f"Cannot start profile in state {self.state.value}",
                    )
                    self._last_result = result
                    return result
            if self._worker and self._worker.is_alive():
                result = CommandResult.make("rejected", "Worker still busy")
                self._last_result = result
                return result

            if self.state == EngineState.IDLE:
                self._set_state(EngineState.READY, "auto-ready for profile")
            self._set_state(EngineState.RUNNING, f"start {profile.name}")
            self._active_profile = profile.name
            self._current_step = "0"
            self.dashboard.update(active_profile=profile.name, current_step="0")

        self._runner = ProfileRunner(
            aout=self.aout,
            oxygen=self.oxygen,
            channels=self.channels,
            rzxa=self.rzxa,
            dry_run=self.dry_run,
            event_log=self._emit,
        )
        self._worker = threading.Thread(
            target=self._run_profile_worker,
            args=(profile,),
            name=f"profile-{profile.name}",
            daemon=True,
        )
        self._worker.start()
        result = CommandResult.make(
            "ok",
            f"Started profile {profile.name}",
            data={"profile": profile.name},
        )
        self._last_result = result
        return result

    def _run_profile_worker(self, profile: Profile) -> None:
        assert self._runner is not None
        try:
            outcome = self._runner.run(profile)
            status = outcome.get("status")
            with self._lock:
                if self.state == EngineState.RUNNING:
                    self._set_state(EngineState.STOPPING, f"profile {status}")
                if self.state == EngineState.STOPPING:
                    next_state = EngineState.READY if self.software_armed else EngineState.IDLE
                    self._set_state(next_state, f"profile finished: {status}")
                elif self.state == EngineState.FAULT:
                    pass
            self._emit({"event": "profile_outcome", **outcome})
            if status == "ok":
                self._last_result = CommandResult.make(
                    "ok",
                    f"Profile {profile.name} completed",
                    data=outcome,
                )
            elif status == "cancelled":
                self._last_result = CommandResult.make(
                    "cancelled",
                    f"Profile {profile.name} cancelled",
                    data=outcome,
                )
            elif status == "fault":
                self._last_result = CommandResult.make(
                    "fault",
                    f"Profile {profile.name} fault",
                    error=str(outcome.get("error")),
                    data=outcome,
                )
                # Best-effort cleanup
                self._best_effort_cleanup()
                with self._lock:
                    if self.state not in {EngineState.FAULT, EngineState.DISCONNECTED}:
                        try:
                            self._set_state(EngineState.FAULT, outcome.get("error", "profile fault"))
                        except RuntimeError:
                            pass
        except Exception as exc:
            logger.exception("Profile worker crashed")
            self._emit({"event": "profile_worker_crash", "error": str(exc)})
            self._best_effort_cleanup()
            try:
                with self._lock:
                    if self.state == EngineState.RUNNING:
                        self._set_state(EngineState.STOPPING, "crash")
                    if EngineState.FAULT in _TRANSITIONS.get(self.state, set()):
                        self._set_state(EngineState.FAULT, str(exc))
            except RuntimeError:
                pass
        finally:
            self._active_profile = None
            self._current_step = None
            self.dashboard.update(active_profile=None, current_step=None)

    def cancel(self, reason: str = "cancel") -> CommandResult:
        """
        Cancel active automation. This is software cancellation only —
        NOT a physical emergency stop.
        """
        self._emit({"event": "cancel", "reason": reason})
        with self._lock:
            if self.state != EngineState.RUNNING:
                result = CommandResult.make(
                    "ok",
                    f"No active profile to cancel (state={self.state.value})",
                )
                self._last_result = result
                return result
            self._set_state(EngineState.STOPPING, reason)
        if self._runner:
            self._runner.request_cancel()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5.0)
        self._best_effort_cleanup()
        with self._lock:
            if self.state == EngineState.STOPPING:
                next_state = EngineState.READY if self.software_armed else EngineState.IDLE
                self._set_state(next_state, "cancelled")
        result = CommandResult.make(
            "cancelled",
            f"Cancelled: {reason} (software cancel — not a physical E-stop)",
        )
        self._last_result = result
        return result

    def _best_effort_cleanup(self) -> None:
        """Best-effort configured cleanup; not a guaranteed neutralization."""
        if not self.aout:
            return
        for role in self.config.aout_roles:
            try:
                self.aout.return_to_neutral(role.logical_name, dry_run=self.dry_run)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cleanup failed for %s: %s", role.logical_name, exc)
        self._emit({"event": "cleanup_best_effort"})

    def wait_until_idle(self, timeout_s: float = 60.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            with self._lock:
                if self.state not in {EngineState.RUNNING, EngineState.STOPPING}:
                    if not (self._worker and self._worker.is_alive()):
                        return True
            time.sleep(0.05)
        return False

    def list_profiles(self) -> list[Profile]:
        path = Path(self.config.profiles_dir)
        if not path.exists():
            path = Path(__file__).resolve().parent.parent / "profiles"
        return list_profiles(path)

    def validate_setpoint(self, logical_name: str, value: float) -> None:
        if not self.aout:
            raise AoutError("AOUT controller not ready")
        self.aout.validate_setpoint(logical_name, value)

    @property
    def mock_server(self) -> MockOxygenServer | None:
        return self._mock_server

    @property
    def last_result(self) -> CommandResult | None:
        return self._last_result
