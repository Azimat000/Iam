"""Мини-проверка JSON Schema (подмножество draft 2020-12), только стандартная библиотека.

Поддержано: type, enum, const, required, properties, additionalProperties(false), items, minItems,
uniqueItems, minLength, maxLength, minimum, maximum, pattern, allOf, if/then, not, $ref на файл рядом.
Неизвестное ключевое слово — ошибка (чтобы схема не «молча» ничего не проверяла).
Перенесено из tests/schema_lite.py (S02) в пакет в S04; tests/schema_lite.py теперь реэкспорт.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

KNOWN = {"$schema", "$id", "title", "description", "type", "enum", "const", "required", "properties",
         "additionalProperties", "items", "minItems", "uniqueItems", "minLength", "maxLength", "minimum",
         "maximum", "pattern", "allOf", "if", "then", "not", "$ref"}
TYPES = {"string": str, "object": dict, "array": list, "boolean": bool, "null": type(None)}


def _is_type(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, TYPES[t])


@lru_cache(maxsize=64)
def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check(schema: dict, value, base: Path, path: str = "$") -> list[str]:
    """Список ошибок (пустой — значение подходит под схему). `base` — каталог для `$ref`."""
    errs: list[str] = []
    unknown = set(schema) - KNOWN
    if unknown:
        return [f"{path}: схема использует неподдерживаемые ключевые слова {sorted(unknown)}"]
    if "$ref" in schema:
        errs += check(_load(Path(base) / schema["$ref"]), value, base, path)
    if "type" in schema:
        ts = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in ts):
            return errs + [f"{path}: тип {type(value).__name__}, нужен {ts}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} не из enum")
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: ожидалось {schema['const']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: строка короче {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: строка длиннее {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: «{value}» не соответствует шаблону {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} меньше {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} больше {schema['maximum']}")
    if isinstance(value, dict):
        for r in schema.get("required", []):
            if r not in value:
                errs.append(f"{path}: нет обязательного поля {r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append(f"{path}: лишнее поле {k}")
        for k, sub in props.items():
            if k in value:
                errs += check(sub, value[k], base, f"{path}.{k}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: элементов меньше {schema['minItems']}")
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            errs.append(f"{path}: элементы не уникальны")
        if "items" in schema:
            for i, x in enumerate(value):
                errs += check(schema["items"], x, base, f"{path}[{i}]")
    for sub in schema.get("allOf", []):
        errs += check(sub, value, base, path)
    if "if" in schema and not check(schema["if"], value, base, path):
        if "then" in schema:
            errs += check(schema["then"], value, base, path)
    if "not" in schema and not check(schema["not"], value, base, path):
        errs.append(f"{path}: нарушено условие not")
    return errs


SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
SCHEMA_NAMES = ("event", "memory_record", "transition", "proposal", "request", "response")


class ValidationError(ValueError):
    """Значение не подходит под схему; `errors` — список причин."""

    def __init__(self, name: str, errors: list[str]):
        self.name = name
        self.errors = errors
        super().__init__(f"{name}: " + "; ".join(errors))


def errors(name: str, value) -> list[str]:
    """Ошибки проверки `value` по схеме `name` (имя без `.schema.json`)."""
    if name not in SCHEMA_NAMES:
        raise KeyError(f"неизвестная схема {name!r}")
    return check(_load(SCHEMA_DIR / f"{name}.schema.json"), value, SCHEMA_DIR)


def validate(name: str, value) -> None:
    """Бросает ValidationError, если `value` не подходит под схему `name`."""
    errs = errors(name, value)
    if errs:
        raise ValidationError(name, errs)
