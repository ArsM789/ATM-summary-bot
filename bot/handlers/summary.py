"""Кнопка «Сводка» (и команда /summary), плюс «Обновить» под сообщением."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.keyboards.main_menu import BTN_SUMMARY, summary_keyboard
from database.models import User
from services.summary_service import SummaryService
from utils.helpers import InFlightGuard

logger = logging.getLogger(__name__)

router = Router(name="summary")
router.message.filter(F.chat.type == ChatType.PRIVATE)

ACTION = "summary"


@router.message(F.text == BTN_SUMMARY)
@router.message(Command("summary"))
async def on_summary(message: Message, user: User, summaries: SummaryService, guard: InFlightGuard, bot: Bot) -> None:
    if not guard.try_acquire(user.telegram_id, ACTION):
        return  # сводка уже готовится — повторный тап игнорируем
    try:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        result = await summaries.build(user)
        await message.answer(result.text, reply_markup=summary_keyboard())
    finally:
        guard.release(user.telegram_id, ACTION)


@router.callback_query(F.data == "summary:refresh")
async def on_refresh(callback: CallbackQuery, user: User, summaries: SummaryService, guard: InFlightGuard) -> None:
    if not guard.try_acquire(user.telegram_id, ACTION):
        await callback.answer("Уже обновляю…")
        return
    try:
        await callback.answer()
        result = await summaries.build(user)
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(result.text, reply_markup=summary_keyboard())
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc):
                    raise
        else:  # сообщение слишком старое и недоступно для редактирования
            await callback.bot.send_message(callback.from_user.id, result.text, reply_markup=summary_keyboard())
    finally:
        guard.release(user.telegram_id, ACTION)
