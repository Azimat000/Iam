from iam.capabilities import Registry, TELEGRAM_ID
from iam.creator_bridge import Bridge, Requests, delivery_id, records
from iam.interfaces import Capability, CreatorRequest
from iam.state import EventSpec
from iam.cycle import Cycle
from iam.telegram import CreatorBridge as TelegramTransport
from iam.stubs import Reply, ScriptedProvider, StubCreatorBridge, StubMemory, act, ask

from cycle.helpers import CAP_PLOT, INTENTION, CycleCase
from telegram import helpers as telegram_helpers


class RequestsTest(CycleCase):
    def test_missing_tool_and_refusal_keep_intention_and_block_repeats(self):
        text = "Нужен инструмент для графиков"
        cycle = self.build(Reply((act(CAP_PLOT, {}), ask("plot", text))),
                           Reply((ask("plot", text),)), Reply((ask("plot", text),)),
                           Reply((ask("plot", text),)))
        cycle.run(self.due())
        self.assertEqual([e.type for e in self.c.events_after(0) if e.type == "action.confirmed"], [])
        self.assertIn("capability_not_available", [e.payload["reason"] for e in self.c.events_after(0)
                                                  if e.type == "proposal.rejected"])
        first = records(self.c.state)["plot"]["crq_id"]
        bridge = Requests(self.c)
        bridge.respond("plot", first, "decline", "Сегодня графики не нужны")
        self.assertEqual(records(self.c.state)["plot"]["outcome"], "declined")
        self.assertEqual(self.c.state["intentions"][INTENTION]["status"], "active")
        self.assertEqual(cycle.run(self.due()).outcome, "rejected")
        bridge.respond("plot", first, "circumstances", "Появились данные для графика")
        self.assertEqual(cycle.run(self.due()).outcome, "done")
        again = records(self.c.state)["plot"]
        self.assertEqual(again["previous_request"], first)
        self.assertNotEqual(again["crq_id"], first)
        self.assertEqual(cycle.run(self.due()).outcome, "rejected")
        self.assertEqual(len(self.creator.submitted), 2)
        with self.assertRaises(ValueError):
            bridge.respond("plot", first, "accept", "Устаревший ответ")
        bridge.respond("plot", again["crq_id"], "accept", "Попробую помочь")
        bridge.respond("plot", again["crq_id"], "record_result", "Проверю новый файл завтра")
        self.assertEqual(records(self.c.state)["plot"]["outcome"], "operator_reported")
        with self.assertRaises(ValueError):
            bridge.respond("plot", again["crq_id"], "record_result", "Повтор результата")

    def test_linked_telegram_reply_is_visible_only_after_read(self):
        from iam.cycle import Cycle
        from iam.cycle.runner import input_id_for
        from iam.social import SocialMemory
        from iam.stubs import attend

        self.build(Reply((ask("help", "Помоги с графиком"),)))
        self.cycle.run(self.due())
        crq_id = records(self.c.state)["help"]["crq_id"]
        outbox = [{"action_id": delivery_id(crq_id), "status": "sent", "external_id": "101"}]
        memory = SocialMemory(self.c)
        self.cycle = Cycle(self.c, self.provider, memory, self.world, self.caps, self.creator)
        iid = input_id_for("telegram", "9")
        event = self.c.record_event(EventSpec("input.arrived", "rx:telegram:9", {
            "input_id": iid, "channel": "telegram", "sender": "123", "text": "Посмотрю позже",
            "reply_to_message_id": 101,
        }), actor_ref="telegram_receiver").event
        self.assertEqual(Requests.telegram_replies(self.c.events_after(0), outbox), {})
        self.provider.queue(Reply((attend("dialogue", iid),)))
        self.cycle.run(event)
        result = Requests.telegram_replies(self.c.events_after(0), outbox)
        self.assertEqual(result[crq_id], [{"text": "Посмотрю позже", "event_id": event.event_id}])
        self.assertEqual(records(self.c.state)["help"]["status"], "open")

    def test_revoked_telegram_does_not_send_a_request(self):
        ctl = self.controller()
        registry = Registry(ctl, [Capability(TELEGRAM_ID, "Telegram")])
        transport = StubCreatorBridge()
        bridge = Bridge(transport, registry)
        self.assertEqual(bridge.poll_replies(), [])
        req = CreatorRequest("crq_Example001", "key", "Текст")
        self.assertTrue(bridge.submit(req).delivered)
        registry.set_permission(TELEGRAM_ID, grant=False, reason="Не отправлять")
        self.assertFalse(bridge.submit(req).delivered)
        self.assertEqual(transport.submitted, [req])

    def test_identical_text_cannot_bypass_duplicate_check_with_a_new_key(self):
        cycle = self.build(Reply((ask("original", "Нужен график"),)),
                           Reply((ask("another", "  НУЖЕН  график "),)))
        cycle.run(self.due())
        self.assertEqual(cycle.run(self.due()).outcome, "rejected")
        self.assertEqual(list(records(self.c.state)), ["original"])



class TelegramRequestsTest(telegram_helpers.TelegramCase):
    def test_real_bridge_uses_the_same_quota_and_links_a_reply(self):
        user = telegram_helpers.USER
        self.c.start_session()
        tool = Capability(TELEGRAM_ID, "Telegram", limits={"max_uses_per_session": 1})
        registry = Registry(self.c, [tool])
        transport = TelegramTransport(self.rx, self.tx, user)
        bridge = Bridge(transport, registry, user)
        provider = ScriptedProvider([
            Reply((ask("first", "Нужен график"),)),
            Reply((ask("second", "Нужен калькулятор"),)),
        ])
        cycle = Cycle(self.c, provider, StubMemory(), self.tx, registry, bridge)
        for i in (1, 2):
            event = self.c.record_event(EventSpec("input.arrived", f"rx:test:{i}", {"text": "Входящее"})).event
            cycle.run(event)
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(len(records(self.c.state)), 2)
        update = telegram_helpers.update(10)
        update["message"]["reply_to_message"] = {"message_id": 1}
        self.api.updates = [update]
        self.assertEqual(self.rx.poll_once(), 1)
        self.assertEqual(self.c.find_event(key="rx:telegram:10").payload["reply_to_message_id"], 1)
