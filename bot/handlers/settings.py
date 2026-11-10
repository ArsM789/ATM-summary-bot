"""⚙️ Настройки. ЭТАП 2: показывает реальные сохранённые настройки (только чтение).
Редактирование через inline-кнопки добавляется на этапе 3."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.keyboards.main_menu import BTN_SETTINGS
from database.models import User
from database.repositories import LessonsRepository
from services.time_service import WEEKDAYS_SHORT
from utils.formatting import esc

router = Router(name="settings")
router.message.filter(F.chat.type == ChatType.PRIVATE)


def format_week(week: dict[int, int]) -> str:
    return " · ".join(
        f"{WEEKDAYS_SHORT[d]} <b>{week[d]}</b>" if week.get(d) else f"{WEEKDAYS_SHORT[d]} —" for d in range(7)
    )


@router.message(F.text == BTN_SETTINGS)
@router.message(Command("settings"))
async def show_settings(message: Message, user: User, lessons: LessonsRepository) -> None:
    week = await lessons.get_week(user.telegram_id)
    status = "включены ✅" if user.notifications_enabled else "выключены ⏸"
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"👤 Имя: <b>{esc(user.name)}</b>\n"
        f"🏙 Город: <b>{esc(user.city)}</b>\n"
        f"🕒 Часовой пояс: <b>{esc(user.timezone)}</b>\n"
        f"🔔 Автоматические сводки: <b>{status}</b>\n"
        f"📚 Уроки: {format_week(week)}\n\n"
        "<i>Изменение этих параметров прямо в боте появится на следующем этапе разработки.</i>"
    )
