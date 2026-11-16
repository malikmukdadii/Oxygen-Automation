"""Data-driven JSON test profiles and step execution."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ProfileError(Exception):
    """Profile load or execution failure."""


STEP_TYPES = {
    "set_aout",
    "ramp_aout",
    "hold",
    "request_waveform",
    "start_recording",
    "stop_recording",
    "query_measurements",
    "wait_threshold",
    "add_marker",
    "instrument_op",
    "return_neutral",
}


@dataclass
class ProfileStep:
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class Profile:
    name: str
    description: str
    steps: list[ProfileStep]
    timeout_s: float = 300.0
    expected_conditions: dict[str, Any] = field(default_factory=dict)
    recording: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None


def load_profile(path: str | Path) -> Profile:
    profile_path = Path(path)
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    return parse_profile(raw, source_path=str(profile_path))


def parse_profile(raw: dict[str, Any], source_path: str | None = None) -> Profile:
    if not isinstance(raw, dict):
        raise ProfileError("Profile root must be an object")
    name = raw.get("name")
    if not name:
        raise ProfileError("Profile missing 'name'")
    steps_raw = raw.get("steps", [])
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ProfileError(f"Profile {name} must contain a non-empty steps list")
    steps: list[ProfileStep] = []
    for i, item in enumerate(steps_raw):
        if not isinstance(item, dict):
            raise ProfileError(f"Step {i} must be an object")
        stype = item.get("type")
        if stype not in STEP_TYPES:
            raise ProfileError(f"Step {i} has unsupported type: {stype}")
        steps.append(
            ProfileStep(
                type=stype,
                params=dict(item.get("params", {})),
                description=str(item.get("description", "")),
            )
        )
    return Profile(
        name=str(name),
        description=str(raw.get("description", "")),
        steps=steps,
        timeout_s=float(raw.get("timeout_s", 300.0)),
        expected_conditions=dict(raw.get("expected_conditions", {})),
        recording=dict(raw.get("recording", {})),
        source_path=source_path,
    )


def list_profiles(profiles_dir: str | Path) -> list[Profile]:
    directory = Path(profiles_dir)
    if not directory.exists():
        return []
    profiles: list[Profile] = []
    for path in sorted(directory.glob("*.json")):
        try:
            profiles.append(load_profile(path))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load profile %s: %s", path, exc)
    return profiles


class ProfileRunner:
    """Execute profile steps against engine services."""

    def __init__(
        self,
        *,
        aout,
        oxygen,
        channels,
        rzxa,
        dry_run: bool = True,
        event_log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.aout = aout
        self.oxygen = oxygen
        self.channels = channels
        self.rzxa = rzxa
        self.dry_run = dry_run
        self.event_log = event_log or (lambda _e: None)
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    @property
    def cancelled(self) -> bool:
        return self._cancel

    def run(self, profile: Profile) -> dict[str, Any]:
        self._cancel = False
        started = time.time()
        step_results: list[dict[str, Any]] = []
        self.event_log(
            {
                "event": "profile_start",
                "profile": profile.name,
                "dry_run": self.dry_run,
                "steps": len(profile.steps),
            }
        )
        try:
            for index, step in enumerate(profile.steps):
                if self._cancel:
                    self.event_log({"event": "profile_cancelled", "profile": profile.name, "step": index})
                    return {
                        "status": "cancelled",
                        "profile": profile.name,
                        "completed_steps": step_results,
                        "elapsed_s": time.time() - started,
                    }
                if time.time() - started > profile.timeout_s:
                    raise ProfileError(f"Profile {profile.name} timed out after {profile.timeout_s}s")
                result = self._run_step(index, step)
                step_results.append(result)
                self.event_log(
                    {
                        "event": "profile_step",
                        "profile": profile.name,
                        "step": index,
                        "type": step.type,
                        "result": result,
                    }
                )
            self.event_log({"event": "profile_complete", "profile": profile.name})
            return {
                "status": "ok",
                "profile": profile.name,
                "completed_steps": step_results,
                "elapsed_s": time.time() - started,
            }
        except Exception as exc:
            self.event_log(
                {
                    "event": "profile_fault",
                    "profile": profile.name,
                    "error": str(exc),
                }
            )
            return {
                "status": "fault",
                "profile": profile.name,
                "error": str(exc),
                "completed_steps": step_results,
                "elapsed_s": time.time() - started,
            }

    def _run_step(self, index: int, step: ProfileStep) -> dict[str, Any]:
        p = step.params
        logger.info("Profile step %s: %s %s", index, step.type, step.description or "")
        if step.type == "set_aout":
            return self.aout.set_constant(p["channel"], float(p["value"]), dry_run=self.dry_run)
        if step.type == "ramp_aout":
            return self.aout.ramp(
                p["channel"],
                float(p["target"]),
                float(p.get("rate_per_s", 1.0)),
                dry_run=self.dry_run,
                cancel_check=lambda: self._cancel,
                step_interval_s=float(p.get("step_interval_s", 0.02)),
            )
        if step.type == "hold":
            duration = float(p.get("duration_s", 1.0))
            end = time.time() + duration
            while time.time() < end:
                if self._cancel:
                    return {"type": "hold", "cancelled": True}
                time.sleep(min(0.05, end - time.time()))
            return {"type": "hold", "duration_s": duration}
        if step.type == "request_waveform":
            return self.aout.select_waveform(
                p["channel"], int(p.get("slot", 0)), str(p.get("profile", "")), dry_run=self.dry_run
            )
        if step.type == "start_recording":
            if not self.dry_run:
                self.oxygen.start_recording()
            return {"type": "start_recording", "dry_run": self.dry_run}
        if step.type == "stop_recording":
            if not self.dry_run:
                self.oxygen.stop_recording()
            return {"type": "stop_recording", "dry_run": self.dry_run}
        if step.type == "query_measurements":
            names = list(p.get("channels", []))
            values: dict[str, float] = {}
            for name in names:
                try:
                    ch = self.channels.get(name)
                except Exception:
                    ch = self.channels.get_by_oxygen_name(name)
                if self.dry_run:
                    # Still query mock if connected — useful logs.
                    try:
                        values[name] = self.oxygen.read_measurement(ch.channel_id)
                    except Exception:
                        values[name] = float("nan")
                else:
                    values[name] = self.oxygen.read_measurement(ch.channel_id)
            return {"type": "query_measurements", "values": values}
        if step.type == "wait_threshold":
            return self._wait_threshold(p)
        if step.type == "add_marker":
            text = str(p.get("text", "marker"))
            if not self.dry_run:
                self.oxygen.add_marker(text)
            return {"type": "add_marker", "text": text, "dry_run": self.dry_run}
        if step.type == "instrument_op":
            return self._instrument_op(p)
        if step.type == "return_neutral":
            return self.aout.return_to_neutral(p["channel"], dry_run=self.dry_run)
        raise ProfileError(f"Unhandled step type: {step.type}")

    def _wait_threshold(self, p: dict[str, Any]) -> dict[str, Any]:
        name = p["channel"]
        op = p.get("op", ">=")
        threshold = float(p["value"])
        timeout_s = float(p.get("timeout_s", 30.0))
        poll_s = float(p.get("poll_s", 0.1))
        try:
            ch = self.channels.get(name)
        except Exception:
            ch = self.channels.get_by_oxygen_name(name)
        deadline = time.time() + timeout_s
        last = float("nan")
        while time.time() < deadline:
            if self._cancel:
                return {"type": "wait_threshold", "cancelled": True, "last": last}
            last = self.oxygen.read_measurement(ch.channel_id)
            if self._compare(last, op, threshold):
                return {"type": "wait_threshold", "satisfied": True, "value": last}
            time.sleep(poll_s)
        raise ProfileError(
            f"Threshold not met for {name}: last={last}, wanted {op} {threshold}"
        )

    @staticmethod
    def _compare(value: float, op: str, threshold: float) -> bool:
        if op == ">=":
            return value >= threshold
        if op == "<=":
            return value <= threshold
        if op == ">":
            return value > threshold
        if op == "<":
            return value < threshold
        if op == "==":
            return abs(value - threshold) < 1e-6
        raise ProfileError(f"Unsupported comparison op: {op}")

    def _instrument_op(self, p: dict[str, Any]) -> dict[str, Any]:
        instrument = p.get("instrument", "rzxa")
        op = p.get("op")
        if instrument != "rzxa":
            raise ProfileError(f"Unknown instrument: {instrument}")
        if op == "set_voltage":
            self.rzxa.set_voltage(float(p["value"]))
        elif op == "set_output":
            self.rzxa.set_output(bool(p["value"]))
        elif op == "set_current_limit":
            self.rzxa.set_current_limit(float(p["value"]))
        elif op == "query":
            return {
                "type": "instrument_op",
                "instrument": "rzxa",
                "voltage": self.rzxa.query_voltage(),
                "output": self.rzxa.query_output(),
                "current_limit": self.rzxa.query_current_limit(),
            }
        else:
            raise ProfileError(f"Unknown RZ-XA op: {op}")
        return {"type": "instrument_op", "instrument": "rzxa", "op": op, "params": p}
