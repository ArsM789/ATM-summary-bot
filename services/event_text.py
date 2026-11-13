"""Оформление событий: предпросмотр, карточка, строка списка и короткое напоминание. Чистые функции.

Все функции принимают «поля события» — словарь с ключами title, recurrence, weekdays_mask, month_day,
event_date, start_time, duration_min, remind_before_min (его даёт fields_from_event или черновик диалога)."""
from __future__ import annotations

import random
from datetime import date, datetime

from database.models import UserEvent, mask_to_weekdays
from services.event_schedule import LAST_DAY, end_time_text, ends_next_day
from services.time_service import WEEKDAYS, WEEKDAYS_SHORT, format_date, weekday_name
from utils.formatting import capitalize_first, esc, plural

REPEAT_LABELS = {
    "none": "Без повторения", "daily": "Каждый день", "weekly": "Каждую неделю", "monthly": "Каждый месяц",
}
STATUS_LABELS = {"active": "активно ✅", "disabled": "отключено ⏸", "done": "завершено"}


def fields_from_event(event: UserEvent) -> dict:
    return {
        "title": event.title, "recurrence": event.recurrence, "weekdays_mask": event.weekdays_mask,
        "month_day": event.month_day, "event_date": event.event_date, "start_time": event.start_time,
        "duration_min": event.duration_min, "remind_before_min": event.remind_before_min,
    }


# ----------------------------------------------------------------- отдельные значения

