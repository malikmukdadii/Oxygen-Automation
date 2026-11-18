"""Advisory process ownership for the two CLI paths using one OXYGEN endpoint."""

import hashlib
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path

from .errors import ControlError


class OutputLease:
    def __init__(self, connection):
        host = connection['host'].casefold()
        if host in {'localhost', '127.0.0.1', '::1'}:
            host = 'loopback'
        key = hashlib.sha256(f'{host}:{connection["port"]}'.encode()).hexdigest()[:24]
        self.path = Path(tempfile.gettempdir()) / f'oxygen-output-{key}.lock'
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b'0')
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise ControlError('Another dispatcher/experiment owns this OXYGEN endpoint') from exc
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def output_lease(config):
    return OutputLease(config.raw['connection']) if config.transport == 'oxygen_scpi' else nullcontext()
