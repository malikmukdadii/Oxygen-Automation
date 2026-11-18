"""Strict experiment feedback readers; no output writes or GUI automation."""

from .errors import InterlockError
from .experiment_plan import BOOLEAN_ROLES, MOTOR, NUMERIC_UNITS, SUPPLY


class OxygenExperimentFeedback:
    def __init__(self, backend, specs, clock):
        self.backend, self.specs, self.clock = backend, specs, clock
        self.ids = {}
        self.numeric_roles = [r for r in specs if r in NUMERIC_UNITS]
        with backend.lock:
            self._pin()
            for role, spec in specs.items():
                cid = self.ids[role]
                if role in NUMERIC_UNITS:
                    unit = backend.adapter.get_property_value(cid, 'Unit')
                    if unit.kind != 'STRING' or unit.values != (spec['unit'],):
                        raise InterlockError(f'{role}: feedback unit must be {spec["unit"]!r}')
                elif spec['property'] not in backend.adapter.list_properties(cid):
                    raise InterlockError(f'{role}: Boolean property unavailable')
            names = tuple(specs[r]['channel'] for r in self.numeric_roles)
            backend.transport.experiment_numeric_names = names
            backend.adapter.configure_numeric_measurements(list(names))
            backend.error()

    def _pin(self):
        channels = self.backend.adapter.list_channels()
        forbidden = set(self.backend.channel_ids.values()) | set(self.backend.do_channels.values())
        used = set()
        for role, spec in self.specs.items():
            matches = [c.channel_id for c in channels if c.name == spec['channel']]
            if len(matches) != 1 or matches[0] in forbidden or matches[0] in used:
                raise InterlockError(f'{role}: exact independent feedback identity required')
            cid = matches[0]
            if role in self.ids and self.ids[role] != cid:
                raise InterlockError(f'{role}: experiment feedback identity changed')
            self.ids[role] = cid
            used.add(cid)

    def read(self):
        backend = self.backend
        with backend.lock:
            started = self.clock()
            self._pin()
            if backend.adapter.acquisition_state() != 'Started':
                raise InterlockError('Experiment requires live OXYGEN acquisition Started')
            numbers = backend.adapter.read_selected_measurements(len(self.numeric_roles))
            values = dict(zip(self.numeric_roles, numbers))
            for role in self.specs:
                if role in BOOLEAN_ROLES:
                    values[role] = backend._boolean(self.ids[role], self.specs[role]['property'])
            backend.error()
            return {'values': values, 'sampled_at': started, 'completed_at': self.clock(),
                    'source': 'OXYGEN pinned experiment transfer', 'simulated': not backend.physical}


class MemoryExperimentFeedback:
    """Deterministic fake plant/native-sampler model around the existing MemoryBackend.

    Instantaneous bus/speed response is intentionally not a hardware model or commissioning.
    """

    def __init__(self, backend, specs, clock):
        self.backend, self.specs, self.clock = backend, specs, clock
        self.state = None
        self.stable_since = clock()
        self.last_capture = clock()
        self.count = 0

    def read(self):
        now = self.clock()
        outputs = self.backend.config.raw['outputs']
        bus = self.backend.read_output(outputs[SUPPLY]['binding']) * 80
        drive = self.backend.read_output(outputs[MOTOR]['binding'])
        state = (bus, drive)
        if state != self.state:
            self.state, self.stable_since = state, now
        stable = now - self.stable_since >= 2
        if stable and drive > 0 and now - self.last_capture >= 1:
            self.count += 1
            self.last_capture = now
        current = .2 + 2 * drive
        power = bus * current
        values = {'dc_bus_voltage': bus, 'dc_current': current, 'dc_power': power,
                  'ac_power': power * .85 if drive else 0.0, 'erpm': drive * 1000,
                  'motor_current': drive * 4, 'acquisition_time_s': now,
                  'vesc_telemetry_age_s': 0.0, 'source_sink_ready': True, 'physical_fault': False,
                  'matrix_sample_count': self.count, 'matrix_sample_valid': stable}
        return {'values': {r: values[r] for r in self.specs}, 'sampled_at': now, 'completed_at': now,
                'source': 'FAKE experiment plant and native sampler', 'simulated': True}
