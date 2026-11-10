"""Время, часовые пояса, дни недели, части суток. Без внешних зависимостей."""
from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


class DayPart(str, Enum):
    MORNING = "morning"  # 05:00–11:59
    DAY = "day"          # 12:00–16:59
    EVENING = "evening"  # 17:00–22:59
    NIGHT = "night"      # 23:00–04:59


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Неизвестный часовой пояс: {name!r}") from exc


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def now_in(tz_name: str) -> datetime:
    """Текущее время в часовом поясе пользователя (aware datetime)."""
    return datetime.now(get_zone(tz_name))


def day_part(dt: datetime) -> DayPart:
    hour = dt.hour
    if 5 <= hour < 12:
        return DayPart.MORNING
    if 12 <= hour < 17:
        return DayPart.DAY
    if 17 <= hour < 23:
        return DayPart.EVENING
    return DayPart.NIGHT


def weekday_name(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def format_date(d: date) -> str:
    """date(2026, 11, 9) -> «9 ноября»."""
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


_TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])\s*[:.\-]\s*([0-5]\d)\s*$")


def parse_hhmm(text: str) -> str | None:
    """«7:05», «07.05», «7-05» -> «07:05»; некорректный ввод -> None."""
    match = _TIME_RE.match(text or "")
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


_OFFSET_RE = re.compile(r"^\s*(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})\s*$", re.IGNORECASE)
_ALIASES = {"мск": "Europe/Moscow", "msk": "Europe/Moscow", "москва": "Europe/Moscow"}


@lru_cache(maxsize=1)
def _timezone_index() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


def parse_timezone(text: str) -> str | None:
    """Понимает «Europe/Moscow», «europe/samara», «МСК», «+3», «UTC+5», «GMT-2».

    Смещения превращаются в Etc/GMT∓N (в IANA знак инвертирован: UTC+3 = Etc/GMT-3).
    Дробные смещения (+5:30) не поддерживаются — для них нужно выбрать город-зону IANA.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.lower() in _ALIASES:
        return _ALIASES[raw.lower()]
    if is_valid_timezone(raw):
        return raw
    canonical = _timezone_index().get(raw.lower())
    if canonical:
        return canonical
    match = _OFFSET_RE.match(raw)
    if match:
        sign, hours = match.group(1), int(match.group(2))
        if hours == 0:
            return "UTC"
        candidate = f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"
        if is_valid_timezone(candidate):
            return candidate
    return None


def describe_weekdays(days: "tuple[int, ...] | list[int]") -> str:
    """(0,1,2,3,4,5,6) -> «каждый день»; (0,1,2,3,4) -> «по будням»; иначе «Пн, Ср, Пт»."""
    unique = tuple(sorted(set(days)))
    if unique == tuple(range(7)):
        return "каждый день"
    if unique == (0, 1, 2, 3, 4):
        return "по будням"
    if unique == (5, 6):
        return "по выходным"
    return ", ".join(WEEKDAYS_SHORT[d] for d in unique)
