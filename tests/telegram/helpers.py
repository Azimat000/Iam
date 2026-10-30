import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from iam.telegram import Client, Receiver, Sender
from state.helpers import StateCase

TOKEN = "123456:" + "CANARY_TELEGRAM_SECRET_" * 2
USER = 1234567890123


def update(uid=10, text="привет", *, sender=USER, chat=USER, kind="private"):
    return {"update_id": uid, "message": {"from": {"id": sender, "is_bot": False},
            "chat": {"id": chat, "type": kind}, "text": text}}


class FakeTelegram:
    def __init__(self):
        self.updates, self.calls, self.messages = [], [], []
        self.mode, self.result = "ok", None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append((method, body))
                if method == "sendMessage":
                    owner.messages.append(body)
                if owner.mode == "drop":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if owner.mode == "bad_status":
                    self.connection.sendall((TOKEN + "\r\n\r\n").encode())
                    self.close_connection = True
                    return
                if owner.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/leaked")
                    self.end_headers()
                    return
                status, payload = 200, {"ok": True}
                if owner.mode == "bad_json":
                    raw = b"not json"
                else:
                    if owner.mode in ("403", "429", "500"):
                        status = int(owner.mode)
                        payload = {"ok": False, "description": TOKEN, "error_code": status}
                    elif owner.mode == "api_error":
                        payload = {"ok": False, "error_code": 400, "description": TOKEN}
                    elif owner.mode == "bad_envelope":
                        payload = [TOKEN]
                    elif owner.mode == "bad_code":
                        payload = {"ok": False, "error_code": TOKEN}
                    elif method == "getUpdates":
                        owner.updates = [u for u in owner.updates if u.get("update_id", 0) >= body["offset"]]
                        payload["result"] = owner.updates if owner.result is None else owner.result
                    elif method == "getMe":
                        payload["result"] = {"id": 123456, "is_bot": True}
                    else:
                        payload["result"] = owner.result if owner.result is not None else {
                            "message_id": len(owner.messages), "chat": {"id": body["chat_id"], "type": "private"}}
                    raw = json.dumps(payload).encode()
                self.send_response(status)
                if owner.mode != "no_length":
                    self.send_header("Content-Length", str(len(raw) + (100 if owner.mode == "truncated" else 0)))
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class TelegramCase(StateCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        self.api = FakeTelegram()
        self.addCleanup(self.api.close)
        self.client = Client(base_url=self.api.url, timeout=0)
        self.c = self.controller()
        self.rx = Receiver(self.c, self.client, [USER])
        self.tx = Sender(self.c, self.client, [USER])
