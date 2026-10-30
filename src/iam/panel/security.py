"""Host + Origin + CSRF: loopback сам по себе не защищает от чужой вкладки браузера."""
import hmac
from urllib.parse import parse_qs

from starlette.responses import PlainTextResponse


class LocalOnly:
    def __init__(self, app, *, authority):
        self.app, self.authority = app, authority

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope["headers"]
        hosts = [v.decode("latin1") for k, v in headers if k == b"host"]
        origins = [v.decode("latin1") for k, v in headers if k == b"origin"]
        fetch = [v for k, v in headers if k == b"sec-fetch-site"]
        if hosts != [self.authority]:
            response = PlainTextResponse("Недопустимый Host. Откройте панель по адресу 127.0.0.1.", 400)
        elif scope["method"] not in ("GET", "HEAD") and (origins != ["http://" + self.authority] or b"cross-site" in fetch):
            response = PlainTextResponse("Запрос отклонён. Отправьте форму из самой панели.", 403)
        else:
            async def secured(message):
                if message["type"] == "http.response.start":
                    message["headers"] = [*message.get("headers", []),
                        (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"same-origin"), (b"x-frame-options", b"DENY"),
                        (b"content-security-policy", b"default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")]
                await send(message)
            await self.app(scope, receive, secured)
            return
        await response(scope, receive, send)


async def read_form(request, token):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
        raise ValueError("Отправьте обычную форму панели.")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > 16_384:
            raise ValueError("Форма слишком велика. Сократите причину действия.")
    fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=12)
    if any(len(values) != 1 for values in fields.values()):
        raise ValueError("Повторяющиеся поля формы не разрешены.")
    form = {key: values[0] for key, values in fields.items()}
    if not hmac.compare_digest(form.get("csrf", "").encode(), token.encode()):
        raise PermissionError("Форма устарела. Обновите страницу панели.")
    return form
