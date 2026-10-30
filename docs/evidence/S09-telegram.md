# S09 — Telegram: свидетельства и границы проверки

Дата: 2026-10-30. Настоящий бот не подключался. Все проверки транспорта ниже — на локальном
поддельном Telegram HTTP-сервере; интеллект — программная заглушка. Барьер G02 остаётся открытым,
требования R-016, R-158, R-159, R-161, R-162, R-188, R-189 получают implemented.

## Что реализовано

`iam.telegram.Client`, `Receiver`, `Sender`, `CreatorBridge`; транспортная точка записи
`iam.state.telegram.TelegramMailbox`. События и состояние по-прежнему меняет StateController;
прямых операций SQL из iam.telegram нет. Хранилище и его схема, существующий цикл и контроллер
не изменены. Используются inbox/outbox и служебные метки meta в существующей базе.

## Соответствие проверкам среза

| Требование / критерий | Конкретное свидетельство |
|---|---|
| Приём при остановке/паузе без модели, R-016 | `test_pause_durable_inbox_then_ack_restart_no_duplicate`, `test_runner_paused_no_model_then_notice_and_no_rights_command`: версия/время не меняются, вызовов модели во время остановки ноль; после запуска запрос содержит только уведомление. |
| Сохранение до подтверждения, R-161 | `test_storage_failure_rolls_back_event_inbox_and_offset`, `test_partial_batch_crash_preserves_completed_updates`: искусственный сбой после записи inbox откатывает также событие и offset, повторный опрос восстанавливает данные; обработанная часть пакета остаётся сохранённой. |
| Дедупликация и перезапуск, R-162 | `test_duplicates_identical_text_distinct_updates`, `test_pause_durable_inbox_then_ack_restart_no_duplicate`: дубли update_id дают одно событие; два разных update_id с одинаковым текстом остаются двумя событиями. Подтверждение сервером — только при следующем getUpdates с сохранённым offset. |
| Только числовой белый список и private, R-158 | `test_filter_unknown_group_username_bot_and_unsupported`: чужой ID, имя, bool, группа, несовпадение ID чата и отправителя, бот, некорректные и неподдерживаемые обновления не попадают в inbox. Текст отвергнутого не сохраняется. |
| Сообщение не выдаёт права, R-159 | `test_runner_paused_no_model_then_notice_and_no_rights_command`: текст `/grant admin ...` остаётся социальным входящим; список возможностей не меняется, operator.intervention отсутствует. |
| Непрочитанное скрыто | `test_notices_hide_body_until_read_event`: N1 — только количество, N2 — метаданные; доступ к тексту запрещён до input.read. Интеграционный тест Runner проверяет отсутствие текста в запросе модели. Полное внимание — S20. |
| Потеря подтверждения, R-188 | `test_dropped_confirmation_uncertain_never_auto_retried`, `test_uncertain_cycle_records_unknown_not_success_or_memory`: сервер принимает отправку и обрывает ответ; outbox uncertain, цикл пишет message.out_unknown, успешного события и воспоминания нет. |
| Предел повторов, R-189 | `test_explicit_resend_bound_survives_restart_and_no_retry_chains`: повтор без решения оператора запрещён; один явный повтор новым действием допускается; после перезапуска счётчик сохраняется, повторить повтор нельзя. |
| Авария вокруг отправки | `test_crash_after_attempt_record_before_status_is_uncertain_on_restart`, `test_same_process_reentry_after_crash_not_retried`: после записи попытки обрыв не вызывает автоматической пересылки, pending переводится в uncertain. |
| Реальная связка с ядром | `test_execute_adapter_and_full_cycle_events_memory`, `test_creator_bridge_cycle_and_delivery_idempotency`: сообщение и просьба проходят существующий Cycle без его изменения, подтверждённый исход создаёт память; повтор не отправляет снова. |
| Сетевые ошибки и секреты | `test_errors_and_redirect_never_expose_token`, `test_canary_not_in_database_logs_or_export`, `test_error_classification`: HTTP/JSON/сетевые ошибки, обрезанный ответ, неверная строка HTTP, 403/429/500, запрещённый редирект; контрольный токен не раскрывается в ошибках/выводе/служебных метках/экспорте. |
| Сеть Telegram не блокирует внутренние дела | `test_telegram_network_failure_does_not_block_internal_decision`: при обрыве опроса внутренний плановый момент всё равно запускает решение, ошибка видима, offset не меняется. |
| Скрипт ручной проверки | `tests/telegram/test_scripts.py`: приём, повторный запуск, явная отправка теста, неопределённая отправка и ошибки настроек проверены на том же поддельном HTTP-сервере. Это проверка скрипта, не настоящего Telegram. |
| Единственная точка записи | Неизменённые `tests/state/test_boundaries.py::SingleWriterTest`: Telegram не вызывает SQL/методы записи Store; отдельное расширение ядра отвечает за транспортные записи. |

