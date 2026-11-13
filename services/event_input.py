"""Разбор пользовательского ввода для событий: название, время, продолжительность, дата, число месяца.
Чистые функции; ошибки возвращаются понятным текстом, который можно показать пользователю."""
from __future__ import annotations

import re
from datetime import date, timedelta

from services.schedule_parser import DAY_WORDS, NUMBER_WORDS
from services.time_service import MONTHS_GEN

MAX_TITLE_LEN = 60
MAX_MINUTES = 1440  # сутки
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def parse_title(text: str) -> tuple[str | None, str | None]:
    title = " ".join(_CONTROL_RE.sub(" ", text or "").split())
    if not title:
        return None, "Название не может быть пустым — напиши, как назвать событие."
    if len(title) > MAX_TITLE_LEN:
        return None, f"Слишком длинное название ({len(title)}). Уложись в {MAX_TITLE_LEN} символов."
    if not any(ch.isalnum() for ch in title):
        return None, "В названии должна быть хотя бы одна буква или цифра."
    return title[0].upper() + title[1:], None


# ----------------------------------------------------------------- время начала

_TIME_RE = re.compile(
    r"^\s*(?:в\s*)?(?P<h>\d{1,2})(?:\s*[:.\-]\s*(?P<m1>\d{2})|\s+(?P<m2>\d{2}))?\s*(?:ч|час|часа|часов)?\s*$",
    re.IGNORECASE,
)


