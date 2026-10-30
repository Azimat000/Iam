"""Узкий клиент Bot API. Токен — только из окружения; ошибки не содержат URL и ответ сервера."""
from __future__ import annotations

import json
import http.client
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class TelegramError(Exception):
    def __init__(self, code: int = 0):
        self.code = code
        super().__init__(f"Telegram: ошибка {code}" if code else "Telegram: результат запроса неизвестен")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, *, base_url: str = "https://api.telegram.org", timeout: int = 30):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("задайте TELEGRAM_BOT_TOKEN в окружении")
        url = urllib.parse.urlsplit(base_url)
        if base_url != "https://api.telegram.org" and not (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
            and not url.username and not url.password and url.path in ("", "/")
            and not url.query and not url.fragment
        ):
            raise ValueError("Bot API: разрешены официальный HTTPS и локальный тестовый сервер")
        if type(timeout) is not int or not 0 <= timeout <= 50:
            raise ValueError("timeout: целое от 0 до 50 секунд")
        self.bot_id = int(token.split(":", 1)[0])
        self.timeout = timeout
        self._url = base_url.rstrip("/") + "/bot" + token + "/"
        self._opener = urllib.request.build_opener(_NoRedirect())

    def call(self, method: str, params: dict):
        if method not in ("getUpdates", "sendMessage", "getMe"):
            raise ValueError("метод Bot API не разрешён")
        request = urllib.request.Request(self._url + method, json.dumps(params).encode(),
                                         {"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout + 10) as response:
                raw = response.read(2_000_001)
                length = response.getheader("Content-Length")
                if len(raw) > 2_000_000 or (length is not None and len(raw) != int(length)):
                    raise TelegramError()
                payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Даже тело ошибки может содержать токен или присланный текст. Не сохраняем его.
            code = exc.code
            exc.close()
            raise TelegramError(code) from None
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
            raise TelegramError() from None
        if not isinstance(payload, dict) or payload.get("ok") is not True or "result" not in payload:
            code = payload.get("error_code", 0) if isinstance(payload, dict) else 0
            raise TelegramError(code if type(code) is int else 0)
        return payload["result"]
