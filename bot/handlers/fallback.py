"""Страховочные обработчики и естественный ввод. Подключаются последними.

- устаревшая кнопка: короткое уведомление и снятие кнопок с сообщения (чтобы больше не нажимались);
- обычный текст вне сценария: понимаем намерение («что надеть», «расписание», «добавь событие …»)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.events import begin_quick_event, show_events_menu
from bot.handlers.feedback import begin_feedback
from bot.handlers.lessons import show_overview
from bot.handlers.settings import show_settings
from bot.handlers.start import hub_text
from bot.handlers.summary import show_summary
from bot.helpers import reply
from bot.keyboards.nav import hub_keyboard, tour_keyboard
from bot.texts import TOUR_PAGES
from bot.ui import ui
from config import Settings
from database.models import User
from database.repositories import ScheduleRepository, UserEventRepository, WeeklyScheduleRepository
from services.intents import Intent, detect_intent
from services.summary_service import SummaryService
from utils.helpers import InFlightGuard

router = Router(name="fallback")


@router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    """Кнопка устарела (сценарий завершён, бот перезапускался) или недоступна этому пользователю."""
    await callback.answer("Эта кнопка уже неактуальна — открой нужный раздел заново.", show_alert=False)
    await ui.strip_keyboard(callback)


@router.message(F.chat.type == ChatType.PRIVATE, F.text, StateFilter(None))
async def natural_text(message: Message, user: User, state: FSMContext, settings: Settings,
                       summaries: SummaryService, guard: InFlightGuard, weekly: WeeklyScheduleRepository,
                       schedules: ScheduleRepository, user_events: UserEventRepository) -> None:
    intent = detect_intent(message.text or "")
    if intent is Intent.SUMMARY:
        await show_summary(message, user, summaries, guard, fresh=True)
    elif intent is Intent.LESSONS:
        await show_overview(message, user, weekly, fresh=True)
    elif intent is Intent.EVENTS:
        await show_events_menu(message, user, user_events, settings, fresh=True)
    elif intent is Intent.SETTINGS:
        await show_settings(message, user, schedules, fresh=True)
    elif intent is Intent.FEEDBACK:
        await begin_feedback(message, state, fresh=True)
    elif intent is Intent.HELP:
        await reply(message, TOUR_PAGES[0], reply_markup=tour_keyboard(0), fresh=True)
    elif intent in (Intent.HOME, Intent.CANCEL):
        await reply(message, hub_text(user), reply_markup=hub_keyboard(), fresh=True)
    elif intent is Intent.ADD_EVENT:
        await begin_quick_event(message, user, state, user_events, settings, message.text or "")
    else:
        await reply(message, "Не совсем понял 🙂 Выбери раздел или напиши, например: «что надеть», «расписание», "
                             "«добавь событие».\n\n" + hub_text(user), reply_markup=hub_keyboard(), fresh=True)
