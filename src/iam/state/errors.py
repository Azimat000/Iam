"""Ошибки привратника состояния."""


class StateError(Exception):
    """Общая ошибка StateController."""


class StateCorrupt(StateError):
    """Журнал и снимки не согласованы (переход без снимка, пропуск версии и т. п.)."""


class ClockRegression(StateError):
    """Время жизни пытались записать меньше уже записанного (нарушение «не убывает вдоль seq»)."""


class ReplayError(StateError):
    """Воспроизведение журнала невозможно или расходится со снимком."""


class NotSupported(StateError):
    """Возможность появится в более позднем срезе (в сообщении указано, в каком)."""
