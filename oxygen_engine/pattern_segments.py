"""Shared timeline segment types and strict log rotation for the dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

from .constant_pattern import finite_number


@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float
    start_v: float
    end_v: float
    phase: str


@dataclass(frozen=True)
class OutputPattern:
    name: str
    segments: tuple[Segment, ...]

    @property
    def duration_s(self):
        return self.segments[-1].end_s

    @property
    def boundaries(self):
        return tuple(s.end_s for s in self.segments)

    def value_at(self, elapsed_s):
        t = max(0.0, finite_number(elapsed_s, 'elapsed time'))
        for s in self.segments:
            if t < s.end_s:
                return s.start_v + (s.end_v - s.start_v) * (t - s.start_s) / (s.end_s - s.start_s)
        return 0.0

    def phase_at(self, t):
        return next((s.phase for s in self.segments if t < s.end_s), 'complete')


class StrictRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        raise
