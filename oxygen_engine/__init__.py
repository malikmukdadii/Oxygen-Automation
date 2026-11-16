"""OXYGEN Automation Engine for DEWETRON OXYGEN 8.2."""

__version__ = "0.1.0"

from .engine import AutomationEngine, EngineState, CommandResult

__all__ = ["AutomationEngine", "EngineState", "CommandResult", "__version__"]
