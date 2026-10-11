"""Часы жизни личности (docs/spec/time.md#life-clock).

Два времени не смешиваются: календарное (часы компьютера, UTC) и время жизни — собственные часы
личности. Время жизни идёт **только** пока сеанс в состоянии `running`, измеряется монотонными часами
процесса и никогда не убывает. Пока сеанс на паузе или остановлен, значение стоит; календарный скачок
(перевод часов, смена пояса) на него не влияет.

Часы ничего не пишут сами: события и состояние фиксирует StateController. Часы лишь отвечают на вопрос
«сколько прожито» и умеют восстановиться после аварии по сохранённым отметкам (heartbeat и события).
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Callable

from iam.storage import Store


class ClockError(Exception):
    """Неверное обращение к часам (двойной запуск, остановка остановленных, значение из будущего)."""


def calendar_now() -> str:
    """Календарное время компьютера: UTC, ISO 8601 с миллисекундами (формат `occurred_at`)."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_calendar(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def calendar_diff_ms(later: str, earlier: str) -> int:
    """Сколько календарных миллисекунд прошло между двумя метками (может быть отрицательным при скачке часов)."""
    return round((parse_calendar(later) - parse_calendar(earlier)).total_seconds() * 1000)


class LifeClock:
    """Время жизни: `life = base + (monotonic_now − monotonic_at_resume) × time_scale`.

    `monotonic` — источник монотонных часов в наносекундах, `calendar` — источник календарного времени;
    оба подменяются в тестах. `time_scale > 1` — ускоренные сценарии тест-персонажей; постоянная личность
    работает только с 1 (это проверяет StateController).
    """

    def __init__(self, initial_ms: int = 0, *, monotonic: Callable[[], int] = time.monotonic_ns,
                 calendar: Callable[[], str] = calendar_now, time_scale: float = 1.0, heartbeat_s: float = 5.0):
        if initial_ms < 0:
            raise ClockError("время жизни не бывает отрицательным")
        if time_scale < 1:
            raise ClockError("time_scale меньше 1 не поддерживается")
        if heartbeat_s <= 0:
            raise ClockError("heartbeat_s должен быть положительным")
        self._monotonic = monotonic
        self._calendar = calendar
        self.time_scale = time_scale
        self.heartbeat_s = heartbeat_s
        self._base = int(initial_ms)      # значение на момент последнего запуска (или замороженное)
        self._mono0 = 0                   # монотонная отметка последнего запуска
        self._running = False
        self._last = int(initial_ms)      # последнее выданное значение: не даёт времени убывать
        self._hb_mono = 0                 # монотонная отметка последнего heartbeat

    # --- чтение --------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def calendar_now(self) -> str:
        return self._calendar()

    def monotonic_ms(self) -> int:
        """Реальное (не жизненное) монотонное время процесса — для технических таймаутов `deadline_ms`."""
        return self._monotonic() // 1_000_000

    def now_ms(self) -> int:
        """Текущее время жизни. На паузе — замороженное значение. Не убывает даже при сбое источника."""
        if self._running:
            delta = max(0, self._monotonic() - self._mono0)
            value = self._base + int(delta * self.time_scale) // 1_000_000
            self._last = max(self._last, value)
        return self._last

    # --- управление ----------------------------------------------------------------------------

    def resume(self) -> int:
        """Запустить ход времени жизни (начало или возобновление сеанса) с замороженного значения."""
        if self._running:
            raise ClockError("часы уже идут")
        self._base = self._last
        self._mono0 = self._hb_mono = self._monotonic()
        self._running = True
        return self._base

    def freeze(self, at_ms: int | None = None) -> int:
        """Остановить ход (пауза/остановка). `at_ms` — значение, уже записанное в событие паузы: часы
        замораживаются ровно на нём, чтобы запись и часы не расходились. Значение из будущего недопустимо."""
        if not self._running:
            raise ClockError("часы уже стоят")
        now = self.now_ms()
        if at_ms is None:
            at_ms = now
        if not self._base <= at_ms <= now:
            raise ClockError(f"заморозка на {at_ms} вне допустимого отрезка [{self._base}, {now}]")
        self._running = False
        self._base = self._last = at_ms
        return at_ms

    # --- heartbeat и восстановление ------------------------------------------------------------

    def heartbeat_due(self) -> bool:
        """Пора ли записать контрольную точку (раз в `heartbeat_s` секунд монотонного времени, пока идёт ход)."""
        return self._running and (self._monotonic() - self._hb_mono) >= self.heartbeat_s * 1e9

    def heartbeat(self, store: Store, *, force: bool = False) -> bool:
        """Записать контрольную точку времени жизни в служебную таблицу часов (не событие журнала).
        Возвращает, была ли запись. Без хода часов записывать нечего."""
        if not self._running or not (force or self.heartbeat_due()):
            return False
        store.heartbeat(self.now_ms(), self._calendar())
        self._hb_mono = self._monotonic()
        return True

    @classmethod
    def recovered_ms(cls, store: Store) -> int:
        """Время жизни после запуска/аварии: максимум из контрольной точки и последнего события журнала.
        Прошедшее после них не засчитывается — значение всегда в сторону «жизни меньше»."""
        marks = [0]
        clock = store.clock()
        if clock is not None:
            marks.append(clock[0])
        last = store.last_seq()
        if last:
            marks.append(store.get_event(last).life_time_ms)
        return max(marks)

    @classmethod
    def recover(cls, store: Store, **kw) -> "LifeClock":
        """Остановленные часы со значением, восстановленным из хранилища."""
        return cls(cls.recovered_ms(store), **kw)
