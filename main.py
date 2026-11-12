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

from bot.handlers import (
    admin, errors, fallback, feedback, inline_mode, lessons, onboarding, schedule,
    settings as settings_handlers, start, summary,
)
from bot.keyboards.main_menu import summary_keyboard
from bot.middlewares import UserMiddleware
from config import BASE_DIR, ConfigError, load_settings
from database.database import Database
from database.repositories import (
    BroadcastRepository, EventLogRepository, FeedbackRepository, NotificationLogRepository,
    ScheduleRepository, UserRepository, WeeklyScheduleRepository,
)
from services.broadcast import BroadcastService
from services.delivery import SummaryDispatcher
from services.events import EventLog
from services.scheduler import SummaryScheduler
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
    BotCommand(command="lessons", description="Моё расписание уроков"),
    BotCommand(command="schedule", description="Время автосводок"),
    BotCommand(command="feedback", description="Сообщить об ошибке / идея"),
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

    event_repo = EventLogRepository(db)
    events = EventLog(event_repo)

    async def on_user_created(new_user) -> None:
        handle = f", @{new_user.username}" if new_user.username else ""
        await events.log("user_new", f"Новый пользователь: {new_user.name}{handle}", user_id=new_user.telegram_id)

    users = UserRepository(db, settings, on_created=on_user_created)
    weekly = WeeklyScheduleRepository(db)
    schedules = ScheduleRepository(db)
    notification_log = NotificationLogRepository(db)
    feedback_repo = FeedbackRepository(db)
    broadcast_repo = BroadcastRepository(db)
    generator = SummaryGenerator(EmojiPack(settings.custom_emoji))
    summaries = SummaryService(weekly, users, weather, generator, events=events)

    interrupted = await broadcast_repo.mark_interrupted()  # рассылки, оборванные прошлым перезапуском
    if interrupted:
        logger.warning("Помечено прерванных рассылок: %d", interrupted)
        await events.log("broadcast", f"После перезапуска помечено прерванных рассылок: {interrupted}", level="warning")
    if settings.admin_id is None:
        logger.warning("ADMIN_ID не задан: админ-панель и пересылка обращений отключены")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    broadcast = BroadcastService(bot, users, broadcast_repo, settings.broadcast_rate, events=events)
    dispatcher = SummaryDispatcher(
        bot, users, schedules, notification_log, summaries, reply_markup=summary_keyboard(), events=events
    )
    scheduler = SummaryScheduler(dispatcher, events=events)

    dp = Dispatcher()  # FSM: MemoryStorage, ключ (бот, чат, пользователь) — состояния изолированы
    dp.workflow_data.update(
        settings=settings, users=users, weekly=weekly, schedules=schedules, feedback=feedback_repo,
        weather=weather, summaries=summaries, broadcast=broadcast, guard=InFlightGuard(),
        events=events, event_repo=event_repo,
    )
    middleware = UserMiddleware(users)
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)

    # Порядок важен: сначала знакомство и меню, диалоги — внутри своих роутеров, заглушки — в самом конце.
    dp.include_router(errors.router)
    dp.include_router(onboarding.router)
    dp.include_router(start.router)
    dp.include_router(summary.router)
    dp.include_router(lessons.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(schedule.router)
    dp.include_router(feedback.router)
    dp.include_router(admin.router)
    dp.include_router(inline_mode.router)
    dp.include_router(fallback.router)

    exit_code = 0
    try:
        me = await bot.get_me()
        logger.info("Бот запущен: @%s", me.username)
        await events.log("system", f"Бот запущен (@{me.username})")
        await bot.set_my_commands(COMMANDS)
        scheduler.start()
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил BOT_TOKEN. Проверьте токен в .env (получить новый: @BotFather → /token).")
        exit_code = 1
    except TelegramNetworkError as exc:
        logger.error("Нет связи с Telegram: %s", exc)
        exit_code = 1
    finally:
        logger.info("Останавливаюсь: закрываю соединения…")
        await events.log("system", "Бот остановлен", throttle_key="stop", throttle_seconds=0)
        scheduler.shutdown()
        await weather.close()
        await db.close()
        await bot.session.close()
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
