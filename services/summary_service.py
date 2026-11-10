"""Склейка: пользователь + расписание уроков + погода -> готовое сообщение.

Этим сервисом пользуются и кнопка «Сводка», и (на этапе 4) планировщик — так
ручные и автоматические сводки формируются одним и тем же кодом.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from database.models import User
from database.repositories import LessonsRepository
from services.summary_generator import SummaryContext, SummaryGenerator
from services.time_service import now_in
from services.weather import WeatherService
from services.weather_models import WeatherReport, WeatherUnavailableError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SummaryResult:
    text: str
    weather_ok: bool


class SummaryService:
    def __init__(self, lessons: LessonsRepository, weather: WeatherService, generator: SummaryGenerator) -> None:
        self._lessons = lessons
        self._weather = weather
        self._generator = generator

    async def build(self, user: User, now: datetime | None = None) -> SummaryResult:
        local_now = now if now is not None else now_in(user.timezone)
        lessons = await self._lessons.get_week(user.telegram_id)

        report: WeatherReport | None
        try:
            report = await self._weather.get_report(user.city, user.latitude, user.longitude, user.timezone)
        except WeatherUnavailableError as exc:
            logger.error("Погода недоступна для пользователя %s: %s", user.telegram_id, exc)
            report = None  # честно скажем об этом в сообщении, а не выдумаем прогноз

        text = self._generator.generate(SummaryContext(user=user, lessons=lessons, now=local_now, report=report))
        return SummaryResult(text=text, weather_ok=report is not None)
