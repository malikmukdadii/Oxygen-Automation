"""Configured V2 outputs, inspected without output or transfer-configuration writes."""

import json
import re

from .adapters.oxygen import OxygenAdapter
from .aout_discovery import AoutDiscovery
from .dispatcher_v2_schema import load_v2_config
from .scpi_transport import ScpiProtocolError, ScpiTransport, parse_scpi, quote_scpi


class AoutDiagnosticTransport(ScpiTransport):
    def write(self, command):
        raise ScpiProtocolError('AOUT diagnostic is read-only')

    def query(self, command):
        allowed = command in {'*IDN?', ':CHANNELlist:NAMes?'}
        allowed = allowed or re.fullmatch(r':CHANNELlist:ITEM[0-9]+:ATTR:NAMes\?', command) is not None
        prefix = ':CHANNELlist:PROPerty? '
        if command.startswith(prefix):
            args = parse_scpi(command[len(prefix):])
            allowed = (len(args) == 2 and all(isinstance(a, str) for a in args) and
                       command == prefix + ','.join(quote_scpi(a) for a in args))
        if not allowed:
            raise ScpiProtocolError(f'AOUT diagnostic query refused: {command!r}')
        return super().query(command)


def run_aout_diagnostic(args):
    config = load_v2_config(args.bindings)
    if config.raw['execution_backend'] not in {'oxygen_demo', 'oxygen_hardware'}:
        raise ValueError('Use an oxygen_demo or oxygen_hardware V2 configuration for SCPI discovery; memory_fixture stays offline')
    connection = config.raw['connection']
    transport = AoutDiagnosticTransport(host=connection['host'], port=connection['port'],
                                        timeout_s=connection['timeout_s'])
    report = {'read_only': True, 'physical_hardware_authorized': False,
              'endpoint': f'{connection["host"]}:{connection["port"]}', 'outputs': {}, 'failures': []}
    try:
        transport.connect()
        adapter = OxygenAdapter(transport)
        report['identification'] = adapter.identify()
        if report['identification'].split(',')[:2] != ['DEWETRON', 'OXYGEN']:
            raise ScpiProtocolError('Expected DEWETRON OXYGEN')
        report['outputs'] = AoutDiscovery(adapter, config.raw['outputs']).inspect()
    except Exception as exc:
        report['failures'].append(str(exc))
    finally:
        transport.close()
    print(json.dumps(report, indent=2))
    return int(bool(report['failures']) or any(r['mismatches'] for r in report['outputs'].values()))
