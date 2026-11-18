"""One-shot, serialized two-output experiments through the unchanged V3 validated writer."""

import math
import threading
import time
import uuid
from statistics import mean

from .constant_pattern import execute_timeline
from .dispatcher import Dispatcher, HeldPattern
from .errors import ControlError, InterlockError
from .experiment_feedback import MemoryExperimentFeedback, OxygenExperimentFeedback
from .experiment_plan import BOOLEAN_ROLES, MOTOR, NUMERIC_UNITS, SUPPLY, validate_physical_plan
from .pattern_segments import Segment


class ExperimentRunner:
    def __init__(self, config, plan, backend, emit, *, clock=time.monotonic, wait=time.sleep,
                 feedback_factory=None):
        self.config, self.plan, self.backend, self.emit = config, plan, backend, emit
        self.clock, self.wait = clock, wait
        self.id = uuid.uuid4().hex
        self.writer = Dispatcher(config, backend, self._writer_event, clock=clock, wait=wait)
        self.factory = feedback_factory or (MemoryExperimentFeedback if config.transport == 'memory' else OxygenExperimentFeedback)
        self.feedback = None
        self.current = None
        self.phase = 'not_started'
        self.results = []
        self.last_values = None
        self.last_acquisition = None
        self.acquisition_changed_at = None
        self.last_matrix_count = None
        self.used = False
        self.lock = threading.Lock()

    def _writer_event(self, record):
        if record['event'] == 'open_loop_warning':
            record = {**record, 'message': 'Writer is open loop; the experiment layer separately enforces fresh measured safety gates. Readback is not physical voltage.'}
        self.emit({'experiment_id': self.id, **record})

    def event(self, event, **fields):
        self.emit({'event': event, 'experiment_id': self.id, 'phase': self.phase,
                   'simulated': not self.config.physical, 'transport': self.config.transport, **fields})

    def read(self, *, drive=False):
        frame = self.feedback.read()
        now = self.clock()
        safety = self.plan.raw['safety']
        values = frame['values']
        if (frame['simulated'] is not (not self.config.physical) or
                not frame['source'].startswith('OXYGEN ' if self.config.physical else 'FAKE ') or
                not 0 <= now - frame['sampled_at'] <= safety['max_age_s'] or
                not frame['sampled_at'] <= frame['completed_at'] <= now):
            raise InterlockError('Stale, future or untrusted experiment feedback')
        if set(values) != set(self.plan.raw['feedback']):
            raise InterlockError('Incomplete experiment feedback frame')
        for role, value in values.items():
            if role in BOOLEAN_ROLES:
                if type(value) is not bool:
                    raise InterlockError(f'{role}: expected actual Boolean feedback')
            elif type(value) not in (int, float) or not math.isfinite(value):
                raise InterlockError(f'{role}: invalid/nonfinite numeric feedback')
        if values['physical_fault'] or not values['source_sink_ready']:
            raise InterlockError('Physical fault or loss of external source/sink readiness')
        for role, limit in [('dc_current', 'max_abs_dc_current_a'), ('motor_current', 'max_abs_motor_current_a'),
                            ('erpm', 'max_abs_erpm')]:
            if abs(values[role]) > safety[limit]:
                raise InterlockError(f'{role}: measured protection limit exceeded')
        if not safety['bus_min_v'] <= values['dc_bus_voltage'] <= safety['bus_max_v']:
            raise InterlockError('Actual bus voltage exceeded experiment envelope')
        if not 0 <= values['vesc_telemetry_age_s'] <= safety['max_age_s']:
            raise InterlockError('Stale VESC CAN feedback')
        acquisition = values['acquisition_time_s']
        if self.last_acquisition is not None and acquisition < self.last_acquisition:
            raise InterlockError('Acquisition time reset; do not resume')
        if self.last_acquisition is None or acquisition > self.last_acquisition:
            self.acquisition_changed_at = now
        elif now - self.acquisition_changed_at > safety['max_age_s']:
            raise InterlockError('Frozen acquisition feedback')
        self.last_acquisition = acquisition
        count = values.get('matrix_sample_count')
        if count is not None:
            if count < 0 or count != int(count) or (self.last_matrix_count is not None and count < self.last_matrix_count):
                raise InterlockError('Native Matrix Sampler counter invalid/reset')
            self.last_matrix_count = count
        if abs(values['dc_power'] - values['dc_bus_voltage'] * values['dc_current']) > safety['dc_power_consistency_w']:
            raise InterlockError('Measured DC power disagrees with voltage/current beyond configured tolerance')
        if drive and abs(values['dc_bus_voltage'] - self.current['bus_target_v']) > safety['bus_tolerance_v']:
            raise InterlockError('Actual bus left the authorized drive window')
        self.last_values = values.copy()
        self.event('experiment_feedback', values=values, source=frame['source'],
                   age_s=now - frame['sampled_at'], simulated=frame['simulated'])
        return values

    def pause(self, seconds):
        self.wait(min(self.plan.raw['timing']['poll_s'], max(0, seconds)))

    def neutral_speed(self):
        self.phase = 'neutral_before_supply'
        self.writer.motor_neutral()
        deadline = self.clock() + self.plan.raw['timing']['neutral_timeout_s']
        while True:
            values = self.read()
            if abs(values['erpm']) <= self.plan.raw['safety']['bus_change_max_abs_erpm']:
                return
            if self.clock() >= deadline:
                raise InterlockError('Motor did not coast into the safe supply-change speed window')
            self.pause(deadline - self.clock())

    def ramp(self, role, target, duration):
        start = self.writer.verify(role)
        rate_key = 'max_supply_rate_v_per_s' if role == SUPPLY else 'max_motor_rate_v_per_s'
        if abs(target - start) / duration > self.plan.raw['timing'][rate_key] + 1e-12:
            raise InterlockError(f'{role}: transition exceeds commissioned slew rate')
        self.writer.active_role = role
        pattern = HeldPattern('experiment_transition', (Segment(0, duration, start, target, self.phase),))

        def guard(value):
            self.writer.verify(role)
            if role == SUPPLY:
                self.writer.motor_neutral()
            else:
                self.writer.verify(SUPPLY)
            values = self.read(drive=role == MOTOR and value > 0)
            if role == SUPPLY and abs(values['erpm']) > self.plan.raw['safety']['bus_change_max_abs_erpm']:
                raise InterlockError('Motor speed unsafe during supply transition')
            self.writer.command(role, value)

        def verify(value):
            self.read(drive=role == MOTOR and value > 0)
            self.writer.verify(role, value)

        execute_timeline(pattern, guard, verify, lambda r: self._writer_event({**r, 'phase': self.phase}),
                         interval_s=self.config.raw['timing']['interval_ms'] / 1000,
                         max_lateness_s=self.config.raw['timing']['max_lateness_ms'] / 1000,
                         clock=self.clock, wait=self.wait)

    def converge_bus(self):
        self.phase = 'bus_convergence'
        timing, safety = self.plan.raw['timing'], self.plan.raw['safety']
        deadline, since = self.clock() + timing['bus_timeout_s'], None
        while True:
            values = self.read()
            if abs(values['dc_bus_voltage'] - self.current['bus_target_v']) <= safety['bus_tolerance_v']:
                since = self.clock() if since is None else since
                if self.clock() - since >= timing['bus_stable_s']:
                    return
            else:
                since = None
            if self.clock() >= deadline:
                raise InterlockError('Actual bus failed bounded convergence')
            self.pause(deadline - self.clock())

    def stable(self, rows):
        safety = self.plan.raw['safety']
        spans = {'dc_bus_voltage': 'steady_bus_span_v', 'dc_current': 'steady_dc_current_span_a',
                 'motor_current': 'steady_motor_current_span_a', 'erpm': 'steady_erpm_span'}
        return all(max(r[key] for r in rows) - min(r[key] for r in rows) <= safety[limit]
                   for key, limit in spans.items())

    def settle(self):
        self.phase = 'settling'
        timing = self.plan.raw['timing']
        deadline, history = self.clock() + timing['settle_timeout_s'], []
        while True:
            self.writer.verify(MOTOR)
            self.writer.verify(SUPPLY)
            now, values = self.clock(), self.read(drive=True)
            history.append((now, values))
            while len(history) > 1 and history[1][0] <= now - timing['settle_s']:
                history.pop(0)
            if (len(history) >= 3 and now - history[0][0] >= timing['settle_s']
                    and self.stable([v for _, v in history])):
                return values
            if self.clock() >= deadline:
                raise InterlockError('Operating point failed bounded steady-state settling')
            self.pause(deadline - self.clock())

    def hold(self, duration, drive):
        deadline, rows = self.clock() + duration, []
        while True:
            self.writer.verify(MOTOR)
            self.writer.verify(SUPPLY)
            rows.append(self.read(drive=drive))
            if self.clock() >= deadline:
                return rows
            self.pause(deadline - self.clock())

    def point_result(self, *, valid, status, rows=(), baseline=None, reason=None):
        point = self.current
        values = {key: mean(r[key] for r in rows) for key in NUMERIC_UNITS if rows and key in rows[0]}
        dc, ac = values.get('dc_power'), values.get('ac_power')
        efficiency = (100 * ac / dc if valid and dc is not None and dc > self.plan.raw['safety']['minimum_dc_power_w']
                      and ac >= 0 else None)
        record = {'point_number': point['point_number'], 'commanded_bus_v': point['bus_target_v'],
                  'commanded_supply_aout_v': point['bus_target_v'] / 80, 'commanded_motor_adc_v': point['adc_v'],
                  'measured_bus_v': values.get('dc_bus_voltage'), 'dc_voltage_v': values.get('dc_bus_voltage'),
                  'dc_current_a': values.get('dc_current'), 'dc_power_w': dc, 'ac_power_w': ac,
                  'can_erpm': values.get('erpm'), 'motor_current_a': values.get('motor_current'),
                  'dc_to_ac_efficiency_pct': efficiency, 'settle_status': 'settled' if baseline is not None else 'not_established',
                  'efficiency_defined': efficiency is not None,
                  'status': status, 'sample_valid': valid, 'reason': reason, 'measurement_count': len(rows),
                  'native_capture_confirmed': valid,
                  'matrix_samples_before': baseline.get('matrix_sample_count') if baseline else None,
                  'matrix_samples_after': rows[-1].get('matrix_sample_count') if rows else None}
        self.results.append(record)
        self.event('operating_point_result', **record)

    def execute(self):
        timing = self.plan.raw['timing']
        last_bus = None
        for point in self.plan.points:
            self.current = point
            if not point['enabled']:
                self.point_result(valid=False, status='disabled', reason=point['disabled_reason'])
                self.current = None
                continue
            self.event('operating_point_started', point_number=point['point_number'], target=point)
            if last_bus != point['bus_target_v']:
                self.neutral_speed()
                self.phase = 'supply_ramp'
                self.ramp(SUPPLY, point['bus_target_v'] / 80, timing['supply_ramp_s'])
                self.converge_bus()
                last_bus = point['bus_target_v']
            self.phase = 'drive_ramp'
            dynamic = self.plan.raw['type'] == 'dynamic'
            self.ramp(MOTOR, point['adc_v'], point['ramp_s'] if dynamic else timing['motor_ramp_s'])
            if dynamic:
                self.phase = point['label']
                rows = self.hold(point['hold_s'], drive=point['adc_v'] > 0)
                self.point_result(valid=False, status='demonstration_completed', rows=rows,
                                  reason='Dynamic playback is not a valid native Matrix Sampler point')
            else:
                baseline = self.settle()
                self.phase = 'capture_window'
                rows = self.hold(timing['capture_s'], drive=True)
                if (len(rows) < 3 or not self.stable([baseline, *rows]) or not all(r['matrix_sample_valid'] for r in rows)
                        or rows[-1]['matrix_sample_count'] <= baseline['matrix_sample_count']):
                    raise InterlockError('Native Matrix Sampler did not confirm a valid steady capture window')
                self.phase = 'return_neutral'
                self.ramp(MOTOR, 0, timing['neutral_ramp_s'])
                self.point_result(valid=True, status='valid', rows=rows, baseline=baseline)
            self.current = None

    def run(self):
        with self.lock:
            if self.used:
                raise ControlError('Experiment is one-shot; no automatic resume/replay after completion or fault')
            self.used = True
            code, status = 1, 'fault'
            try:
                if self.config.physical:
                    validate_physical_plan(self.plan)
                self.backend.connect()
                self.feedback = self.factory(self.backend, self.plan.raw['feedback'], self.clock)
                self.read()
                self.writer.prepare()
                self.writer.active_profile = 'experiment:' + self.id
                self.execute()
                code, status = 0, 'completed_enabled_points'
            except KeyboardInterrupt:
                code, status = 130, 'cancelled'
                if self.current:
                    self.point_result(valid=False, status='cancelled', reason='Operator cancellation')
            except Exception as exc:
                if self.current:
                    self.point_result(valid=False, status='invalid', reason=str(exc))
                self.event('experiment_fault', error_type=type(exc).__name__, error=str(exc),
                           last_valid_feedback=self.last_values, automatic_resume=False)
            finally:
                self.phase = 'cleanup'
                try:
                    if not self.writer.cleanup():
                        code, status = 1, 'cleanup_unknown'
                finally:
                    self.writer.active_profile = self.writer.active_role = None
                    self.backend.close()
            self.phase = 'stopped'
            self.event('experiment_summary', status=status, planned_points=len(self.plan.points),
                       completed_steps=sum(r['status'] in {'valid', 'demonstration_completed'} for r in self.results),
                       valid_points=sum(r['sample_valid'] for r in self.results),
                       disabled_points=sum(r['status'] == 'disabled' for r in self.results),
                       full_grid_complete=self.plan.raw['type'] == 'grid' and sum(r['sample_valid'] for r in self.results) == len(self.plan.points),
                       output_states=self.writer.output_records(), automatic_resume=False)
            return code