def format_amount(minutes: int) -> str:
    """90 -> «1 час 30 минут», 120 -> «2 часа», 5 -> «5 минут», 1 -> «1 минуту» (винительный: «за …», «через …»)."""
    hours, rest = divmod(int(minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if rest:
        parts.append(f"{rest} {plural(rest, 'минуту', 'минуты', 'минут')}")
    return " ".join(parts)


def format_duration(minutes: int) -> str:
    """Продолжительность (именительный): «2 часа», «45 минут», «1 час 30 минут»."""
    hours, rest = divmod(int(minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} {plural(hours, 'час', 'часа', 'часов')}")
    if rest:
        parts.append(f"{rest} {plural(rest, 'минута', 'минуты', 'минут')}")
    return " ".join(parts)


def format_remind(minutes: int) -> str:
    return f"За {format_amount(minutes)}"


def format_weekdays(days: tuple[int, ...] | list[int]) -> str:
    unique = tuple(sorted(set(days)))
    if unique == tuple(range(7)):
        return "Каждый день"
    if unique == (0, 1, 2, 3, 4):
        return "Будни"
    if unique == (5, 6):
        return "Выходные"
    names = [WEEKDAYS[d] for d in unique]
    return capitalize_first(", ".join(names))


def format_time_range(start_time: str, duration_min: int) -> str:
    text = f"{start_time}–{end_time_text(start_time, duration_min)}"
    return text + " (до следующего дня)" if ends_next_day(start_time, duration_min) else text


def format_month_day(month_day: int | None) -> str:
    if month_day == LAST_DAY:
        return "Последний день месяца"
    if month_day is None:
        return "—"
    if month_day >= 29:
        return f"{month_day}-е число (если такого нет в месяце — последний день)"
    return f"{month_day}-е число"


def when_line(fields: dict, today: date | None = None) -> tuple[str, str]:
    """(подпись, значение): «Дни: Вторник, четверг» / «Дата: 12 ноября, четверг» / «Число: 15-е число»."""
    rec = fields["recurrence"]
    if rec == "none":
        d = date.fromisoformat(fields["event_date"]) if fields.get("event_date") else None
        if d is None:
            return "Дата", "—"
        year = f" {d.year}" if today and d.year != today.year else ""
        return "Дата", f"{format_date(d)}{year}, {weekday_name(d)}"
    if rec == "daily":
        return "Дни", "Каждый день"
    if rec == "weekly":
        return "Дни", format_weekdays(mask_to_weekdays(fields.get("weekdays_mask", 0)))
    return "Число", format_month_day(fields.get("month_day"))


def short_label(fields: dict, status: str = "active") -> str:
    """Подпись кнопки в списке: «Урок английского · Вт, Чт 16:00»."""
    rec, start = fields["recurrence"], fields["start_time"]
    if rec == "none":
        d = date.fromisoformat(fields["event_date"]) if fields.get("event_date") else None
        when = f"{d.day:02d}.{d.month:02d} {start}" if d else start
    elif rec == "daily":
        when = f"ежедневно {start}"
    elif rec == "weekly":
        days = mask_to_weekdays(fields.get("weekdays_mask", 0))
        when = (", ".join(WEEKDAYS_SHORT[d] for d in days) if len(days) <= 3 else f"{len(days)} дн.") + f" {start}"
    else:
        md = fields.get("month_day")
        when = ("посл. дня" if md == LAST_DAY else f"{md}-го") + f" {start}"
    icon = "🔔" if status == "active" else "⏸"
    title = fields["title"] if len(fields["title"]) <= 22 else fields["title"][:21] + "…"
    return f"{icon} {title} · {when}"


# ----------------------------------------------------------------- карточки

def _lines(fields: dict, today: date | None) -> list[str]:
    label, value = when_line(fields, today)
    return [
        f"Название: <b>{esc(fields['title'])}</b>",
        f"{label}: <b>{esc(value)}</b>",
        f"Время: <b>{format_time_range(fields['start_time'], fields['duration_min'])}</b>",
        f"Повторение: <b>{REPEAT_LABELS[fields['recurrence']]}</b>",
        f"Уведомление: <b>{format_remind(fields['remind_before_min'])}</b>",
    ]


def preview_text(fields: dict, today: date | None = None, *, editing: bool = False) -> str:
    head = "✏️ <b>Изменение события</b>" if editing else "📅 <b>Новое событие</b>"
    return head + "\n\n" + "\n".join(_lines(fields, today)) + "\n\n<i>Проверь и сохрани.</i>"


def card_text(event: UserEvent, next_start: datetime | None, now_local: datetime | None) -> str:
    fields = fields_from_event(event)
    today = now_local.date() if now_local else None
    lines = _lines(fields, today)
    lines.append(f"Статус: <b>{STATUS_LABELS.get(event.status, event.status)}</b>")
    if next_start is not None and event.status == "active":
        lines.append(f"Ближайшее: <b>{_when_text(next_start, today)}</b>")
    return f"📅 <b>{esc(event.title)}</b>\n\n" + "\n".join(lines[1:])


def _when_text(start: datetime, today: date | None) -> str:
    day = start.date()
    if today is not None:
        diff = (day - today).days
        if diff == 0:
            return f"сегодня, {start:%H:%M}"
        if diff == 1:
            return f"завтра, {start:%H:%M}"
    return f"{format_date(day)}, {weekday_name(day)}, {start:%H:%M}"


# ----------------------------------------------------------------- напоминание

NEUTRAL_NAME = "друг"


def reminder_text(
    address: str | None, title: str, minutes_left: int, start: datetime, end: datetime,
    today_local: date, rng: random.Random | None = None,
) -> str:
    """Короткое напоминание: приветствие, «через N … начнётся: Название», время. Без погоды и прочего."""
    rng = rng or random.Random()
    has_name = bool(address) and address.casefold() != NEUTRAL_NAME
    name = esc(address) if has_name else ""
    greeting = rng.choice(
        [f"{name}, привет)", f"Привет, {name}!", f"{name}, напоминаю:"] if has_name else ["Привет!", "Напоминание:"]
    )
    left = format_amount(max(1, minutes_left))
    t = esc(title)
    middle = rng.choice([
        f"Через {left} у тебя начнётся: <b>{t}</b>.",
        f"Через {left} начинается: <b>{t}</b>.",
    ])
    time_line = f"🕓 {start:%H:%M}–{end:%H:%M}"
    diff = (start.date() - today_local).days
    if diff == 1:
        time_line += " · завтра"
    elif diff >= 2:
        time_line += f" · {format_date(start.date())}"
    return f"{greeting}\n\n{middle}\n\n{time_line}"
