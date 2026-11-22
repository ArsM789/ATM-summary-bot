"""💬 Сообщить об ошибке / предложить улучшение: текст пользователя -> SQLite -> личное сообщение админу.
Сценарий живёт в одном экране: ввод пользователя удаляется, ошибки показываются в самом экране, итог — тоже."""
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
from bot.helpers import reply
from bot.keyboards.common import button, markup
from bot.keyboards.main_menu import BTN_FEEDBACK
from bot.keyboards.nav import home_keyboard
from bot.states.settings_states import FeedbackInput
from config import Settings
from database.models import User
from database.repositories import FeedbackRepository
from services.events import EventLog
from services.feedback_format import format_feedback_for_admin
from services.telegram_send import SendStatus, send_message_safe
from services.time_service import get_zone

logger = logging.getLogger(__name__)

router = Router(name="feedback")
router.message.filter(F.chat.type == ChatType.PRIVATE)

MAX_FEEDBACK_LEN = 3000
MAX_PER_HOUR = 5

PROMPT = (
    "💬 Напиши одним сообщением, что пошло не так или что стоит улучшить — ошибку, идею или рекомендацию. "
    "Я передам это автору бота."
)


def _cancel_keyboard():
    return markup([button("❌ Отмена", "fb:cancel")])


async def begin_feedback(event: Message | CallbackQuery, state: FSMContext, *, fresh: bool = False) -> None:
    """Экран ввода обращения (из меню, команды /feedback или inline-хаба)."""
    await state.clear()
    await state.set_state(FeedbackInput.text)
    await reply(event, PROMPT, reply_markup=_cancel_keyboard(), fresh=fresh)


@router.message(F.text == BTN_FEEDBACK)
@router.message(Command("feedback"))
async def open_feedback(message: Message, state: FSMContext) -> None:
    await begin_feedback(message, state, fresh=True)


@router.callback_query(StateFilter(FeedbackInput.text), F.data == "fb:cancel")
async def cancel_feedback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await reply(callback, "Хорошо, ничего не отправляю.", reply_markup=home_keyboard())


@router.message(StateFilter(FeedbackInput.text), TEXT_INPUT)
async def got_feedback(message: Message, user: User, state: FSMContext, feedback: FeedbackRepository,
                       settings: Settings, bot: Bot, events: EventLog) -> None:
    text = (message.text or "").strip()
    if not text:
        await reply(message, "⚠️ Сообщение пустое — напиши хотя бы пару слов.\n\n" + PROMPT, reply_markup=_cancel_keyboard())
        return
    if len(text) > MAX_FEEDBACK_LEN:
        await reply(message, f"⚠️ Слишком длинно ({len(text)} символов). Уложись, пожалуйста, в {MAX_FEEDBACK_LEN}.\n\n" + PROMPT,
                    reply_markup=_cancel_keyboard())
        return
    if await feedback.count_since(user.telegram_id, int(time.time()) - 3600) >= MAX_PER_HOUR:
        await state.clear()
        await reply(message, "Ты уже отправил несколько обращений за последний час. Попробуй чуть позже 🙏",
                    reply_markup=home_keyboard())
        return

    feedback_id = await feedback.create(user.telegram_id, text)  # сохраняем до отправки: обращение не потеряется
    await state.clear()
    await events.log("feedback", f"Новое обращение #{feedback_id} от {user.name}", user_id=user.telegram_id)

    if settings.admin_id is None:
        await feedback.set_status(feedback_id, "delivery_failed")
        logger.error("Обращение %s сохранено, но ADMIN_ID не задан — отправить некому", feedback_id)
        await events.log("delivery", f"Обращение #{feedback_id} не доставлено: ADMIN_ID не задан", level="error",
                         throttle_key="feedback-no-admin")
        await reply(message, "Сейчас не могу передать сообщение автору (канал связи не настроен). Оно сохранено.",
                    reply_markup=home_keyboard())
        return

    try:
        when = datetime.now(get_zone(settings.default_timezone))
    except ValueError:
        when = datetime.now()
    result = await send_message_safe(bot, settings.admin_id, format_feedback_for_admin(user, text, when))
    if result.status is SendStatus.SENT:
        await feedback.set_status(feedback_id, "delivered")
        await reply(message, "🙌 Спасибо! Сообщение передано администратору.", reply_markup=home_keyboard())
    else:
        await feedback.set_status(feedback_id, "delivery_failed")
        logger.error("Не удалось доставить обращение %s администратору: %s", feedback_id, result.error)
        await events.log("delivery", f"Обращение #{feedback_id} не доставлено админу: {str(result.error)[:120]}",
                         level="error", user_id=user.telegram_id)
        await reply(message, "Не получилось передать сообщение автору. Оно сохранено — попробуй ещё раз чуть позже.",
                    reply_markup=home_keyboard())


@router.message(StateFilter(FeedbackInput.text), ~F.text)
async def feedback_not_text(message: Message) -> None:
    await reply(message, "⚠️ Пока я принимаю только текст. Напиши обращение словами.\n\n" + PROMPT,
                reply_markup=_cancel_keyboard())
