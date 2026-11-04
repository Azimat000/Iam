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
