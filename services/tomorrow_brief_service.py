from __future__ import annotations

import logging
from random import random

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from database.models import User
from database.repositories import UserRepository, WeeklyScheduleRepository
from services.names import pick_address
from services.tomorrow_brief_generator import TomorrowBriefGenerator
from services.weather_models import DayForecast, WeatherUnavailableError

if TYPE_CHECKING:
    from services.weather import WeatherService


logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class TomorrowBriefResult:
    text: str
    weather_ok: bool


class TommorowBriefService:
    def __init__(
            self,
            schedule: WeeklyScheduleRepository,
            users: UserRepository,
            weather: "WeatherService",
            generator: TomorrowBriefGenerator,
            rng: random.Random | None = None,
    ) -> None:
        self._schedule = schedule
        self._users = users
        self._weather = weather
        self._generator = generator
        self._rng = rng or random.Random()

    async def build(
            self,
            user: User,
            now: datetime | None = None,
    ) -> TomorrowBriefResult:

        if now is None:
            from services.time_service import now_in
            now = now_in(user.timezone)

        target_date = now.date() + timedelta(days=1)


        lessons = await self._schedule.get_day(
            user.telegram_id,
            target_date.weekday(),
        )

        forecast: DayForecast | None = None

        try:
            report = await self._weather.get_report(
                user.city,
                user.latitude,
                user.longitude,
                user.timezone,
            )
            forecast = report.day(target_date)

        except WeatherUnavailableError as exc:
            logger.warning(
                "Не удалось получить погоду для вечерней сводки пользователя %s: %s",
                user.telegram_id,
                exc
            )


        address = pick_address(user, self._rng)

        if address != user.last_used_name:
            await self._users.set_last_used_name(
                user.telegram_id,
                address,
            )

        text = self._generator.generate(
            user=user,
            target_date=target_date,
            forecast=forecast,
            lessons=lessons,
            address=address,
        )

        return TomorrowBriefResult(
            text=text,
            weather_ok=forecast is not None,
        )