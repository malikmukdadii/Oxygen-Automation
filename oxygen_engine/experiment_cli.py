"""Offline preview, accelerated memory rehearsal, or explicitly authorized experiment execution."""

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .experiment_plan import load_experiment, memory_mirror, validate_physical_plan
from .experiments import ExperimentRunner
from .memory_backend import MemoryBackend
from .output_lease import output_lease
from .pattern_segments import StrictRotatingHandler
from .schema import confirm_outputs, load_dispatcher_config


class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.now += seconds


def run_experiment_cli(args):
    config = load_dispatcher_config(args.bindings)
    plan = load_experiment(args.experiment, config)
    memory = args.memory or config.transport == 'memory'
    if memory and (args.live or args.confirm_output):
        raise ValueError('Memory rehearsal cannot accept physical confirmations or --live')
    if not memory and not config.physical:
        raise ValueError('Experiments require memory rehearsal or explicitly physical OXYGEN bindings')
    preview = {'event': 'experiment_plan', 'name': plan.raw['name'], 'type': plan.raw['type'],
               'memory': memory, 'physical_actuation_requested': args.execute and not memory,
               'connected': False, 'points': plan.points, 'commissioning': plan.raw['commissioning'],
               'feedback': plan.raw['feedback'], 'configured_bounds': {
                   role: [s['min_aout_v'], s['max_aout_v']] for role, s in config.raw['outputs'].items()},
               'native_matrix_mode': 'observe native accepted counter and validity; no GUI or trigger writes'}
    if not args.execute:
        print(json.dumps(preview, indent=2, allow_nan=False))
        return 0
    if memory:
        if not args.confirm_simulated_output:
            raise ValueError('Memory execution requires --confirm-simulated-output')
        config = memory_mirror(config)
    else:
        if args.confirm_simulated_output:
            raise ValueError('Physical experiments cannot accept simulated-output confirmation')
        confirm_outputs(config, args.confirm_output, live=args.live)
        validate_physical_plan(plan)
    log_path = Path(args.log_file or f'logs/experiment-{uuid.uuid4().hex}.jsonl').resolve()
    protected = {config.path, plan.path} | {p.path for p in config.profiles.values()}
    protected |= {(config.path.parent / a['profile']).resolve() for a in config.raw['assignments']
                  if isinstance(a.get('profile'), str)}
    candidates = [log_path] + [log_path.with_name(log_path.name + f'.{i}') for i in range(1, 6)]
    if any(p in protected for p in candidates):
        raise ValueError('Experiment log would overwrite bindings, plan or profile')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = StrictRotatingHandler(log_path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    clock = VirtualClock() if memory else time.monotonic
    wait = clock.wait if memory else time.sleep
    try:
        def emit(record):
            record = {'ts': datetime.now(timezone.utc).isoformat(), 'monotonic_s': clock(), **record}
            line = json.dumps(record, allow_nan=False)
            handler.emit(logging.LogRecord('experiments', logging.INFO, '', 0, line, (), None))
            if record['event'] in {'operating_point_result', 'experiment_summary', 'experiment_fault',
                                    'scpi_command_failed', 'output_readback_failed', 'motor_neutral_unknown'}:
                print(line, flush=True)

        emit(preview)
        if memory:
            backend = MemoryBackend(config, clock=clock)
        else:
            from .oxygen_backend import OxygenBackend
            backend = OxygenBackend(config, emit, confirmed_outputs=args.confirm_output, live=args.live, clock=clock)
        with output_lease(config):
            return ExperimentRunner(config, plan, backend, emit, clock=clock, wait=wait).run()
    finally:
        handler.close()
