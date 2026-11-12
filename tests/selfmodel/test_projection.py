from iam.panel.selfmodel import render_selfmodel
from iam.panel.views import e
from iam.selfmodel import PARTS, SelfModel
from iam.social import SocialMemory
from state.helpers import set_
from world.helpers import LivingCase


class ProjectionTest(LivingCase):
    def test_six_parts_follow_current_sources_and_fiction_stays_out_of_biography(self):
        self.assertTrue(self.c.apply(self.prop(self.c,
            set_("traits", "traits", [{"name": "care", "value": 0.5}]),
            set_("traits", "values", [{"name": "care", "weight": 0.8}]),
            set_("preferences", "likes", [{"topic_or_activity": "reading", "score": 0.4}]),
        )).applied)
        reading = self.read()
        work = self.write("В вымышленном рассказе я управляла кораблём.")
        source = self.c.find_event(event_id=reading.sources[0])
        autobiographical = self.m.prepare(kind="autobiographical", origin="observation",
                                         text="Я прочитала материал", sources=[source.event_id],
                                         content={"experience": "live"})
        method = self.m.prepare(kind="procedural", origin="own_hypothesis", text="Прочесть дважды",
                                sources=[reading.memory_id], layer="interpretation",
                                content={"procedure": {"skill_id": "reread"}})
        for row in (autobiographical, method):
            self.assertTrue(self.m.write(row).applied)

        # Set the source immediately before projection; earlier activity updates load.
        self.assertTrue(self.c.apply(self.prop(self.c, set_("affect", "load", 0.2))).applied)
        model = SelfModel(SocialMemory(self.c), self.caps)
        data = model.view()
        self.assertEqual(set(data["parts"]), set(PARTS))
        parts = data["parts"]
        self.assertEqual(parts["now"]["load"], 0.2)
        self.assertIn(self.root(self.c), parts["now"]["refs"])
        self.assertEqual(parts["values_view"]["values"][0]["weight"], 0.8)
        self.assertEqual(parts["values_view"]["likes"][0]["score"], 0.4)
        self.assertIn("creator", parts["relations_view"])
        self.assertTrue(parts["skills_view"]["capabilities"])
        self.assertEqual(parts["skills_view"]["methods"][0]["ref"], method.memory_id)
        self.assertTrue(any(item["ref"] == autobiographical.memory_id for item in parts["history"]))
        self.assertEqual(next(item["text"] for item in parts["history"] if item["ref"] == work.memory_id),
                         "Создана работа.")
        self.assertFalse(parts["fallibility"])
        view = render_selfmodel(data, e)
        for label in ("Внимание", "Предпочтения", "Создатель", "Чтение библиотеки", "Создана работа.", "Источники"):
            self.assertIn(label, view)
        self.assertTrue(self.m.fade(reading.memory_id).applied)
        after = model.view()["parts"]
        self.assertFalse(after["skills_view"]["methods"])
        self.assertTrue(all(item["ref"] != reading.memory_id for item in after["history"]))

    def test_render_keeps_levels_and_escapes_model_text(self):
        parts = {key: [] for key in PARTS}
        entries = [dict(level=level, part="now", text="<script>test</script>", state_version=4,
                        refs={"mem_Example01": 1}, traces=[], trait="care", before=0.5, after=0.54)
                   for level in ("observation", "hypothesis", "stable_change")]
        page = render_selfmodel({"parts": parts, "history": entries}, e)
        for label in ("Наблюдение", "Гипотеза", "Изменение черты", "Основания", "0.5 → 0.54"):
            self.assertIn(label, page)
        self.assertIn("&lt;script&gt;test&lt;/script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_failed_result_appears_as_fallibility(self):
        from iam.interfaces import Capability
        from iam.stubs import StubWorld, act

        cap = Capability("cap_Unstable01", "Внешняя возможность")
        self.caps.register(cap)
        self.cycle.world.external = StubWorld("fail")
        report = self.run_reply(act(cap.capability_id))
        self.assertEqual(report.actions[0].status, "failed")
        errors = self.cycle.reflection.model.view()["parts"]["fallibility"]
        self.assertEqual(errors[0]["ref"], report.actions[0].memory_id)
        self.assertIn("Подтверждена неудача", errors[0]["text"])
