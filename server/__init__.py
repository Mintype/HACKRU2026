"""Server layer: Flask app, vision thread, state machine, SQLite, SSE."""

from .state_machine import (
    AWAY,
    CALIBRATING,
    GOOD,
    ROASTED,
    SLOUCHING,
    WARNED,
    PostureStateMachine,
    StateMachineConfig,
)

__all__ = [
    "AWAY",
    "CALIBRATING",
    "GOOD",
    "SLOUCHING",
    "WARNED",
    "ROASTED",
    "PostureStateMachine",
    "StateMachineConfig",
]