def parse_time_input(text: str) -> str | None:
    """«16:00», «16.30», «16 30», «в 9», «9:05» -> «ЧЧ:ММ»; иначе None."""
    m = _TIME_RE.match(text or "")
    if not m:
        return None
    hour = int(m.group("h"))
    minute = int(m.group("m1") or m.group("m2") or 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


# ----------------------------------------------------------------- продолжительность

_PAIR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(часов|часа|час|ч|h|минуты|минуту|минут|мин|м|m)(?![а-яa-z])")
_NUM_WORD_RE = re.compile(r"\b(" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")\b(?=\s*(?:час|ч\b|мин|м\b))")


def parse_duration(text: str, bare_unit: str = "auto") -> tuple[int | None, str | None]:
    """Возвращает (минуты, None) или (None, причина).

    Понимает: «2 часа», «2ч», «1.5 часа», «90 мин», «1 час 30 минут», «1:30», «полчаса», «полтора часа»,
    «два часа». Голое число: bare_unit='auto' — целое ≥10 это минуты, иначе часы («2» -> 2 часа, «45» -> 45 минут;
    «1,5» -> 1,5 часа); bare_unit='minutes' — всегда минуты (для «свой вариант» напоминания)."""
    raw = " ".join((text or "").lower().replace(",", ".").split())
    if not raw:
        return None, "Напиши продолжительность, например «2 часа» или «90 минут»."

    minutes: float | None = None
    special = {"полчаса": 30, "полтора часа": 90, "полтора": 90, "час": 60}
    if raw in special:
        minutes = special[raw]
    elif (m := re.fullmatch(r"(\d{1,2}):(\d{2})", raw)):
        minutes = int(m.group(1)) * 60 + int(m.group(2))
    elif re.fullmatch(r"\d+(?:\.\d+)?", raw):
        value = float(raw)
        if bare_unit == "minutes":
            minutes = value
        elif "." in raw:
            minutes = value * 60
        else:
            minutes = value if value >= 10 else value * 60
    else:
        prepared = _NUM_WORD_RE.sub(lambda mo: str(NUMBER_WORDS[mo.group(1)]), raw)
        prepared = prepared.replace("полтора часа", "1.5 часа").replace("полчаса", "0.5 часа")
        pairs = list(_PAIR_RE.finditer(prepared))
        leftover = _PAIR_RE.sub(" ", prepared)
        if not pairs or leftover.replace("и", " ").strip(" .,;"):
            return None, "Не понял продолжительность. Примеры: «2 часа», «90 мин», «1:30»."
        total = 0.0
        for pair in pairs:
            value, unit = float(pair.group(1)), pair.group(2)
            total += value * 60 if unit.startswith(("ч", "h")) else value
        minutes = total

    result = int(round(minutes))
    if result < 1:
        return None, "Продолжительность должна быть больше нуля."
    if result > MAX_MINUTES:
        return None, "Максимум — 24 часа. Напиши поменьше."
    return result, None


def parse_remind_minutes(text: str) -> tuple[int | None, str | None]:
    """«Свой вариант»: число минут («45»), либо с единицами («1 час», «90 мин»). От 1 минуты до 24 часов."""
    minutes, error = parse_duration(text, bare_unit="minutes")
    if error:
        return None, "Напиши, за сколько минут напомнить: например «45» или «1 час»."
    return minutes, None


# ----------------------------------------------------------------- дата

_MONTHS = {name: i + 1 for i, name in enumerate(MONTHS_GEN)}
_MONTH_PREFIXES = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6, "июл": 7, "авг": 8,
                   "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
_NUMERIC_DATE_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?$")
_TEXT_DATE_RE = re.compile(r"^(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?(?:\s*г\.?)?$")


def _month_from_word(word: str) -> int | None:
    """«ноября», «ноябрь», «ноя» -> 11."""
    if word in _MONTHS:
        return _MONTHS[word]
    return _MONTH_PREFIXES.get(word[:3]) if len(word) >= 3 else None


def parse_date_input(text: str, today: date) -> tuple[date | None, str | None]:
    """«сегодня», «завтра», «в пятницу», «15.11», «15.11.2027», «15 ноября» -> (дата, None).
    Прошедшая дата без года переносится на следующий год; с явным годом — ошибка."""
    raw = " ".join((text or "").lower().split()).strip(" .")
    raw = re.sub(r"^(?:в|во|на)\s+", "", raw)
    if not raw:
        return None, "Напиши дату, например «завтра», «15.11» или «15 ноября»."
    if raw == "сегодня":
        return today, None
    if raw == "завтра":
        return today + timedelta(days=1), None
    if raw == "послезавтра":
        return today + timedelta(days=2), None
    if raw in DAY_WORDS:
        delta = (DAY_WORDS[raw] - today.weekday()) % 7
        return today + timedelta(days=delta), None

    day = month = year = None
    m = _NUMERIC_DATE_RE.match(raw)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
    else:
        t = _TEXT_DATE_RE.match(raw)
        if t and (mon := _month_from_word(t.group(2))):
            day, month = int(t.group(1)), mon
            year = int(t.group(3)) if t.group(3) else None
    if day is None:
        return None, "Не понял дату. Примеры: «завтра», «15.11», «15 ноября», «в пятницу»."

    explicit_year = year is not None
    for attempt_year in ([year] if explicit_year else [today.year, today.year + 1]):
        try:
            candidate = date(attempt_year, month, day)
        except ValueError:
            if not explicit_year and attempt_year == today.year:
                continue  # например, 29.02 в невисокосный год — пробуем следующий
            return None, "Такой даты нет в календаре."
        if candidate >= today:
            return candidate, None
        if explicit_year:
            return None, "Эта дата уже прошла."
    return None, "Такой даты нет в календаре."


# ----------------------------------------------------------------- число месяца

_MONTH_DAY_RE = re.compile(r"^(\d{1,2})(?:\s*-?\s*(?:е|го|ое|ого|число|числа|й))?$")


def parse_month_day(text: str) -> tuple[int | None, str | None]:
    """«15», «15-го», «15 числа» -> 15; «последний день», «конец месяца» -> -1."""
    raw = " ".join((text or "").lower().split()).strip(" .")
    if raw.startswith(("послед", "конец", "конц")):
        return -1, None
    m = _MONTH_DAY_RE.match(raw)
    if not m:
        return None, "Напиши число месяца от 1 до 31 или «последний день»."
    value = int(m.group(1))
    if not 1 <= value <= 31:
        return None, "Число месяца — от 1 до 31."
    return value, None
