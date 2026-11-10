"""Модели данных (то, что хранится в SQLite)."""
from __future__ import annotations

from dataclasses import dataclass

ALL_WEEKDAYS_MASK = 0b1111111  # Пн..Вс


def mask_to_weekdays(mask: int) -> tuple[int, ...]:
    return tuple(day for day in range(7) if mask & (1 << day))


def weekdays_to_mask(days: "tuple[int, ...] | list[int] | set[int]") -> int:
    mask = 0
    for day in days:
        if not 0 <= day <= 6:
            raise ValueError(f"День недели вне диапазона 0..6: {day}")
        mask |= 1 << day
    return mask


@dataclass(frozen=True, slots=True)
class User:
    telegram_id: int
    name: str
    city: str
    latitude: float
    longitude: float
    timezone: str
    notifications_enabled: bool


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    id: int
    user_id: int
    send_time: str          # «ЧЧ:ММ» в часовом поясе пользователя
    enabled: bool
    weekdays_mask: int
    last_sent_on: str | None  # локальная дата пользователя «ГГГГ-ММ-ДД»

    @property
    def weekdays(self) -> tuple[int, ...]:
        return mask_to_weekdays(self.weekdays_mask)
