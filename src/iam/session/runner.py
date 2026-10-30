"""Сеанс: один писатель, постоянный бюджет, пауза с сохранением времени жизни."""
from iam.backup import SpaceGuard, guarded_call
from iam.domain import Op, new_id
from iam.interfaces import ProviderError
from iam.providers import PausingProvider
from iam.providers.errors import SessionLimitReached
from iam.state.session import SessionLedger


class SessionConflict(ValueError):
    """Команда несовместима с состоянием сеанса."""


def protect_panel(check):
    if check.actor == "operator":
        return ()
    before = check.before["world_time"].get("world", {})
    after = check.after["world_time"].get("world", {})
    if any(before.get(key) != after.get(key) for key in ("panel_session", "panel_command")):
        return ("Настройки и команды панели меняет только оператор.",)
    return ()


class SessionRunner:
    def __init__(self, controller, *, space=None):
        self.ctl = controller
        self.ledger = SessionLedger(controller)
        self.ledger.seed_from_journal()
        self.space = space or SpaceGuard(controller.store.path)
        self.in_flight = False
        self.request_id = None
        controller.add_validator("session_panel", protect_panel)

    def config(self):
        cfg = self.ctl.state["world_time"].get("world", {}).get("panel_session", {})
        sid = self.ctl.session_id
        if cfg and (sid is None or cfg.get("session_id") == sid):
            return cfg
        # Сеанс, запущенный старым кодом: отсчёт от session.started, даже без запросов.
        starts = [e for e in self.ctl.events_after(0) if e.type == "session.started" and e.payload["session_id"] == sid]
        return {"session_id": sid, "max_calls": 10, "max_ms": 1_800_000,
                "started_ms": starts[-1].life_time_ms if starts else self.ctl.life_time_ms}

    def usage(self):
        cfg = self.config()
        sid = cfg["session_id"]
        calls = len(self.ledger.read(sid)["attempts"]) if sid else 0
        return {**cfg, "calls": calls, "elapsed_ms": max(0, self.ctl.life_time_ms - cfg["started_ms"]),
                "in_flight": self.in_flight}

    def exhausted(self, *, include_calls=True):
        u = self.usage()
        if u["elapsed_ms"] >= u["max_ms"]:
            return "time"
        if include_calls and u["calls"] >= u["max_calls"]:
            return "calls"
        return None

    def _apply_ok(self, result):
        if not result.applied:
            raise SessionConflict("Операция отклонена ядром: " + str(result.reason))

    def audit(self, action, reason):
        if not reason.strip():
            raise ValueError("Укажите причину действия.")
        self._apply_ok(self.ctl.intervene("technical_fix", reason, (
            Op("world_time", "world.panel_command", "set", {"action": action, "id": new_id("opr")}, True),
        ), target="session"))

    def command(self, action, reason, *, max_calls=10, max_ms=1_800_000):
        def perform():
            current = self.ctl.session_state
            allowed = {"start": ("stopped",), "pause": ("running",), "resume": ("paused",),
                       "stop": ("running", "paused")}
            if action not in allowed or current not in allowed[action]:
                raise SessionConflict("Команда недоступна в текущем состоянии сеанса.")
            if action in ("start", "resume") and self.in_flight:
                raise SessionConflict("Дождитесь окончания текущего обращения к модели.")
            if action == "resume" and self.exhausted():
                raise SessionConflict("Лимит исчерпан. Завершите этот сеанс и начните новый.")
            if action == "start":
                if type(max_calls) is not int or type(max_ms) is not int or not 1 <= max_calls <= 10 or not 1 <= max_ms <= 1_800_000:
                    raise ValueError("Лимиты: от 1 до 10 обращений и не больше 30 минут.")
                if not reason.strip():
                    raise ValueError("Укажите причину действия.")
                sid = new_id("ses")
                cfg = {"session_id": sid, "max_calls": max_calls, "max_ms": max_ms, "started_ms": self.ctl.life_time_ms}
                self._apply_ok(self.ctl.intervene("technical_fix", reason, (
                    Op("world_time", "world.panel_session", "set", cfg, True),), target="session"))
                result = self.ctl.start_session(session_id=sid)
            else:
                self.audit(action, reason)
                method = {"pause": self.ctl.pause_session, "resume": self.ctl.resume_session, "stop": self.ctl.stop_session}[action]
                result = method()
            self._apply_ok(result)
        return guarded_call(self.ctl, self.space, perform)

    def check_limits(self):
        if self.ctl.session_state != "running":
            return
        kind = self.exhausted(include_calls=not self.in_flight)
        if kind:
            self.ctl.pause_session("session_limit")

    def charge(self):
        self.space.ensure()
        if self.ctl.session_state != "running":
            raise ProviderError("Сеанс на паузе: исполнитель не запускается.")
        kind = self.exhausted()
        if kind:
            self.ctl.pause_session("session_limit")
            raise SessionLimitReached(kind, "Лимит сеанса исчерпан.")
        cfg = self.config()
        if not self.ledger.reserve(self.ctl.session_id, cfg["max_calls"], self.request_id):
            raise SessionLimitReached("calls", "Лимит обращений исчерпан.")

    def provider(self, inner, *, charge=True):
        """Для Claude charge=False: учитывается каждый запуск CLI, включая исправление формата."""
        owner = self

        class Counted:
            name = inner.name

            def propose(self, request):
                owner.request_id = request["request_id"]
                if charge:
                    owner.charge()
                return inner.propose(request)

        return PausingProvider(Counted(), self.ctl)

    def step(self, cycle_runner):
        def perform():
            self.check_limits()
            reports = cycle_runner.run_pending()
            self.check_limits()
            return reports
        return guarded_call(self.ctl, self.space, perform)
