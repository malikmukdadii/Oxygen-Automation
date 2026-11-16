"""Configuration loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


@dataclass
class ConnectionConfig:
    host: str = "127.0.0.1"
    port: int = 10001
    timeout_s: float = 2.0
    reconnect_min_s: float = 0.5
    reconnect_max_s: float = 8.0
    reconnect_max_attempts: int = 10
    use_mock: bool = True
    mock_port: int = 0  # 0 = auto-bind ephemeral


@dataclass
class ChannelMapping:
    """Logical name -> optional known channel ID (discoverable at runtime)."""

    logical_name: str
    oxygen_name: str
    channel_id: str | None = None
    role: str = "none"  # none | vesc | rzxa | measurement | digital_output | aout


@dataclass
class DOCommandAssignment:
    logical_name: str
    oxygen_name: str
    command: str
    edge: str = "rising"  # rising | falling
    channel_id: str | None = None
    # Unverified until hardware probe confirms the property name.
    state_property: str | None = None


@dataclass
class AoutRole:
    logical_name: str
    oxygen_name: str
    channel_id: str | None = None
    role: str = "none"  # none | vesc | rzxa | other
    min_value: float = -10.0
    max_value: float = 10.0
    neutral_value: float = 0.0
    const_property: str = "TRION/SourceChannel_A_CONST/Const0"
    mode_property: str = "Mode"
    range_property: str = "Range"


@dataclass
class LiveControlPermissions:
    """Live actuator control is disabled until explicitly enabled and verified."""

    enable_live_aout: bool = False
    enable_live_do_reset: bool = False
    enable_real_rzxa: bool = False
    aout_commands_verified: bool = False
    do_property_verified: bool = False
    rzxa_commands_verified: bool = False


@dataclass
class RecordingConfig:
    auto_start_on_profile: bool = False
    marker_prefix: str = "AUTO"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    jsonl_dir: str = "logs"
    console_dashboard: bool = True
    diagnostic_poll_logging: bool = False


@dataclass
class InstrumentConfig:
    rzxa: dict[str, Any] = field(default_factory=lambda: {
        "host": "127.0.0.1",
        "port": 5025,
        "use_mock": True,
        "voltage_min": 0.0,
        "voltage_max": 80.0,
        "current_limit_default": 10.0,
    })


@dataclass
class EngineConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    polling_interval_ms: int = 250
    debounce_ms: int = 50
    channels: list[ChannelMapping] = field(default_factory=list)
    do_commands: list[DOCommandAssignment] = field(default_factory=list)
    aout_roles: list[AoutRole] = field(default_factory=list)
    live_control: LiveControlPermissions = field(default_factory=LiveControlPermissions)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    instruments: InstrumentConfig = field(default_factory=InstrumentConfig)
    profiles_dir: str = "profiles"
    software_armed_default: bool = False
    do_state_property_candidates: list[str] = field(
        default_factory=lambda: [
            "Value",
            "State",
            "DigitalValue",
            "TRION/DigitalOut/Value",
            "Output",
        ]
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require(mapping: dict[str, Any], key: str, typ: type) -> Any:
    if key not in mapping:
        raise ConfigError(f"Missing required config key: {key}")
    value = mapping[key]
    if not isinstance(value, typ):
        raise ConfigError(f"Config key '{key}' must be {typ.__name__}, got {type(value).__name__}")
    return value


def _opt(mapping: dict[str, Any], key: str, default: Any) -> Any:
    return mapping[key] if key in mapping else default


def load_config(path: str | Path) -> EngineConfig:
    """Load and validate a JSON configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a JSON object")
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> EngineConfig:
    """Validate and parse a config dictionary."""
    conn_raw = _opt(raw, "connection", {})
    if not isinstance(conn_raw, dict):
        raise ConfigError("'connection' must be an object")
    connection = ConnectionConfig(
        host=str(_opt(conn_raw, "host", "127.0.0.1")),
        port=int(_opt(conn_raw, "port", 10001)),
        timeout_s=float(_opt(conn_raw, "timeout_s", 2.0)),
        reconnect_min_s=float(_opt(conn_raw, "reconnect_min_s", 0.5)),
        reconnect_max_s=float(_opt(conn_raw, "reconnect_max_s", 8.0)),
        reconnect_max_attempts=int(_opt(conn_raw, "reconnect_max_attempts", 10)),
        use_mock=bool(_opt(conn_raw, "use_mock", True)),
        mock_port=int(_opt(conn_raw, "mock_port", 0)),
    )

    channels: list[ChannelMapping] = []
    for item in _opt(raw, "channels", []):
        if not isinstance(item, dict):
            raise ConfigError("Each channel mapping must be an object")
        channels.append(
            ChannelMapping(
                logical_name=str(_require(item, "logical_name", str)),
                oxygen_name=str(_require(item, "oxygen_name", str)),
                channel_id=item.get("channel_id"),
                role=str(_opt(item, "role", "none")),
            )
        )

    do_commands: list[DOCommandAssignment] = []
    for item in _opt(raw, "do_commands", []):
        if not isinstance(item, dict):
            raise ConfigError("Each do_commands entry must be an object")
        edge = str(_opt(item, "edge", "rising")).lower()
        if edge not in {"rising", "falling"}:
            raise ConfigError(f"Invalid DO edge '{edge}'")
        do_commands.append(
            DOCommandAssignment(
                logical_name=str(_require(item, "logical_name", str)),
                oxygen_name=str(_require(item, "oxygen_name", str)),
                command=str(_require(item, "command", str)),
                edge=edge,
                channel_id=item.get("channel_id"),
                state_property=item.get("state_property"),
            )
        )

    aout_roles: list[AoutRole] = []
    for item in _opt(raw, "aout_roles", []):
        if not isinstance(item, dict):
            raise ConfigError("Each aout_roles entry must be an object")
        role = str(_opt(item, "role", "none"))
        if role not in {"none", "vesc", "rzxa", "other"}:
            raise ConfigError(f"Invalid AOUT role '{role}' — operator must set explicitly")
        min_v = float(_opt(item, "min_value", -10.0))
        max_v = float(_opt(item, "max_value", 10.0))
        if min_v > max_v:
            raise ConfigError(f"AOUT min_value > max_value for {item.get('logical_name')}")
        neutral = float(_opt(item, "neutral_value", 0.0))
        if not (min_v <= neutral <= max_v):
            raise ConfigError(f"AOUT neutral_value out of range for {item.get('logical_name')}")
        aout_roles.append(
            AoutRole(
                logical_name=str(_require(item, "logical_name", str)),
                oxygen_name=str(_require(item, "oxygen_name", str)),
                channel_id=item.get("channel_id"),
                role=role,
                min_value=min_v,
                max_value=max_v,
                neutral_value=neutral,
                const_property=str(
                    _opt(item, "const_property", "TRION/SourceChannel_A_CONST/Const0")
                ),
                mode_property=str(_opt(item, "mode_property", "Mode")),
                range_property=str(_opt(item, "range_property", "Range")),
            )
        )

    live_raw = _opt(raw, "live_control", {})
    if not isinstance(live_raw, dict):
        raise ConfigError("'live_control' must be an object")
    live_control = LiveControlPermissions(
        enable_live_aout=bool(_opt(live_raw, "enable_live_aout", False)),
        enable_live_do_reset=bool(_opt(live_raw, "enable_live_do_reset", False)),
        enable_real_rzxa=bool(_opt(live_raw, "enable_real_rzxa", False)),
        aout_commands_verified=bool(_opt(live_raw, "aout_commands_verified", False)),
        do_property_verified=bool(_opt(live_raw, "do_property_verified", False)),
        rzxa_commands_verified=bool(_opt(live_raw, "rzxa_commands_verified", False)),
    )

    rec_raw = _opt(raw, "recording", {})
    recording = RecordingConfig(
        auto_start_on_profile=bool(_opt(rec_raw, "auto_start_on_profile", False)),
        marker_prefix=str(_opt(rec_raw, "marker_prefix", "AUTO")),
    )

    log_raw = _opt(raw, "logging", {})
    logging_cfg = LoggingConfig(
        level=str(_opt(log_raw, "level", "INFO")),
        jsonl_dir=str(_opt(log_raw, "jsonl_dir", "logs")),
        console_dashboard=bool(_opt(log_raw, "console_dashboard", True)),
        diagnostic_poll_logging=bool(_opt(log_raw, "diagnostic_poll_logging", False)),
    )

    inst_raw = _opt(raw, "instruments", {})
    instruments = InstrumentConfig(rzxa=dict(_opt(inst_raw, "rzxa", InstrumentConfig().rzxa)))

    polling = int(_opt(raw, "polling_interval_ms", 250))
    if polling < 10:
        raise ConfigError("polling_interval_ms must be >= 10")

    return EngineConfig(
        connection=connection,
        polling_interval_ms=polling,
        debounce_ms=int(_opt(raw, "debounce_ms", 50)),
        channels=channels,
        do_commands=do_commands,
        aout_roles=aout_roles,
        live_control=live_control,
        recording=recording,
        logging=logging_cfg,
        instruments=instruments,
        profiles_dir=str(_opt(raw, "profiles_dir", "profiles")),
        software_armed_default=bool(_opt(raw, "software_armed_default", False)),
        do_state_property_candidates=list(
            _opt(
                raw,
                "do_state_property_candidates",
                [
                    "Value",
                    "State",
                    "DigitalValue",
                    "TRION/DigitalOut/Value",
                    "Output",
                ],
            )
        ),
    )


