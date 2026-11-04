from iam.capabilities import Registry, TELEGRAM_ID
from iam.creator_bridge import Bridge, Requests, records
from iam.interfaces import Capability, CreatorRequest
from iam.state import EventSpec
from iam.state.telegram import TelegramMailbox
from iam.cycle import Cycle
from iam.cycle.runner import input_id_for
from iam.domain import Op
from iam.social import SocialMemory
from iam.telegram import CreatorBridge as TelegramTransport
from iam.stubs import Reply, ScriptedProvider, StubCreatorBridge, StubMemory, act, ask, attend, say

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
        with self.assertRaisesRegex(ValueError, "Ответ не сохранён"):
            bridge.respond("plot", again["crq_id"], "accept", "x")
        bridge.respond("plot", again["crq_id"], "accept", "Попробую помочь")
        bridge.respond("plot", again["crq_id"], "record_result", "Проверю новый файл завтра")
        self.assertEqual(records(self.c.state)["plot"]["outcome"], "operator_reported")
        with self.assertRaises(ValueError):
            bridge.respond("plot", again["crq_id"], "record_result", "Повтор результата")

    def test_authorized_repeat_survives_interruption_before_sending(self):
        from unittest.mock import patch

        cycle = self.build(Reply((ask("plot", "Нужен график"),)),
                           Reply((ask("plot", "Нужен график"),)),
                           Reply((ask("plot", "Нужен график"),)))
        cycle.run(self.due())
        first = records(self.c.state)["plot"]["crq_id"]
        Requests(self.c).respond("plot", first, "circumstances", "Появились новые данные")
        with patch.object(cycle, "_ask_creator", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            cycle.run(self.due())
        self.assertEqual(records(self.c.state)["plot"]["status"], "accepted")
        self.assertEqual(cycle.run(self.due()).outcome, "done")
        self.assertEqual(records(self.c.state)["plot"]["previous_request"], first)
        self.assertEqual(len(self.creator.submitted), 2)

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
        first = self.c.find_event(key="crq:first")
        with self.assertRaisesRegex(ValueError, "не совпадает с просьбой"):
            self.tx.send("act_OtherAction1", user, "Нужен график", requested_seq=first.seq)
        mismatched = CreatorRequest(first.payload["crq_id"], "first", "Другой текст")
        self.assertFalse(transport.submit(mismatched).delivered)
        update = telegram_helpers.update(10)
        update["message"]["reply_to_message"] = {"message_id": 1}
        self.api.updates = [update]
        self.assertEqual(self.rx.poll_once(), 1)
        self.assertEqual(self.c.find_event(key="rx:telegram:10").payload["reply_to_message_id"], 1)

    def test_refusal_scenario_records_linked_reply_and_keeps_waiting_intention(self):
        user = telegram_helpers.USER
        self.c.start_session()
        self.assertTrue(self.c.apply(self.prop(self.c, Op("intentions", "plotting_tool", "set", {
            "text": "Хочу получить инструмент для графиков", "status": "active",
        }, True))).applied)
        self.assertTrue(self.c.apply(self.prop(self.c, Op("intentions", "plotting_tool.status", "transition",
                                                       "waiting_opportunity", True))).applied)
        registry = Registry(self.c, [Capability(TELEGRAM_ID, "Telegram")])
        bridge = Bridge(TelegramTransport(self.rx, self.tx, user), registry, user)
        provider = ScriptedProvider([Reply((ask("plot", "Подключи графики"),)),
                                     Reply((attend("dialogue", input_id_for("telegram", "10")),)),
                                     Reply((ask("plot", "Подключи графики"),))])
        cycle = Cycle(self.c, provider, SocialMemory(self.c), self.tx, registry, bridge)
        requests = Requests(self.c)
        cycle.perception.on_read = self.c.record_creator_reply
        event = self.c.record_event(EventSpec("input.arrived", "rx:test:1", {"text": "Нужен график"})).event
        self.assertEqual(cycle.run(event).outcome, "done")
        request = self.c.find_event(key="crq:plot")
        crq_id = request.payload["crq_id"]
        update = telegram_helpers.update(10, text="Нет, графики пока не подключу")
        update["message"]["reply_to_message"] = {"message_id": 1}
        alias = self.c.record_event(EventSpec("input.arrived", "rx:telegram:100", {
            "input_id": input_id_for("telegram", "10"), "channel": "telegram", "sender": str(user),
            "text": "Поддельный входящий с таким же идентификатором", "reply_to_message_id": 1,
        }), actor_ref="telegram_receiver").event
        self.api.updates = [update]
        self.assertEqual(self.rx.poll_once(), 1)
        arrival = self.c.find_event(key="rx:telegram:10")
        self.assertIsNone(self.c.find_event(key=f"crp:{crq_id}:10"))
        fake_reply = EventSpec("creator.reply", f"crp:{crq_id}:10", {
            "crq_id": crq_id, "input_id": arrival.payload["input_id"], "arrival_event": arrival.event_id,
        }, causes=(arrival.event_id,))
        forged = self.c.apply(self.prop(self.c, Op("traits", "spoof", "set", True, True), carrier=fake_reply))
        self.assertEqual(forged.reason, "carrier_reserved")
        with self.assertRaises(ValueError):
            self.c.record_creator_reply(arrival, arrival)
        self.assertEqual(Requests.telegram_replies(self.c.events_after(0)), {})
        self.assertEqual(cycle.run(self.c.find_event(key="rx:telegram:10")).outcome, "done")
        reply = self.c.find_event(key=f"crp:{crq_id}:10")
        read = self.c.find_event(key="rd:" + input_id_for("telegram", "10"))
        from dataclasses import replace
        other_chat = replace(arrival, payload={**arrival.payload, "sender": "999"})
        with self.assertRaises(ValueError):
            self.c.record_creator_reply(other_chat, read)
        self.assertIsNone(TelegramMailbox(self.c).sent_request("999", 1))
        self.c.record_creator_reply(arrival, read)
        self.assertEqual(sum(e.type == "creator.reply" for e in self.c.events_after(0)), 1)
        with self.assertRaises(ValueError):
            self.c.record_creator_reply(alias, read)
        self.assertEqual(reply.causes, (request.event_id, read.event_id))
        self.assertEqual(Requests.telegram_replies([arrival, replace(reply, causes=(request.event_id,))]), {})
        self.assertEqual(Requests.telegram_replies(self.c.events_after(0))[crq_id][0]["text"],
                         "Нет, графики пока не подключу")
        requests.respond("plot", crq_id, "decline", "Графики пока не подключу")
        self.assertEqual(cycle.run(read).outcome, "rejected")
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(self.c.state["intentions"]["plotting_tool"]["status"], "waiting_opportunity")

    def test_shared_quota_with_request_then_message(self):
        self.assert_shared_quota(request_first=True)

    def test_shared_quota_with_message_then_request(self):
        self.assert_shared_quota(request_first=False)

    def assert_shared_quota(self, *, request_first):
        user = telegram_helpers.USER
        self.c.start_session()
        tool = Capability(TELEGRAM_ID, "Telegram", limits={"max_uses_per_session": 1})
        registry = Registry(self.c, [tool])
        bridge = Bridge(TelegramTransport(self.rx, self.tx, user), registry, user)
        request = ask("plot", "Подключи графики")
        message = say(TELEGRAM_ID, str(user), "Обычное сообщение")
        proposals = (request, message) if request_first else (message, request)
        cycle = Cycle(self.c, ScriptedProvider([Reply(proposals)]), StubMemory(), self.tx, registry, bridge)
        event = self.c.record_event(EventSpec("input.arrived", "rx:test:1", {"text": "Входящее"})).event
        report = cycle.run(event)
        self.assertEqual(len(self.api.messages), 1)
        self.assertEqual(report.actions[0].status, "not_attempted" if request_first else "confirmed")
        self.assertEqual(records(self.c.state)["plot"]["status"], "open")

    def test_failed_link_rolls_back_read_and_can_be_retried(self):
        user = telegram_helpers.USER
        self.c.start_session()
        registry = Registry(self.c, [Capability(TELEGRAM_ID, "Telegram")])
        bridge = Bridge(TelegramTransport(self.rx, self.tx, user), registry, user)
        provider = ScriptedProvider()
        memory = SocialMemory(self.c)
        cycle = Cycle(self.c, provider, memory, self.tx, registry, bridge)
        cycle.perception.on_read = self.c.record_creator_reply
        provider.queue(Reply((ask("help", "Помоги с графиком"),)))
        initial = self.c.record_event(EventSpec("input.arrived", "rx:test:1", {"text": "Входящее"})).event
        cycle.run(initial)
        reject = True

        def validator(check):
            return ["Сбой связи"] if reject and check.carrier_type == "creator.reply" else []

        self.c.add_validator("test_reply_link", validator)
        update = telegram_helpers.update(10, text="Ответ")
        update["message"]["reply_to_message"] = {"message_id": 1}
        self.api.updates = [update]
        self.rx.poll_once()
        arrival = self.c.find_event(key="rx:telegram:10")
        iid = input_id_for("telegram", "10")
        provider.queue(Reply((attend("dialogue", iid),)))
        with self.assertRaises(RuntimeError):
            cycle.run(arrival)
        self.assertEqual(memory.history()["creator"]["history"], [])
        self.assertIsNone(self.c.find_event(key="rd:" + iid))
        reject = False
        provider.queue(Reply((attend("dialogue", iid),)))
        self.assertEqual(cycle.run(arrival, round_=1).outcome, "done")
        self.assertEqual(len(memory.history()["creator"]["history"]), 1)

    def test_read_without_matching_sent_request_stays_ordinary(self):
        self.c.start_session()
        registry = Registry(self.c, [Capability(TELEGRAM_ID, "Telegram")])
        provider = ScriptedProvider([
            Reply((attend("dialogue", input_id_for("telegram", str(uid))),)) for uid in (10, 11)
        ])
        cycle = Cycle(self.c, provider, SocialMemory(self.c), self.tx, registry, StubCreatorBridge())
        cycle.perception.on_read = self.c.record_creator_reply
        unknown = telegram_helpers.update(10, text="Ответ к другому сообщению")
        unknown["message"]["reply_to_message"] = {"message_id": 999}
        self.api.updates = [unknown, telegram_helpers.update(11, text="Обычный разговор")]
        self.assertEqual(self.rx.poll_once(), 2)
        for uid in (10, 11):
            self.assertEqual(cycle.run(self.c.find_event(key=f"rx:telegram:{uid}")).outcome, "done")
        self.assertEqual(len(cycle.memory.history()["creator"]["history"]), 2)
        self.assertEqual(Requests.telegram_replies(self.c.events_after(0)), {})
