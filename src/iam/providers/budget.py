"""Бюджет сеанса и техническая пауза (R-191, PLAN §5): 30 минут или 10 обращений к исполнителю — что раньше.

Обращение — каждый запуск исполнителя (повтор по формату тоже). Исчерпание бюджета, ошибка входа, лимит подписки
и повторяющийся сбой останавливают сеанс **с сохранением состояния**: пауза с причиной, модель больше не
вызывается. Пишет пауза привратник (`pause_session`), здесь только решение «пора».
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from iam.domain import Op, Transition, new_id
from iam.interfaces import CognitiveProvider, ProviderError
from iam.state import ApplyResult, EventSpec, Proposal, RecordResult

from .errors import (ProviderAuthError, ProviderLimitError, SessionLimitReached)
from .run import RunResult, Runner

class SessionControl(Protocol):
    """Что нужно бюджету от привратника (настоящий `iam.state.StateController` подходит)."""

    persona_id: str
    clock: object

    @property
    def epoch(self) -> int: ...

    @property
    def state_version(self) -> int: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def session_state(self) -> str: ...

    def apply(self, proposal: Proposal) -> ApplyResult: ...

    def record_event(self, spec, *, actor: str = "core", actor_ref: str = "state_controller") -> RecordResult: ...

    def pause_session(self, reason: str = "operator", *, last_alive_at: str | None = None) -> ApplyResult: ...


DEFAULT_MAX_CALLS = 10
DEFAULT_MAX_MINUTES = 30


@dataclass
class SessionBudget:
    """Счётчики одного сеанса. При смене сеанса обнуляются; время считается по монотонным часам с первого обращения."""

    max_calls: int = DEFAULT_MAX_CALLS
    max_ms: int = DEFAULT_MAX_MINUTES * 60_000
    session_id: str | None = None
    calls: int = 0
    started_ms: int = 0

    def charge(self, session_id: str | None, now_ms: int) -> str | None:
        """Списать одно обращение. Вернуть `None`, если можно, иначе `calls` или `time` (что наступило первым:
        время проверяется раньше счётчика, чтобы по истечении 30 минут отказ называл именно время)."""
        if session_id != self.session_id:
            self.session_id, self.calls, self.started_ms = session_id, 0, now_ms
        if now_ms - self.started_ms >= self.max_ms:
            return "time"
        if self.calls >= self.max_calls:
            return "calls"
        self.calls += 1
        return None


class BudgetedRunner:
    """Обёртка над исполнителем запуска: каждый запуск списывает обращение, а при исчерпании — не выполняется."""

    def __init__(self, runner: Runner, budget: SessionBudget, controller: SessionControl):
        self._runner, self.budget, self._ctl = runner, budget, controller

    def __call__(self, argv, stdin, timeout_s, cleanup=None) -> RunResult:
        over = self.budget.charge(self._ctl.session_id, self._ctl.clock.monotonic_ms())
        if over:
            raise SessionLimitReached(over, f"бюджет сеанса исчерпан ({'время' if over == 'time' else 'обращения'})")
        return self._runner(argv, stdin, timeout_s, cleanup)


class PausingProvider:
    """Провайдер с технической паузой: при бюджете/входе/лимите/повторном сбое останавливает сеанс и пробрасывает ошибку.
    Реализует `CognitiveProvider`; внутри — любой другой провайдер."""

    def __init__(self, inner: CognitiveProvider, controller: SessionControl, *, failure_threshold: int = 2):
        self.inner, self._ctl, self.failure_threshold = inner, controller, failure_threshold
        self.failures = 0

    @property
    def name(self) -> str:
        return self.inner.name

    def propose(self, request: dict) -> dict:
        if self._ctl.session_state != "running":
            raise ProviderError("сеанс не идёт: модель не вызывается")
        try:
            response = self.inner.propose(request)
        except SessionLimitReached as e:
            self._stop("session_limit", e.kind, str(e), limit=True)
            raise
        except ProviderLimitError as e:   # лимит подписки; по time.md — та же пауза, что и потеря входа
            self._stop("auth_lost", "subscription", str(e), limit=True)
            raise
        except ProviderAuthError as e:
            self._stop("auth_lost", "auth", str(e), limit=False)
            raise
        except ProviderError as e:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self._stop("repeated_failure", "failures", str(e), limit=False)
            raise
        self.failures = 0
        return response

    def _stop(self, reason: str, kind: str, message: str, *, limit: bool) -> None:
        """Записать причину и поставить сеанс на паузу. Порядок: запись о лимите/ошибке → `session.paused`."""
        sid = self._ctl.session_id
        if limit:
            event_id = new_id("evt")
            t = Transition(new_id("trn"), self._ctl.persona_id, self._ctl.epoch, self._ctl.state_version,
                           (Op("world_time", f"world.limits.{kind}", "set", {"reason": reason, "detail": message[:200]}, True),),
                           f"достигнут предел: {kind}", (event_id,))
            spec = EventSpec("system.limit_reached", f"lim:{sid}:{kind}", {"kind": kind, "reason": reason}, event_id=event_id)
            self._ctl.apply(Proposal(t, "core", "provider_guard", spec))
        else:
            self._ctl.record_event(EventSpec("system.error", f"err:{kind}:{sid}", {"fault": kind, "reason": reason,
                                                                                    "detail": message[:200]}))
        self.failures = 0
        self._ctl.pause_session(reason)
