"""Домен: типы из спецификации S02 (docs/spec), сериализация и проверка по JSON-схемам."""
from .catalog import CATALOG, EVENT_TYPES, EventKind, check_event_kind
from .ids import is_experimental, is_persona_id, new_id, prefix_of
from .jsonschema import ValidationError, validate
from .types import (MEMORY_KINDS, MEMORY_LAYERS, ORIGINS, STATE_GROUPS, Event, Link, MemoryRecord, Op, Source,
                    Transition, canonical_json, validate_envelope)

__all__ = [
    "CATALOG", "EVENT_TYPES", "EventKind", "check_event_kind", "is_experimental", "is_persona_id", "new_id",
    "prefix_of", "ValidationError", "validate", "MEMORY_KINDS", "MEMORY_LAYERS", "ORIGINS", "STATE_GROUPS",
    "Event", "Link", "MemoryRecord", "Op", "Source", "Transition", "canonical_json", "validate_envelope",
]
