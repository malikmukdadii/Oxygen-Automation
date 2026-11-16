"""JSONL event logging and console dashboard."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonlEventLogger:
    """Append structured events to a timestamped JSONL file."""

    def __init__(self, directory: str | Path, prefix: str = "oxygen-engine") -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = self.directory / f"{prefix}-{stamp}.jsonl"
        self._lock = threading.Lock()

    def log(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(payload, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class Dashboard:
    """Readable live console dashboard (overwrite in-place when TTY)."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "connection": "DISCONNECTED",
            "engine_state": "DISCONNECTED",
            "software_armed": False,
            "recording": False,
            "active_profile": None,
            "current_step": None,
            "last_command": None,
            "last_scpi_error": None,
            "do_values": {},
            "aout_values": {},
        }

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._state.update(kwargs)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def render(self) -> str:
        s = self.snapshot()
        lines = [
            "=== OXYGEN Automation Engine ===",
            f"Connection : {s.get('connection')}",
            f"State      : {s.get('engine_state')}",
            f"Armed      : {s.get('software_armed')}",
            f"Recording  : {s.get('recording')}",
            f"Profile    : {s.get('active_profile') or '-'}",
            f"Step       : {s.get('current_step') or '-'}",
            f"Last cmd   : {s.get('last_command') or '-'}",
            f"SCPI error : {s.get('last_scpi_error') or '-'}",
            f"DO values  : {s.get('do_values')}",
            f"AOUT       : {s.get('aout_values')}",
            "================================",
        ]
        return "\n".join(lines)

    def print(self) -> None:
        if not self.enabled:
            return
        text = self.render()
        if sys.stdout.isatty():
            # Move up / clear roughly — simple full redraw
            sys.stdout.write("\033[H\033[J")
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        else:
            print(text, flush=True)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


class DashboardRefresher:
    def __init__(self, dashboard: Dashboard, interval_s: float = 0.5) -> None:
        self.dashboard = dashboard
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.dashboard.enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dashboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.dashboard.print()
            self._stop.wait(self.interval_s)
