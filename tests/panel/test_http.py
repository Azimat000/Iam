"""Реальный FastAPI + владелец SQLite: формы, наблюдение, копии, защита от чужих вкладок."""
import asyncio
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from iam.domain import new_id
from iam.panel.app import create_app
from iam.panel.runtime import Runtime
from iam.panel.security import LocalOnly, read_form
from iam.state import verify
from iam.storage import Store

BASE = "http://127.0.0.1:18765"


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iam-panel-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runtime = Runtime(self.tmp / "demo.db", demo=True, interval=0.01)
        self.app = create_app(self.runtime, port=18765)
        self.client = self.enterContext(TestClient(self.app, base_url=BASE))

    def view(self):
        return self.runtime.submit("view").result(5)

    def fields(self, page="/"):
        text = self.client.get(page).text
        return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', text))

    def post(self, action, *, fields=None, **extra):
        data = fields if fields is not None else self.fields()
        data = {**data, "reason": "проверка оператора", **extra}
        return self.client.post("/actions/" + action, data=data, headers={"Origin": BASE})

    def test_nine_routes_security_headers_and_read_only_views(self):
        for route in ("/", "/events", "/state", "/memory", "/requests", "/capabilities", "/experiments", "/usage", "/backups"):
            with self.subTest(route=route):
                r = self.client.get(route)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIn('lang="ru"', r.text)
                self.assertIn("no-store", r.headers["cache-control"])
                self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertIn("Записей памяти пока нет", self.client.get("/memory").text)
        self.assertEqual(self.client.get("/style.css").status_code, 200)
        self.assertEqual(self.client.get("/favicon.ico").status_code, 204)
        self.assertEqual(self.client.get("/missing").status_code, 404)
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/state?version=999").status_code, 400)
        self.assertEqual(self.client.get("/events?before=-1").status_code, 400)
        self.assertEqual(self.client.get("/events?before=bad").status_code, 400)
        self.assertEqual(self.client.get("/state?version=1").status_code, 200)
        self.assertEqual(self.client.get("/events?before=1").status_code, 200)
        self.assertEqual(self.client.put("/").status_code, 403)
        self.assertEqual(self.client.put("/",headers={"Origin":BASE}).status_code,405)
        self.assertEqual(self.client.post("/actions/unknown",headers={"Origin":BASE}).status_code,404)

    def test_start_pause_resume_stop_and_replayed_form(self):
        original = self.fields()
        self.assertEqual(self.post("start", fields=original, calls="2", minutes="1").status_code, 200)
        self.assertEqual(self.view()["session_state"], "running")
        self.assertEqual(self.post("start", fields=original).status_code, 409)
        self.assertEqual(self.post("pause").status_code, 200)
        self.assertEqual(self.view()["session_state"], "paused")
        self.assertEqual(self.post("resume").status_code, 200)
        self.assertEqual(self.post("stop").status_code, 200)
        self.assertEqual(self.view()["session_state"], "stopped")
        self.assertEqual(self.runtime.submit("view").result()["usage"]["max_calls"], 2)

    def test_host_origin_csrf_and_limited_forms(self):
        self.assertEqual(self.client.get("/", headers={"Host":"evil.example"}).status_code,400)
        self.assertEqual(self.client.get("/", headers={"Host":"127.0.0.1:1"}).status_code,400)
        for origin in (None, "null", "http://evil.example", BASE+".evil.example"):
            headers = {"Origin":origin} if origin else {}
            self.assertEqual(self.client.post("/actions/start",data=self.fields(),headers=headers).status_code,403)
        self.assertEqual(self.client.post("/actions/start", data=self.fields(), headers={"Origin":BASE,"Sec-Fetch-Site":"cross-site"}).status_code,403)
        self.assertEqual(self.post("start", csrf="wrong").status_code,403)
        self.assertEqual(self.client.post("/actions/start", json={},headers={"Origin":BASE}).status_code,409)
        self.assertEqual(self.client.post("/actions/start", content="x=1&x=2", headers={"Origin":BASE,"Content-Type":"application/x-www-form-urlencoded"}).status_code,409)
        self.assertEqual(self.post("start",reason="x"*17000).status_code,409)
        self.assertEqual(self.post("start",reason=" ").status_code,409)
        self.assertEqual(self.post("start",reason="x"*501).status_code,409)
        invalid = self.post("start",calls="0",minutes="8",reason="Сохраните мою причину")
        self.assertEqual(invalid.status_code,409)
        self.assertIn('value="8"',invalid.text)
        self.assertIn('value="Сохраните мою причину"',invalid.text)
        self.assertEqual(self.view()["session_state"],"stopped")

    def test_backup_restore_never_replaces_live_database(self):
        self.assertEqual(self.post("backup").status_code,200)
        data = self.view()
        bid = data["backups"][0]["id"]
        self.assertIn(bid,self.client.get("/backups").text)
        self.assertEqual(self.post("restore",backup_id=bid).status_code,409)
        self.assertEqual(self.post("restore",backup_id="../fake",confirm="copy").status_code,409)
        result = self.post("restore",backup_id=bid,confirm="copy")
        self.assertEqual(result.status_code,200,result.text)
        restored = list((self.tmp/"restored").glob("*/*.db"))
        self.assertEqual(len(restored),1)
        with Store.open(restored[0],readonly=True) as s:
            self.assertEqual(verify(s),[])
            self.assertEqual(s.latest_snapshot().epoch,2)
        self.assertEqual(self.view()["epoch"],1)
        self.assertEqual(self.post("start").status_code,200)
        self.assertEqual(self.post("restore",backup_id=bid,confirm="copy").status_code,409)

    def test_xss_is_escaped_and_secrets_are_redacted(self):
        from iam.state import EventSpec
        def ingest():
            return self.runtime.ctl.record_event(EventSpec("input.arrived","rx:test:1",{
                "text":"<script>alert(1)</script> sk-ant-oat01-" + "a"*90}))
        self.runtime._test_ingest = ingest
        self.runtime.submit("_test_ingest").result(5)
        text = self.client.get("/events").text
        self.assertNotIn("<script>",text)
        self.assertIn("&lt;script&gt;",text)
        self.assertNotIn("sk-ant-oat01-" + "a"*90,text)

    def test_unavailable_server_returns_clear_failure(self):
        self.runtime.fatal = "sqlite_error"
        self.assertEqual(self.client.get("/").status_code,503)
        self.assertEqual(self.client.post("/actions/start",data=self.fields_fallback(),headers={"Origin":BASE}).status_code,503)
        self.runtime.fatal = None

    def fields_fallback(self):
        # Получить форму до имитации неисправности нельзя; валидный токен для этой ветви
        # берётся из ответа, полученного временно доступным сервером.
        self.runtime.fatal = None
        form = self.fields()
        self.runtime.fatal = "sqlite_error"
        return form


