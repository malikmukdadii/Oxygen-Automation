"""AOUT configuration reads and gated constant-output control."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .adapters.oxygen import OxygenAdapter
from .channel_manager import ChannelError, ChannelManager
from .config import AoutRole, EngineConfig, LiveControlPermissions

logger = logging.getLogger(__name__)


class AoutError(Exception):
    """AOUT validation or control failure."""


@dataclass
class AoutSnapshot:
    logical_name: str
    oxygen_name: str
    channel_id: str
    mode: str | None
    range_value: str | None
    const_value: float | None
    waveform_lookups: dict[str, str]


class AoutController:
    """
    Read AOUT configuration; write only when live control is explicitly enabled
    and command mappings are marked verified.
    """

    KNOWN_MODES = ("ConstOutput", "FunctionGenerator", "StreamOutput")

    def __init__(
        self,
        adapter: OxygenAdapter,
        channels: ChannelManager,
        config: EngineConfig,
        *,
        simulation: bool = True,
    ) -> None:
        self.adapter = adapter
        self.channels = channels
        self.config = config
        self.simulation = simulation
        self._sim_values: dict[str, float] = {
            role.logical_name: role.neutral_value for role in config.aout_roles
        }
        self._roles = {r.logical_name: r for r in config.aout_roles}

    def _perms(self) -> LiveControlPermissions:
        return self.config.live_control

    def _role(self, logical_name: str) -> AoutRole:
        if logical_name not in self._roles:
            raise AoutError(f"Unknown AOUT role: {logical_name}")
        return self._roles[logical_name]

    def _channel_id(self, role: AoutRole) -> str:
        try:
            return self.channels.get(role.logical_name).channel_id
        except ChannelError:
            return self.channels.get_by_oxygen_name(role.oxygen_name).channel_id

    def read_snapshot(self, logical_name: str) -> AoutSnapshot:
        role = self._role(logical_name)
        cid = self._channel_id(role)
        mode = range_value = None
        const_value: float | None = None
        lookups: dict[str, str] = {}
        if not self.config.connection.use_mock:
            logger.warning('AOUT property mapping unverified; use scpi-smoke --aout for exact attribute names')
            return AoutSnapshot(logical_name, role.oxygen_name, cid, None, None, None, {})
        try:
            mode = self.adapter.get_property(cid, role.mode_property)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AOUT mode read failed: %s", exc)
        try:
            range_value = self.adapter.get_property(cid, role.range_property)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AOUT range read failed: %s", exc)
        try:
            raw = self.adapter.get_property(cid, role.const_property)
            const_value = float(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AOUT const read failed: %s", exc)
            if self.simulation:
                const_value = self._sim_values.get(logical_name)
        for i in range(4):
            prop = f"TRION/FGenProperties/AoWaveformLookup{i}"
            try:
                lookups[prop] = self.adapter.get_property(cid, prop)
            except Exception:  # noqa: BLE001
                lookups[prop] = ""
        return AoutSnapshot(
            logical_name=logical_name,
            oxygen_name=role.oxygen_name,
            channel_id=cid,
            mode=mode,
            range_value=range_value,
            const_value=const_value,
            waveform_lookups=lookups,
        )

    def validate_setpoint(self, logical_name: str, value: float) -> None:
        role = self._role(logical_name)
        if value < role.min_value or value > role.max_value:
            raise AoutError(
                f"Setpoint {value} outside approved range "
                f"[{role.min_value}, {role.max_value}] for {logical_name}"
            )

    def _live_writes_allowed(self) -> bool:
        p = self._perms()
        return (
            not self.simulation
            and p.enable_live_aout
            and p.aout_commands_verified
        )

    def set_constant(self, logical_name: str, value: float, *, dry_run: bool = False) -> dict[str, Any]:
        self.validate_setpoint(logical_name, value)
        role = self._role(logical_name)
        result: dict[str, Any] = {
            "logical_name": logical_name,
            "value": value,
            "dry_run": dry_run,
            "simulated": False,
            "written": False,
        }
        if dry_run or self.simulation or not self._live_writes_allowed():
            self._sim_values[logical_name] = value
            result["simulated"] = True
            result["written"] = False
            result["message"] = (
                "Simulated AOUT setpoint (live AOUT disabled or unverified)"
            )
            logger.info("AOUT sim set %s=%s", logical_name, value)
            return result

        cid = self._channel_id(role)
        self.adapter.set_property(cid, role.mode_property, "ConstOutput")
        self.adapter.set_property(cid, role.const_property, str(value))
        result["written"] = True
        result["message"] = "Live AOUT constant written"
        logger.info("AOUT live set %s=%s", logical_name, value)
        return result

    def ramp(
        self,
        logical_name: str,
        target: float,
        rate_per_s: float,
        *,
        dry_run: bool = False,
        cancel_check: Callable[[], bool] | None = None,
        step_interval_s: float = 0.05,
    ) -> dict[str, Any]:
        self.validate_setpoint(logical_name, target)
        if rate_per_s <= 0:
            raise AoutError("Ramp rate must be > 0")
        snap = self.read_snapshot(logical_name)
        current = snap.const_value
        if current is None:
            current = self._sim_values.get(logical_name, self._role(logical_name).neutral_value)
        direction = 1.0 if target >= current else -1.0
        steps_log: list[float] = []
        value = float(current)
        while abs(target - value) > 1e-9:
            if cancel_check and cancel_check():
                return {
                    "logical_name": logical_name,
                    "cancelled": True,
                    "final_value": value,
                    "steps": steps_log,
                }
            delta = direction * rate_per_s * step_interval_s
            if abs(delta) >= abs(target - value):
                value = target
            else:
                value += delta
            self.set_constant(logical_name, value, dry_run=dry_run)
            steps_log.append(value)
            if not dry_run:
                time.sleep(step_interval_s)
        return {
            "logical_name": logical_name,
            "cancelled": False,
            "final_value": value,
            "steps": steps_log,
        }

    def return_to_neutral(self, logical_name: str, *, dry_run: bool = False) -> dict[str, Any]:
        role = self._role(logical_name)
        return self.set_constant(logical_name, role.neutral_value, dry_run=dry_run)

    def discover_generator_params(self, logical_name: str) -> dict[str, Any]:
        snap = self.read_snapshot(logical_name)
        return {
            "mode": snap.mode,
            "range": snap.range_value,
            "waveform_lookups": snap.waveform_lookups,
            "known_modes": list(self.KNOWN_MODES),
            "note": "Waveform selection uses existing OXYGEN lookup slots when supported.",
        }

    def select_waveform(self, logical_name: str, slot: int, profile_name: str, *, dry_run: bool = False) -> dict:
        if slot < 0 or slot > 3:
            raise AoutError("Waveform slot must be 0..3")
        role = self._role(logical_name)
        prop = f"TRION/FGenProperties/AoWaveformLookup{slot}"
        if dry_run or self.simulation or not self._live_writes_allowed():
            return {
                "simulated": True,
                "property": prop,
                "value": profile_name,
                "message": "Simulated waveform selection",
            }
        cid = self._channel_id(role)
        self.adapter.set_property(cid, prop, profile_name)
        self.adapter.set_property(cid, role.mode_property, "FunctionGenerator")
        return {"simulated": False, "property": prop, "value": profile_name, "written": True}

    def latest_values(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name in self._roles:
            try:
                snap = self.read_snapshot(name)
                if snap.const_value is not None:
                    out[name] = snap.const_value
                    continue
            except Exception:  # noqa: BLE001
                pass
            out[name] = self._sim_values.get(name, 0.0)
        return out
