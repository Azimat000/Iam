#!/usr/bin/env python3
"""Ручная проверка настоящего бота. Секреты только в окружении, тестовая личность отдельно."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from iam.cycle import Cycle
from iam.domain import new_id
from iam.interfaces import Capability
from iam.state import StateController
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, StubCapabilities, StubCreatorBridge, StubMemory, say
from iam.telegram import Client, Receiver, Sender


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="отдельный каталог вне репозитория")
    parser.add_argument("--polls", type=int, default=2)
    parser.add_argument("--send-test", action="store_true", help="отправить одно тестовое сообщение через ядро")
    args = parser.parse_args(argv)
    if args.polls < 1:
        parser.error("--polls должен быть положительным")
    directory = args.data_dir.expanduser().resolve()
    if directory.is_relative_to(ROOT):
        parser.error("данные проверки должны находиться вне репозитория")
    try:
        user = int(os.environ.get("TELEGRAM_USER_ID", ""))
        if user <= 0:
            raise ValueError()
        client = Client()
        me = client.call("getMe", {})
        if not isinstance(me, dict) or me.get("id") != client.bot_id or me.get("is_bot") is not True:
            raise ValueError()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"telegram-check-{client.bot_id}-{user}.db"
        store = Store.open(path) if path.exists() else Store.create(path, new_id("exp"))
        with store:
            ctl = StateController(store)
            if ctl.state_version == 0:
                ctl.create_persona({})
            rx, tx = Receiver(ctl, client, [user]), Sender(ctl, client, [user])
            print("Авторизация подтверждена. Напишите боту в личном диалоге. Модель не вызывается.")
            for _ in range(args.polls):
                count = rx.poll_once()
                print(f"Новых сохранено: {count}; всего непрочитанных: {rx.notices()['count']}; offset: {rx.offset}")
            print(f"Последний успешный опрос: {rx.last_poll_at}")
            if args.send_test:
                if ctl.session_state == "paused":
                    ctl.resume_session()
                elif ctl.session_state == "stopped":
                    ctl.start_session()
                provider = ScriptedProvider([Reply((say(tx.capability_id, str(user), "Iam: проверка отправки Telegram."),))])
                cycle = Cycle(ctl, provider, StubMemory(), tx,
                              StubCapabilities([Capability(tx.capability_id, "Telegram")]), StubCreatorBridge())
                # Отдельное событие для проверки транспорта; живого интеллекта здесь нет.
                report = cycle.run(store.get_event(store.last_seq()))
                if not report.actions:
                    raise RuntimeError()
                print("Исход тестовой отправки: " + report.actions[0].status)
                ctl.stop_session()
                if report.actions[0].status != "confirmed":
                    return 1
            print("Приём проверен. G02 автоматически не закрывается: сверяйте сообщения и сохранённые данные.")
        return 0
    except Exception as error:
        # Полный traceback сетевого запроса способен раскрыть URL с токеном.
        print(f"Проверка не пройдена ({type(error).__name__}). Проверьте окружение, сеть и отсутствие webhook.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
