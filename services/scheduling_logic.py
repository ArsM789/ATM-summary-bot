"""Чистая логика «кому пора слать»: без БД, сети и aiogram — легко тестируется."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from database.models import ScheduleEntry, User
from services.time_service import get_zone

logger = logging.getLogger(__name__)

# Если бот был недоступен, сводка догоняется в пределах этого окна после запланированного времени.
CATCHUP_WINDOW_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class Due:
    entry: ScheduleEntry
    user: User
    local_date: str            # локальная дата пользователя, на которую запланирована отправка
    slot: str                  # «ЧЧ:ММ» из расписания
    scheduled_ts: float        # unix-время запланированного момента


def find_due(
    items: Iterable[tuple[ScheduleEntry, User]],
    now_utc: datetime,
    window_seconds: int = CATCHUP_WINDOW_SECONDS,
) -> list[Due]:
    """Для каждой пары (расписание, пользователь) считает ЛОКАЛЬНОЕ время пользователя и решает,
    наступил ли момент отправки. Проверяются сегодняшняя и вчерашняя локальные даты (на случай
    перехода через полночь у отправок вроде 23:50 + опоздание). Отправка действительна только если
    её момент не раньше entry.effective_from (добавили время в 08:20 — сводка за 08:00 не придёт)."""
    now_ts = now_utc.timestamp()
    due: list[Due] = []
    for entry, user in items:
        try:
            tz = get_zone(user.timezone)
            hour, minute = (int(x) for x in entry.send_time.split(":"))
        except ValueError:
            logger.error("Пропускаю расписание %s пользователя %s: некорректные данные", entry.id, user.telegram_id)
            continue
        today = now_utc.astimezone(tz).date()
        for day in (today, today - timedelta(days=1)):
            if day.weekday() not in entry.weekdays:
                continue
            scheduled_ts = datetime.combine(day, time(hour, minute), tzinfo=tz).timestamp()
            if 0 <= now_ts - scheduled_ts <= window_seconds and scheduled_ts >= entry.effective_from:
                due.append(Due(entry, user, day.isoformat(), entry.send_time, scheduled_ts))
    due.sort(key=lambda d: d.scheduled_ts)
    return due