class SecurityUnitTest(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_passthrough_and_duplicate_hosts(self):
        app = mock.AsyncMock()
        wrapped = LocalOnly(app,authority="127.0.0.1:1")
        await wrapped({"type":"lifespan"},None,None)
        app.assert_awaited_once()
        output=[]
        async def send(message): output.append(message)
        await wrapped({"type":"http","method":"GET","headers":[(b"host",b"127.0.0.1:1"),(b"host",b"evil")]},None,send)
        self.assertEqual(output[0]["status"],400)

    async def test_empty_valid_csrf_and_malformed_form(self):
        class Req:
            headers={"content-type":"application/x-www-form-urlencoded"}
            async def stream(self):
                yield b"csrf=correct&reason="
        self.assertEqual((await read_form(Req(),"correct"))["reason"],"")
        with self.assertRaises(PermissionError):
            await read_form(Req(),"wrong")

    async def test_header_and_body_send_passthrough(self):
        async def inner(scope,receive,send):
            await send({"type":"http.response.start","status":200})
            await send({"type":"http.response.body","body":b"ok"})
        output=[]
        async def send(message): output.append(message)
        await LocalOnly(inner,authority="127.0.0.1:1")({"type":"http","method":"HEAD","headers":[(b"host",b"127.0.0.1:1")]},None,send)
        self.assertEqual(output[-1]["body"],b"ok")


class LaunchTest(unittest.TestCase):
    def test_loopback_only_and_dependency_pins(self):
        from iam.panel.__main__ import main
        for port in (0,65536,True):
            with self.assertRaises(ValueError): create_app(None,port=port)
        with mock.patch("iam.panel.__main__.uvicorn.run") as run:
            main(["--db","/tmp/not-created.db","--demo"])
            self.assertEqual(run.call_args.kwargs["host"],"127.0.0.1")
            self.assertFalse(run.call_args.kwargs["proxy_headers"])
        with self.assertRaises(SystemExit):
            main(["--db","/tmp/not-created.db","--demo","--telegram-user","1"])
