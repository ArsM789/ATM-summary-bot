"""Точка входа ATM Morning Assistant."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand

from bot.handlers import errors, schedule, settings as settings_handlers, start, summary
from bot.middlewares import UserMiddleware
from config import BASE_DIR, ConfigError, load_settings
from database.database import Database
from database.repositories import LessonsRepository, ScheduleRepository, UserRepository
from services.summary_generator import SummaryGenerator
from services.summary_service import SummaryService
from services.weather import WeatherService
from utils.formatting import EmojiPack
from utils.helpers import InFlightGuard, setup_logging

logger = logging.getLogger("atm")

COMMANDS = [
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="summary", description="Получить сводку сейчас"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="schedule", description="Расписание автосводок"),
    BotCommand(command="help", description="Помощь и меню"),
]


async def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 1

    setup_logging(settings.log_level, BASE_DIR / "logs")

    db = Database(settings.database_path)
    await db.connect()
    weather = WeatherService(
        timeout=settings.weather_timeout, retries=settings.weather_retries, cache_ttl=settings.weather_cache_ttl
    )
    await weather.start()

    users = UserRepository(db, settings)
    lessons = LessonsRepository(db, settings)
    schedules = ScheduleRepository(db)
    generator = SummaryGenerator(EmojiPack(settings.custom_emoji))
    summaries = SummaryService(lessons, weather, generator)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher()
    dp.workflow_data.update(
        settings=settings, users=users, lessons=lessons, schedules=schedules,
        summaries=summaries, guard=InFlightGuard(),
    )
    middleware = UserMiddleware(users)
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)

    dp.include_router(errors.router)
    dp.include_router(start.router)
    dp.include_router(summary.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(schedule.router)

    exit_code = 0
    try:
        me = await bot.get_me()
        logger.info("Бот запущен: @%s", me.username)
        await bot.set_my_commands(COMMANDS)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил BOT_TOKEN. Проверьте токен в .env (получить новый: @BotFather → /token).")
        exit_code = 1
    except TelegramNetworkError as exc:
        logger.error("Нет связи с Telegram: %s", exc)
        exit_code = 1
    finally:
        logger.info("Останавливаюсь: закрываю соединения…")
        await weather.close()
        await db.close()
        await bot.session.close()
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
