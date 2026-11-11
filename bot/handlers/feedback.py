"""💬 Сообщить об ошибке / предложить улучшение: текст пользователя -> SQLite -> личное сообщение админу."""
from __future__ import annotations

import logging
import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.filters import TEXT_INPUT
from bot.helpers import edit_or_reply
from bot.keyboards.common import button, markup
from bot.keyboards.main_menu import BTN_FEEDBACK
from bot.states.settings_states import FeedbackInput
from config import Settings
from database.models import User
from database.repositories import FeedbackRepository
from services.feedback_format import format_feedback_for_admin
from services.telegram_send import SendStatus, send_message_safe
from services.time_service import get_zone

logger = logging.getLogger(__name__)

router = Router(name="feedback")
router.message.filter(F.chat.type == ChatType.PRIVATE)

MAX_FEEDBACK_LEN = 3000
MAX_PER_HOUR = 5


@router.message(F.text == BTN_FEEDBACK)
@router.message(Command("feedback"))
async def open_feedback(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FeedbackInput.text)
    await message.answer(
        "💬 Напиши одним сообщением, что пошло не так или что стоит улучшить — ошибку, идею или рекомендацию. "
        "Я передам это автору бота.",
        reply_markup=markup([button("Отмена", "fb:cancel")]),
    )


@router.callback_query(StateFilter(FeedbackInput.text), F.data == "fb:cancel")
async def cancel_feedback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await edit_or_reply(callback, "Хорошо, ничего не отправляю.")


@router.message(StateFilter(FeedbackInput.text), TEXT_INPUT)
async def got_feedback(message: Message, user: User, state: FSMContext, feedback: FeedbackRepository,
                       settings: Settings, bot: Bot) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Сообщение пустое — напиши, пожалуйста, хотя бы пару слов.")
        return
    if len(text) > MAX_FEEDBACK_LEN:
        await message.answer(f"Слишком длинно ({len(text)} символов). Уложись, пожалуйста, в {MAX_FEEDBACK_LEN}.")
        return
    if await feedback.count_since(user.telegram_id, int(time.time()) - 3600) >= MAX_PER_HOUR:
        await state.clear()
        await message.answer("Ты уже отправил несколько обращений за последний час. Попробуй чуть позже 🙏")
        return

    feedback_id = await feedback.create(user.telegram_id, text)  # сохраняем до отправки: обращение не потеряется
    await state.clear()

    if settings.admin_id is None:
        await feedback.set_status(feedback_id, "delivery_failed")
        logger.error("Обращение %s сохранено, но ADMIN_ID не задан — отправить некому", feedback_id)
        await message.answer("Сейчас не могу передать сообщение автору (канал связи не настроен). Оно сохранено.")
        return

    try:
        when = datetime.now(get_zone(settings.default_timezone))
    except ValueError:
        when = datetime.now()
    result = await send_message_safe(bot, settings.admin_id, format_feedback_for_admin(user, text, when))
    if result.status is SendStatus.SENT:
        await feedback.set_status(feedback_id, "delivered")
        await message.answer("Спасибо! Сообщение передано администратору 🙌")
    else:
        await feedback.set_status(feedback_id, "delivery_failed")
        logger.error("Не удалось доставить обращение %s администратору: %s", feedback_id, result.error)
        await message.answer("Не получилось передать сообщение автору. Оно сохранено — попробуй ещё раз чуть позже.")


@router.message(StateFilter(FeedbackInput.text))
async def feedback_not_text(message: Message) -> None:
    await message.answer("Пока я принимаю только текст. Напиши обращение словами.")
