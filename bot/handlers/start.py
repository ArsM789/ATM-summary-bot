"""/start, /menu, /help для пользователей с подтверждённым именем (остальных ведёт onboarding).

/start присылает приветствие с постоянной клавиатурой-меню (так у существующих пользователей появляются новые
кнопки); /menu открывает главное меню кнопками на месте; /help запускает тур по возможностям."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.helpers import reply
from bot.keyboards.main_menu import main_menu
from bot.keyboards.nav import hub_keyboard, tour_keyboard
from bot.texts import DEVELOPMENT_SHORT, TOUR_PAGES
from config import Settings
from database.models import User
from utils.formatting import esc

router = Router(name="start")
router.message.filter(F.chat.type == ChatType.PRIVATE)


def hub_text(user: User) -> str:
    return (
        "🏠 <b>Главное меню</b>\n\n"
        "Выбери раздел или просто напиши, что нужно — например «что надеть» или "
        "«добавь урок английского завтра в 16:00».\n\n"
        f"<i>Город — {esc(user.city)}, часовой пояс — {esc(user.timezone)}.</i>"
    )


def welcome_text(user: User, settings: Settings) -> str:
    return (
        f"<b>{esc(settings.bot_name)}</b> 🌅\n\n"
        f"Привет, {esc(user.name)}! Утром я присылаю сводку на сегодня, а в 18:00 — на завтра: "
        f"погода, что надеть, уроки и ближайшая задача.\n\n"
        f"Меню — кнопки внизу. Напиши «помощь», чтобы пройти знакомство с возможностями ещё раз.\n\n"
        f"{DEVELOPMENT_SHORT}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, user: User, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    await reply(message, welcome_text(user, settings), reply_markup=main_menu(), fresh=True)


@router.message(Command("menu"))
async def cmd_menu(message: Message, user: User, state: FSMContext) -> None:
    await state.clear()
    await reply(message, hub_text(user), reply_markup=hub_keyboard(), fresh=True)


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await reply(message, TOUR_PAGES[0], reply_markup=tour_keyboard(0), fresh=True)
