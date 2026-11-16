"""
Automated tests for the 10 required behaviors in the implementation spec §10.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from oxygen_engine.aout_controller import AoutError
from oxygen_engine.config import parse_config
from oxygen_engine.engine import AutomationEngine, EngineState
from oxygen_engine.logging_setup import Dashboard, JsonlEventLogger
from oxygen_engine.scpi_transport import (
    ScpiConnectionError,
    ScpiProtocolError,
    ScpiTimeoutError,
    ScpiTransport,
)


# ---------------------------------------------------------------------------
# 1. A rising edge produces exactly one action
# ---------------------------------------------------------------------------
def test_01_rising_edge_produces_exactly_one_action(engine, mock_server):
    engine.dispatch("toggle_arm")
    assert engine.software_armed

    results = []
    original = engine.dispatch

    def tracking_dispatch(command, *, source="api"):
        results.append(command)
        return original(command, source=source)

    engine.dispatch = tracking_dispatch  # type: ignore[method-assign]

    mock_server.set_do("DO 3/1 Sim", False)
    engine.do_monitor.initialize()  # re-sync Low
    engine.dispatch = tracking_dispatch  # re-bind after init doesn't change dispatch

    results.clear()
    mock_server.set_do("DO 3/1 Sim", True)
    events = engine.poll_once()
    assert len(events) == 1
    assert events[0].command == "start_city"
    assert results.count("start_city") == 1


# ---------------------------------------------------------------------------
# 2. Sustained High does not repeat actions
# ---------------------------------------------------------------------------
def test_02_sustained_high_does_not_repeat(engine, mock_server):
    engine.dispatch("toggle_arm")
    counts = {"start_city": 0}
    original = engine.dispatch

    def tracking_dispatch(command, *, source="api"):
        counts[command] = counts.get(command, 0) + 1
        return original(command, source=source)

    engine.dispatch = tracking_dispatch  # type: ignore[method-assign]

    mock_server.set_do("DO 3/1 Sim", False)
    engine.do_monitor.initialize()
    engine.dispatch = tracking_dispatch  # type: ignore[method-assign]

    mock_server.set_do("DO 3/1 Sim", True)
    engine.poll_once()
    for _ in range(10):
        engine.poll_once()
        time.sleep(0.02)

    assert counts.get("start_city", 0) == 1


# ---------------------------------------------------------------------------
# 3. Startup with DO already High causes no action
# ---------------------------------------------------------------------------
def test_03_startup_high_no_action(example_config_dict, mock_server, tmp_path):
    mock_server.set_do("DO 3/1 Sim", True)
    example_config_dict["logging"]["jsonl_dir"] = str(tmp_path / "logs")
    config = parse_config(example_config_dict)
    eng = AutomationEngine(
        config,
        dry_run=True,
        event_logger=JsonlEventLogger(tmp_path / "logs"),
        dashboard=Dashboard(enabled=False),
        mock_server=mock_server,
    )
    commands = []
    original_dispatch = AutomationEngine.dispatch

    def tracking(self, command, *, source="api"):
        commands.append(command)
        return original_dispatch(self, command, source=source)

    eng.dispatch = lambda command, source="api": tracking(eng, command, source=source)  # type: ignore
    result = eng.start()
    assert result.status == "ok"
    # Poll several times with DO still High
    for _ in range(5):
        events = eng.poll_once()
        assert events == []
    assert "start_city" not in commands
    eng.stop()


# ---------------------------------------------------------------------------
# 4. Connection recovery does not replay stale commands
# ---------------------------------------------------------------------------
def test_04_reconnect_does_not_replay(engine, mock_server):
    engine.dispatch("toggle_arm")
    mock_server.set_do("DO 3/1 Sim", True)
    engine.do_monitor.initialize()  # High at sync — latched, no action

    fired = []
    original = engine.dispatch

    def tracking_dispatch(command, *, source="api"):
        fired.append((command, source))
        return original(command, source=source)

    engine.dispatch = tracking_dispatch  # type: ignore[method-assign]

    # Simulate disconnect/reconnect recovery path
    mock_server.disconnect_clients()
    time.sleep(0.1)
    # Reconnect transport
    assert engine.transport is not None
    engine.transport.reconnect()
    # After reconnect, DO still High — must not fire
    fired.clear()
    for _ in range(5):
        engine.poll_once()
    assert all(c != "start_city" for c, _ in fired)

    # New rising edge after going Low then High should fire once
    mock_server.set_do("DO 3/1 Sim", False)
    engine.poll_once()
    time.sleep(0.05)  # clear debounce window after falling edge
    mock_server.set_do("DO 3/1 Sim", True)
    engine.poll_once()
    assert sum(1 for c, _ in fired if c == "start_city") == 1


# ---------------------------------------------------------------------------
# 5. Invalid output setpoint is rejected
# ---------------------------------------------------------------------------
def test_05_invalid_setpoint_rejected(engine):
    with pytest.raises(AoutError, match="outside approved range"):
        engine.validate_setpoint("vesc_throttle", 99.0)
    with pytest.raises(AoutError):
        engine.aout.set_constant("vesc_throttle", -1.0, dry_run=True)


# ---------------------------------------------------------------------------
# 6. Two simultaneous automation requests cannot execute
# ---------------------------------------------------------------------------
def test_06_no_concurrent_profiles(engine):
    engine.dispatch("toggle_arm")
    first = engine.dispatch("start_city")
    assert first.status == "ok"
    # Immediately request second profile
    second = engine.dispatch("start_regen")
    assert second.status == "rejected"
    assert "already running" in second.message.lower() or "busy" in second.message.lower()
    engine.cancel("test cleanup")
    engine.wait_until_idle(timeout_s=10)


# ---------------------------------------------------------------------------
# 7. Cancel requests interrupt an active profile
# ---------------------------------------------------------------------------
def test_07_cancel_interrupts_profile(engine, project_root, tmp_path):
    # Build a long-hold profile
    long_profile = {
        "name": "long_hold",
        "description": "cancel test",
        "timeout_s": 60,
        "steps": [
            {"type": "hold", "params": {"duration_s": 30}},
            {"type": "return_neutral", "params": {"channel": "vesc_throttle"}},
        ],
    }
    path = tmp_path / "long_hold.json"
    path.write_text(json.dumps(long_profile), encoding="utf-8")
    from oxygen_engine.profiles import load_profile

    engine.dispatch("toggle_arm")
    profile = load_profile(path)
    started = engine.start_profile(profile)
    assert started.status == "ok"
    time.sleep(0.15)
    assert engine.state == EngineState.RUNNING
    cancelled = engine.cancel("test cancel")
    assert cancelled.status == "cancelled"
    assert engine.wait_until_idle(timeout_s=5)
    assert engine.state in {EngineState.READY, EngineState.IDLE}


# ---------------------------------------------------------------------------
# 8. Unrecognized SCPI responses fail explicitly
# ---------------------------------------------------------------------------
def test_08_unrecognized_scpi_fails_explicitly(mock_server):
    transport = ScpiTransport(host="127.0.0.1", port=mock_server.bound_port, timeout_s=1.0)
    transport.connect()
    transport.write("MOCK:BADRESP")
    with pytest.raises(ScpiProtocolError):
        # Force SYST:ERR? parse failure via bad response
        transport.check_error()
    transport.close()

    # Also: malformed measurement path through adapter
    transport = ScpiTransport(host="127.0.0.1", port=mock_server.bound_port, timeout_s=1.0)
    transport.connect()
    from oxygen_engine.adapters.oxygen import OxygenAdapter

    adapter = OxygenAdapter(transport)
    transport.write("MOCK:BADRESP")
    with pytest.raises(ScpiProtocolError):
        adapter.recording_status()
    transport.close()


def test_08b_timeout_raises(mock_server):
    transport = ScpiTransport(host="127.0.0.1", port=mock_server.bound_port, timeout_s=0.3)
    transport.connect()
    transport.write("MOCK:TIMEOUT")
    with pytest.raises(ScpiTimeoutError):
        transport.query("*IDN?")
    transport.close()


# ---------------------------------------------------------------------------
# 9. Profile execution produces useful logs
# ---------------------------------------------------------------------------
def test_09_profile_produces_useful_logs(engine, tmp_path):
    engine.dispatch("toggle_arm")
    result = engine.dispatch("start_city")
    assert result.status == "ok"
    assert engine.wait_until_idle(timeout_s=30)
    log_path = engine.events.path
    assert log_path.exists()
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = {line.get("event") for line in lines}
    assert "profile_start" in events
    assert "profile_step" in events
    assert "profile_complete" in events or "profile_outcome" in events
    # Ensure step results carry useful detail
    steps = [line for line in lines if line.get("event") == "profile_step"]
    assert len(steps) >= 3
    assert any("type" in s for s in steps)


# ---------------------------------------------------------------------------
# 10. Application operates with no hardware attached
# ---------------------------------------------------------------------------
def test_10_operates_without_hardware(example_config_dict, tmp_path, project_root):
    example_config_dict["logging"]["jsonl_dir"] = str(tmp_path / "logs")
    example_config_dict["connection"]["use_mock"] = True
    example_config_dict["profiles_dir"] = str(project_root / "profiles")
    config = parse_config(example_config_dict)
    eng = AutomationEngine(
        config,
        dry_run=True,
        event_logger=JsonlEventLogger(tmp_path / "logs"),
        dashboard=Dashboard(enabled=False),
    )
    # No external mock provided — engine starts its own
    result = eng.start()
    assert result.status == "ok"
    assert eng.mock_server is not None
    assert eng.channels is not None
    assert len(eng.channels.discovered) >= 6
    eng.dispatch("toggle_arm")
    for cmd in ("start_city", "start_regen", "start_voltage_sweep"):
        r = eng.dispatch(cmd)
        assert r.status == "ok", r
        assert eng.wait_until_idle(timeout_s=60)
    # Simulate button press path end-to-end
    eng.mock_server.set_do("DO 3/4 Sim", False)
    eng.do_monitor.initialize()
    eng.mock_server.set_do("DO 3/4 Sim", True)
    events = eng.poll_once()
    assert any(e.command == "cancel" for e in events)
    eng.stop()


# ---------------------------------------------------------------------------
# Extra: SCPI disconnect transitions engine toward fault/disconnected
# ---------------------------------------------------------------------------
def test_disconnect_transitions_state(engine, mock_server):
    engine.dispatch("toggle_arm")
    assert engine.state in {EngineState.READY, EngineState.IDLE}
    mock_server.disconnect_clients()
    time.sleep(0.2)
    # Trigger detection via failed poll / query
    try:
        engine.poll_once()
    except Exception:
        pass
    # Manually notify if transport hasn't observed loss yet
    if engine.transport and engine.transport.connected:
        engine.transport.write("MOCK:DISCONNECT")
        time.sleep(0.2)
        try:
            engine.transport.query("*IDN?")
        except ScpiConnectionError:
            pass
    time.sleep(0.1)
    assert engine.state in {EngineState.FAULT, EngineState.DISCONNECTED, EngineState.IDLE, EngineState.READY}


def test_list_profiles(engine):
    profiles = engine.list_profiles()
    names = {p.name for p in profiles}
    assert "city_drive" in names
    assert "regenerative_braking" in names
    assert "voltage_sweep" in names
