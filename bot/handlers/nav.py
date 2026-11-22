"""Навигация, общая для всех экранов: главное меню (inline), тур по возможностям, отмена любого сценария.

«🏠 В меню» открывает меню на месте (редактирует текущее сообщение); «Отмена» (кнопка, /cancel или слово «отмена»)
выходит из любого незавершённого сценария."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.filters import WantsCancel
from bot.handlers.events import show_events_menu
from bot.handlers.feedback import begin_feedback
from bot.handlers.lessons import show_overview
from bot.handlers.settings import show_settings
from bot.handlers.start import hub_text
from bot.handlers.summary import show_summary
from bot.helpers import reply
from bot.keyboards.main_menu import main_menu
from bot.keyboards.nav import hub_keyboard, tour_keyboard
from bot.texts import TOUR_PAGES
from bot.ui import ui
from config import Settings
from database.models import User
from database.repositories import ScheduleRepository, UserEventRepository, WeeklyScheduleRepository
from services.summary_service import SummaryService
from utils.helpers import InFlightGuard

router = Router(name="nav")
router.message.filter(F.chat.type == ChatType.PRIVATE)


# ----------------------------------------------------------------- отмена

@router.message(Command("cancel"))
@router.message(WantsCancel())
async def cancel_scenario(message: Message, user: User, state: FSMContext) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    notice = "Отменено." if had_state else "Отменять нечего."
    await reply(message, f"{notice}\n\n" + hub_text(user), reply_markup=hub_keyboard(), fresh=True)


# ----------------------------------------------------------------- главное меню (inline)

@router.callback_query(F.data.in_({"nav:home", "ev:exit"}))
async def go_home(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await reply(callback, hub_text(user), reply_markup=hub_keyboard())


@router.callback_query(F.data == "nav:sum")
async def go_summary(callback: CallbackQuery, user: User, state: FSMContext, summaries: SummaryService,
                     guard: InFlightGuard) -> None:
    await state.clear()
    await show_summary(callback, user, summaries, guard)


@router.callback_query(F.data == "nav:les")
async def go_lessons(callback: CallbackQuery, user: User, state: FSMContext, weekly: WeeklyScheduleRepository) -> None:
    await state.clear()
    await callback.answer()
    await show_overview(callback, user, weekly)


@router.callback_query(F.data == "nav:evt")
async def go_events(callback: CallbackQuery, user: User, state: FSMContext, user_events: UserEventRepository,
                    settings: Settings) -> None:
    await state.clear()
    await callback.answer()
    await show_events_menu(callback, user, user_events, settings)


@router.callback_query(F.data == "nav:set")
async def go_settings(callback: CallbackQuery, user: User, state: FSMContext, schedules: ScheduleRepository) -> None:
    await state.clear()
    await callback.answer()
    await show_settings(callback, user, schedules)


@router.callback_query(F.data == "nav:fb")
async def go_feedback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await begin_feedback(callback, state)


# ----------------------------------------------------------------- тур по возможностям

@router.callback_query(F.data.regexp(r"^tour:\d{1,2}$"))
async def tour_page(callback: CallbackQuery) -> None:
    page = max(0, min(int((callback.data or "tour:0").split(":")[1]), len(TOUR_PAGES) - 1))
    await callback.answer()
    await reply(callback, TOUR_PAGES[page], reply_markup=tour_keyboard(page))


@router.callback_query(F.data == "tour:done")
async def tour_done(callback: CallbackQuery) -> None:
    """Конец знакомства: убираем тур и присылаем одно сообщение с постоянной клавиатурой-меню."""
    await callback.answer()
    uid = callback.from_user.id
    await ui.retire(callback.bot, uid, ui.anchor_of(uid))
    sent = await callback.bot.send_message(
        uid,
        "Готово! Главное меню — кнопки внизу 👇\n\n"
        "Можно и просто написать, что нужно: «что надеть», «покажи расписание», "
        "«добавь урок английского завтра в 16:00».",
        reply_markup=main_menu(),
    )
    ui.remember(uid, sent.message_id)
