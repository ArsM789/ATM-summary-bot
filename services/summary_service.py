"""Склейка: пользователь + его расписание + погода + вариант обращения -> готовое сообщение.

Этим сервисом пользуются и кнопка «Сводка», и планировщик — ручные и автоматические сводки
формируются одним кодом. Метод build() работает только с переданным пользователем (по его
telegram_id): никаких общих данных между вызовами для разных пользователей нет."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from database.models import User
from database.repositories import UserRepository, WeeklyScheduleRepository
from services.names import pick_address
from services.summary_generator import SummaryContext, SummaryGenerator
from services.time_service import now_in
from services.weather_models import WeatherReport, WeatherUnavailableError

if TYPE_CHECKING:  # aiohttp тянется только при реальной работе, не при импорте логики
    from services.weather import WeatherService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SummaryResult:
    text: str
    weather_ok: bool


class SummaryService:
    def __init__(
        self, schedule: WeeklyScheduleRepository, users: UserRepository,
        weather: "WeatherService", generator: SummaryGenerator, rng: random.Random | None = None,
    ) -> None:
        self._schedule = schedule
        self._users = users
        self._weather = weather
        self._generator = generator
        self._rng = rng or random.Random()

    async def build(self, user: User, now: datetime | None = None) -> SummaryResult:
        local_now = now if now is not None else now_in(user.timezone)
        week = await self._schedule.get_week(user.telegram_id)

        report: WeatherReport | None
        try:
            report = await self._weather.get_report(user.city, user.latitude, user.longitude, user.timezone)
        except WeatherUnavailableError as exc:
            logger.error("Погода недоступна для пользователя %s: %s", user.telegram_id, exc)
            report = None  # честно скажем об этом в сообщении, а не выдумаем прогноз

        address = pick_address(user, self._rng)
        if address != user.last_used_name:
            await self._users.set_last_used_name(user.telegram_id, address)

        text = self._generator.generate(
            SummaryContext(user=user, schedule=week, now=local_now, report=report, address=address)
        )
        return SummaryResult(text=text, weather_ok=report is not None)
