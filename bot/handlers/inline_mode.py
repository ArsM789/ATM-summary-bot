"""Inline Mode: «@бот погода завтра» -> прогноз на завтра.

Используется тот же WeatherService (общий кэш), что и для сводок. Город и часовой пояс берутся из
настроек пользователя Telegram, если он уже пользовался ботом; иначе — значения по умолчанию из .env."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent

from config import Settings
from database.repositories import UserRepository
from services.events import EventLog
from services.time_service import is_valid_timezone, now_in
from services.tomorrow_weather import build_tomorrow, matches_query
from services.weather import WeatherService
from services.weather_models import WeatherUnavailableError

logger = logging.getLogger(__name__)

router = Router(name="inline_mode")

INLINE_TIMEOUT = 6.0  # Telegram ждёт ответ на inline-запрос недолго


def _article(result_id: str, title: str, description: str, html: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text=html, parse_mode=ParseMode.HTML),
    )


@router.inline_query()
async def on_inline_query(query: InlineQuery, users: UserRepository, weather: WeatherService,
                          settings: Settings, events: EventLog) -> None:
    if not matches_query(query.query):
        await _answer(query, [], cache_time=1)
        return

    # Настройки конкретного пользователя (если он есть в БД), иначе — по умолчанию из конфигурации.
    user = await users.get(query.from_user.id)
    if user is not None and is_valid_timezone(user.timezone):
        city, lat, lon, tz = user.city, user.latitude, user.longitude, user.timezone
    else:
        city, lat, lon, tz = (
            settings.default_city, settings.default_latitude, settings.default_longitude, settings.default_timezone,
        )

    error_text = "⚠️ Не удалось получить прогноз погоды. Попробуйте позже."
    try:
        report = await asyncio.wait_for(weather.get_report(city, lat, lon, tz), timeout=INLINE_TIMEOUT)
        forecast = build_tomorrow(report, now_in(tz))
    except (WeatherUnavailableError, asyncio.TimeoutError) as exc:
        logger.warning("Inline: погода недоступна (%s)", exc)
        await events.log("weather", f"Inline-запрос: прогноз недоступен ({type(exc).__name__})", level="warning",
                         throttle_key="inline-weather", throttle_seconds=300)
        forecast = None

    if forecast is None:
        article = _article("tomorrow-error", "Погода недоступна", "Не удалось получить прогноз — попробуйте позже", error_text)
        await _answer(query, [article], cache_time=1)
        return
    article = _article("tomorrow", forecast.title, forecast.description, forecast.html)
    await _answer(query, [article], cache_time=300)


async def _answer(query: InlineQuery, results: list[InlineQueryResultArticle], cache_time: int) -> None:
    try:
        # is_personal: ответ зависит от города/пояса конкретного пользователя — не кэшировать для всех.
        await query.answer(results=results, cache_time=cache_time, is_personal=True)
    except TelegramAPIError as exc:  # например, «query is too old»
        logger.warning("Не удалось ответить на inline-запрос: %s", exc)
