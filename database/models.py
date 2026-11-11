"""Модели данных (то, что хранится в SQLite)."""
from __future__ import annotations

from dataclasses import dataclass, field

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
    name: str                       # основное имя (preferred_name)
    city: str
    latitude: float
    longitude: float
    timezone: str
    notifications_enabled: bool
    username: str | None = None
    first_name: str | None = None   # имя из профиля Telegram
    name_variants: tuple[str, ...] = field(default_factory=tuple)
    last_used_name: str | None = None
    name_confirmed: bool = False    # False — имя ещё не вводилось пользователем
    bot_blocked: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    id: int
    user_id: int
    send_time: str          # «ЧЧ:ММ» в часовом поясе пользователя
    enabled: bool
    weekdays_mask: int
    effective_from: int = 0  # unix-время: расписание действует только для моментов не раньше этого

    @property
    def weekdays(self) -> tuple[int, ...]:
        return mask_to_weekdays(self.weekdays_mask)


@dataclass(frozen=True, slots=True)
class LessonItem:
    """Один предмет дня: название и сколько уроков подряд/за день."""

    subject: str
    count: int


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    id: int
    user_id: int
    message: str
    status: str
    created_at: str
    user_name: str | None = None
    username: str | None = None


@dataclass(frozen=True, slots=True)
class Broadcast:
    id: int
    admin_id: int
    message: str
    status: str
    total_count: int
    success_count: int
    failed_count: int
    blocked_count: int
