"""Все обращения к SQLite выполняет один поток. Во время внешнего I/O он обслуживает
команды панели: пауза не ждёт ответа модели. Второй цикл при этом никогда не запускается.
"""
import datetime as dt
import json
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from iam.backup import create_backup, default_backup_dir, list_backups, open_for_write, restore_backup
from iam.backup.scan import SecretScanner
from iam.cycle import Cycle, Runner
from iam.domain import new_id
from iam.interfaces import Capability
from iam.providers import ClaudeCodeProvider, SandboxConfig, subprocess_runner
from iam.session import SessionConflict, SessionRunner
from iam.state import EventSpec, StateController
from iam.state.session import panel_records
from iam.state.telegram import TelegramMailbox
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, StoreMemory, StubCapabilities, StubCreatorBridge, StubWorld, rest
from iam.telegram import Client, CreatorBridge, Receiver, Sender


class Runtime:
    def __init__(self, db, *, demo=False, token_file=None, telegram_user=None, interval=0.5):
        self.db, self.demo = Path(db).resolve(), demo
        self.token_file, self.telegram_user, self.interval = token_file, telegram_user, interval
        self.jobs = queue.Queue()
        self.ready = Future()
        self.closed = False
        self.fatal = None
        self.sending = False
        self.stop_requested = False
        self.thread = threading.Thread(target=self._serve, name="iam-state", daemon=True)

    def start(self):
        self.thread.start()
        self.ready.result(timeout=15)

    def submit(self, name, *args, **kwargs):
        future = Future()
        if self.closed or self.fatal:
            future.set_exception(RuntimeError("Панель остановлена. Перезапустите сервер."))
        else:
            self.jobs.put((future, name, args, kwargs))
        return future

    def close(self):
        if not self.closed and not self.fatal:
            self.submit("shutdown").result(timeout=15)
        self.thread.join(timeout=120)

    def _pump(self, timeout):
        try:
            future, name, args, kwargs = self.jobs.get(timeout=timeout)
        except queue.Empty:
            return
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(getattr(self, name)(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)

    def _io(self, fn, *args, **kwargs):
        work = self.pool.submit(fn, *args, **kwargs)
        while not work.done():
            self._pump(0.02)
            if not self.sending:
                self.session.check_limits()
        return work.result()

    def _telegram_call(self, call, method, params):
        self.sending = method == "sendMessage"
        try:
            return self._io(call, method, params)
        finally:
            self.sending = False

    def _model_run(self, argv, stdin, timeout, cleanup=None):
        self.session.charge()
        self.session.in_flight = True
        try:
            return self._io(subprocess_runner, argv, stdin, timeout, cleanup)
        finally:
            self.session.in_flight = False

    def _setup(self):
        if self.demo and not self.db.exists():
            self.store = Store.create(self.db, new_id("exp"))
        else:
            self.store = open_for_write(self.db, default_backup_dir(self.db))
        self.ctl = StateController(self.store)
        if self.demo and self.store.store_kind != "experimental":
            raise ValueError("Демонстрация разрешена только в экспериментальной базе.")
        if self.ctl.state_version == 0:
            self.ctl.create_persona()
        self.session = SessionRunner(self.ctl)
        self.scanner = SecretScanner(use_env=True)
        self.receiver = None
        self.caps = StubCapabilities()
        creator, world = StubCreatorBridge(deliver=False), StubWorld("fail")
        if self.telegram_user is not None:
            client = Client(timeout=0)
            call = client.call
            client.call = lambda method, params: self._telegram_call(call, method, params)
            self.receiver = Receiver(self.ctl, client, [self.telegram_user])
            world = Sender(self.ctl, client, [self.telegram_user])
            creator = CreatorBridge(self.receiver, world, self.telegram_user)
            self.caps = StubCapabilities([Capability("cap_Telegram01", "Telegram", "Сообщения создателю")])
        if self.demo:
            provider = ScriptedProvider([lambda request: Reply(proposals=(rest("Демонстрация: действий нет."),))] * 100,
                                        name="demo")
        else:
            provider = ClaudeCodeProvider(self._model_run, SandboxConfig(token_file=self.token_file))
        provider = self.session.provider(provider, charge=self.demo)
        self.cycle = Cycle(self.ctl, provider, StoreMemory(self.store), world, self.caps, creator)
        self.runner = Runner(self.cycle, self.ctl, creator, max_cycles=1)

    def _serve(self):
        self.store = None
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="iam-io") as self.pool:
                self._setup()
                self.ready.set_result(None)
                while not self.closed:
                    self._pump(self.interval)
                    if not self.closed:
                        try:
                            self.session.step(self.runner)
                            if self.stop_requested:
                                self.shutdown()
                        except Exception as exc:
                            # Нет места: step уже пытался остановить сеанс; не продолжаем цикл вслепую.
                            self.fatal = type(exc).__name__
                            break
        except Exception as exc:
            self.fatal = type(exc).__name__
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            if self.store is not None:
                self.store.close()
            self.closed = True
            while not self.jobs.empty():
                future, _, _, _ = self.jobs.get_nowait()
                if not future.done():
                    future.set_exception(RuntimeError("Сервер остановлен. Перезапустите панель."))

    def shutdown(self):
        if self.sending:
            self.stop_requested = True
            return
        if self.ctl.session_state == "running":
            self.ctl.pause_session("operator")
        self.closed = True

    def command(self, action, reason, **kwargs):
        self._writable_now()
        self.session.command(action, reason, **kwargs)

    def _writable_now(self):
        if self.sending:
            raise SessionConflict("Telegram завершает отправку. Повторите команду после записи её исхода.")

    def mutate(self, action, fields):
        self._writable_now()
        if (int(fields.get("epoch", 0)), int(fields.get("version", 0))) != (self.ctl.epoch, self.ctl.state_version):
            raise SessionConflict("Состояние изменилось. Обновите страницу и повторите действие.")
        reason = fields.get("reason", "").strip()
        if not reason or len(reason) > 500:
            raise ValueError("Укажите причину действия длиной от 1 до 500 символов.")
        if action == "backup":
            return self.backup(reason)
        if action == "restore":
            if fields.get("confirm") != "copy":
                raise ValueError("Отметьте создание отдельной базы из копии.")
            return self.restore(fields.get("backup_id"), reason)
        limits = {}
        if action == "start":
            limits = {"max_calls": int(fields.get("calls", 10)), "max_ms": int(fields.get("minutes", 30)) * 60_000}
        return self.command(action, reason, **limits)

    def view(self, *, before=None, version=None):
        ctl = self.ctl
        events = ctl.events_after(0)
        selected = [e for e in events if before is None or e.seq < before][-50:]
        records = panel_records(ctl)
        state = self.store.get_snapshot(ctl.epoch, version).state if version else ctl.state
        infos, broken = list_backups(default_backup_dir(self.db))
        box = TelegramMailbox(ctl)
        last_poll = box.get("last_poll_at")
        age = ((dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(last_poll)).total_seconds() if last_poll else None)
        requests = [e.to_dict() for e in events if e.type == "creator.request"][-50:]
        return {"persona_id": ctl.persona_id, "demo": self.demo, "session_state": ctl.session_state,
                "session_id": ctl.session_id, "pause_reason": ctl.state["world_time"].get("pause_reason"),
                "epoch": ctl.epoch, "version": ctl.state_version, "view_version": version,
                "life_ms": ctl.life_time_ms, "usage": self.session.usage(), "sending": self.sending, "state": state,
                "events": [e.to_dict() for e in reversed(selected)], "history": records["history"],
                "memories": records["memories"], "requests": requests, "capabilities": [c.__dict__ for c in self.caps.list()],
                "errors": [e.to_dict() for e in events if e.type in ("system.error", "system.limit_reached")][-50:],
                "backups": [{"id": i.manifest["snapshot_id"], "created_at": i.manifest["created_at"],
                             "version": i.manifest["state_version"]} for i in infos],
                "broken_backups": len(broken), "last_poll": last_poll, "poll_stale": age is None or age > 60,
                "outbox": records["outbox"]}

    def backup(self, reason):
        self._writable_now()
        self.session.audit("backup", reason)
        result = create_backup(self.db, default_backup_dir(self.db), reason=reason, controller=self.ctl)
        return result.info.manifest["snapshot_id"]

    def restore(self, backup_id, reason):
        self._writable_now()
        if self.ctl.session_state == "running" or self.session.in_flight:
            raise SessionConflict("Перед восстановлением поставьте сеанс на паузу и дождитесь ответа модели.")
        infos, _ = list_backups(default_backup_dir(self.db))
        found = next((i for i in infos if i.manifest["snapshot_id"] == backup_id), None)
        if found is None:
            raise ValueError("Копия не найдена. Обновите список копий.")
        self.session.audit("restore_copy", reason)
        target = self.db.parent / "restored" / new_id("opr")
        result = restore_backup(found.path, target, reason=reason, live_db=self.db)
        return str(result.path)
