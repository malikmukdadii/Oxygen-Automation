"""Command-line interface for schema-v3 open-loop dispatcher and read-only diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import ConfigError as DispatcherConfigError
from .errors import OxygenAutomationError
from .scpi_transport import ScpiTransport
from .adapters.oxygen import OxygenAdapter


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_dispatcher_path() -> Path:
    """Authoritative operator config is dispatcher.json (gitignored); fall back to the committed example."""
    root = _project_root()
    active = root / 'dispatcher.json'
    if active.exists():
        return active
    return root / 'dispatcher.example.json'


def detect_experiment_schema(path: str | Path) -> str:
    """Backward-compatible experiment schema detection for run-experiment."""
    raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(raw, dict):
        raise DispatcherConfigError('Experiment config must be a JSON object')
    if raw.get('experiment_type') == 'regenerative_deceleration':
        return 'regen'
    if raw.get('type') in {'grid', 'dynamic'} or raw.get('experiment_version') == 1:
        return 'grid_dynamic'
    raise DispatcherConfigError(
        'Unrecognized experiment schema: expected grid/dynamic (experiment_version 1) '
        'or regenerative_deceleration (experiment_type)')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='oxygen_engine',
        description='Schema-v3 open-loop dual-AOUT dispatcher (memory / simulated / physical)',
    )
    parser.add_argument(
        '--live',
        action='store_true',
        help='Physical oxygen_scpi acknowledgement (also accepted after the subcommand)',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    smoke = sub.add_parser(
        'scpi-smoke',
        help='Isolated read-only SCPI identity/channel probe (no dispatcher, no AOUT writes)',
    )
    smoke.add_argument('--host', default='127.0.0.1')
    smoke.add_argument('--port', type=int, default=10001)
    smoke.add_argument('--timeout', type=float, default=2.0)
    smoke.add_argument('--aout', action='append', default=[], metavar='CHANNEL_NAME',
                       help='List exact live attribute names for this channel (repeatable)')

    watch = sub.add_parser(
        'watch-do',
        help='Isolated read-only DO observation; never dispatches automation or changes outputs',
    )
    watch.add_argument('--channel', required=True, help='Exact OXYGEN channel name')
    watch.add_argument('--host', default='127.0.0.1')
    watch.add_argument('--port', type=int, default=10001)
    watch.add_argument('--property', default='DO_Value')
    watch.add_argument('--interval-ms', type=float, default=50.0)
    watch.add_argument('--timeout', type=float, default=0.5)
    watch.add_argument('--max-gap-ms', type=float, default=250.0)
    watch.add_argument('--reconnect-delay', type=float, default=1.0)
    watch.add_argument('--duration', type=float, default=0.0)
    watch.add_argument('--compare-numeric', action='store_true')
    watch.add_argument('--event-source', choices=['property', 'numeric'], default='property')
    watch.add_argument('--expected-presses', type=int)
    watch.add_argument('--interaction-ms', type=float)
    watch.add_argument('--log-file')
    watch.add_argument('--console', choices=['events', 'samples'], default='events')
    watch.add_argument('--status-interval', type=float, default=1.0)

    dispatcher = sub.add_parser(
        'dispatch-patterns',
        help='Validate or run the schema-v3 dispatcher (default config: dispatcher.json)',
    )
    dispatcher.add_argument(
        '--bindings',
        default=str(_default_dispatcher_path()),
        help='Dispatcher JSON (default: ./dispatcher.json if present, else dispatcher.example.json)',
    )
    dispatcher.add_argument('--execute', action='store_true',
                            help='Run persistently; default validates offline')
    dispatcher.add_argument('--confirm-simulated-output', action='store_true',
                            help='Confirm simulation/memory execution (no physical actuation)')
    dispatcher.add_argument('--log-file', help='Rotating JSONL log; default logs/dispatcher-*.jsonl')
    dispatcher.add_argument('--scenario', help='Memory transport fixture events only; never used with OXYGEN')
    dispatcher.add_argument('--confirm-output', action='append', default=[], metavar='EXACT_CHANNEL_NAME',
                            help='Confirm each configured AOUT channel name')
    dispatcher.add_argument('--confirm-demo-output', action='append', default=[], metavar='EXACT_CHANNEL_NAME',
                            help=argparse.SUPPRESS)
    dispatcher.add_argument('--confirm-physical-output', action='append', default=[], metavar='EXACT_CHANNEL_NAME',
                            help=argparse.SUPPRESS)

    def _add_experiment_args(p: argparse.ArgumentParser) -> None:
        p.add_argument('--bindings', default=str(_default_dispatcher_path()),
                       help='V3 dispatcher JSON (grid/dynamic default; regen may override via experiment)')
        p.add_argument('--experiment', required=True, help='Experiment plan JSON; V3 bindings remain separate')
        p.add_argument('--execute', action='store_true')
        p.add_argument('--memory', action='store_true',
                       help='Grid/dynamic: no-I/O accelerated rehearsal retaining configured clamps')
        p.add_argument('--confirm-simulated-output', action='store_true')
        p.add_argument('--confirm-output', action='append', default=[])
        p.add_argument('--log-file')
        p.add_argument('--output-dir', help='Regen artifacts directory for results/CSV/JSONL')

    experiment = sub.add_parser(
        'run-experiment',
        help='Preview/run grid|dynamic experiments, or dispatch regenerative_deceleration by schema',
    )
    _add_experiment_args(experiment)

    regen = sub.add_parser(
        'run-regen-experiment',
        help='Validate or run regenerative_deceleration (explicit alias; same engine as schema dispatch)',
    )
    _add_experiment_args(regen)

    aout_discovery = sub.add_parser(
        'discover-aout',
        help='Read-only discovery/validation of configured AOUT roles from a V3 dispatcher JSON',
    )
    aout_discovery.add_argument(
        '--bindings',
        default=str(_default_dispatcher_path()),
        help='Dispatcher JSON with transport oxygen_scpi (default: dispatcher.json / example)',
    )

    sub.add_parser('list-profiles', help='List JSON profiles under ./profiles (no engine lifecycle)')

    analyze = sub.add_parser(
        'analyze-regen-csv',
        help='Offline energy analysis / trace export from an OXYGEN sync CSV (no hardware)',
    )
    analyze.add_argument('--csv', required=True, help='Sync acquisition CSV path')
    analyze.add_argument('--simulated', action='store_true',
                         help='Label metrics as simulated (required for non-acquisition fixtures)')
    analyze.add_argument('--output-dir', default='logs/regen-analysis')
    analyze.add_argument('--map', action='append', default=[], metavar='LOGICAL=COLUMN',
                         help='Channel mapping override, e.g. dc_power_w=PowerDC (repeatable)')
    analyze.add_argument('--trial-id', help='Associate analysis with a trial id (requires window/markers)')
    analyze.add_argument('--window-start', type=float, help='Trial window start time_s')
    analyze.add_argument('--window-end', type=float, help='Trial window end time_s')
    analyze.add_argument('--marker-start', help='Start marker text in marker column')
    analyze.add_argument('--marker-end', help='End marker text in marker column')
    analyze.add_argument('--marker-column', default='marker', help='CSV column holding markers')
    analyze.add_argument('--polarity', choices=['rzxa_supply_positive', 'invert'],
                         default='rzxa_supply_positive',
                         help='Sign convention: positive = RZ-XA supplying VESC by default')
    analyze.add_argument('--time-unit', choices=['s', 'ms'], default='s')
    return parser


def cmd_dispatch_patterns(args: argparse.Namespace) -> int:
    from .dispatcher import run_dispatch_cli

    return run_dispatch_cli(args)


def cmd_run_experiment(args: argparse.Namespace) -> int:
    """Preserve grid/dynamic run-experiment; route regen schema to the regen runner."""
    kind = detect_experiment_schema(args.experiment)
    if kind == 'regen':
        from .regen_experiment import run_experiment_cli as regen_cli
        return regen_cli(args)
    from .experiment_cli import run_experiment_cli
    return run_experiment_cli(args)


def cmd_run_regen_experiment(args: argparse.Namespace) -> int:
    from .regen_experiment import run_experiment_cli as regen_cli

    kind = detect_experiment_schema(args.experiment)
    if kind != 'regen':
        raise DispatcherConfigError(
            'run-regen-experiment requires experiment_type regenerative_deceleration')
    return regen_cli(args)


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


def cmd_list_profiles(_args: argparse.Namespace) -> int:
    root = _project_root() / 'profiles'
    files = sorted(root.glob('*.json')) if root.is_dir() else []
    if not files:
        print('No profiles found under profiles/.')
        return 1
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding='utf-8-sig'))
            name = raw.get('name') or path.stem
            description = raw.get('description') or ''
            steps = raw.get('steps') if isinstance(raw.get('steps'), list) else []
            print(f'{name}: {description} ({len(steps)} steps) [{path.name}]')
        except Exception as exc:  # noqa: BLE001
            print(f'{path.name}: <unreadable: {exc}>')
    return 0


def cmd_analyze_regen_csv(args: argparse.Namespace) -> int:
    from .analysis_viz import analyze_csv_cli

    channel_map = {}
    for item in getattr(args, 'map', None) or []:
        if '=' not in item:
            raise DispatcherConfigError(f'--map expects LOGICAL=COLUMN, got {item!r}')
        logical, column = item.split('=', 1)
        channel_map[logical.strip()] = column.strip()
    window = None
    if args.window_start is not None or args.window_end is not None:
        if args.window_start is None or args.window_end is None:
            raise DispatcherConfigError('--window-start and --window-end must be provided together')
        window = (args.window_start, args.window_end)
    report = analyze_csv_cli(
        args.csv,
        simulated=args.simulated,
        output_dir=args.output_dir,
        channel_map=channel_map or None,
        trial_id=args.trial_id,
        window=window,
        marker_start=args.marker_start,
        marker_end=args.marker_end,
        marker_column=args.marker_column,
        polarity=args.polarity,
        time_unit=args.time_unit,
    )
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


def _normalize_argv(argv: list[str] | None) -> list[str]:
    """Allow global --live before or after the subcommand."""
    raw = list(sys.argv[1:] if argv is None else argv)
    globals_taken: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(raw):
        tok = raw[i]
        if tok == '--live':
            globals_taken.append(tok)
            i += 1
            continue
        # Legacy global --config is retired; surface a clear error.
        if tok in {'--config'} or tok.startswith('--config='):
            raise SystemExit(
                'Global --config is retired. Use schema-v3 dispatcher.json via '
                '`dispatch-patterns --bindings` (default: dispatcher.json).'
            )
        rest.append(tok)
        i += 1
    return globals_taken + rest


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(argv))
    handlers = {
        'run-experiment': cmd_run_experiment,
        'run-regen-experiment': cmd_run_regen_experiment,
        'discover-aout': cmd_discover_aout,
        'dispatch-patterns': cmd_dispatch_patterns,
        'watch-do': cmd_watch_do,
        'scpi-smoke': cmd_scpi_smoke,
        'list-profiles': cmd_list_profiles,
        'analyze-regen-csv': cmd_analyze_regen_csv,
    }
    try:
        return handlers[args.command](args)
    except DispatcherConfigError as exc:
        print(f'Config error: {exc}', file=sys.stderr)
        return 2
    except OxygenAutomationError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
