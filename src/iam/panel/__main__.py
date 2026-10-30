"""Запуск панели исключительно на 127.0.0.1 (без внешних прокси и автоперезагрузки)."""
import argparse

import uvicorn

from .app import create_app
from .runtime import Runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description="Локальная панель Iam")
    parser.add_argument("--db", required=True, help="Путь базы вне репозитория")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="Создать отдельную экспериментальную базу, без внешних вызовов")
    mode.add_argument("--claude-token-file", help="Внешний файл авторизации Claude Code, права 600")
    parser.add_argument("--telegram-user", type=int, help="Числовой Telegram ID; токен только в окружении")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.demo and args.telegram_user is not None:
        parser.error("Демонстрация не подключается к Telegram.")
    runtime = Runtime(args.db, demo=args.demo, token_file=args.claude_token_file, telegram_user=args.telegram_user)
    app = create_app(runtime, port=args.port)
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
