"""Кнопка «Сводка» (и команда /summary), «Обновить» и вход из главного меню (inline).

Сводка — это один экран: «Обновить» редактирует то же сообщение, повторное нажатие меню не плодит сообщений."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.main_menu import BTN_SUMMARY, summary_keyboard
from bot.ui import ui
from database.models import User
from services.summary_service import SummaryService
from utils.helpers import InFlightGuard

logger = logging.getLogger(__name__)

router = Router(name="summary")
router.message.filter(F.chat.type == ChatType.PRIVATE)

ACTION = "summary"


async def show_summary(event: Message | CallbackQuery, user: User, summaries: SummaryService, guard: InFlightGuard,
                       *, fresh: bool = False) -> None:
    """Собирает и показывает сводку. Двойной тап не запускает вторую сборку."""
    if not guard.try_acquire(user.telegram_id, ACTION):
        if isinstance(event, CallbackQuery):
            await event.answer("Уже готовлю…")
        else:
            await ui.delete(event.bot, event.chat.id, event.message_id)   # лишнее нажатие меню убираем из чата
        return
    try:
        if isinstance(event, CallbackQuery):
            await event.answer()
        else:
            await event.bot.send_chat_action(event.chat.id, ChatAction.TYPING)
        result = await summaries.build(user)
        await ui.show(event, result.text, summary_keyboard(), fresh=fresh)
    finally:
        guard.release(user.telegram_id, ACTION)


@router.message(F.text == BTN_SUMMARY)
@router.message(Command("summary"))
async def on_summary(message: Message, user: User, summaries: SummaryService, guard: InFlightGuard,
                     state: FSMContext) -> None:
    await state.clear()  # нажатие кнопки меню завершает любой незаконченный диалог
    await show_summary(message, user, summaries, guard, fresh=True)


@router.callback_query(F.data == "summary:refresh")
async def on_refresh(callback: CallbackQuery, user: User, summaries: SummaryService, guard: InFlightGuard) -> None:
    await show_summary(callback, user, summaries, guard)
