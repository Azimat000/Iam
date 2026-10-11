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
    # Успешный исход пишет только подтверждающий источник; «исход неизвестен» — ядро (или канал, потерявший ответ).
    "message.out_sent": frozenset({"world"}),
    "message.out_unknown": frozenset({"core", "world"}),
}
# Остальные типы событий, меняющих состояние, нести переход могут core и model (через проверку ядром).
DEFAULT_CARRIER_ACTORS = frozenset({"core", "model", "operator"})

# События, которые пишут только методы самого контроллера (начало/пауза/возобновление/остановка сеанса,
# создание личности, наступивший момент, вмешательство оператора — только через `intervene`): через общий `apply` их не провести, иначе журнал мог бы
# сообщить о сеансе или моменте, которых в состоянии нет.
RESERVED_CARRIERS = frozenset({"operator.intervention", "persona.created", "session.started", "session.paused", "session.resumed",
                               "session.stopped", "time.moment_due"})

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
    needs_cause={"done": frozenset({"action.confirmed", "activity.completed"})},
    initial=frozenset({"active"}))

SESSION_MACHINE = StateMachine(
    "session_state", "world_time", ("session_state",),
    {"stopped": frozenset({"running"}),
     "running": frozenset({"paused", "stopped"}),
     "paused": frozenset({"running", "stopped"})},
    initial=frozenset({"stopped"}))

# Исход действия (R-184, R-199, R-222): принято → попытка → ровно один исход. «confirmed» только после попытки,
# попытка — только после принятого предложения; сам исход пишет подтверждающий источник (CARRIER_ACTORS).
_ATTEMPTS = frozenset({"action.attempted", "message.out_requested"})
ACTION_MACHINE = StateMachine(
    "action_status", "world_time", ("world", "actions", "*", "status"),
    {"accepted": frozenset({"attempted"}),
     "attempted": frozenset({"confirmed", "failed", "unknown_outcome"}),
     "confirmed": frozenset(), "failed": frozenset(), "unknown_outcome": frozenset()},
    needs_cause={"attempted": frozenset({"proposal.accepted"}), "confirmed": _ATTEMPTS, "failed": _ATTEMPTS,
                 "unknown_outcome": _ATTEMPTS},
    # Сам переход несёт событие исхода: цепочкой state_change со ссылками на «нужные» события статус не получить.
    via={"attempted": _ATTEMPTS, "confirmed": frozenset({"action.confirmed", "message.out_sent"}),
         "failed": frozenset({"action.failed"}),
         "unknown_outcome": frozenset({"action.unknown_outcome", "message.out_unknown"})},
    initial=frozenset({"accepted"}))

# Пути, которые ведёт только цикл через разборщики видов и события исхода; модель не правит их как state_change.
RESERVED_MODEL_PATHS = (("world_time", "world", "actions"), ("world_time", "world", "creator_requests"))

MACHINES = (INTENTION_MACHINE, SESSION_MACHINE, ACTION_MACHINE)

# Начальное значение группы world_time (docs/spec/state.md#world-time): личность создана, сеанса нет.
WORLD_TIME_DEFAULT = {
    "session_state": "stopped", "session_id": None, "life_time_ms": 0, "pause_reason": None, "paused_at": None,
    "pause_count": 0, "resume_count": 0, "moments": {}, "world": {},
}
