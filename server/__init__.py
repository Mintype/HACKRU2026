"""Server layer: Flask app, vision thread, state machine, SQLite, SSE."""

from .state_machine import (
    AWAY,
    CALIBRATING,
    GOOD,
    ROASTED,
    SLEEPING,
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
    "SLEEPING",
    "PostureStateMachine",
    "StateMachineConfig",
]
