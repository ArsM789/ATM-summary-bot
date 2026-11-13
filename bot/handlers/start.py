"""/start, /help, /menu для пользователей с подтверждённым именем (остальных ведёт onboarding)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards.main_menu import main_menu
from config import Settings
from database.models import User
from utils.formatting import (esc)

DEVELOPMENT_NOTICE = (
    "📢 <b>Важное объявление</b>\n\n"
    "Дорогие пользователи Deylo!\n\n"
    "Сейчас Deylo находится на этапе разработки, и пока у нас нет собственного сервера.\n\n"
    "Я днями и ночами стараюсь улучшать бота и добавлять новые функции. Однако, поскольку сейчас он запущен локально, иногда бот может временно не работать.\n\n"
    "Утренние сводки будут отправляться в обычном режиме, пока бот запущен и работает.\n\n"
    "Сейчас, когда я только экспериментирую с функционалом, каждый пользователь — на вес золота. Поэтому, пожалуйста, не удаляйте бота, если он временно перестал отвечать.\n\n"
    "Совсем скоро у нас будет собственный сервер, и Deylo сможет работать 24/7.\n\n"
    "Все новости об обновлениях и улучшениях в телеграм канале: https://t.me/this_atm\n"
    "Спасибо большое, что поддерживаете моё начинание! ❤️"
)

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


def _welcome(user: User, settings: Settings) -> str:
    return (
        f"<b>{esc(settings.bot_name)}</b> ☀️\n\n"
        f"Привет, {esc(user.name)}! Я присылаю короткую сводку дня: погоду, что надеть и расписание уроков.\n\n"
        f"🌤 <b>Сводка</b> — получить прямо сейчас\n"
        f"📅 <b>Моё расписание</b> — уроки по дням недели\n"
        f"⚙️ <b>Настройки</b> — имя, город, часовой пояс, время автосводок\n"
        f"💬 <b>Сообщить об ошибке</b> — написать автору бота\n\n"
        f"<i>Город — {esc(user.city)}, часовой пояс — {esc(user.timezone)}.</i>"
    )


@router.message(CommandStart())
@router.message(Command("menu", "help"))
async def cmd_start(message: Message, user: User, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await message.answer(DEVELOPMENT_NOTICE)
    await message.answer(_welcome(user, settings), reply_markup=main_menu())
