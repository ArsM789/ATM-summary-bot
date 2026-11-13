"""Правила повторения событий и расчёт ближайших вхождений. Чистая логика: без БД, сети и aiogram.

В базе хранится ПРАВИЛО (тип повторения, дни недели / число месяца, время, длительность, пояс), а не
отдельные записи на каждую дату. Из правила вычисляются вхождения на нужные даты.

Календарные особенности:
- месяц без нужного числа (31-е в апреле, 30-е в феврале): событие переносится на последний день месяца;
  отдельно есть «последний день месяца» (month_day = -1);
- переход через месяцы, годы и 29 февраля — по настоящему календарю (calendar.monthrange);
- перевод часов: вхождение — «16:00 по местному времени», instants считаются через UTC, поэтому
  в день перехода событие не пропадает и не дублируется; длительность считается в реальных минутах;
- событие, переходящее через полночь, относится к дате НАЧАЛА."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from database.models import UserEvent, mask_to_weekdays

UTC = timezone.utc
LAST_DAY = -1  # month_day: «последний день месяца»


@dataclass(frozen=True, slots=True)
class EventRule:
    recurrence: str                  # none | daily | weekly | monthly
    start_time: str                  # «ЧЧ:ММ»
    duration_min: int
    timezone: str
    anchor_date: date | None         # none: дата события; recurring: первая возможная дата
    weekdays: tuple[int, ...] = ()
    month_day: int | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def rule_from_event(event: UserEvent) -> EventRule:
    return EventRule(
        recurrence=event.recurrence, start_time=event.start_time, duration_min=event.duration_min,
        timezone=event.timezone, anchor_date=date.fromisoformat(event.event_date) if event.event_date else None,
        weekdays=event.weekdays, month_day=event.month_day,
    )


def last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def effective_month_day(month_day: int, year: int, month: int) -> int:
    """15 -> 15; 31 в апреле -> 30; 30 в феврале 2027 -> 28 (2028 — 29); -1 -> последний день."""
    last = last_day_of_month(year, month)
    return last if month_day == LAST_DAY else min(month_day, last)


def occurs_on(rule: EventRule, day: date) -> bool:
    """Есть ли вхождение события в этот локальный день (по дате НАЧАЛА)."""
    anchor = rule.anchor_date
    if rule.recurrence == "none":
        return anchor is not None and day == anchor
    if anchor is not None and day < anchor:
        return False
    if rule.recurrence == "daily":
        return True
    if rule.recurrence == "weekly":
        return day.weekday() in rule.weekdays
    if rule.recurrence == "monthly":
        if rule.month_day is None:
            return False
        return day.day == effective_month_day(rule.month_day, day.year, day.month)
    return False


def start_datetime(rule: EventRule, day: date) -> datetime:
    hour, minute = (int(x) for x in rule.start_time.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=rule.tz)


def end_datetime(start: datetime, duration_min: int) -> datetime:
    """Конец = начало + РЕАЛЬНЫЕ минуты (через UTC), в местном времени."""
    return (start.astimezone(UTC) + timedelta(minutes=duration_min)).astimezone(start.tzinfo)


def crosses_midnight(rule: EventRule, day: date) -> bool:
    start = start_datetime(rule, day)
    return end_datetime(start, rule.duration_min).date() != start.date()


def end_time_text(start_time: str, duration_min: int) -> str:
    """«16:00» + 120 -> «18:00» (без учёта перевода часов — для карточки; для расчётов см. end_datetime)."""
    hour, minute = (int(x) for x in start_time.split(":"))
    total = (hour * 60 + minute + duration_min) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def ends_next_day(start_time: str, duration_min: int) -> bool:
    hour, minute = (int(x) for x in start_time.split(":"))
    return hour * 60 + minute + duration_min >= 24 * 60


def occurrences_between(rule: EventRule, from_ts: float, to_ts: float) -> list[datetime]:
    """Все начала вхождений с from_ts <= начало <= to_ts (unix-время), по возрастанию."""
    tz = rule.tz
    first = datetime.fromtimestamp(from_ts, tz).date() - timedelta(days=1)
    last = datetime.fromtimestamp(to_ts, tz).date() + timedelta(days=1)
    found: list[datetime] = []
    day = first
    while day <= last:
        if occurs_on(rule, day):
            start = start_datetime(rule, day)
            if from_ts <= start.timestamp() <= to_ts:
                found.append(start)
        day += timedelta(days=1)
    return found


def next_occurrence(rule: EventRule, after_ts: float, horizon_days: int = 400) -> datetime | None:
    """Ближайшее начало >= after_ts или None (разовое уже прошло / дальше горизонта)."""
    tz = rule.tz
    if rule.recurrence == "none":
        if rule.anchor_date is None:
            return None
        start = start_datetime(rule, rule.anchor_date)
        return start if start.timestamp() >= after_ts else None
    day = datetime.fromtimestamp(after_ts, tz).date() - timedelta(days=1)
    for _ in range(horizon_days):
        if occurs_on(rule, day):
            start = start_datetime(rule, day)
            if start.timestamp() >= after_ts:
                return start
        day += timedelta(days=1)
    return None


# ----------------------------------------------------------------- напоминания

@dataclass(frozen=True, slots=True)
class DueReminder:
    event: UserEvent
    start: datetime            # начало вхождения (локальное)
    remind_ts: float           # когда надо было напомнить
    local_date: str            # дата начала вхождения «ГГГГ-ММ-ДД» (часть ключа дедупликации)

    @property
    def slot(self) -> str:
        """Ключ дедупликации: событие + время начала (смена времени = новое вхождение)."""
        return f"{self.event.id}@{self.event.start_time}"


def find_due_reminders(
    events: list[UserEvent], now_ts: float, grace_seconds: int = 300,
) -> list[DueReminder]:
    """Напоминания, которые пора отправить СЕЙЧАС.

    Условия (все обязательны):
    - момент напоминания (начало − «за N минут») уже наступил, но не старше grace (по умолчанию 5 минут):
      если бот был выключен дольше — устаревшее напоминание не шлём;
    - событие ещё не началось (по прошедшим событиям не напоминаем);
    - момент напоминания не раньше effective_from (создание/правка/включение события): созданное в 15:50
      событие на 16:00 «за 20 минут» не пришлёт напоминание задним числом."""
    due: list[DueReminder] = []
    for event in events:
        if event.status != "active":
            continue
        try:
            rule = rule_from_event(event)
        except (ValueError, KeyError):
            continue
        lead = event.remind_before_min * 60
        for start in occurrences_between(rule, now_ts - grace_seconds + lead, now_ts + lead):
            start_ts = start.timestamp()
            remind_ts = start_ts - lead
            if start_ts <= now_ts or remind_ts > now_ts or remind_ts < now_ts - grace_seconds:
                continue
            if remind_ts < event.effective_from:
                continue
            due.append(DueReminder(event, start, remind_ts, start.date().isoformat()))
    due.sort(key=lambda d: (d.remind_ts, d.event.id))
    return due


def is_finished_one_time(event: UserEvent, now_ts: float, keep_seconds: int = 3600) -> bool:
    """Разовое событие закончилось больше keep_seconds назад — его можно перевести в «done»."""
    if event.recurrence != "none" or not event.event_date:
        return False
    rule = rule_from_event(event)
    start = start_datetime(rule, rule.anchor_date)
    end = end_datetime(start, rule.duration_min)
    return end.timestamp() + keep_seconds < now_ts


def rule_from_fields(fields: dict, tz_name: str) -> EventRule:
    """Правило из «полей события» (черновик диалога / словарь для записи в БД)."""
    return EventRule(
        recurrence=fields["recurrence"], start_time=fields["start_time"], duration_min=fields["duration_min"],
        timezone=tz_name, anchor_date=date.fromisoformat(fields["event_date"]) if fields.get("event_date") else None,
        weekdays=mask_to_weekdays(fields.get("weekdays_mask", 0)), month_day=fields.get("month_day"),
    )
