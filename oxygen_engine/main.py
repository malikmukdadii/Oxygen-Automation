"""Command-line interface for the OXYGEN Automation Engine."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .adapters.oxygen import OxygenAdapter
from .config import ConfigError, default_example_config, load_config
from .engine import AutomationEngine
from .errors import ConfigError as DispatcherConfigError
from .errors import OxygenAutomationError
from .logging_setup import Dashboard, DashboardRefresher, JsonlEventLogger, setup_logging
from .scpi_transport import ScpiTransport


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_config_path() -> Path:
    root = _project_root()
    for candidate in (root / "config.json", root / "config.example.json"):
        if candidate.exists():
            return candidate
    return root / "config.example.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oxygen_engine",
        description="OXYGEN Automation Engine (mock/simulation by default)",
    )
    parser.add_argument(
        "--config",
        default=str(_default_config_path()),
        help="Path to JSON configuration file (accepted before or after the subcommand)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Explicitly enable live mode path (still requires verified live_control "
            "flags in config before actuators are driven)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    smoke = sub.add_parser('scpi-smoke', help='Read-only discovery on real OXYGEN (no engine startup)')
    smoke.add_argument('--host', default='127.0.0.1')
    smoke.add_argument('--port', type=int, default=10001)
    smoke.add_argument('--timeout', type=float, default=2.0, help='Response timeout in seconds')
    smoke.add_argument('--aout', action='append', default=[], metavar='CHANNEL_NAME',
                       help='List exact live attribute names for this channel (repeatable)')

    watch = sub.add_parser('watch-do', help='Observe one DO channel; never dispatch automation or change outputs')
    watch.add_argument('--channel', required=True, help='Exact OXYGEN channel name')
    watch.add_argument('--host', default='127.0.0.1')
    watch.add_argument('--port', type=int, default=10001)
    watch.add_argument('--property', default='DO_Value', help='Must exist in live attribute discovery')
    watch.add_argument('--interval-ms', type=float, default=50.0)
    watch.add_argument('--timeout', type=float, default=0.5)
    watch.add_argument('--max-gap-ms', type=float, default=250.0,
                       help='Invalidate edges across longer sampling gaps or slower samples')
    watch.add_argument('--reconnect-delay', type=float, default=1.0)
    watch.add_argument('--duration', type=float, default=0.0, help='Seconds; zero means until Ctrl+C')
    watch.add_argument('--compare-numeric', action='store_true',
                       help='Opt in to SCPI numeric transfer setup and comparison (no output changes)')
    watch.add_argument('--event-source', choices=['property', 'numeric'], default='property')
    watch.add_argument('--expected-presses', type=int, help='Operator ground-truth count for this trial')
    watch.add_argument('--interaction-ms', type=float, help='Operator target pulse duration; a label, not a measurement')
    watch.add_argument('--log-file', help='Append JSONL records here; otherwise use a unique file in logs')
    watch.add_argument('--console', choices=['events', 'samples'], default='events',
                       help='Events and occasional status (default), or display every sample; logs always retain all samples')
    watch.add_argument('--status-interval', type=float, default=1.0,
                       help='Seconds between unchanged console status samples; zero shows changes only')

    dispatcher = sub.add_parser('dispatch-patterns', help='Validate or run schema v3 dual-AOUT dispatcher (open_loop default)')
    dispatcher.add_argument('--bindings', default=str(_project_root() / 'dispatcher.v3.example.json'),
                            help='Dispatcher JSON file; profile paths are relative to this file')
    dispatcher.add_argument('--execute', action='store_true', help='Run persistently; default validates offline')
    dispatcher.add_argument('--confirm-simulated-output', action='store_true',
                            help='Confirm simulation/memory execution (no physical actuation)')
    dispatcher.add_argument('--log-file', help='Rotating JSONL log; default logs/dispatcher-*.jsonl')
    dispatcher.add_argument('--scenario', help='Memory transport fixture events only; never used with OXYGEN')
    dispatcher.add_argument('--confirm-output', action='append', default=[], metavar='EXACT_CHANNEL_NAME',
                            help='Confirm each configured AOUT channel name (repeat for both)')
    dispatcher.add_argument('--confirm-demo-output', action='append', default=[], metavar='EXACT_CHANNEL_NAME',
                            help=argparse.SUPPRESS)
    dispatcher.add_argument('--confirm-physical-output', action='append', default=[], metavar='EXACT_CHANNEL_NAME',
                            help=argparse.SUPPRESS)

    aout_discovery = sub.add_parser('discover-aout', help='Read-only discovery and validation of both configured AOUTs')
    aout_discovery.add_argument('--bindings', default=str(_project_root() / 'dispatcher.v3.oxygen-demo.example.json'))

    sub.add_parser("discover", help="Discover OXYGEN channels and properties")

    probe = sub.add_parser(
        "probe-do",
        help="Capture DO properties; use --watch to diff after a manual press",
    )
    probe.add_argument("--channel", required=True, help='OXYGEN channel name, e.g. "DO 3/1 Sim"')
    probe.add_argument(
        "--watch",
        type=float,
        default=0.0,
        help="Seconds to wait before capturing after-snapshot (operator presses button)",
    )

    sub.add_parser("monitor", help="Poll DO channels and show live dashboard")

    run = sub.add_parser("run", help="Run engine continuously")
    run.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate profile outputs (default)",
    )
    run.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Disable dry-run (still gated by live_control permissions)",
    )

    test_cmd = sub.add_parser("test-command", help="Dispatch a single logical command")
    test_cmd.add_argument("name", help="Command name, e.g. start_city")
    test_cmd.add_argument("--dry-run", action="store_true", default=True)
    test_cmd.add_argument("--no-dry-run", action="store_true")
    test_cmd.add_argument("--arm", action="store_true", help="Arm software before command")

    sub.add_parser("list-profiles", help="List available JSON profiles")
    return parser


def _make_engine(args: argparse.Namespace) -> AutomationEngine:
    config = load_config(args.config)
    setup_logging(config.logging.level)
    if getattr(args, "live", False):
        # Live mode path: prefer real connection if configured; do not flip
        # enable_live_aout automatically — operator must set verified flags.
        if config.connection.use_mock:
            print(
                "WARNING: --live set but connection.use_mock=true in config. "
                "Set use_mock=false and verified live_control flags for hardware.",
                file=sys.stderr,
            )
    dry_run = True
    if getattr(args, "no_dry_run", False):
        dry_run = False
    elif getattr(args, "dry_run", True):
        dry_run = True
    events = JsonlEventLogger(config.logging.jsonl_dir)
    dashboard = Dashboard(enabled=config.logging.console_dashboard and args.command in {"monitor", "run"})
    # Resolve profiles_dir relative to config file / project
    cfg_path = Path(args.config).resolve().parent
    profiles = Path(config.profiles_dir)
    if not profiles.is_absolute() and not profiles.exists():
        candidate = cfg_path / config.profiles_dir
        if candidate.exists():
            config.profiles_dir = str(candidate)
        else:
            config.profiles_dir = str(_project_root() / "profiles")
    return AutomationEngine(config, dry_run=dry_run, event_logger=events, dashboard=dashboard)


def cmd_dispatch_patterns(args: argparse.Namespace) -> int:
    from .dispatcher import run_dispatch_cli

    return run_dispatch_cli(args)


def cmd_discover_aout(args: argparse.Namespace) -> int:
    from .aout_diagnostic import run_aout_diagnostic

    return run_aout_diagnostic(args)


def cmd_watch_do(args: argparse.Namespace) -> int:
    from .do_diagnostic import run_watch_do

    return run_watch_do(args)


def cmd_scpi_smoke(args: argparse.Namespace) -> int:
    """No engine, output controllers, numeric configuration or cleanup actions."""
    transport = ScpiTransport(host=args.host, port=args.port, timeout_s=args.timeout)
    adapter = OxygenAdapter(transport)
    report = {
        'endpoint': f'{args.host}:{args.port}',
        'read_only': True,
        'aout_do_property_mappings': 'unverified; attribute names below are live discovery only',
        'aout_attributes': [],
        'failures': [],
    }

    def capture(label, operation):
        try:
            value = operation()
            report[label] = value
            return value
        except Exception as exc:  # noqa: BLE001
            report['failures'].append({'operation': label, 'error': str(exc)})
            return None

    try:
        transport.connect()
        capture('identification', adapter.identify)
        channels = capture('channels', lambda: [
            {'id': c.channel_id, 'name': c.name} for c in adapter.list_channels()
        ]) or []
        by_name = {c['name']: c['id'] for c in channels}
        for name in args.aout:
            if name not in by_name:
                report['failures'].append({'operation': 'aout_attributes', 'error': f'Unknown channel: {name}'})
                continue
            attributes = capture('selected_attributes', lambda: adapter.list_properties(by_name[name]))
            report['aout_attributes'].append({'name': name, 'id': by_name[name], 'attributes': attributes})
        report.pop('selected_attributes', None)
        capture('acquisition_state', adapter.acquisition_state)
        capture('recording_state', adapter.recording_state)
        capture('scpi_errors', lambda: [
            {'code': e.code, 'message': e.message} for e in transport.drain_errors()
        ])
    except Exception as exc:  # noqa: BLE001
        report['failures'].append({'operation': 'connect', 'error': str(exc)})
    finally:
        transport.close()
    print(json.dumps(report, indent=2))
    return 1 if report['failures'] or report.get('scpi_errors') else 0


def cmd_discover(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    result = engine.start()
    if result.status != "ok":
        print(json.dumps(result.__dict__, indent=2))
        engine.stop()
        return 1
    assert engine.channels and engine.oxygen
    channels = engine.channels.discovered
    report = []
    for ch in channels:
        props = []
        try:
            props = engine.oxygen.list_properties(ch.channel_id)
        except Exception as exc:  # noqa: BLE001
            props = [f"<error:{exc}>"]
        report.append(
            {
                "id": ch.channel_id,
                "name": ch.name,
                "type": ch.channel_type,
                "properties": props,
            }
        )
    print(json.dumps({"identify": engine.oxygen.identify(), "channels": report}, indent=2))
    engine.stop()
    return 0


def cmd_probe_do(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    result = engine.start()
    if result.status != "ok":
        print(json.dumps(result.__dict__, indent=2))
        engine.stop()
        return 1
    assert engine.do_monitor
    if args.watch > 0:
        print(f"Capturing before-snapshot for {args.channel!r}...")
        before_probe = engine.do_monitor.probe_channel(args.channel)
        print("Press the OXYGEN button now..." if not engine.config.connection.use_mock else "Waiting...")
        if engine.config.connection.use_mock and engine.mock_server:
            # Flip DO for demonstration in mock mode
            time.sleep(min(0.2, args.watch))
            engine.mock_server.set_do(args.channel, True)
            time.sleep(max(0.0, args.watch - 0.2))
        else:
            time.sleep(args.watch)
        probe = engine.do_monitor.complete_probe(args.channel, before_probe["before"])
    else:
        probe = engine.do_monitor.probe_channel(args.channel)
        print(
            "Before-snapshot captured. Re-run with --watch <seconds> while pressing the button "
            "to obtain a diff. DO property is NOT verified on hardware yet."
        )
    print(json.dumps(probe, indent=2))
    engine.stop()
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    result = engine.start()
    if result.status != "ok":
        print(json.dumps(result.__dict__, indent=2))
        return 1
    refresher = DashboardRefresher(engine.dashboard)
    refresher.start()
    engine.start_monitoring()
    print("Monitoring DO channels. Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(1.0)
            if engine.transport and engine.transport.last_error:
                engine.dashboard.update(last_scpi_error=str(engine.transport.last_error))
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        refresher.stop()
        engine.stop()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    result = engine.start()
    if result.status != "ok":
        print(json.dumps(result.__dict__, indent=2))
        return 1
    # Arm by default in continuous run only if configured
    refresher = DashboardRefresher(engine.dashboard)
    refresher.start()
    engine.start_monitoring()
    print(
        f"Engine running (dry_run={engine.dry_run}, armed={engine.software_armed}). Ctrl+C to stop.",
        flush=True,
    )
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        refresher.stop()
        engine.stop()
    return 0


def cmd_test_command(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    result = engine.start()
    if result.status != "ok":
        print(json.dumps(result.__dict__, indent=2))
        engine.stop()
        return 1
    if args.arm or args.name.startswith("start_"):
        engine.dispatch("toggle_arm", source="cli")
    result = engine.dispatch(args.name, source="cli")
    if args.name.startswith("start_"):
        engine.wait_until_idle(timeout_s=120.0)
        if engine.last_result:
            result = engine.last_result
    print(json.dumps(result.__dict__, indent=2, default=str))
    # Also print event log path
    print(f"Event log: {engine.events.path}", file=sys.stderr)
    engine.stop()
    return 0 if result.status in {"ok", "cancelled"} else 1


def cmd_list_profiles(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    profiles = engine.list_profiles()
    for profile in profiles:
        print(f"{profile.name}: {profile.description} ({len(profile.steps)} steps)")
    if not profiles:
        print("No profiles found.")
        return 1
    return 0


def _normalize_argv(argv: list[str] | None) -> list[str]:
    """Allow global --config/--live before or after the subcommand."""
    raw = list(sys.argv[1:] if argv is None else argv)
    globals_taken: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok in {"--config"} and i + 1 < len(raw):
            globals_taken.extend([tok, raw[i + 1]])
            i += 2
            continue
        if tok.startswith("--config="):
            globals_taken.append(tok)
            i += 1
            continue
        if tok == "--live":
            globals_taken.append(tok)
            i += 1
            continue
        rest.append(tok)
        i += 1
    return globals_taken + rest


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(argv))
    handlers = {
        "discover-aout": cmd_discover_aout,
        "dispatch-patterns": cmd_dispatch_patterns,
        "watch-do": cmd_watch_do,
        "scpi-smoke": cmd_scpi_smoke,
        "discover": cmd_discover,
        "probe-do": cmd_probe_do,
        "monitor": cmd_monitor,
        "run": cmd_run,
        "test-command": cmd_test_command,
        "list-profiles": cmd_list_profiles,
    }
    try:
        return handlers[args.command](args)
    except (ConfigError, DispatcherConfigError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except OxygenAutomationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
