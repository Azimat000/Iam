"""Валидатор предложений модели (R-104, R-117): модель только предлагает.

Тело предложения — открытый объект, поэтому здесь два заслона:
1. `tool_use_signs` — признаки использования инструментов или готового «выполнено» в теле предложения
   (поля вроде `tool_calls`, `stdout`, `executed`, `result`). Нашлись — весь ответ непригоден для изменения
   личности (R-117): цикл поднимает `tool_use_detected`, привратник отклоняет все предложения.
2. `body_violation` — каждый вид предложения допускает только свои поля (envelopes.md#proposal); лишнее поле
   отклоняет это предложение, остальные не страдают.
"""
from __future__ import annotations

from typing import Any

# Поля тела по видам (docs/spec/envelopes.md §3). Ничего, кроме них, предложение содержать не вправе.
BODY_FIELDS: dict[str, frozenset[str]] = {
    "action": frozenset({"capability_id", "params", "expected_duration_ms"}),
    "message": frozenset({"capability_id", "to", "text"}),
    "memory_write": frozenset({"draft"}),
    "intention": frozenset({"op", "intention_id", "text", "to_status"}),
    "creator_request": frozenset({"request_key", "text"}),
    "no_action": frozenset({"reason"}),
    "attend": frozenset({"focus", "ref"}),
}

# Следы того, что модель сама что-то запускала (на любой глубине в телах, кроме данных состояния).
TOOL_KEYS = frozenset({"tool_use", "tool_calls", "tool_call", "tool_result", "tool_results", "function_call",
                       "function_calls", "exit_code", "stdout", "stderr"})
# Заявления «уже сделано» на верхнем уровне тела: исход сообщает только мир.
CLAIM_KEYS = frozenset({"executed", "execution", "result", "outcome", "confirmed", "completed", "succeeded",
                        "success", "done"})


def _norm(key: Any) -> str:
    """Регистр и разделители не должны прятать признак: `Tool_Calls`, `toolCalls`, `STDOUT`."""
    return str(key).lower().replace("_", "").replace("-", "")


_TOOL = frozenset(map(_norm, TOOL_KEYS))
_CLAIM = frozenset(map(_norm, CLAIM_KEYS))


def _deep_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(_norm(k))
            found |= _deep_keys(v)
    elif isinstance(value, list):
        for v in value:
            found |= _deep_keys(v)
    return found


def tool_use_signs(response: Any) -> list[str]:
    """Признаки использования инструментов/выполнения в ответе (пустой список — чисто). Неверный формат
    ответа сюда не относится: его отклоняет проверка схемы."""
    if not isinstance(response, dict) or not isinstance(response.get("proposals"), list):
        return []
    signs = []
    for pr in response["proposals"]:
        body = pr.get("body") if isinstance(pr, dict) else None
        if not isinstance(body, dict):
            continue
        # В state_change значения операций — данные состояния, их ключи не показатель; смотрим верхний уровень.
        keys = {_norm(k) for k in body} if pr.get("kind") == "state_change" else _deep_keys(body)
        hits = sorted(keys & _TOOL) + sorted({_norm(k) for k in body} & _CLAIM)
        signs += [f"{pr.get('proposal_id')}: {h}" for h in hits]
    return signs


def body_violation(kind: str, body: dict) -> str | None:
    """Текст нарушения, если в теле есть поля, не положенные этому виду; иначе None."""
    extra = sorted(set(body) - BODY_FIELDS[kind])
    return f"в теле {kind} лишние поля: {extra}" if extra else None
