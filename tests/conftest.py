"""Shared fixtures for OXYGEN Automation Engine tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oxygen_engine.adapters.mock import MockOxygenServer
from oxygen_engine.config import parse_config
from oxygen_engine.engine import AutomationEngine
from oxygen_engine.logging_setup import Dashboard, JsonlEventLogger


@pytest.fixture
def project_root() -> Path:
    return ROOT


@pytest.fixture
def example_config_dict(project_root: Path) -> dict:
    path = project_root / "examples" / "legacy-engine-config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["profiles_dir"] = str(project_root / "profiles")
    raw["logging"] = {
        **raw.get("logging", {}),
        "jsonl_dir": str(project_root / "logs" / "test"),
        "console_dashboard": False,
    }
    raw["connection"]["use_mock"] = True
    raw["polling_interval_ms"] = 50
    raw["debounce_ms"] = 10
    return raw


@pytest.fixture
def mock_server() -> MockOxygenServer:
    server = MockOxygenServer(host="127.0.0.1", port=0)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def engine(example_config_dict, mock_server, tmp_path) -> AutomationEngine:
    example_config_dict["logging"]["jsonl_dir"] = str(tmp_path / "logs")
    example_config_dict["connection"]["mock_port"] = mock_server.bound_port
    # Engine will use provided mock_server rather than starting its own
    config = parse_config(example_config_dict)
    # Force connection to the running mock
    config.connection.use_mock = True
    eng = AutomationEngine(
        config,
        dry_run=True,
        event_logger=JsonlEventLogger(tmp_path / "logs"),
        dashboard=Dashboard(enabled=False),
        mock_server=mock_server,
    )
    # Point transport at mock without owning/stopping it incorrectly:
    # start() will see use_mock and existing mock_server
    result = eng.start()
    assert result.status == "ok", result
    yield eng
    eng.stop()
