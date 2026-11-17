"""Shared exceptions for the unified dispatcher path."""

from __future__ import annotations


class OxygenAutomationError(Exception):
    """Base for dispatcher/backend failures (prefer this over bare RuntimeError)."""


class ConfigError(OxygenAutomationError, ValueError):
    """Invalid configuration or profile."""


class ControlError(OxygenAutomationError):
    """Command, readback, or session control failure."""


class ControlNotReady(OxygenAutomationError):
    """Requested control mode cannot run (e.g. closed_loop without a controller)."""


class InterlockError(OxygenAutomationError):
    """Open-loop setpoint precondition or closed-loop feedback interlock failure."""


class AuthorizationError(OxygenAutomationError, PermissionError):
    """Missing or invalid execution authorization."""
