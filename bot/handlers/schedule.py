"""📅 Расписание. ЭТАП 2: показывает реальные сохранённые уведомления (только чтение).
Добавление, удаление и изменение времени — этап 4 вместе с планировщиком."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.keyboards.main_menu import BTN_SCHEDULE
from database.models import User
from database.repositories import ScheduleRepository
from services.time_service import describe_weekdays

router = Router(name="schedule")
router.message.filter(F.chat.type == ChatType.PRIVATE)


@router.message(F.text == BTN_SCHEDULE)
@router.message(Command("schedule"))
async def show_schedule(message: Message, user: User, schedules: ScheduleRepository) -> None:
    entries = await schedules.list_for_user(user.telegram_id)
    lines = ["📅 <b>Расписание сводок</b>\n"]
    lines.append(f"🔔 Автоматическая отправка: <b>{'включена ✅' if user.notifications_enabled else 'выключена ⏸'}</b>")
    if entries:
        lines.append(f"Запланировано уведомлений: <b>{len(entries)}</b>\n")
        for e in entries:
            mark = "✅" if e.enabled else "⏸"
            lines.append(f"{mark} <b>{e.send_time}</b> — {describe_weekdays(e.weekdays)}")
    else:
        lines.append("Уведомлений пока нет.")
    lines.append(
        "\n<i>Время — по твоему часовому поясу. Автоотправка и управление временем "
        "появятся на следующих этапах разработки.</i>"
    )
    await message.answer("\n".join(lines))
