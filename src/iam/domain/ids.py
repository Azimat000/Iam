"""Идентификаторы (docs/spec/events.md#ids): `<префикс>_<8–40 символов [A-Za-z0-9]>`."""
from __future__ import annotations

import re
import secrets
import string

ALPHABET = string.ascii_letters + string.digits
PREFIXES = frozenset({
    "per", "exp", "evt", "req", "prop", "act", "mem", "trn", "int", "opr", "cap",
    "ses", "cyc", "inp", "mom", "crq", "snp",
})
ID_RE = re.compile(r"^([a-z]{2,4})_[A-Za-z0-9]{8,40}$")
PERSONA_RE = re.compile(r"^(per|exp)_[A-Za-z0-9]{8,40}$")


def new_id(prefix: str, length: int = 20) -> str:
    """Новый непрозрачный случайный идентификатор с префиксом из каталога."""
    if prefix not in PREFIXES:
        raise ValueError(f"неизвестный префикс идентификатора {prefix!r}")
    if not 8 <= length <= 40:
        raise ValueError("длина хвоста идентификатора — от 8 до 40")
    return prefix + "_" + "".join(secrets.choice(ALPHABET) for _ in range(length))


def prefix_of(value: str) -> str:
    """Префикс корректного идентификатора; ValueError, если формат неверен."""
    m = ID_RE.match(value) if isinstance(value, str) else None
    if not m or m.group(1) not in PREFIXES:
        raise ValueError(f"неверный идентификатор {value!r}")
    return m.group(1)


def is_persona_id(value: str) -> bool:
    return isinstance(value, str) and PERSONA_RE.match(value) is not None


def is_experimental(persona_id: str) -> bool:
    """Экспериментальный персонаж (`exp_…`) — никогда не в постоянном хранилище."""
    if not is_persona_id(persona_id):
        raise ValueError(f"неверный persona_id {persona_id!r}")
    return persona_id.startswith("exp_")