def default_example_config() -> dict[str, Any]:
    """Return the example configuration dictionary."""
    return {
        "connection": {
            "host": "127.0.0.1",
            "port": 10001,
            "timeout_s": 2.0,
            "reconnect_min_s": 0.5,
            "reconnect_max_s": 8.0,
            "reconnect_max_attempts": 10,
            "use_mock": True,
            "mock_port": 0,
        },
        "polling_interval_ms": 250,
        "debounce_ms": 50,
        "software_armed_default": False,
        "profiles_dir": "profiles",
        "do_state_property_candidates": [
            "Value",
            "State",
            "DigitalValue",
            "TRION/DigitalOut/Value",
            "Output",
        ],
        "channels": [
            {
                "logical_name": "aout1",
                "oxygen_name": "Test Aout1",
                "channel_id": "4152600322822373457",
                "role": "aout",
            },
            {
                "logical_name": "aout2",
                "oxygen_name": "Test Aout2",
                "channel_id": "4152600322822373458",
                "role": "aout",
            },
            {
                "logical_name": "dc_power",
                "oxygen_name": "DC Power",
                "role": "measurement",
            },
            {
                "logical_name": "ac_power",
                "oxygen_name": "AC Power",
                "role": "measurement",
            },
            {
                "logical_name": "rpm",
                "oxygen_name": "RPM",
                "role": "measurement",
            },
            {
                "logical_name": "energy",
                "oxygen_name": "Energy",
                "role": "measurement",
            },
        ],
        "do_commands": [
            {
                "logical_name": "do_3_1",
                "oxygen_name": "DO 3/1 Sim",
                "command": "start_city",
                "edge": "rising",
                "state_property": "Value",
            },
            {
                "logical_name": "do_3_2",
                "oxygen_name": "DO 3/2 Sim",
                "command": "start_regen",
                "edge": "rising",
                "state_property": "Value",
            },
            {
                "logical_name": "do_3_3",
                "oxygen_name": "DO 3/3 Sim",
                "command": "start_voltage_sweep",
                "edge": "rising",
                "state_property": "Value",
            },
            {
                "logical_name": "do_3_4",
                "oxygen_name": "DO 3/4 Sim",
                "command": "cancel",
                "edge": "rising",
                "state_property": "Value",
            },
            {
                "logical_name": "do_3_5",
                "oxygen_name": "DO 3/5 Sim",
                "command": "toggle_recording",
                "edge": "rising",
                "state_property": "Value",
            },
            {
                "logical_name": "do_3_6",
                "oxygen_name": "DO 3/6 Sim",
                "command": "toggle_arm",
                "edge": "rising",
                "state_property": "Value",
            },
        ],
        "aout_roles": [
            {
                "logical_name": "vesc_throttle",
                "oxygen_name": "Test Aout1",
                "channel_id": "4152600322822373457",
                "role": "vesc",
                "min_value": 0.0,
                "max_value": 5.0,
                "neutral_value": 0.0,
                "const_property": "TRION/SourceChannel_A_CONST/Const0",
                "mode_property": "Mode",
                "range_property": "Range",
            },
            {
                "logical_name": "aux_aout",
                "oxygen_name": "Test Aout2",
                "channel_id": "4152600322822373458",
                "role": "none",
                "min_value": -10.0,
                "max_value": 10.0,
                "neutral_value": 0.0,
            },
        ],
        "live_control": {
            "enable_live_aout": False,
            "enable_live_do_reset": False,
            "enable_real_rzxa": False,
            "aout_commands_verified": False,
            "do_property_verified": False,
            "rzxa_commands_verified": False,
        },
        "recording": {
            "auto_start_on_profile": False,
            "marker_prefix": "AUTO",
        },
        "logging": {
            "level": "INFO",
            "jsonl_dir": "logs",
            "console_dashboard": True,
            "diagnostic_poll_logging": False,
        },
        "instruments": {
            "rzxa": {
                "host": "127.0.0.1",
                "port": 5025,
                "use_mock": True,
                "voltage_min": 0.0,
                "voltage_max": 80.0,
                "current_limit_default": 10.0,
            }
        },
    }
