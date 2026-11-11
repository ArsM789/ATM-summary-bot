"""⚙️ Настройки: имя и обращения, город, часовой пояс, автосводки вкл/выкл, тестовая сводка.
Время автосводок — в handlers/schedule.py (кнопка «⏰ Время сводок»)."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.filters import TEXT_INPUT
from bot.handlers.onboarding import begin_onboarding
from bot.helpers import edit_or_reply, reply
from bot.keyboards.main_menu import BTN_SETTINGS, summary_keyboard
from bot.keyboards.settings_menu import cancel_keyboard, cities_keyboard, settings_keyboard
from bot.states.settings_states import CityInput, TimezoneInput
from database.models import User
from database.repositories import ScheduleRepository, UserRepository
from services.summary_service import SummaryService
from services.time_service import is_valid_timezone, now_in, parse_timezone
from services.weather import WeatherService
from services.weather_models import CityNotFoundError, WeatherUnavailableError
from utils.formatting import esc, join_ru
from utils.helpers import InFlightGuard

logger = logging.getLogger(__name__)

router = Router(name="settings")
router.message.filter(F.chat.type == ChatType.PRIVATE)


async def _settings_text(user: User, schedules: ScheduleRepository) -> str:
    entries = await schedules.list_for_user(user.telegram_id)
    active = sum(1 for e in entries if e.enabled)
    status = "включены ✅" if user.notifications_enabled else "выключены ⏸"
    try:
        local = now_in(user.timezone).strftime("%H:%M")
    except ValueError:
        local = "—"
    variants = join_ru(user.name_variants) if user.name_variants else user.name
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"👤 Имя: <b>{esc(user.name)}</b>\n"
        f"💬 Обращения: {esc(variants)}\n"
        f"🏙 Город: <b>{esc(user.city)}</b>\n"
        f"🕒 Часовой пояс: <b>{esc(user.timezone)}</b> <i>(сейчас {local})</i>\n"
        f"🔔 Автосводки: <b>{status}</b>\n"
        f"⏰ Времени отправки: <b>{len(entries)}</b>, активных: <b>{active}</b>"
    )


async def show_settings(event: Message | CallbackQuery, user: User, schedules: ScheduleRepository, *, edit: bool = False) -> None:
    text = await _settings_text(user, schedules)
    markup = settings_keyboard(user.notifications_enabled)
    if edit and isinstance(event, CallbackQuery):
        await edit_or_reply(event, text, reply_markup=markup)
    else:
        await reply(event, text, reply_markup=markup)


@router.message(F.text == BTN_SETTINGS)
@router.message(Command("settings"))
async def open_settings(message: Message, user: User, schedules: ScheduleRepository, state: FSMContext) -> None:
    await state.clear()
    await show_settings(message, user, schedules)


@router.callback_query(F.data == "st:home")
@router.callback_query(F.data == "st:cancel")
async def settings_home(callback: CallbackQuery, user: User, schedules: ScheduleRepository, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await show_settings(callback, user, schedules, edit=True)


# ----------------------------------------------------------------- имя

@router.callback_query(F.data == "st:name")
async def edit_name(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await callback.answer()
    await begin_onboarding(callback, user, state, edit=True)


# ----------------------------------------------------------------- автосводки и тест

@router.callback_query(F.data == "st:notif")
async def toggle_notifications(callback: CallbackQuery, user: User, users: UserRepository, schedules: ScheduleRepository) -> None:
    new_value = not user.notifications_enabled
    await users.set_notifications_enabled(user.telegram_id, new_value)
    await callback.answer("Автосводки включены" if new_value else "Автосводки выключены")
    fresh = await users.get(user.telegram_id)
    await show_settings(callback, fresh or user, schedules, edit=True)


@router.callback_query(F.data == "st:test")
async def test_summary(callback: CallbackQuery, user: User, summaries: SummaryService, guard: InFlightGuard, bot: Bot) -> None:
    if not guard.try_acquire(user.telegram_id, "summary"):
        await callback.answer("Уже готовлю…")
        return
    try:
        await callback.answer("Готовлю тестовую сводку…")
        await bot.send_chat_action(user.telegram_id, ChatAction.TYPING)
        result = await summaries.build(user)
        await bot.send_message(user.telegram_id, "🧪 <i>Тестовая сводка — так будут выглядеть автоматические:</i>")
        await bot.send_message(user.telegram_id, result.text, reply_markup=summary_keyboard())
    finally:
        guard.release(user.telegram_id, "summary")


# ----------------------------------------------------------------- город

@router.callback_query(F.data == "st:city")
async def ask_city(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CityInput.query)
    await callback.answer()
    await edit_or_reply(callback, "🏙 Напиши название города (например: <i>Казань</i>).",
                        reply_markup=cancel_keyboard())


@router.message(StateFilter(CityInput.query), TEXT_INPUT)
async def got_city(message: Message, state: FSMContext, weather: WeatherService) -> None:
    query = (message.text or "").strip()
    try:
        places = await weather.search_city(query, count=5)
    except CityNotFoundError:
        await message.answer("Не нашёл такой город. Проверь написание и попробуй ещё раз.",
                             reply_markup=cancel_keyboard())
        return
    except WeatherUnavailableError:
        await message.answer("Сервис поиска городов сейчас недоступен. Попробуй через минуту.",
                             reply_markup=cancel_keyboard())
        return
    await state.update_data(cities=[
        {"name": p.name, "lat": p.latitude, "lon": p.longitude, "tz": p.timezone} for p in places
    ])
    await message.answer("Выбери подходящий вариант:", reply_markup=cities_keyboard([p.label for p in places]))


@router.callback_query(StateFilter(CityInput.query), F.data.regexp(r"^ct:\d$"))
async def pick_city(callback: CallbackQuery, user: User, state: FSMContext, users: UserRepository,
                    schedules: ScheduleRepository) -> None:
    data = await state.get_data()
    cities = data.get("cities") or []
    index = int((callback.data or "ct:-1").split(":")[1])
    if not 0 <= index < len(cities):
        await callback.answer("Этот список устарел, введи город заново", show_alert=True)
        return
    place = cities[index]
    await users.update_city(user.telegram_id, place["name"], place["lat"], place["lon"])
    note = ""
    tz = place.get("tz")
    if tz and is_valid_timezone(tz) and tz != user.timezone:
        await users.update_timezone(user.telegram_id, tz)
        note = f"\nЧасовой пояс заменён на <b>{esc(tz)}</b> — по месту города. Изменить можно в настройках."
    await state.clear()
    await callback.answer("Город обновлён")
    fresh = await users.get(user.telegram_id)
    await reply(callback, f"✅ Город: <b>{esc(place['name'])}</b>.{note}")
    await show_settings(callback, fresh or user, schedules)


# ----------------------------------------------------------------- часовой пояс

@router.callback_query(F.data == "st:tz")
async def ask_timezone(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TimezoneInput.value)
    await callback.answer()
    await edit_or_reply(
        callback,
        "🕒 Напиши часовой пояс: название вида <code>Europe/Moscow</code>, <code>Asia/Yekaterinburg</code>, "
        "или смещение от UTC, например <code>+3</code>. Для Москвы можно просто <code>МСК</code>.",
        reply_markup=cancel_keyboard(),
    )


@router.message(StateFilter(TimezoneInput.value), TEXT_INPUT)
async def got_timezone(message: Message, user: User, state: FSMContext, users: UserRepository,
                       schedules: ScheduleRepository) -> None:
    tz = parse_timezone(message.text or "")
    if tz is None:
        await message.answer(
            "Не получилось распознать часовой пояс. Примеры: <code>Europe/Moscow</code>, <code>+3</code>, <code>МСК</code>.",
            reply_markup=cancel_keyboard(),
        )
        return
    await users.update_timezone(user.telegram_id, tz)
    await state.clear()
    fresh = await users.get(user.telegram_id)
    await message.answer(f"✅ Часовой пояс: <b>{esc(tz)}</b>. Сейчас у тебя {now_in(tz).strftime('%H:%M')}.")
    await show_settings(message, fresh or user, schedules)
