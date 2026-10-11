"""Ошибки копий, экспорта и восстановления."""


class BackupError(Exception):
    """Общая ошибка подсистемы копий."""


class BackupCorrupt(BackupError):
    """Копия или пакет экспорта повреждены или не согласованы (контрольная сумма, целостность, журнал)."""

    def __init__(self, path, problems: list[str]):
        self.path = path
        self.problems = list(problems)
        super().__init__(f"{path}: " + "; ".join(self.problems))


class NotEnoughSpace(BackupError):
    """На диске не хватает места: работа остановлена до того, как данные могли бы пострадать (R-205)."""

    def __init__(self, free: int, need: int, where: str = ""):
        self.free, self.need = free, need
        super().__init__(f"мало места{' в ' + where if where else ''}: свободно {free} байт, нужно не меньше {need}")


class SecretFound(BackupError):
    """В пакете экспорта остался секрет — пакет не создан (R-204). Значения секретов в сообщение не попадают."""

    def __init__(self, findings: list[str]):
        self.findings = list(findings)
        super().__init__("в экспорте найдены секреты: " + "; ".join(findings))


class RestoreRefused(BackupError):
    """Восстановление отклонено до записи (целевая база уже есть, копия не прошла проверку…)."""
