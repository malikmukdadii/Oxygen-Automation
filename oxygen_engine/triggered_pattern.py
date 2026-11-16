from __future__ import annotations

import time

from .adapters.oxygen import OxygenAdapter
from .constant_pattern import execute_timeline, run_simulated_pattern
from .do_monitor import DiagnosticEdgeTracker
from .scpi_transport import ScpiProtocolError

TRIGGER_CHANNEL = 'DO 3/1 Sim'


def prepare_trigger(transport, emit) -> None:
    adapter = OxygenAdapter(transport)
    matches = [c for c in adapter.list_channels() if c.name == TRIGGER_CHANNEL]
    if len(matches) != 1 or matches[0].channel_id == transport.expected_id:
        raise ScpiProtocolError('Exactly one distinct DO 3/1 Sim trigger channel is required')
    transport.trigger_id = matches[0].channel_id
    if 'DO_Value' not in adapter.list_properties(transport.trigger_id):
        raise ScpiProtocolError('Trigger DO_Value property is missing; no alternatives will be guessed')
    read_trigger(transport)
    emit({'event': 'trigger_preflight_passed', 'channel': TRIGGER_CHANNEL,
          'channel_id': transport.trigger_id, 'property': 'DO_Value', 'read_only': True})


def read_trigger(transport) -> bool:
    value = OxygenAdapter(transport).get_property_value(transport.trigger_id, 'DO_Value')
    error = transport.check_error()
    if error.code:
        raise ScpiProtocolError(f'Trigger SCPI error: {error}')
    if value.kind not in {'BOOL', 'BOOLEAN'} or len(value.values) != 1 or type(value.values[0]) is not bool:
        raise ScpiProtocolError(f'Trigger must report a valid Boolean DO_Value: {value!r}')
    return value.values[0]


def triggered_timeline(pattern, setpoint, verify, emit, *, read_state,
                       poll_s=.05, max_gap_s=.25, clock=time.monotonic,
                       wait=time.sleep, timeline=execute_timeline, **kwargs):
    completed = 0
    while True:
        tracker = DiagnosticEdgeTracker(max_gap_s)
        last_verify = clock()
        emit({'event': 'trigger_waiting', 'completed_cycles': completed,
              'message': 'Observe LOW, then HIGH to start; startup/held HIGH does not trigger'})
        while True:
            started = clock()
            state = read_state()
            now = clock()
            if now - started > max_gap_s:
                tracker.reset()
                emit({'event': 'trigger_stale', 'latency_ms': (now - started) * 1000})
            else:
                observation = tracker.observe(state, now)
                emit({'event': 'trigger_sample', 'state': state, **observation})
                if observation['edge'] != 'steady':
                    emit({'event': 'trigger_state', **observation})
                if observation['press']:
                    verify(0.0)
                    emit({'event': 'pattern_cycle_started', 'cycle': completed + 1})
                    result = timeline(pattern, setpoint, verify, emit, **kwargs)
                    verify(0.0)
                    completed += 1
                    emit({'event': 'pattern_cycle_complete', 'cycle': completed,
                          'final_state': 'configured_zero_verified', **result})
                    break
            if now - last_verify >= .5:
                verify(0.0)
                last_verify = clock()
            wait(poll_s)


def run_triggered_pattern(transport, pattern, emit, *, poll_s=.05, max_gap_s=.25,
                          timeline=execute_timeline, clock=time.monotonic, wait=time.sleep,
                          **kwargs):
    def monitor(pattern, setpoint, verify, emit, **timing):
        return triggered_timeline(pattern, setpoint, verify, emit,
                                  read_state=lambda: read_trigger(transport),
                                  poll_s=poll_s, max_gap_s=max_gap_s, timeline=timeline,
                                  clock=clock, wait=wait, **timing)

    return run_simulated_pattern(transport, pattern, emit, timeline=monitor,
                                 before_prepare=lambda: prepare_trigger(transport, emit), **kwargs)
