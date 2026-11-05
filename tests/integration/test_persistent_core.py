"""Сквозные проверки постоянных модулей с накопленным опытом."""

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from iam.backup import create_backup, restore_backup
from iam.capabilities import Registry, TELEGRAM_ID
from iam.clock import life_moment
from iam.creator_bridge import Bridge
from iam.domain import new_id
from iam.interfaces import Capability
from iam.session import SessionRunner
from iam.social import SocialMemory
from iam.state import StateController, verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, StubCreatorBridge, act, rest
from iam.world import CAPABILITIES, READ, WRITE, PersistentWorld
from iam.world.cycle import LivingCycle


def wire(controller, *, provider=None, charge=True, client=None, recipient=None, motivation_off=False, cycle_type=LivingCycle, **cycle_options):
    session = SessionRunner(controller)
    world = PersistentWorld(controller)
    world.initialize()
    status = "available" if client is not None else "unavailable_temporarily"
    telegram = Capability(TELEGRAM_ID, "Telegram", "Сообщения создателю", status)
    registry = Registry(controller, (telegram, *CAPABILITIES))
    creator = Bridge(StubCreatorBridge(deliver=False), registry, recipient)
    if client is not None:
        from iam.telegram import Sender
        world.external = Sender(controller, client, [recipient])
    provider = provider if provider is not None else ScriptedProvider()
    cycle = cycle_type(controller, session.provider(provider, charge=charge), SocialMemory(controller),
                        world, registry, creator, motivation_off=motivation_off, **cycle_options)
    cycle.perception.on_read = controller.record_creator_reply
    return session, cycle, provider


def tick(cycle):
    ctl = cycle.ctl
    moment = life_moment(new_id("mom"), ctl.life_time_ms, "world")
    if not ctl.schedule_moment(moment).applied:
        raise AssertionError("Не удалось назначить момент выбора")
    trigger = ctl.fire_due_moments()[0].events[0]
    return cycle.run(trigger)


def choose(cycle, provider, *proposals, replies=()):
    provider.queue(Reply(proposals), *replies)
    return tick(cycle)


def interrupt(path, phase):
    def wait_for_kill():
        print("READY", flush=True)
        sys.stdin.read()
        raise AssertionError("Дочерний процесс завершён без SIGKILL")

    with Store.open(path) as store:
        ctl = StateController(store)
        session, cycle, provider = wire(ctl)
        if ctl.session_state == "paused":
            session.command("stop", "Подготовка следующего прерывания")
        session.command("start", "Проверка прерывания", max_calls=2)
        if phase == "reserved":
            session.charge()
        elif phase == "transaction":
            with store.transaction():
                report = choose(cycle, provider, act(WRITE, {"project_id": "rolled_back", "title": "Черновик",
                                                            "text": "Эта запись ещё в транзакции"}))
                if len(report.actions) != 1 or report.actions[0].status != "confirmed":
                    raise AssertionError(report)
                wait_for_kill()
        elif phase == "action":
            original = cycle.world.execute

            def lose_result(request):
                if original(request).status != "confirmed":
                    raise AssertionError("Ожидалась готовая, но ещё не сохранённая работа")
                wait_for_kill()

            cycle.world.execute = lose_result
            choose(cycle, provider, act(WRITE, {"project_id": "interrupted", "title": "Черновик",
                                              "text": "Этот текст не был подтверждён"}))
            raise AssertionError("Цикл не дошёл до границы подтверждения")
        wait_for_kill()


class PersistentCoreTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="iam-s25-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "live" / "core.db"
        self.path.parent.mkdir()
        with Store.create(self.path, new_id("exp")) as store:
            ctl = StateController(store)
            ctl.create_persona()
            session, cycle, provider = wire(ctl)
            self.material = cycle.world.add_material("Заметки о свете", "Свет падает на стол.", "Опыт до сбоя")
            session.command("start", "Накопить опыт")
            report = choose(cycle, provider, act(READ, {"material_id": self.material}))
            result, = report.actions
            self.assertEqual(result.status, "confirmed")
            self.read_memory = result.memory_id
            goal = {"proposal_id": new_id("prop"), "kind": "intention", "rationale": "Продолжить прочитанное",
                    "cites": [self.read_memory],
                    "body": {"op": "create", "intention_id": "light", "text": "Написать зарисовку о свете",
                             "criterion": "Описан свет у окна", "project_id": "light", "operation": "write"}}
            report = choose(cycle, provider, goal)
            self.assertEqual(report.outcome, "done")
            self.intention = cycle.intentions.visible()["light"]
            report = choose(cycle, provider, act(WRITE, {"project_id": "light", "title": "Зарисовка",
                                                       "text": "У окна свет ложится на стол."}))
            result, = report.actions
            self.assertEqual(result.status, "confirmed")
            self.work_memory = result.memory_id
            self.work = store.get_memory(self.work_memory).content["action_id"]
            session.command("stop", "Сохранить исходное состояние")

    def assert_history(self, store, cycle):
        self.assertEqual(store.integrity_problems(), [])
        self.assertEqual(verify(store), [])
        self.assertTrue(cycle.world.state["library"][self.material]["read"])
        self.assertEqual(cycle.world.state["works"][self.work]["text"], "У окна свет ложится на стол.")
        self.assertEqual(cycle.intentions.visible()["light"], self.intention)
        self.assertEqual(store.get_memory(self.read_memory).content["outcome"], "confirmed")
        self.assertEqual(store.get_memory(self.work_memory).content["action_id"], self.work)

    def test_hundred_kills_keep_confirmed_work_and_do_not_confirm_interrupted_action(self):
        root = Path(__file__).resolve().parents[2]
        child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(root / part) for part in ("src", "tests"))}
        for index in range(100):
            phase = ("started", "reserved", "transaction", "action")[index % 4]
            with self.subTest(index=index, phase=phase):
                with subprocess.Popen(
                    [sys.executable, "-m", "integration.test_persistent_core", str(self.path), phase],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env,
                ) as child:
                    try:
                        ready, _, _ = select.select([child.stdout], [], [], 15)
                        self.assertTrue(ready, "Дочерний процесс не достиг контрольной точки")
                        line = child.stdout.readline().strip()
                        self.assertEqual(line, "READY", child.stderr.read() if not line else line)
                        child.send_signal(signal.SIGKILL)
                        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
                    finally:
                        if child.poll() is None:
                            child.kill()
                            child.wait()

                with Store.open(self.path) as store:
                    ctl = StateController(store)
                    session, cycle, _ = wire(ctl)
                    self.assertEqual(ctl.session_state, "paused")
                    self.assertEqual(ctl.state["world_time"]["pause_reason"], "crash")
                    self.assertEqual(session.usage()["calls"], int(phase in ("reserved", "action")))
                    self.assertNotIn("rolled_back", cycle.world.state["projects"])
                    self.assertFalse(any(action.get("params", {}).get("project_id") == "rolled_back"
                                         for action in cycle.world.state["actions"].values()))
                    self.assertNotIn("interrupted", cycle.world.state["projects"])
                    if phase == "action":
                        session.command("resume", "Разобрать незавершённое действие")
                        self.assertEqual(len(cycle.settle_unfinished()), 1)
                        self.assertEqual(cycle.settle_unfinished(), [])
                        interrupted = {aid: action for aid, action in cycle.world.state["actions"].items()
                                       if action.get("params", {}).get("project_id") == "interrupted"}
                        self.assertEqual(len(interrupted), (index + 1) // 4)
                        self.assertEqual({action["status"] for action in interrupted.values()}, {"unknown_outcome"})
                        self.assertTrue(all("receipt" not in action for action in interrupted.values()))
                        outcomes = {aid: [] for aid in interrupted}
                        for event in store.events():
                            aid = event.payload.get("action_id")
                            if aid in outcomes and event.type in (
                                "action.unknown_outcome", "action.confirmed", "activity.completed"
                            ):
                                outcomes[aid].append(event.type)
                        self.assertTrue(all(types == ["action.unknown_outcome"] for types in outcomes.values()))
                        self.assertFalse(any(row.content.get("action_id") in interrupted
                                             and row.content.get("outcome") == "confirmed"
                                             for row in ctl.readable_memories()))
                        session.command("stop", "Завершить проверку неизвестного исхода")
                    self.assert_history(store, cycle)

    def test_controls_branch_from_identical_history_and_change_scripted_choice(self):
        def decide(request):
            catalog = json.loads(request["context"][-1]["text"])
            if "light" not in catalog["intentions"] or not any(
                item["ref"] == self.read_memory for item in request["context"]
            ):
                return Reply((rest("В пакете нет основания для продолжения"),))
            return Reply((act(WRITE, {"project_id": "light", "title": "Продолжение",
                                     "text": "Утренний свет отражается от стекла."},
                              cites=(self.read_memory,)),))

        with Store.open(self.path) as store:
            baseline = StateController(store).state
            events = list(store.events())
        for mode in ("full", "memory_off", "motivation_off"):
            with self.subTest(mode=mode):
                branch = self.root / (mode + ".db")
                shutil.copyfile(self.path, branch)
                with Store.open(branch) as store:
                    ctl = StateController(store)
                    self.assertEqual(ctl.state, baseline)
                    self.assertEqual(list(store.events()), events)
                    session, cycle, provider = wire(ctl)
                    cycle.memory.off = mode == "memory_off"
                    cycle.motivation.off = mode == "motivation_off"
                    session.command("start", "Сравнение из одной истории")
                    provider.queue(decide)
                    report = tick(cycle)
                    self.assertEqual(report.outcome, "done")
                    request, = provider.calls
                    catalog = json.loads(request["context"][-1]["text"])
                    serialized = json.dumps(request, ensure_ascii=False)
                    if mode == "full":
                        self.assertIn("light", catalog["intentions"])
                        result, = report.actions
                        self.assertEqual(result.status, "confirmed")
                        self.assertIn("Утренний свет", store.get_memory(result.memory_id).text)
                    else:
                        self.assertFalse(catalog["motivation"]["enabled"])
                        self.assertEqual(catalog["intentions"], {})
                        self.assertNotIn(self.intention["text"], serialized)
                        self.assertEqual(report.actions, [])
                        self.assertEqual(cycle.world.state["projects"]["light"]["latest_work"], self.work)
                    if mode == "memory_off":
                        self.assertNotIn(self.read_memory, serialized)
                        self.assertNotIn("Свет падает на стол", serialized)
                        self.assertEqual(catalog["world_results"], [])
                        self.assertEqual(catalog["projects"], {})
                    else:
                        self.assertIn(self.read_memory, serialized)
                        self.assertIn("Свет падает на стол", serialized)
                    self.assertEqual(verify(store), [])

    def test_experience_appraisal_and_motivation_survive_restart_and_ground_later_reflection(self):
        def draft(kind, field, spec, text, *cites):
            return {"proposal_id": new_id("prop"), "kind": "memory_write", "rationale": "Осмыслить опыт",
                    "cites": list(cites), "body": {"draft": {"kind": kind, "origin": "own_hypothesis",
                                                            "content": {"text": text, field: spec}}}}

        def reflection(body, *cites):
            return {"proposal_id": new_id("prop"), "kind": "reflection", "rationale": "Осмыслить опыт",
                    "cites": list(cites), "body": body}

        with Store.open(self.path) as store:
            ctl = StateController(store)
            session, cycle, provider = wire(ctl)
            session.command("start", "Осмыслить прочитанное")
            appraisal = draft("emotional_associative", "appraisal",
                              {"valence": 0.7, "arousal": 0.5, "relevance": 0.9, "regulation": 0.4},
                              "Прочитанное привлекло внимание", self.read_memory)
            self.assertEqual(choose(cycle, provider, appraisal).outcome, "done")
            emotion = ctl.state["affect"]["emotion"]["memory_id"]
            self.assertIn(self.read_memory, store.get_memory(emotion).sources)

            motivation = draft("semantic", "motivation", {"topic": "light", "interest": 0.8, "need": 0.4},
                               "Хочу продолжить работу о свете", self.read_memory, emotion)
            self.assertEqual(choose(cycle, provider, motivation).outcome, "done")
            motive = ctl.state["preferences"]["s22"]["light"]["memory_id"]
            self.assertEqual(set(store.get_memory(motive).sources), {self.read_memory, emotion})
            session.command("stop", "Сохранить интерпретацию")
            state_at_stop = ctl.state

        with Store.open(self.path) as store:
            ctl = StateController(store)
            self.assertEqual(ctl.state, state_at_stop)
            session, cycle, provider = wire(ctl)
            session.command("start", "Вернуться к незавершённой работе")

            def decide(request):
                catalog = json.loads(request["context"][-1]["text"])
                source = catalog["motivation"]["interests"]["light"]["source"]
                self.assertEqual(source, motive)
                self.assertEqual(catalog["intentions"]["light"], self.intention)
                self.assertIn(emotion, {entry["ref"] for entry in request["context"]})
                return Reply((act(WRITE, {"project_id": "light", "title": "Новая зарисовка",
                                          "text": "Свет у окна стал мягче."}, cites=(source, emotion)),))

            provider.queue(decide)
            report = tick(cycle)
            self.assertEqual(report.outcome, "done")
            work, = report.actions
            self.assertEqual(work.status, "confirmed")
            work_memory = store.get_memory(work.memory_id)
            self.assertEqual(work_memory.content["outcome"], "confirmed")
            self.assertNotEqual(work.memory_id, self.work_memory)

            def reflect_on_result(request):
                self.assertEqual(request["purpose"], "reflect")
                self.assertIn(work.memory_id, {item["ref"] for item in request["context"]})
                return Reply((reflection({"part": "history", "level": "observation",
                                          "text": "Создана работа."}, work.memory_id),))

            report = choose(cycle, provider, reflection({}), replies=(reflect_on_result,))
            self.assertEqual((report.outcome, len(report.requests)), ("done", 2))
            saved, = cycle.reflection.model.view()["history"]
            self.assertEqual(saved["level"], "observation")
            self.assertIn(work.memory_id, saved["refs"])
            self.assertEqual(verify(store), [])

    def test_backup_keeps_connected_history_and_excludes_later_work(self):
        with Store.open(self.path) as store:
            ctl = StateController(store)
            session, cycle, provider = wire(ctl)
            backup = create_backup(self.path, self.root / "backups", controller=ctl)
            session.command("start", "Работа после копии")
            result, = choose(cycle, provider, act(WRITE, {"project_id": "later", "title": "После копии",
                                                         "text": "Поздняя работа."})).actions
            self.assertEqual(result.status, "confirmed")
            session.command("stop", "Перед восстановлением")
            live_state = ctl.state
        restored = restore_backup(backup.info.path, self.root / "restored", reason="Проверка истории",
                                  live_db=self.path)
        self.assertNotEqual(restored.path, self.path)
        with Store.open(restored.path) as store:
            ctl = StateController(store)
            _, cycle, _ = wire(ctl)
            self.assertEqual(ctl.epoch, restored.from_epoch + 1)
            self.assertEqual(sum(event.type == "system.restored" for event in store.events()), 1)
            self.assertNotIn("later", cycle.world.state["projects"])
            self.assert_history(store, cycle)
        with Store.open(self.path) as store:
            self.assertEqual(StateController(store).state, live_state)


if __name__ == "__main__":
    interrupt(Path(sys.argv[1]), sys.argv[2])
