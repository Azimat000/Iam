"""Правила привратника: кто что может, что разрешено в паузе, конечные автоматы, защищённые поля.

Всё здесь — данные и чистые функции; решение «применять или нет» принимает StateController.
"""
from __future__ import annotations

from iam.domain import Source

from .paths import StateMachine

# --- кто порождает переход ---------------------------------------------------------------------
# operator — оператор через панель; core — решение ядра/часов; model — предложение модели, принятое
# ядром (само событие пишет ядро); world — подтверждённый результат мира/канала.
ACTORS = ("operator", "core", "model", "world")

# Типы событий, которые вправе нести переход только перечисленные исполнители (I-1: вмешательства и
# создание личности — только оператор через панель; ответ модели переход «нести» может лишь как
# state.transition_applied/proposal.accepted).
CARRIER_ACTORS: dict[str, frozenset[str]] = {
    "persona.created": frozenset({"operator"}),
    "operator.intervention": frozenset({"operator"}),
    "session.started": frozenset({"operator", "core"}),
    "session.paused": frozenset({"operator", "core"}),
    "session.resumed": frozenset({"operator", "core"}),
    "session.stopped": frozenset({"operator", "core"}),
    "time.moment_due": frozenset({"core"}),
    "action.confirmed": frozenset({"world"}),
    "action.failed": frozenset({"world"}),
    "activity.completed": frozenset({"world"}),
}
# Остальные типы событий, меняющих состояние, нести переход могут core и model (через проверку ядром).
DEFAULT_CARRIER_ACTORS = frozenset({"core", "model", "operator"})

EVENT_SOURCE_KIND = {"operator": "operator_panel", "core": "core", "model": "core", "world": "world"}


def event_source(actor: str, ref: str) -> Source:
    """Источник события: сам ответ модели событием не становится, его записывает ядро (`model:` в ref)."""
    return Source(EVENT_SOURCE_KIND[actor], f"model:{ref}" if actor == "model" else ref)

# --- что допустимо в паузе (docs/spec/time.md#sessions) -----------------------------------------
# paused/stopped: приём входящего, системные записи, управление сеансом, вмешательства оператора и
# создание личности. Отчётность о запросе к модели, который уже был в полёте (ответ приходит в паузе),
# тоже допустима: это запись «ответ пришёл/устарел», состояние она не меняет (D-012).
_ALWAYS = frozenset({"input.arrived", "operator.intervention", "persona.created", "proposal.received",
                     "proposal.stale", "proposal.rejected"})


def permitted_in(session_state: str, event_type: str) -> bool:
    """Можно ли писать событие этого типа при данном состоянии сеанса."""
    if session_state == "running":
        return True
    return event_type.startswith(("session.", "system.")) or event_type in _ALWAYS


# --- защищённые поля ----------------------------------------------------------------------------
# Поля времени и сеанса принадлежат самому контроллеру: их меняют только его методы управления
# сеансом и моментами, а не произвольный переход (ни модели, ни даже оператора через `apply`).
PROTECTED_WORLD_TIME = frozenset({"session_state", "session_id", "life_time_ms", "pause_reason", "paused_at",
                                  "pause_count", "resume_count", "moments"})

SESSION_STATES = ("stopped", "running", "paused")
PAUSE_REASONS = ("operator", "session_limit", "auth_lost", "repeated_failure", "crash")

# --- конечные автоматы ------------------------------------------------------------------------
INTENTION_MACHINE = StateMachine(
    "intention_status", "intentions", ("*", "status"),
    {"active": frozenset({"waiting_opportunity", "deferred", "done", "abandoned"}),
     "waiting_opportunity": frozenset({"active"}),
     "deferred": frozenset({"active"}),
     "done": frozenset(),
     "abandoned": frozenset()},
    # «done» — только по подтверждённому результату действия (state.md#intentions; правило проекта).
    needs_cause={"done": frozenset({"action.confirmed", "activity.completed"})})

SESSION_MACHINE = StateMachine(
    "session_state", "world_time", ("session_state",),
    {"stopped": frozenset({"running"}),
     "running": frozenset({"paused", "stopped"}),
     "paused": frozenset({"running", "stopped"})})

MACHINES = (INTENTION_MACHINE, SESSION_MACHINE)

# Начальное значение группы world_time (docs/spec/state.md#world-time): личность создана, сеанса нет.
WORLD_TIME_DEFAULT = {
    "session_state": "stopped", "session_id": None, "life_time_ms": 0, "pause_reason": None, "paused_at": None,
    "pause_count": 0, "resume_count": 0, "moments": {}, "world": {},
}
