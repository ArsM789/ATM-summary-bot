"""Склейка: пользователь + его расписание + погода + вариант обращения -> готовое сообщение.

Этим сервисом пользуются и кнопка «Сводка», и планировщик — ручные и автоматические сводки
формируются одним кодом. Метод build() работает только с переданным пользователем (по его
telegram_id): никаких общих данных между вызовами для разных пользователей нет."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from database.models import User
from database.repositories import UserEventRepository, UserRepository, WeeklyScheduleRepository
from services.events import EventLog, NullEventLog
from services.names import pick_address
from services.event_schedule import next_occurrence, rule_from_event
from services.summary_generator import NextEvent, SummaryContext, SummaryGenerator
from services.time_service import now_in
from services.weather_models import WeatherReport, WeatherUnavailableError

if TYPE_CHECKING:  # aiohttp тянется только при реальной работе, не при импорте логики
    from services.weather import WeatherService

logger = logging.getLogger(__name__)

SLOW_WEATHER_SECONDS = 6.0  # дольше — считаем задержкой и пишем в журнал админа


@dataclass(frozen=True, slots=True)
class SummaryResult:
    text: str
    weather_ok: bool


class SummaryService:
    def __init__(
        self, schedule: WeeklyScheduleRepository, users: UserRepository,
        weather: "WeatherService", generator: SummaryGenerator, rng: random.Random | None = None,
        events: EventLog | None = None, user_events: UserEventRepository | None = None,
    ) -> None:
        self._schedule = schedule
        self._users = users
        self._weather = weather
        self._generator = generator
        self._rng = rng or random.Random()
        self._events = events or NullEventLog()
        self._user_events = user_events

    async def build(self, user: User, now: datetime | None = None) -> SummaryResult:
        local_now = now if now is not None else now_in(user.timezone)
        week = await self._schedule.get_week(user.telegram_id)

        report: WeatherReport | None
        started = time.monotonic()
        try:
            report = await self._weather.get_report(user.city, user.latitude, user.longitude, user.timezone)
        except WeatherUnavailableError as exc:
            logger.error("Погода недоступна для пользователя %s: %s", user.telegram_id, exc)
            report = None  # честно скажем об этом в сообщении, а не выдумаем прогноз
            await self._events.log(
                "weather", f"Open-Meteo недоступен: {str(exc)[:120]}", level="warning",
                throttle_key="weather-down",
            )
        elapsed = time.monotonic() - started
        if report is not None and elapsed > SLOW_WEATHER_SECONDS:
            await self._events.log(
                "delay", f"Open-Meteo отвечает медленно: {elapsed:.1f} с", level="warning",
                throttle_key="weather-slow",
            )

        address = pick_address(user, self._rng)
        if address != user.last_used_name:
            await self._users.set_last_used_name(user.telegram_id, address)

        next_event = await self._next_event(user, local_now)
        text = self._generator.generate(
            SummaryContext(user=user, schedule=week, now=local_now, report=report, address=address,
                           next_event=next_event)
        )
        return SummaryResult(text=text, weather_ok=report is not None)

    async def _next_event(self, user: User, local_now: datetime) -> NextEvent | None:
        """Ближайшая по времени задача пользователя на сегодня (начало не раньше «сейчас» и в тот же местный день).
        Несколько задач — берётся самая ранняя; нет задач — None (блок в сводке не выводится). Только свои события."""
        if self._user_events is None:
            return None
        best: NextEvent | None = None
        try:
            events = await self._user_events.list_for_user(user.telegram_id)
        except Exception:  # noqa: BLE001 — задачи не должны ломать сводку
            logger.exception("Не удалось загрузить задачи пользователя %s", user.telegram_id)
            return None
        now_ts = local_now.timestamp()
        for event in events:
            if event.status != "active":
                continue
            try:
                start = next_occurrence(rule_from_event(event), now_ts)
            except Exception:  # noqa: BLE001 — битые данные одного события пропускаем
                continue
            if start is None:
                continue
            start_local = start.astimezone(local_now.tzinfo)
            if start_local.date() != local_now.date():
                continue
            if best is None or start_local < best.start:
                best = NextEvent(event.title, start_local)
        return best