Все названные тесты находятся в `tests/telegram/test_receiver.py`, `test_sender.py`, `test_client.py`
(кроме отдельно указанного файла). Точные команды и результаты полного прогона приведены в итоге S09.

## Независимая проверка

Read-only подагент обнаружил неполную обработку исключений HTTP и блокировку внутренних циклов
при недоступном опросе Telegram. Исправлено: обработка HTTPException и проверка длины ответа,
безопасная ошибка на границе CreatorBridge, тест продолжения внутреннего дела. Общий тест границ
также потребовал вынести запись транспортных данных в iam.state.telegram; тест не ослаблен.

## Что нужно проверить живьём и передать дальше

- G02: создать бота, задать токен только во внешнем окружении, числовой ID, пройти
  `scripts/telegram/README.md`. Сверить личный приём, отказ чужому/группе, отсутствие дублей после
  перезапуска и подтверждённую тестовую отправку. G02 не закрывается автоматически.
- S10: один процесс-писатель; приём работает при паузе; организовать опрос, не блокирующий панель,
  маршрутизацию возможностей и явное решение оператора для ограниченных повторов. Показать
  last_poll_at и предупреждение после 24 часов без успешного опроса. S27: перенести это в RUNBOOK.
- Восстановленная старая копия может содержать старые offset и outbox; сверить внешние отправки
  перед разрешением новых действий. Нельзя обещать одновременно гарантированную доставку и
  отсутствие дубля после неизвестного сетевого исхода.
- Токен транспорта не записывается в базу. Присланный человеком текст сохраняется целиком,
  в том числе если человек сам прислал секрет; существующий экспорт удаляет известные секреты.

