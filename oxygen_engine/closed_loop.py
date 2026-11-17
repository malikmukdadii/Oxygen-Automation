"""Closed-loop control interface. No controller is registered in this release."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .errors import ControlNotReady


@runtime_checkable
class ClosedLoopController(Protocol):
    """Future feedback correction. Implementations must never be faked as open-loop."""

    def correct(self, role: str, commanded_v: float, measured: dict) -> float:
        """Return a corrected AOUT demand from commanded setpoint and fresh measurements."""


_REGISTERED: ClosedLoopController | None = None


def register_controller(controller: ClosedLoopController | None) -> None:
    global _REGISTERED
    _REGISTERED = controller


def get_controller() -> ClosedLoopController | None:
    return _REGISTERED


def require_controller() -> ClosedLoopController:
    controller = _REGISTERED
    if controller is None:
        raise ControlNotReady(
            'control.mode is closed_loop but no ClosedLoopController is registered; '
            'closed-loop is not implemented and will not silently run as open_loop'
        )
    return controller
