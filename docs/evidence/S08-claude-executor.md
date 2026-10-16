# Свидетельство S08: изолированный исполнитель Claude Code

Снято командами ниже в сессии S08 (2026-10-16T20:30Z). Python 3.13.16, coverage.py 7.10.6, Claude Code CLI 2.1.301 (Claude Code).

**Важно.** Всё ниже — проверки на подделках (FakeClaude, подделка docker). Настоящая изоляция, вход по подписке и форма успешного ответа проверяются на Mac по барьеру G11 (`docs/evidence/claude-isolation.md`); пока он не закрыт, требования изоляции — `implemented`, не `verified`.

## Реальный образец, снятый в среде разработки
Ошибка авторизации (токен-пустышка), `claude -p --output-format json --tools ""`: объект с `is_error: true`, `api_error_status: 401`, `terminal_reason: api_error`, код возврата процесса 0. Он положен в `iam.providers.fake.auth_error` и разобран классификатором.

## `cd tests && python3 -m unittest providers.test_command providers.test_classify providers.test_provider providers.test_budget providers.test_run providers.test_scripts -v`
