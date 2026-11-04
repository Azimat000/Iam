import re
import unittest

from iam.state import EventSpec
from iam.stubs import Reply, ScriptedProvider, ask

from . import test_world


class CreatorPanelTest(unittest.TestCase):
    setUp = test_world.WorldHTTPTest.setUp

    def test_permissions_and_request_response_are_separate(self):
        self.assertIn("Просьб пока нет", self.client.get("/requests").text)
        self.assertIn("Отношения и общение", self.client.get("/state").text)
        page = self.client.get("/capabilities")
        self.assertIn("Временно недоступно", page.text)
        origin = {"Origin": "http://127.0.0.1:18765"}

        def fields(page):
            return {key: re.search(f'name="{key}" value="([^"]*)"', page.text)[1]
                    for key in ("csrf", "epoch", "version")}

        denied = self.client.post("/actions/capability_revoke", data={
            **fields(page), "csrf": "wrong", "capability_id": "cap_Project01", "reason": "Проверка"},
            headers=origin)
        self.assertEqual(denied.status_code, 403)
        revoked = self.client.post("/actions/capability_revoke", data={
            **fields(page), "capability_id": "cap_Project01", "reason": "Проверка"}, headers=origin)
        self.assertEqual(revoked.status_code, 200)
        self.assertIn("Отозвано", revoked.text)
        self.assertEqual(self.client.post("/actions/capability_limits", data={
            **fields(revoked), "capability_id": "cap_Project01", "max_uses_per_session": "2",
            "reason": "Ограничить работу"}, headers=origin).status_code, 200)

        def inject():
            self.runtime.ctl.start_session()
            cycle = self.runtime.cycle
            cycle.provider = ScriptedProvider([Reply((ask("need_access", "<Инструмент>"),))])
            event = self.runtime.ctl.record_event(EventSpec("input.arrived", "rx:test:1", {"text": "проверка"})).event
            cycle.run(event)
            self.runtime.ctl.pause_session()

        self.runtime.inject = inject
        self.runtime.submit("inject").result(5)
        page = self.client.get("/requests")
        self.assertIn("&lt;Инструмент&gt;", page.text)
        self.assertNotIn("<Инструмент>", page.text)
        crq = re.search('name="crq_id" value="([^"]*)"', page.text)[1]
        accepted = self.client.post("/actions/creator_response", data={
            **fields(page), "request_key": "need_access", "crq_id": crq,
            "decision": "accept", "reason": "Смогу проверить"}, headers=origin)
        self.assertEqual(accepted.status_code, 200)
        self.assertIn("Ожидается результат", accepted.text)
        self.assertIn("Смогу проверить", accepted.text)
        self.assertIn("Отозвано", self.client.get("/capabilities").text)

    def test_populated_social_panel_escapes_and_separates_dimensions(self):
        from iam.panel.social import render_social

        html = render_social({
            "people": {"creator": {"history": ["mem_Interaction01"], "last_contact_calendar": "2026-11-04",
                                   "closeness": 0.8, "trust_as_source": 0.2}},
            "agreement": {"<утверждение>": -0.5},
            "readiness": {"<действие>": 0.1},
        })
        for text in ("Близость — 0.8", "Доверие к сведениям — 0.2", "&lt;утверждение&gt; — -0.5",
                     "&lt;действие&gt; — 0.1", "mem_Interaction01", "2026-11-04"):
            self.assertIn(text, html)
        self.assertNotIn("<утверждение>", html)

    def test_result_and_retry_after_circumstances_are_visible(self):
        from iam.panel.requests import render_requests
        from iam.panel.views import event_rows, form

        data = {"state": {"world_time": {"world": {"creator_requests": {"plot": {
            "status": "retry_allowed", "text": "График", "crq_id": "crq_Test0001",
            "outcome": "operator_reported", "answer": "Попробую", "result": "Проверено",
            "circumstances": "Появились данные",
        }}}}}, "requests": [], "telegram_replies": {"crq_Test0001": [{"text": "<Ответ>"}]},
                "sending": False, "epoch": 1, "version": 3}
        html = render_requests(data, "token", form, event_rows)
        for text in ("Проверено", "Появились данные", "&lt;Ответ&gt;", "Можно снова обратиться"):
            self.assertIn(text, html)
        self.assertNotIn("creator_response", html)