Документация Telegram проверена по первичным источникам:
[getUpdates](https://core.telegram.org/bots/api#getupdates),
[получение обновлений](https://core.telegram.org/bots/api#getting-updates),
[sendMessage](https://core.telegram.org/bots/api#sendmessage).
Обновления хранятся не дольше 24 часов; подтверждение входящего происходит при следующем запросе
с offset больше update_id. В sendMessage нет документированного ключа дедупликации — поэтому
автоматическая пересылка после потери ответа небезопасна (инженерный вывод из контракта API).

## Замер покрытия на этом Mac

Штатный `PYTHONPATH=src .venv/bin/python tools/covcheck.py` выполнил 737 тестов успешно
и показал 100 % строк и ветвей всех модулей src, но не собрал покрытие дочерних CLI-процессов
служебных программ (memory, reqs, covcheck). Поэтому сам штатный измеритель завершился с кодом 1.
Причина автоматического сбора с coverage 7.10.6 в этом окружении не установлена; штатная команда
не объявляется успешно пройденной. Исходники измерителя и пороги не менялись.

Для проверки тех же порогов выполнен отдельный полный прогон с явным измерением только
этих трёх CLI-команд, с сохранением всех исходных аргументов и окружения тестов. Подагент проверил
обход: тесты не исключаются, результаты не подделываются. Воспроизведение из корня репозитория:

1. Сохранить следующий код во временный файл `/tmp/iam-s09-explicit-coverage.py`.
2. Запустить команды после кода. Нужен проектный coverage 7.10.6 (dev-зависимость).

```python
"""Measure CLI tools explicitly when automatic subprocess coverage misses them on this Mac."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path.cwd().resolve()
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
CLI_TOOLS = {str(ROOT / 'tools' / name) for name in ('memory.py', 'reqs.py', 'covcheck.py')}
original_run = subprocess.run
traced_calls = 0

def measured_run(args, *positional, **kwargs):
    global traced_calls
    if isinstance(args, (list, tuple)) and len(args) >= 2 and str(args[1]) in CLI_TOOLS:
        args = [args[0], '-m', 'coverage', 'run', '-p', '--rcfile=' + str(ROOT / 'pyproject.toml'), *args[1:]]
        env = dict(kwargs.get('env') or os.environ)
        kwargs['env'] = {k: v for k, v in env.items() if not k.startswith('COVERAGE_PROCESS_')}
        traced_calls += 1
    return original_run(args, *positional, **kwargs)

subprocess.run = measured_run
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
result = unittest.TextTestRunner(verbosity=1).run(suite)
print('CLI calls explicitly measured:', traced_calls)
sys.exit(not result.wasSuccessful())
```

```sh
.venv/bin/python -m coverage erase
PYTHONPATH=src .venv/bin/python -m coverage run -p /tmp/iam-s09-explicit-coverage.py
.venv/bin/python -m coverage combine -q
.venv/bin/python -m coverage json -q -o /tmp/iam-s09-coverage.json
.venv/bin/python tools/covcheck.py --json /tmp/iam-s09-coverage.json
```

Не запускать одновременно другие замеры с тем же файлом данных. Копии исходных тестов
не создаются и не изменяются; объединяются реально снятые данные выполнения.

## Итог измерения

```text
Ran 737 tests in 157.187s

OK
CLI calls explicitly measured: 132
модуль                                         строки    ветви  итог
src/iam/__init__.py                            100.0%   100.0%  ok
src/iam/backup/__init__.py                     100.0%   100.0%  ok [критичный]
src/iam/backup/errors.py                       100.0%   100.0%  ok [критичный]
src/iam/backup/export.py                       100.0%   100.0%  ok [критичный]
src/iam/backup/restore.py                      100.0%   100.0%  ok [критичный]
src/iam/backup/scan.py                         100.0%   100.0%  ok [критичный]
src/iam/backup/snapshots.py                    100.0%   100.0%  ok [критичный]
src/iam/backup/space.py                        100.0%   100.0%  ok [критичный]
src/iam/cli.py                                 100.0%   100.0%  ok
src/iam/clock/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/clock/lifeclock.py                     100.0%   100.0%  ok [критичный]
src/iam/clock/moments.py                       100.0%   100.0%  ok [критичный]
src/iam/cycle/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/cycle/attention.py                     100.0%   100.0%  ok [критичный]
src/iam/cycle/cycle.py                         100.0%   100.0%  ok [критичный]
src/iam/cycle/handlers.py                      100.0%   100.0%  ok [критичный]
src/iam/cycle/runner.py                        100.0%   100.0%  ok [критичный]
src/iam/cycle/validator.py                     100.0%   100.0%  ok [критичный]
src/iam/domain/__init__.py                     100.0%   100.0%  ok
src/iam/domain/catalog.py                      100.0%   100.0%  ok
src/iam/domain/ids.py                          100.0%   100.0%  ok
src/iam/domain/jsonschema.py                   100.0%   100.0%  ok
src/iam/domain/types.py                        100.0%   100.0%  ok
src/iam/interfaces/__init__.py                 100.0%   100.0%  ok
src/iam/interfaces/protocols.py                100.0%   100.0%  ok
src/iam/interfaces/types.py                    100.0%   100.0%  ok
src/iam/providers/__init__.py                  100.0%   100.0%  ok [критичный]
src/iam/providers/budget.py                    100.0%   100.0%  ok [критичный]
src/iam/providers/classify.py                  100.0%   100.0%  ok [критичный]
src/iam/providers/claude_code.py               100.0%   100.0%  ok [критичный]
src/iam/providers/command.py                   100.0%   100.0%  ok [критичный]
src/iam/providers/errors.py                    100.0%   100.0%  ok [критичный]
src/iam/providers/fake.py                      100.0%   100.0%  ok [критичный]
src/iam/providers/run.py                       100.0%   100.0%  ok [критичный]
src/iam/state/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/state/controller.py                    100.0%   100.0%  ok [критичный]
src/iam/state/errors.py                        100.0%   100.0%  ok [критичный]
src/iam/state/paths.py                         100.0%   100.0%  ok [критичный]
src/iam/state/replay.py                        100.0%   100.0%  ok [критичный]
src/iam/state/restore.py                       100.0%   100.0%  ok [критичный]
src/iam/state/rules.py                         100.0%   100.0%  ok [критичный]
src/iam/state/telegram.py                      100.0%   100.0%  ok [критичный]
src/iam/state/types.py                         100.0%   100.0%  ok [критичный]
src/iam/storage/__init__.py                    100.0%   100.0%  ok [критичный]
src/iam/storage/db.py                          100.0%   100.0%  ok [критичный]
src/iam/storage/errors.py                      100.0%   100.0%  ok [критичный]
src/iam/storage/lock.py                        100.0%   100.0%  ok [критичный]
src/iam/storage/migrations.py                  100.0%   100.0%  ok [критичный]
src/iam/storage/runtime.py                     100.0%   100.0%  ok [критичный]
src/iam/storage/store.py                       100.0%   100.0%  ok [критичный]
src/iam/stubs/__init__.py                      100.0%   100.0%  ok
src/iam/stubs/capabilities.py                  100.0%   100.0%  ok
src/iam/stubs/creator.py                       100.0%   100.0%  ok
src/iam/stubs/memory.py                        100.0%   100.0%  ok
src/iam/stubs/provider.py                      100.0%   100.0%  ok
src/iam/stubs/world.py                         100.0%   100.0%  ok
src/iam/telegram/__init__.py                   100.0%   100.0%  ok [критичный]
src/iam/telegram/client.py                     100.0%   100.0%  ok [критичный]
src/iam/telegram/mailbox.py                    100.0%   100.0%  ok [критичный]
src/iam/telegram/receiver.py                   100.0%   100.0%  ok [критичный]
src/iam/telegram/sender.py                     100.0%   100.0%  ok [критичный]
tools/covcheck.py                              100.0%   100.0%  ok
tools/memory.py                                 90.8%    84.4%  ok
tools/reqs.py                                   95.6%    90.6%  ok
tools/scenarios.py                             100.0%    99.4%  ok
Модулей: 65, ниже порога: 0.
```

Код возврата полного прогона: 0. Код возврата tools/covcheck.py --json: 0.
Все 65 модулей прошли исходные пороги D-009, весь src — 100 % строк и ветвей.
