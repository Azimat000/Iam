"""Ошибки хранилища."""


class StorageError(Exception):
    """Общая ошибка хранилища."""


class RuntimeUnsupported(StorageError):
    """Окружение не подходит: Python ниже 3.13 или SQLite без FTS5 (R-147)."""


class AlreadyLocked(StorageError):
    """Личностью уже управляет другой процесс (замок единственного писателя)."""


class UnknownDatabase(StorageError):
    """Файл не похож на базу личности (есть таблицы, но нет таблицы версий) — мигрировать нельзя."""


class SchemaTooNew(StorageError):
    """Формат базы новее, чем знает этот код."""


class SchemaOutdated(StorageError):
    """База открыта только для чтения, а формат требует миграции."""


class PersonaMismatch(StorageError):
    """Запись относится к другой личности или к хранилищу другого вида."""


class IdempotencyConflict(StorageError):
    """Ключ идемпотентности уже занят событием с другим содержимым (оригинал не перезаписывается)."""

    def __init__(self, key: str, existing_seq: int):
        self.key = key
        self.existing_seq = existing_seq
        super().__init__(f"ключ {key!r} уже занят событием seq={existing_seq} с другим содержимым")


class NotFound(StorageError):
    """Запись не найдена."""
