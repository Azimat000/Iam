"""Привратник состояния: единственный писатель состояния и журнала, версии, идемпотентность, воспроизведение."""
from .controller import Check, StateController
from .errors import ClockRegression, NotSupported, ReplayError, StateCorrupt, StateError
from .paths import OpError, StateMachine, apply_op, apply_ops, empty_state
from .replay import ReplayResult, replay, verify
from .rules import ACTORS, MACHINES, PAUSE_REASONS, PROTECTED_WORLD_TIME, SESSION_STATES, WORLD_TIME_DEFAULT, permitted_in
from .types import ApplyResult, EventSpec, Proposal, RecordResult, ResponseResult, Violation

__all__ = [
    "Check", "StateController", "ClockRegression", "NotSupported", "ReplayError", "StateCorrupt", "StateError",
    "OpError", "StateMachine", "apply_op", "apply_ops", "empty_state", "ReplayResult", "replay", "verify", "ACTORS",
    "MACHINES", "PAUSE_REASONS", "PROTECTED_WORLD_TIME", "SESSION_STATES", "WORLD_TIME_DEFAULT", "permitted_in",
    "ApplyResult", "EventSpec", "Proposal", "RecordResult", "ResponseResult", "Violation",
]
