"""Takasago RZ-XA adapter interface. Real SCPI is not sent yet."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .mock import MockRZxaAdapter


class RZxaAdapter(ABC):
    """Interface for voltage commands, output state, current limits, and queries."""

    @abstractmethod
    def set_voltage(self, volts: float) -> None: ...

    @abstractmethod
    def set_output(self, enabled: bool) -> None: ...

    @abstractmethod
    def set_current_limit(self, amps: float) -> None: ...

    @abstractmethod
    def query_voltage(self) -> float: ...

    @abstractmethod
    def query_output(self) -> bool: ...

    @abstractmethod
    def query_current_limit(self) -> float: ...

    @abstractmethod
    def identify(self) -> str: ...


class RealRZxaAdapter(RZxaAdapter):
    """
    Placeholder for real RZ-XA SCPI.

    Command names and SCPI formatting are intentionally isolated here and
    NOT implemented — enable_real_rzxa / rzxa_commands_verified must be set
    only after hardware validation.
    """

    def __init__(self, host: str, port: int, **_: Any) -> None:
        self.host = host
        self.port = port
        raise NotImplementedError(
            "Real RZ-XA SCPI is disabled until commands are verified on hardware. "
            "Use MockRZxaAdapter (instruments.rzxa.use_mock=true)."
        )

    def set_voltage(self, volts: float) -> None:
        raise NotImplementedError

    def set_output(self, enabled: bool) -> None:
        raise NotImplementedError

    def set_current_limit(self, amps: float) -> None:
        raise NotImplementedError

    def query_voltage(self) -> float:
        raise NotImplementedError

    def query_output(self) -> bool:
        raise NotImplementedError

    def query_current_limit(self) -> float:
        raise NotImplementedError

    def identify(self) -> str:
        raise NotImplementedError


def create_rzxa_adapter(
    *,
    use_mock: bool = True,
    enable_real: bool = False,
    commands_verified: bool = False,
    host: str = "127.0.0.1",
    port: int = 5025,
    voltage_min: float = 0.0,
    voltage_max: float = 80.0,
    current_limit_default: float = 10.0,
) -> RZxaAdapter:
    if enable_real and commands_verified and not use_mock:
        return RealRZxaAdapter(host=host, port=port)
    return MockRZxaAdapter(
        voltage_min=voltage_min,
        voltage_max=voltage_max,
        current_limit_default=current_limit_default,
    )
