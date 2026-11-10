"""/start, /help, /menu."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.keyboards.main_menu import main_menu
from config import Settings
from database.models import User
from utils.formatting import esc

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


def _welcome(user: User, settings: Settings) -> str:
    return (
        f"<b>{esc(settings.bot_name)}</b> 🌅\n\n"
        f"Привет, {esc(user.name)}! Я буду присылать тебе короткую сводку дня: "
        f"погоду, что надеть и сколько у тебя уроков.\n\n"
        f"🌤 <b>Сводка</b> — получить прямо сейчас\n"
        f"⚙️ <b>Настройки</b> — имя, город, часовой пояс, уроки\n"
        f"📅 <b>Расписание</b> — когда присылать сводки автоматически\n\n"
        f"<i>Сейчас город — {esc(user.city)}, часовой пояс — {esc(user.timezone)}.</i>"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, user: User, settings: Settings) -> None:
    await message.answer(_welcome(user, settings), reply_markup=main_menu())


@router.message(Command("menu", "help"))
async def cmd_menu(message: Message, user: User, settings: Settings) -> None:
    await message.answer(_welcome(user, settings), reply_markup=main_menu())
