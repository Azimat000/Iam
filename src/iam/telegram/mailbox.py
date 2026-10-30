"""Проверка числового белого списка Telegram."""


def allowed_ids(values):
    values = frozenset(values)
    if not values or any(type(v) is not int or v <= 0 for v in values):
        raise ValueError("белый список: положительные числовые id пользователей")
    return values
