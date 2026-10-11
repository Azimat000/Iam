"""Шесть интерфейсов (R-106): заглушки им соответствуют, цикл работает на любой реализации (тест-двойник)."""
import ast
import unittest
from pathlib import Path

from iam.cycle import Cycle, Runner
from iam.domain import MemoryRecord, Op, Source
from iam.interfaces import (ActionOutcome, ActionRequest, Capabilities, Capability, CognitiveProvider, CreatorBridge,
                            CreatorRequest, Delivery, InboundMessage, Memory, StateController, World)
from iam.state import StateController as RealController
from iam.stubs import (BEHAVIOURS, Reply, ScriptedProvider, StoreMemory, StubCapabilities, StubCreatorBridge,
                       StubMemory, StubWorld, WorldCrash, act, say)

from state.helpers import PID, StateCase

from .helpers import CAP_NOTES, CAP_TG, CycleCase

SRC = Path(__file__).resolve().parents[2] / "src" / "iam"


def imports_of(package: str, banned: set[str]) -> list[str]:
    """Импорты пакета `iam.<package>`, попавшие в запрещённые модули (разбор кода, не поиск по тексту)."""
    found = []
    for path in sorted((SRC / package).glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                    [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            found += [f"{path.name}: {m}" for m in mods if m in banned or m.startswith(tuple(b + "." for b in banned))]
    return found


class ConformanceTest(CycleCase):
    def test_stubs_and_the_real_controller_fit_the_sockets(self):
        self.build()
        for obj, proto in ((ScriptedProvider(), CognitiveProvider), (StubMemory(), Memory), (StubWorld(), World),
                           (StubCapabilities(), Capabilities), (StubCreatorBridge(), CreatorBridge),
                           (self.c, StateController), (StoreMemory(self.c.store), Memory)):
            self.assertIsInstance(obj, proto, f"{type(obj).__name__} не соответствует {proto.__name__}")
        self.assertIsInstance(self.c, RealController)

    def test_foreign_objects_do_not_fit(self):
        for proto in (CognitiveProvider, Memory, World, Capabilities, CreatorBridge, StateController):
            self.assertNotIsInstance(object(), proto)

        class OnlyPropose:
            name = "x"

            def propose(self, request):
                return {}
        self.assertIsInstance(OnlyPropose(), CognitiveProvider)
        self.assertNotIsInstance(OnlyPropose(), World)

    def test_types_validate_themselves(self):
        with self.assertRaises(ValueError):
            Capability("cap_X0000001", "x", status="bogus")
        with self.assertRaises(ValueError):
            ActionRequest("act_X0000001", "teleport", CAP_NOTES)
        with self.assertRaises(ValueError):
            ActionOutcome("great")
        with self.assertRaises(ValueError):
            StubWorld("bogus")
        with self.assertRaises(ValueError):
            StubWorld().will(CAP_NOTES, "bogus")
        self.assertEqual(set(BEHAVIOURS), {"confirm", "fail", "unknown", "crash"})
        self.assertTrue(Capability("cap_X0000001", "x").available)
        self.assertFalse(Capability("cap_X0000001", "x", status="revoked").available)

    def test_stub_world_follows_its_script_then_default(self):
        w = StubWorld().will(CAP_NOTES, "fail", "unknown", "crash")
        req = ActionRequest("act_Test000001", "action", CAP_NOTES)
        self.assertEqual([w.execute(req).status, w.execute(req).status], ["failed", "unknown"])
        with self.assertRaises(WorldCrash):
            w.execute(req)
        self.assertEqual(w.execute(req).status, "confirmed")
        self.assertEqual(len(w.executed), 4)

    def test_stub_memory_search_and_descriptions(self):
        def rec(mid, text, layer="personal"):
            return MemoryRecord(memory_id=mid, persona_id=PID, layer=layer, kind="semantic", origin="observation",
                                content={"text": text}, source=Source("core", "t"), created_at="2026-10-11T10:00:00.000Z",
                                created_life_ms=0, sources=("evt_ABCDEFGH",))
        m = StubMemory([rec("mem_A0000001", "чай с мятой"), rec("mem_B0000001", "глава четвёртая"),
                        rec("mem_C0000001", "чай секретный", layer="archive")])
        self.assertEqual([r.memory_id for r in m.retrieve("чай")], ["mem_A0000001"])
        self.assertEqual([r.memory_id for r in m.retrieve("")], ["mem_B0000001", "mem_A0000001"])
        self.assertEqual(m.retrieve("несуществующее"), [])
        act_req = ActionRequest("act_Test000001", "action", CAP_NOTES)
        msg_req = ActionRequest("act_Test000002", "message", CAP_TG, to="operator", text="Привет")
        self.assertIn(CAP_NOTES, m.describe(act_req, ActionOutcome("confirmed", "готово")))
        self.assertIn("Привет", m.describe(msg_req, ActionOutcome("confirmed")))
        for status in ("failed", "unknown"):
            self.assertIsNone(m.describe(act_req, ActionOutcome(status)))

    def test_store_memory_reads_only_personal_and_interpretation(self):
        self.build()
        recs = [MemoryRecord(memory_id=f"mem_Layer{layer[:4]}01", persona_id=PID, layer=layer, kind="semantic",
                             origin="observation", content={"text": "общее слово лес"}, source=Source("core", "t"),
                             created_at="2026-10-11T10:00:00.000Z", created_life_ms=0, sources=("evt_ABCDEFGH",))
                for layer in ("personal", "interpretation", "archive")]
        with self.c.store.transaction():
            for r in recs:
                self.c.store.add_memory(r)
        found = StoreMemory(self.c.store).retrieve("лес")
        self.assertEqual(sorted(r.layer for r in found), ["interpretation", "personal"])
        self.assertIsNone(StoreMemory(self.c.store).describe(ActionRequest("act_Test000001", "action", CAP_NOTES),
                                                             ActionOutcome("failed")))

    def test_stub_creator_bridge_roundtrip(self):
        b = StubCreatorBridge()
        self.assertEqual(b.submit(CreatorRequest("crq_Test000001", "k", "t")), Delivery(True, ""))
        b.say("ответ", "1")
        self.assertEqual(b.poll_replies(), [InboundMessage("stub", "1", "operator", "ответ")])
        self.assertEqual(b.poll_replies(), [])


class Double:
    """Базовый класс тест-двойников: ни одного общего кода с заглушками."""


class OtherProvider(Double):
    name = "rules"

    def __init__(self):
        self.seen = []

    def propose(self, request):
        self.seen.append(request["request_id"])
        proposal = {"proposal_id": "prop_" + "Rules00001", "kind": "action", "rationale": "правило", "cites": [],
                    "body": {"capability_id": CAP_NOTES, "params": {}}}
        return {"request_id": request["request_id"], "epoch": request["epoch"], "state_version": request["state_version"],
                "provider": {"name": self.name, "model": "if-then"}, "status": "ok", "proposals": [proposal],
                "explanation": "по правилу", "usage": {"calls": 0, "duration_ms": 0}, "tool_use_detected": False}


class OtherWorld(Double):
    def __init__(self):
        self.log = []

    def execute(self, request):
        self.log.append(request.action_id)
        return ActionOutcome("confirmed", "двойник")


class OtherMemory(Double):
    def retrieve(self, query, *, limit=5):
        return []

    def describe(self, action, outcome):
        return "запомнено двойником"


class OtherCaps(Double):
    def list(self):
        return [Capability(CAP_NOTES, "заметки")]

    def get(self, capability_id):
        return self.list()[0] if capability_id == CAP_NOTES else None


class OtherBridge(Double):
    def submit(self, request):
        return Delivery(True)

    def poll_replies(self):
        return []


class Proxy(Double):
    """Другая реализация привратника: делегирует настоящему и считает вызовы."""

    def __init__(self, inner):
        self._inner, self.calls = inner, []

    persona_id = property(lambda self: self._inner.persona_id)
    clock = property(lambda self: self._inner.clock)
    epoch = property(lambda self: self._inner.epoch)
    state_version = property(lambda self: self._inner.state_version)
    state = property(lambda self: self._inner.state)
    session_state = property(lambda self: self._inner.session_state)
    session_id = property(lambda self: self._inner.session_id)

    def __getattr__(self, name):   # методы
        target = getattr(self._inner, name)
        def call(*a, **kw):
            self.calls.append(name)
            return target(*a, **kw)
        return call

    def apply(self, proposal):
        self.calls.append("apply")
        return self._inner.apply(proposal)

    def record_event(self, spec, **kw):
        self.calls.append("record_event")
        return self._inner.record_event(spec, **kw)

    def record_request(self, request):
        self.calls.append("record_request")
        return self._inner.record_request(request)

    def receive_response(self, response):
        self.calls.append("receive_response")
        return self._inner.receive_response(response)

    def register_kind(self, kind, handler):
        return self._inner.register_kind(kind, handler)

    def find_event(self, **kw):
        return self._inner.find_event(**kw)

    def events_after(self, seq):
        return self._inner.events_after(seq)

    def tick(self):
        return self._inner.tick()


class SwapTest(CycleCase):
    """Подмена любой заглушки другой реализацией не требует правок цикла."""

    def test_cycle_runs_on_other_implementations_of_every_socket(self):
        self.build()
        proxy = Proxy(self.c)
        for obj, proto in ((OtherProvider(), CognitiveProvider), (OtherWorld(), World), (OtherMemory(), Memory),
                           (OtherCaps(), Capabilities), (OtherBridge(), CreatorBridge), (proxy, StateController)):
            self.assertIsInstance(obj, proto)
        provider, world = OtherProvider(), OtherWorld()
        cycle = Cycle(proxy, provider, OtherMemory(), world, OtherCaps(), OtherBridge())
        report = cycle.run(self.due())
        self.assertEqual(report.outcome, "done", report)
        self.assertEqual(report.names()[-1], "memory_update")
        self.assertEqual(world.log, [report.actions[0].action_id])
        self.assertEqual(self.c.store.get_memory(report.actions[0].memory_id).text, "запомнено двойником")
        self.assertEqual(proxy.calls.count("receive_response"), 1)
        self.assertIn("apply", proxy.calls)
        # раннер на двойниках: повода нет — обращений нет
        runner = Runner(cycle, proxy, OtherBridge())
        n = len(provider.seen)
        runner.cursor = self.c.store.last_seq()
        self.assertEqual(runner.run_pending(), [])
        self.assertEqual(len(provider.seen), n)

    def test_cycle_package_depends_only_on_sockets(self):
        """Цикл не импортирует заглушки, хранилище и часы: связь только через интерфейсы и привратника."""
        self.assertEqual(imports_of("cycle", {"iam.stubs", "iam.storage", "iam.clock", "iam.backup"}), [])

    def test_interfaces_do_not_import_stubs_or_the_cycle(self):
        self.assertEqual(imports_of("interfaces", {"iam.stubs", "iam.cycle", "iam.storage"}), [])

    def test_import_scan_is_not_vacuous(self):
        self.assertTrue(imports_of("cycle", {"iam.interfaces"}), "проверка ничего не видит: сломан разбор импортов")


if __name__ == "__main__":
    unittest.main()
