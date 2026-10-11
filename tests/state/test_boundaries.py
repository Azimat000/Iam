"""Единственный писатель (R-025, R-105): второй процесс отклонён; запись в хранилище из других модулей
запрещена; kill -9 посреди работы контроллера оставляет согласованное состояние."""
import ast
import inspect
import os
import random
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from iam.state import StateController, replay, verify
from iam.storage import AlreadyLocked, Store

from .helpers import StateCase, set_

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
CHILD = HERE / "child_state.py"
KILLS = 25

# Публичные методы хранилища, меняющие базу. Список закреплён намеренно: новый метод записи требует осознанного
# решения — добавить сюда и проверить, что его вызывает только привратник.
WRITERS = {"transaction", "append_event", "put_snapshot", "add_memory", "update_memory", "add_inbox", "add_outbox",
           "set_outbox_status", "heartbeat"}
ALLOWED_PACKAGES = {"storage", "state", "clock"}   # clock пишет только контрольную точку времени


def spawn(*args):
    env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.Popen([sys.executable, str(CHILD), *map(str, args)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)


class SingleWriterTest(StateCase):
    def test_store_write_api_is_pinned(self):
        found = {name for name, fn in inspect.getmembers(Store, inspect.isfunction)
                 if not name.startswith("_") and "self._writable()" in inspect.getsource(fn)}
        self.assertEqual(found, WRITERS, "появился новый метод записи в хранилище: обновите WRITERS и убедитесь, что вызывает его только iam.state")

    def test_only_state_package_writes_to_the_store(self):
        """R-105: изменить состояние и журнал можно только через привратник. Статическая проверка: ни один модуль
        вне storage/state/clock не вызывает методов записи хранилища."""
        offenders = []
        for path in sorted((SRC / "iam").rglob("*.py")):
            package = path.relative_to(SRC / "iam").parts[0]
            if package in ALLOWED_PACKAGES or package == path.name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in WRITERS | {"con"}:
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno} .{node.attr}")
        self.assertEqual(offenders, [], "запись в хранилище в обход StateController")

    def test_scan_actually_detects_offenders(self):
        """Сама проверка не вакуумна: на искусственном нарушителе она срабатывает."""
        bad = ast.parse("def f(store):\n    store.append_event(1)\n")
        hits = [n for n in ast.walk(bad) if isinstance(n, ast.Attribute) and n.attr in WRITERS]
        self.assertEqual(len(hits), 1)

    def test_second_process_cannot_open_for_writing(self):
        c = self.controller(running=True)
        child = spawn("try", self.db)
        out, err = child.communicate(timeout=30)
        self.assertEqual(out.strip(), "LOCKED", err)
        c.store.close()
        child = spawn("try", self.db)
        out, err = child.communicate(timeout=30)
        self.assertEqual(out.strip(), "OPENED", err)

    def test_second_controller_in_same_process_is_refused(self):
        c = self.controller()
        with self.assertRaises(AlreadyLocked):
            Store.open(self.db)
        self.assertEqual(c.state_version, 1)

    def test_race_of_two_writes_on_one_base_is_impossible(self):
        c = self.controller(running=True)
        a, b = self.prop(c, set_("traits", "w", "a")), self.prop(c, set_("traits", "w", "b"))
        self.assertEqual([c.apply(a).status, c.apply(b).status], ["applied", "stale"])


class KillTest(StateCase):
    def test_kill9_leaves_consistent_state(self):
        rnd = random.Random(2026)
        versions = []
        for i in range(KILLS):
            child = spawn("run", self.db)
            line = child.stdout.readline().strip()
            if line != "READY":
                child.kill()
                self.fail(f"подпроцесс не запустился: {child.stderr.read()}")
            time.sleep(rnd.uniform(0.0, 0.15))
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
            child.stdout.close()
            child.stderr.close()
            store = Store.open(self.db)
            try:
                c = StateController(store)         # перезапуск: запись о паузе crash, время жизни не убывает
                self.assertEqual(verify(store), [], f"итерация {i}")
                self.assertEqual(store.integrity_problems(), [])
                self.assertEqual(c.state_version, store.latest_snapshot().state_version)
                self.assertEqual(c.state, list(replay(store))[-1].state)
                self.assertEqual((c.session_state, c.state["world_time"]["pause_reason"]), ("paused", "crash"))
                versions.append(c.state_version)
            finally:
                store.close()
        self.assertEqual(versions, sorted(versions))
        self.assertGreater(versions[-1], versions[0], "подпроцесс успевал совершать переходы")


if __name__ == "__main__":
    unittest.main()
