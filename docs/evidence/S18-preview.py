"""Локальный просмотр страницы S18 на новой тестовой базе, без модели и Telegram.

Запуск: PYTHONPATH=src python docs/evidence/S18-preview.py /tmp/new-s18-preview.db
База обязана отсутствовать. Данные подготовлены только для проверки интерфейса.
"""
import sys
from pathlib import Path

import uvicorn

from iam.domain import new_id
from iam.memory import PersistentMemory
from iam.panel.app import create_app
from iam.panel.runtime import Runtime
from iam.state import StateController
from iam.storage import Store

path = Path(sys.argv[1])
if path.exists():
    raise SystemExit('Для проверки нужна новая тестовая база.')
with Store.create(path, new_id('exp')) as store:
    ctl = StateController(store)
    ctl.create_persona()
    ctl.start_session()
    memory = PersistentMemory(ctl)
    root = store.get_event(1).event_id
    examples = [
        ('semantic', 'own_hypothesis', 'Возможно, короткие записи помогут вернуться к занятию после перерыва.'),
        ('social', 'stated_by_other', 'Проверочный собеседник сообщил: «Мне нравится чай с мятой». Это учебный пример.'),
        ('working', 'imagination', 'ДлинноеПроверочноеНазваниеБезПробелов' * 12 + '\n<script>Этот текст не выполняется</script>'),
    ]
    for kind, origin, text in examples:
        options = {'claim_kind': 'fact_claim', 'independence_group': 'fixture:person'} if origin == 'stated_by_other' else {}
        record = memory.prepare(kind=kind, origin=origin, text=text, sources=(root,),
                                content={'experience': 'prepared'}, **options)
        result = memory.write(record)
        assert result.applied, result
    ctl.stop_session()
runtime = Runtime(path, demo=True)
uvicorn.run(create_app(runtime, port=18768), host='127.0.0.1', port=18768,
            proxy_headers=False, access_log=False)
