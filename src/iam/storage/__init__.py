"""Хранилище: SQLite, схема с миграциями, журнал только на дописывание, транзакции, замок писателя."""
from .db import connect, transaction
from .errors import (AlreadyLocked, IdempotencyConflict, NotFound, PersonaMismatch, RuntimeUnsupported,
                     SchemaOutdated, SchemaTooNew, StorageError, UnknownDatabase)
from .lock import WriterLock
from .migrations import LATEST, MIGRATIONS, Migration, current_version, migrate
from .runtime import check_runtime, runtime_problems
from .store import DEFAULT_LAYERS, InboxItem, OutboxItem, Snapshot, Store, default_path, normalize_text

__all__ = [
    "connect", "transaction", "AlreadyLocked", "IdempotencyConflict", "NotFound", "PersonaMismatch",
    "RuntimeUnsupported", "SchemaOutdated", "SchemaTooNew", "StorageError", "UnknownDatabase", "WriterLock",
    "LATEST", "MIGRATIONS", "Migration", "current_version", "migrate", "check_runtime", "runtime_problems",
    "DEFAULT_LAYERS", "InboxItem", "OutboxItem", "Snapshot", "Store", "default_path", "normalize_text",
]
