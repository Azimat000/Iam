"""Ошибки исполнителя по классам (R-109). Цикл видит их как `ProviderError`; пауза сеанса различает классы."""
from __future__ import annotations

from iam.interfaces import ProviderError


class ProviderAuthError(ProviderError):
    """Нет входа или он недействителен (401/403, истёк токен, аккаунт приостановлен)."""


class ProviderLimitError(ProviderError):
    """Исчерпан лимит подписки (429, «usage limit»)."""


class ProviderUnavailable(ProviderError):
    """Сервис временно недоступен или перегружен (5xx, 529) либо не запустился контейнер."""


class ProviderTimeout(ProviderError):
    """Исполнитель не ответил за отведённое время."""


class ProviderFormatError(ProviderError):
    """Ответ не соответствует формату даже после допустимой повторной попытки (R-194)."""


class SessionLimitReached(ProviderError):
    """Бюджет сеанса (обращения или время) исчерпан; `kind` — `calls` или `time` (R-191)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
