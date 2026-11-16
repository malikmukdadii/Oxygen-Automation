"""Read-only AOUT discovery shared by diagnostics and authorized backends."""

from __future__ import annotations

import math
import re

from .scpi_transport import ScpiProtocolError


class AoutDiscovery:
    def __init__(self, adapter, outputs):
        self.adapter, self.outputs = adapter, outputs
        self.pinned = {}
        self.failed = False

    def inspect(self):
        channels = self.adapter.list_channels()
        reports = {}
        for role, spec in self.outputs.items():
            report = {'role': role, 'display_name': spec.get('display_name', role),
                      'channel': spec['channel'], 'channel_id': None, 'mode': None,
                      'source': None, 'const_property': None, 'unit': None,
                      'range': None, 'current_setpoint': None, 'properties': [],
                      'discovered_values': {}, 'mismatches': []}
            reports[role] = report
            errors = report['mismatches']
            matches = [c for c in channels if c.name == spec['channel']]
            if len(matches) != 1:
                errors.append(f'Expected exactly one channel with this name; found {len(matches)}')
                continue
            cid = report['channel_id'] = matches[0].channel_id
            if spec.get('expected_id') is not None and spec['expected_id'] != cid:
                errors.append(f'expected_id {spec["expected_id"]!r} does not match discovered ID {cid!r}')
            try:
                props = report['properties'] = self.adapter.list_properties(cid)
                candidates = [p for p in props if re.fullmatch(r'TRION/SourceChannel_A_CONST/Const[0-9]+', p)]
                wanted = ['Mode', 'TRION/SourceChannel_A', 'Unit', 'Range', *candidates]
                values = {}
                for prop in dict.fromkeys(wanted):
                    if prop not in props:
                        errors.append(f'Required property unavailable: {prop}')
                        continue
                    try:
                        value = self.adapter.get_property_value(cid, prop)
                        values[prop] = value
                        report['discovered_values'][prop] = {'kind': value.kind, 'values': value.values}
                    except Exception as exc:
                        errors.append(f'{prop}: {exc}')

                def text(prop):
                    value = values.get(prop)
                    if value is not None and value.kind == 'STRING' and len(value.values) == 1:
                        return value.values[0]
                    errors.append(f'{prop}: expected an unambiguous STRING value')
                    return None

                report['mode'] = text('Mode')
                report['source'] = text('TRION/SourceChannel_A')
                report['unit'] = text('Unit')
                if report['mode'] != spec['mode'] or report['mode'] != 'ConstOutput':
                    errors.append('Active mode is not the configured ConstOutput mode')
                source = report['source']
                if source is not None and re.fullmatch(r'CONST[0-9]+', source):
                    matching = [p for p in candidates if p.rsplit('/', 1)[-1].upper() == source]
                else:
                    matching = []
                if len(matching) != 1:
                    errors.append(f'Cannot unambiguously resolve active constant source {source!r}; candidates: {candidates!r}')
                else:
                    prop = report['const_property'] = matching[0]
                    if spec['const_property'] not in {'auto', prop}:
                        errors.append(f'Configured constant property {spec["const_property"]!r} differs from active {prop!r}')
                    value = values.get(prop)
                    if (value is None or value.kind != 'SCALAR' or len(value.values) != 2 or
                            value.values[1] != 'V' or not math.isfinite(value.values[0])):
                        errors.append('Active constant must be a finite SCALAR in V')
                    else:
                        report['current_setpoint'] = value.values[0]
                        if not spec['min_aout_v'] <= value.values[0] <= spec['max_aout_v']:
                            errors.append('Current setpoint is outside configured output limits')
                value = values.get('Range')
                if (value is None or value.kind != 'RANGE' or len(value.values) != 4 or
                        value.values[1] != 'V' or value.values[3] != 'V' or
                        not all(math.isfinite(value.values[i]) for i in (0, 2))):
                    errors.append('Expected a finite RANGE in V')
                else:
                    low, high = value.values[0], value.values[2]
                    report['range'] = [low, high]
                    if low >= high or low > spec['min_aout_v'] or high < spec['max_aout_v']:
                        errors.append('Discovered range does not cover configured output limits')
                    if report['current_setpoint'] is not None and not low <= report['current_setpoint'] <= high:
                        errors.append('Current setpoint is outside the discovered range')
                if report['unit'] != 'V':
                    errors.append('Output unit must be V')
            except Exception as exc:
                errors.append(str(exc))
        ids = [r['channel_id'] for r in reports.values() if r['channel_id'] is not None]
        for report in reports.values():
            if report['channel_id'] is not None and ids.count(report['channel_id']) > 1:
                report['mismatches'].append('Duplicate output channel assignment')
        return reports

    @staticmethod
    def identity(report):
        return {key: report[key] for key in ('channel', 'channel_id', 'mode', 'source',
                                             'const_property', 'unit', 'range')}

    def discover(self):
        if self.failed:
            raise ScpiProtocolError('AOUT session invalidated; close and restart to rediscover')
        try:
            reports = self.inspect()
        except Exception:
            self.failed = bool(self.pinned)
            raise
        for role, report in reports.items():
            if role in self.pinned and self.identity(report) != self.pinned[role]:
                report['mismatches'].append('Session mapping changed or output context changed; restart to rediscover')
        if any(r['mismatches'] for r in reports.values()):
            self.failed = bool(self.pinned)
            raise ScpiProtocolError(f'AOUT discovery failed; no settings changed: {reports!r}')
        if not self.pinned:
            self.pinned = {role: self.identity(report) for role, report in reports.items()}
        return reports
