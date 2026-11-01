"""HTTP-слой: ограниченные формы, серверный HTML, проверка версии до команды."""
import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.exceptions import HTTPException

from iam.backup.scan import SecretScanner
from iam.storage import NotFound

from .security import LocalOnly, read_form
from .views import NAV, render
from .experiments import SNAPSHOT, load_archive, render_archive


def create_app(runtime, *, port=8765, experiment_snapshot=SNAPSHOT):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Порт должен быть от 1 до 65535.")
    token, scanner = secrets.token_urlsafe(32), SecretScanner(use_env=True)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(runtime.start)
        try:
            yield
        finally:
            await asyncio.to_thread(runtime.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalOnly, authority=f"127.0.0.1:{port}")

    def clean(value):
        return json.loads(scanner.redact(json.dumps(value, ensure_ascii=False))[0])

    async def call(name, *args, **kwargs):
        return await asyncio.wrap_future(runtime.submit(name, *args, **kwargs))

    async def response(page, *, status=200, error=None, notice=None, values=None, selection=None, **query):
        data = clean(await call("view", **query))
        if page == 'experiments':
            try:
                archive = clean(load_archive(experiment_snapshot))
            except (ValueError, OSError):
                status = 503
                error = 'Архив результатов недоступен. Восстановите опубликованный снимок и обновите страницу.'
                data['experiment_content'] = '<p><a href="/experiments">Повторить открытие результатов</a></p>'
            else:
                try:
                    data['experiment_content'] = render_archive(archive, **(selection or {}))
                except ValueError as exc:
                    status, error = 400, str(exc)
                    data['experiment_content'] = '<p><a href="/experiments">К списку экспериментов</a></p>'
        return HTMLResponse(render(page, data, token, error=error, notice=notice, values=clean(values)), status_code=status)

    @app.get("/style.css")
    async def stylesheet():
        return PlainTextResponse(Path(__file__).with_name("style.css").read_text(), media_type="text/css")

    @app.get("/favicon.ico", status_code=204)
    async def favicon():
        return PlainTextResponse(status_code=204)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return PlainTextResponse("Страница недоступна. Вернитесь на главную страницу панели.", exc.status_code)

    @app.get("/")
    @app.get("/{page}")
    async def read(request: Request, page: str = ""):
        if page not in NAV:
            return PlainTextResponse("Страница не найдена.", 404)
        try:
            query = {}
            for name in ("before", "version"):
                raw = request.query_params.get(name)
                if raw is not None:
                    value = int(raw)
                    if value < 1:
                        raise ValueError()
                    query[name] = value
            selection = None
            if page == 'experiments':
                selection = {name: request.query_params[name] for name in ('series', 'story') if name in request.query_params}
                if 'moment' in request.query_params:
                    selection['moment'] = int(request.query_params['moment'])
            return await response(page, selection=selection, **query)
        except (ValueError, NotFound):
            message = ('Номер эпизода должен быть целым числом из списка.' if page == 'experiments' else
                       'Версия или номер события не найдены. Обновите страницу.')
            return await response(page, status=400, error=message)
        except RuntimeError:
            return PlainTextResponse("Сервер остановлен после ошибки. Перезапустите панель; состояние сохранено в базе.", 503)

    @app.post("/actions/{action}")
    async def act(request: Request, action: str):
        if action not in ("start", "pause", "resume", "stop", "backup", "restore", "memory_restore"):
            return PlainTextResponse("Неизвестная команда.", 404)
        page = "memory" if action == "memory_restore" else "backups" if action in ("backup", "restore") else ""
        fields = None
        try:
            fields = await read_form(request, token)
            result = await call("mutate", action, clean(fields))
            message = "Запись восстановлена в личную память. Вмешательство оператора сохранено в журнале." if action == "memory_restore" else "Действие сохранено в журнале."
            if action == "restore":
                message = "Копия восстановлена в отдельную базу: " + result + ". Для её открытия завершите сервер и укажите этот путь при запуске."
            return await response(page, notice=message)
        except PermissionError as exc:
            return await response(page, status=403, error=str(exc), values=fields)
        except ValueError as exc:
            return await response(page, status=409, error=scanner.redact(str(exc))[0], values=fields)
        except Exception:
            # Ни токены, ни тела ошибок провайдера в HTTP/логи не возвращаются.
            return PlainTextResponse("Операция не завершена. Обновите панель и проверьте журнал; при остановке сервера перезапустите его.", 503)

    return app
