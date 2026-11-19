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


def _segment_value(segments: tuple[Segment, ...], elapsed_s: float) -> float:
    t = max(0.0, finite_number(elapsed_s, 'elapsed time'))
    if not segments:
        return 0.0
    for segment in segments:
        if t < segment.end_s:
            span = segment.end_s - segment.start_s
            if span <= 0:
                return segment.end_v
            return segment.start_v + (segment.end_v - segment.start_v) * (t - segment.start_s) / span
    return segments[-1].end_v


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
        return _segment_value(self.segments, elapsed_s)

    def phase_at(self, t):
        return next((s.phase for s in self.segments if t < s.end_s), 'complete')


@dataclass(frozen=True)
class MultiOutputPattern:
    """Shared monotonic schedule with independent per-role segments."""

    name: str
    roles: tuple[str, ...]
    segments_by_role: dict[str, tuple[Segment, ...]]

    @property
    def duration_s(self):
        return max((segs[-1].end_s for segs in self.segments_by_role.values() if segs), default=0.0)

    @property
    def boundaries(self):
        ends = {segment.end_s for segs in self.segments_by_role.values() for segment in segs}
        return tuple(sorted(ends))

    @property
    def segments(self):
        # Compatibility for single-role callers/tests that inspect .segments.
        if len(self.roles) == 1:
            return self.segments_by_role[self.roles[0]]
        raise AttributeError('Multi-role patterns expose segments_by_role, not a single segments list')

    def value_at(self, elapsed_s):
        if len(self.roles) != 1:
            raise AttributeError('Use values_at() for coordinated multi-output patterns')
        return _segment_value(self.segments_by_role[self.roles[0]], elapsed_s)

    def values_at(self, elapsed_s):
        return {role: _segment_value(segs, elapsed_s) for role, segs in self.segments_by_role.items()}

    def phase_at(self, t):
        for role in self.roles:
            for segment in self.segments_by_role.get(role, ()):
                if t < segment.end_s:
                    return segment.phase
        return 'complete'


class StrictRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        raise
