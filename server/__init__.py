"""Server layer: Flask app, vision thread, state machine, SQLite, SSE."""

from .state_machine import (
    AWAY,
    CALIBRATING,
    GOOD,
    PHONE,
    ROASTED,
    SLOUCHING,
    PostureStateMachine,
    StateMachineConfig,
)

__all__ = [
    "AWAY",
    "CALIBRATING",
    "GOOD",
    "SLOUCHING",
    "ROASTED",
    "PHONE",
    "PostureStateMachine",
    "StateMachineConfig",
]
