# Совместимость ограничения cites, 2026-11-05

Узкая проверка выполнена read-only исследователем с Deep Research и чтением официальной страницы через Kimi. Платные API, обновления навыков и новые подписки не использовались.

Официальный [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses) относит string pattern к поддерживаемым ограничениям. Для fine-tuned моделей перечислены отдельные исключения, включая pattern. Это подтверждение формата Structured Outputs, а не живой тест конкретной подписочной модели IAM.

Локальный codex exec --help подтверждает параметр --output-schema как путь к JSON Schema финального ответа. Официальный [пример Codex](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex) также использует --output-schema для машинно проверяемого результата.

Уверенность высокая для поддержки pattern в указанном формате и ограничения fine-tuned; средняя для тождественности конкретного подписочного CLI-маршрута. После исправления нового обращения к модели нет; локальная проверка схем и содержимого Docker-образа не выдаётся за такую проверку.
