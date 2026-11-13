"""📅 Мои события: регулярные и разовые события с напоминаниями.

Handler — только общение с пользователем. Данные — в repositories (UserEventRepository), логика повторений и
разбор ввода — в services (event_schedule / event_input / event_text), отправка напоминаний — в
services/event_reminders.py. Все операции идут по user.telegram_id из самого апдейта: чужие события
недоступны. Кнопки идемпотентны (повторное нажатие безопасно), а при потере состояния диалога бот
отвечает «кнопка неактуальна» (bot/handlers/fallback.py)."""
from __future__ import annotations

import logging
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.filters import TEXT_INPUT
from bot.helpers import clip, edit_or_reply
from bot.keyboards.events_menu import (
    PAGE_SIZE, card_keyboard, cancel_keyboard, date_keyboard, default_remind_keyboard, delete_confirm_keyboard,
    duration_keyboard, edit_fields_keyboard, event_settings_keyboard, events_menu_keyboard, list_keyboard,
    month_day_keyboard, preview_keyboard, remind_keyboard, repeat_keyboard, weekdays_keyboard,
)
from bot.keyboards.main_menu import BTN_EVENTS, main_menu
from bot.states.settings_states import EventForm
from config import Settings
from database.models import User, UserEvent
from database.repositories import EventLimitError, UserEventRepository, UserRepository
from services.event_input import (
    parse_date_input, parse_duration, parse_month_day, parse_remind_minutes, parse_time_input, parse_title,
)
from services.event_schedule import (
    end_datetime, end_time_text, next_occurrence, rule_from_event, rule_from_fields, start_datetime,
)
from services.event_text import card_text, fields_from_event, format_remind, preview_text
from services.time_service import now_in
from utils.formatting import esc
from utils.helpers import InFlightGuard

logger = logging.getLogger(__name__)

router = Router(name="events")
router.message.filter(F.chat.type == ChatType.PRIVATE)

FULL_CHAIN = ["title", "repeat", "when", "time", "duration", "remind"]
EDIT_CHAINS = {
    "title": ["title"], "when": ["repeat", "when"], "time": ["time"], "dur": ["duration"], "rem": ["remind"],
}


# ----------------------------------------------------------------- общие помощники

async def _say(event: Message | CallbackQuery, text: str, kb=None) -> None:
    """Сообщение пользователя -> новое сообщение; нажатие кнопки -> правим то же сообщение."""
    if isinstance(event, Message):
        await event.answer(clip(text), reply_markup=kb)
    else:
        await edit_or_reply(event, text, reply_markup=kb)


def _now(user: User) -> datetime:
    return now_in(user.timezone)


def _limit_text(active: int, limit: int) -> str:
    return (f"Ты достиг лимита активных событий ({active}/{limit}). "
            "Удали или отключи одно из существующих событий, чтобы добавить новое.")


def _int_tail(data: str | None) -> int | None:
    try:
        return int((data or "").rsplit(":", 1)[-1])
    except ValueError:
        return None


async def _menu_text(user: User, user_events: UserEventRepository, settings: Settings) -> str:
    active = await user_events.count_active(user.telegram_id)
    return ("📅 <b>Мои события</b>\n\nРегулярные и разовые события с напоминаниями.\n"
            f"Активных: <b>{active}/{settings.max_user_events}</b>")


# ----------------------------------------------------------------- меню

@router.message(F.text == BTN_EVENTS)
@router.message(Command("events"))
async def open_events(message: Message, user: User, state: FSMContext, user_events: UserEventRepository,
                      settings: Settings) -> None:
    await state.clear()
    await message.answer(await _menu_text(user, user_events, settings), reply_markup=events_menu_keyboard())


@router.callback_query(F.data == "ev:menu")
async def back_to_menu(callback: CallbackQuery, user: User, state: FSMContext, user_events: UserEventRepository,
                       settings: Settings) -> None:
    await state.clear()
    await callback.answer()
    await edit_or_reply(callback, await _menu_text(user, user_events, settings), reply_markup=events_menu_keyboard())


@router.callback_query(F.data == "ev:exit")
async def exit_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await edit_or_reply(callback, "📅 Мои события — закрыто.")
    await callback.bot.send_message(callback.from_user.id, "Главное меню 👇", reply_markup=main_menu())


# ----------------------------------------------------------------- список и карточка

async def _list_view(user: User, user_events: UserEventRepository, settings: Settings, page: int):
    events = await user_events.list_for_user(user.telegram_id)
    now_ts = time.time()

    def sort_key(e: UserEvent):
        nxt = None
        if e.status == "active":
            try:
                nxt = next_occurrence(rule_from_event(e), now_ts)
            except Exception:  # noqa: BLE001 — битые данные одного события не ломают список
                nxt = None
        return (0 if e.status == "active" else 1, nxt.timestamp() if nxt else float("inf"), e.id)

    events.sort(key=sort_key)
    active = sum(1 for e in events if e.status == "active")
    pages = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    if not events:
        text = "📋 <b>Мои события</b>\n\nПока пусто. Добавь первое — например, урок или тренировку."
    else:
        text = (f"📋 <b>Мои события</b>\nАктивных: <b>{active}/{settings.max_user_events}</b>"
                + (f" · всего {len(events)}" if len(events) != active else ""))
    return text, list_keyboard(events, page)


@router.callback_query(F.data.regexp(r"^ev:list:\d{1,3}$"))
async def show_list(callback: CallbackQuery, user: User, state: FSMContext, user_events: UserEventRepository,
                    settings: Settings) -> None:
    await state.clear()
    await callback.answer()
    text, kb = await _list_view(user, user_events, settings, _int_tail(callback.data) or 0)
    await edit_or_reply(callback, text, reply_markup=kb)


async def _card_view(user: User, user_events: UserEventRepository, event_id: int, page: int):
    event = await user_events.get(user.telegram_id, event_id)
    if event is None:
        return None, None
    now_local = _now(user)
    nxt = None
    if event.status == "active":
        try:
            nxt = next_occurrence(rule_from_event(event), now_local.timestamp())
        except Exception:  # noqa: BLE001
            nxt = None
    return card_text(event, nxt, now_local), card_keyboard(event, page)


async def _show_card(callback: CallbackQuery, user: User, user_events: UserEventRepository, settings: Settings,
                     event_id: int | None, page: int = 0, notice: str = "") -> None:
    text, kb = (None, None) if event_id is None else await _card_view(user, user_events, event_id, page)
    if text is None:
        await callback.answer("Событие не найдено — возможно, оно уже удалено", show_alert=True)
        list_text, list_kb = await _list_view(user, user_events, settings, page)
        await edit_or_reply(callback, list_text, reply_markup=list_kb)
        return
    await edit_or_reply(callback, (notice + "\n\n" if notice else "") + text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^ev:o:\d+:\d{1,3}$"))
async def open_card(callback: CallbackQuery, user: User, state: FSMContext, user_events: UserEventRepository,
                    settings: Settings) -> None:
    _, _, event_id, page = (callback.data or "").split(":")
    await state.clear()
    await callback.answer()
    await _show_card(callback, user, user_events, settings, int(event_id), int(page))


@router.callback_query(F.data.regexp(r"^ev:(on|off):\d+$"))
async def toggle_event(callback: CallbackQuery, user: User, user_events: UserEventRepository, settings: Settings) -> None:
    action, event_id = (callback.data or "").split(":")[1:]
    target = "active" if action == "on" else "disabled"
    result = await user_events.set_status(
        user.telegram_id, int(event_id), target, max_active=settings.max_user_events if target == "active" else None
    )
    if result == "limit":
        active = await user_events.count_active(user.telegram_id)
        await callback.answer(_limit_text(active, settings.max_user_events), show_alert=True)
        return
    if result == "done":
        await callback.answer("Это событие уже прошло", show_alert=True)
    elif result == "ok":
        await callback.answer("Включено ▶️" if target == "active" else "Отключено ⏸")
    else:  # same / missing — повторное нажатие или уже удалено
        await callback.answer()
    await _show_card(callback, user, user_events, settings, int(event_id))


@router.callback_query(F.data.regexp(r"^ev:del:\d+$"))
async def ask_delete(callback: CallbackQuery, user: User, user_events: UserEventRepository) -> None:
    event = await user_events.get(user.telegram_id, _int_tail(callback.data) or 0)
    if event is None:
        await callback.answer("Событие уже удалено", show_alert=True)
        return
    await callback.answer()
    await edit_or_reply(callback, f"Удалить событие <b>{esc(event.title)}</b>?\nНапоминания по нему тоже будут отменены.",
                        reply_markup=delete_confirm_keyboard(event.id))


@router.callback_query(F.data.regexp(r"^ev:delok:\d+$"))
async def do_delete(callback: CallbackQuery, user: User, user_events: UserEventRepository, settings: Settings) -> None:
    deleted = await user_events.delete(user.telegram_id, _int_tail(callback.data) or 0)
    await callback.answer("Удалено 🗑" if deleted else "Уже удалено")
    text, kb = await _list_view(user, user_events, settings, 0)
    await edit_or_reply(callback, text, reply_markup=kb)


# ----------------------------------------------------------------- настройки уведомлений

async def _settings_view(user: User) -> tuple[str, object]:
    state_text = "включены ✅" if user.event_reminders_enabled else "выключены ⏸"
    text = ("⚙️ <b>Настройки уведомлений</b>\n\n"
            f"Напоминания о событиях: <b>{state_text}</b>\n"
            f"Напоминать по умолчанию: <b>{format_remind(user.default_remind_minutes)}</b>\n\n"
            "<i>Время по умолчанию подставляется в новые события; у каждого события можно задать своё.</i>")
    return text, event_settings_keyboard(user.event_reminders_enabled)


@router.callback_query(F.data == "ev:set")
async def open_settings(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    text, kb = await _settings_view(user)
    await edit_or_reply(callback, text, reply_markup=kb)


@router.callback_query(F.data == "es:tog")
async def toggle_reminders(callback: CallbackQuery, user: User, users: UserRepository) -> None:
    new_value = not user.event_reminders_enabled
    await users.set_event_settings(user.telegram_id, reminders_enabled=new_value)
    await callback.answer("Напоминания включены" if new_value else "Напоминания выключены")
    fresh = await users.get(user.telegram_id)
    text, kb = await _settings_view(fresh or user)
    await edit_or_reply(callback, text, reply_markup=kb)


@router.callback_query(F.data == "es:def")
async def choose_default(callback: CallbackQuery, user: User) -> None:
    await callback.answer()
    await edit_or_reply(callback, "За сколько минут напоминать по умолчанию?",
                        reply_markup=default_remind_keyboard(user.default_remind_minutes))


@router.callback_query(F.data.regexp(r"^es:set:\d{1,4}$"))
async def set_default(callback: CallbackQuery, user: User, users: UserRepository) -> None:
    minutes = _int_tail(callback.data) or 0
    try:
        await users.set_event_settings(user.telegram_id, default_remind_minutes=minutes)
    except ValueError:
        await callback.answer("Недопустимое значение", show_alert=True)
        return
    await callback.answer("Сохранено ✅")
    fresh = await users.get(user.telegram_id)
    text, kb = await _settings_view(fresh or user)
    await edit_or_reply(callback, text, reply_markup=kb)


# ----------------------------------------------------------------- диалог: помощники

def _new_draft(user: User) -> dict:
    return {
        "title": "", "recurrence": "none", "weekdays_mask": 0, "month_day": None, "event_date": None,
        "start_time": "", "duration_min": 60, "remind_before_min": user.default_remind_minutes,
    }


async def _draft(state: FSMContext) -> dict:
    return (await state.get_data()).get("draft") or {}


async def _set_draft(state: FSMContext, **changes) -> dict:
    draft = {**await _draft(state), **changes}
    await state.update_data(draft=draft)
    return draft


async def _goto(event: Message | CallbackQuery, state: FSMContext, user: User, step: str) -> None:
    data = await state.get_data()
    draft = data.get("draft") or {}
    editing = data.get("edit_id") is not None

    if step == "title":
        await state.set_state(EventForm.title)
        await _say(event, ("✏️ Как теперь назвать событие?" if editing else
                           "📅 <b>Новое событие</b>\n\nКак назвать? Например: <i>Урок английского</i>"),
                   cancel_keyboard())
    elif step == "repeat":
        await state.set_state(EventForm.repeat)
        await _say(event, "Как часто повторяется?", repeat_keyboard())
    elif step == "when":
        recurrence = draft.get("recurrence")
        if recurrence == "daily":
            await _advance(event, state, user)
        elif recurrence == "weekly":
            await state.set_state(EventForm.weekdays)
            await _say(event, "В какие дни недели? Можно выбрать несколько.", weekdays_keyboard(draft.get("weekdays_mask", 0)))
        elif recurrence == "monthly":
            await state.set_state(EventForm.month_day)
            await _say(event, "Какого числа каждого месяца?\nВыбери кнопкой или напиши число (1–31).\n"
                              "<i>Если в месяце нет такого числа — событие будет в его последний день.</i>",
                       month_day_keyboard())
        else:
            await state.set_state(EventForm.date)
            await _say(event, "Какого числа? Выбери кнопкой или напиши: <i>15.11</i>, <i>15 ноября</i>, "
                              "<i>в пятницу</i>.", date_keyboard())
    elif step == "time":
        await state.set_state(EventForm.time)
        await _say(event, "Во сколько начало? Напиши время, например <i>16:00</i>.", cancel_keyboard())
    elif step == "duration":
        await state.set_state(EventForm.duration)
        await _say(event, "Сколько длится? Выбери или напиши: <i>2 часа</i>, <i>90 минут</i>, <i>1:30</i>.",
                   duration_keyboard())
    elif step == "remind":
        await state.set_state(EventForm.remind)
        await _say(event, "За сколько напомнить о начале?", remind_keyboard(draft.get("remind_before_min")))
    else:  # preview
        await state.set_state(EventForm.preview)
        await _say(event, preview_text(draft, _now(user).date(), editing=editing), preview_keyboard())


async def _advance(event: Message | CallbackQuery, state: FSMContext, user: User) -> None:
    chain = list((await state.get_data()).get("chain") or [])
    step = chain.pop(0) if chain else "preview"
    await state.update_data(chain=chain)
    await _goto(event, state, user, step)


# ----------------------------------------------------------------- диалог: запуск

@router.callback_query(F.data == "ev:add")
async def start_new(callback: CallbackQuery, user: User, state: FSMContext, user_events: UserEventRepository,
                    settings: Settings) -> None:
    active = await user_events.count_active(user.telegram_id)
    if active >= settings.max_user_events:
        await callback.answer(_limit_text(active, settings.max_user_events), show_alert=True)
        return
    await state.clear()
    await state.update_data(draft=_new_draft(user), chain=list(FULL_CHAIN), edit_id=None)
    await callback.answer()
    await _advance(callback, state, user)


@router.callback_query(F.data.regexp(r"^ev:edit:\d+$"))
async def start_edit(callback: CallbackQuery, user: User, state: FSMContext, user_events: UserEventRepository) -> None:
    event = await user_events.get(user.telegram_id, _int_tail(callback.data) or 0)
    if event is None or event.status == "done":
        await callback.answer("Событие не найдено — возможно, оно уже удалено", show_alert=True)
        return
    await state.clear()
    await state.update_data(draft=fields_from_event(event), chain=[], edit_id=event.id)
    await callback.answer()
    await _goto(callback, state, user, "preview")


@router.callback_query(StateFilter(EventForm), F.data == "ef:cancel")
async def cancel_form(callback: CallbackQuery, user: User, state: FSMContext, user_events: UserEventRepository,
                      settings: Settings) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await edit_or_reply(callback, await _menu_text(user, user_events, settings), reply_markup=events_menu_keyboard())


# ----------------------------------------------------------------- диалог: шаги

@router.message(StateFilter(EventForm.title), TEXT_INPUT)
async def got_title(message: Message, user: User, state: FSMContext) -> None:
    title, error = parse_title(message.text or "")
    if error:
        await message.answer(esc(error), reply_markup=cancel_keyboard())
        return
    await _set_draft(state, title=title)
    await _advance(message, state, user)


@router.callback_query(StateFilter(EventForm.repeat), F.data.regexp(r"^ef:rep:(none|daily|weekly|monthly)$"))
async def pick_repeat(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    recurrence = (callback.data or "").split(":")[2]
    draft = await _draft(state)
    changes: dict = {"recurrence": recurrence}
    if recurrence == "none":
        if draft.get("recurrence") != "none":
            changes["event_date"] = None      # у разового события дату нужно указать
    elif draft.get("recurrence") != recurrence or not draft.get("event_date"):
        changes["event_date"] = _now(user).date().isoformat()   # повторяющееся начинается с сегодняшнего дня
    if recurrence != "weekly":
        changes["weekdays_mask"] = 0
    if recurrence != "monthly":
        changes["month_day"] = None
    await _set_draft(state, **changes)
    await callback.answer()
    await _advance(callback, state, user)


@router.callback_query(StateFilter(EventForm.weekdays), F.data.regexp(r"^ef:wd:[0-6]$"))
async def toggle_weekday(callback: CallbackQuery, state: FSMContext) -> None:
    day = _int_tail(callback.data) or 0
    draft = await _draft(state)
    mask = draft.get("weekdays_mask", 0) ^ (1 << day)
    await _set_draft(state, weekdays_mask=mask)
    await callback.answer()
    await edit_or_reply(callback, "В какие дни недели? Можно выбрать несколько.", reply_markup=weekdays_keyboard(mask))


@router.callback_query(StateFilter(EventForm.weekdays), F.data == "ef:wddone")
async def weekdays_done(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    if not (await _draft(state)).get("weekdays_mask"):
        await callback.answer("Выбери хотя бы один день", show_alert=True)
        return
    await callback.answer()
    await _advance(callback, state, user)


@router.callback_query(StateFilter(EventForm.date), F.data.regexp(r"^ef:date:(today|tomorrow)$"))
async def pick_date_button(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    text = "сегодня" if (callback.data or "").endswith("today") else "завтра"
    value, _ = parse_date_input(text, _now(user).date())
    await _set_draft(state, event_date=value.isoformat())
    await callback.answer()
    await _advance(callback, state, user)


@router.message(StateFilter(EventForm.date), TEXT_INPUT)
async def got_date(message: Message, user: User, state: FSMContext) -> None:
    value, error = parse_date_input(message.text or "", _now(user).date())
    if error:
        await message.answer(esc(error), reply_markup=date_keyboard())
        return
    await _set_draft(state, event_date=value.isoformat())
    await _advance(message, state, user)


@router.callback_query(StateFilter(EventForm.month_day), F.data.regexp(r"^ef:md:(\d{1,2}|last)$"))
async def pick_month_day_button(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    raw = (callback.data or "").split(":")[2]
    await _set_draft(state, month_day=-1 if raw == "last" else int(raw))
    await callback.answer()
    await _advance(callback, state, user)


@router.message(StateFilter(EventForm.month_day), TEXT_INPUT)
async def got_month_day(message: Message, user: User, state: FSMContext) -> None:
    value, error = parse_month_day(message.text or "")
    if error:
        await message.answer(esc(error), reply_markup=month_day_keyboard())
        return
    await _set_draft(state, month_day=value)
    await _advance(message, state, user)


@router.message(StateFilter(EventForm.time), TEXT_INPUT)
async def got_time(message: Message, user: User, state: FSMContext) -> None:
    value = parse_time_input(message.text or "")
    if value is None:
        await message.answer("Не похоже на время. Напиши в формате <b>ЧЧ:ММ</b>, например <i>16:00</i>.",
                             reply_markup=cancel_keyboard())
        return
    await _set_draft(state, start_time=value)
    await _advance(message, state, user)


@router.callback_query(StateFilter(EventForm.duration), F.data.regexp(r"^ef:dur:\d{1,4}$"))
async def pick_duration_button(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await _set_draft(state, duration_min=_int_tail(callback.data) or 60)
    await callback.answer()
    await _advance(callback, state, user)


@router.message(StateFilter(EventForm.duration), TEXT_INPUT)
async def got_duration(message: Message, user: User, state: FSMContext) -> None:
    minutes, error = parse_duration(message.text or "")
    if error:
        await message.answer(esc(error), reply_markup=duration_keyboard())
        return
    await _set_draft(state, duration_min=minutes)
    await _advance(message, state, user)


@router.callback_query(StateFilter(EventForm.remind), F.data.regexp(r"^ef:rem:\d{1,4}$"))
async def pick_remind_button(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await _set_draft(state, remind_before_min=_int_tail(callback.data) or 15)
    await callback.answer()
    await _advance(callback, state, user)


@router.callback_query(StateFilter(EventForm.remind), F.data == "ef:remc")
async def ask_custom_remind(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(EventForm.remind_custom)
    await callback.answer()
    await edit_or_reply(callback, "За сколько минут напомнить? Напиши число, например <i>45</i> (или <i>1 час</i>).",
                        reply_markup=cancel_keyboard())


@router.message(StateFilter(EventForm.remind_custom), TEXT_INPUT)
async def got_custom_remind(message: Message, user: User, state: FSMContext) -> None:
    minutes, error = parse_remind_minutes(message.text or "")
    if error:
        await message.answer(esc(error), reply_markup=cancel_keyboard())
        return
    await _set_draft(state, remind_before_min=minutes)
    await _advance(message, state, user)


# нажатия кнопок, когда ждём текст, и текст, когда ждём кнопку
@router.message(StateFilter(EventForm.repeat, EventForm.weekdays, EventForm.remind, EventForm.preview), (~F.text) | TEXT_INPUT)
async def buttons_only_hint(message: Message) -> None:
    await message.answer("Выбери вариант кнопкой под сообщением 👆")


@router.message(StateFilter(EventForm.title, EventForm.date, EventForm.month_day, EventForm.time,
                            EventForm.duration, EventForm.remind_custom), ~F.text)
async def text_only_hint(message: Message) -> None:
    await message.answer("Пока принимаю только текст. Напиши ответ словами.")


# ----------------------------------------------------------------- диалог: предпросмотр, правка, сохранение

@router.callback_query(StateFilter(EventForm.preview), F.data == "ef:edit")
async def choose_field(callback: CallbackQuery) -> None:
    await callback.answer()
    await edit_or_reply(callback, "Что изменить?", reply_markup=edit_fields_keyboard())


@router.callback_query(StateFilter(EventForm.preview), F.data == "ef:preview")
async def back_to_preview(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    await callback.answer()
    await _goto(callback, state, user, "preview")


@router.callback_query(StateFilter(EventForm.preview), F.data.regexp(r"^ef:f:(title|when|time|dur|rem)$"))
async def edit_field(callback: CallbackQuery, user: User, state: FSMContext) -> None:
    field = (callback.data or "").split(":")[2]
    await state.update_data(chain=list(EDIT_CHAINS[field]))
    await callback.answer()
    await _advance(callback, state, user)


def _build_fields(draft: dict, user: User) -> dict:
    fields = dict(draft)
    fields["timezone"] = user.timezone
    fields["end_time"] = end_time_text(fields["start_time"], fields["duration_min"])
    if fields["recurrence"] != "weekly":
        fields["weekdays_mask"] = 0
    if fields["recurrence"] != "monthly":
        fields["month_day"] = None
    return fields


@router.callback_query(StateFilter(EventForm.preview), F.data == "ef:save")
async def save_event(callback: CallbackQuery, user: User, state: FSMContext, user_events: UserEventRepository,
                     settings: Settings, guard: InFlightGuard) -> None:
    if not guard.try_acquire(user.telegram_id, "event-save"):
        await callback.answer("Уже сохраняю…")
        return
    try:
        # Состояние перепроверяем уже под защитой: второе параллельное нажатие не создаст дубликат.
        if await state.get_state() != EventForm.preview.state:
            await callback.answer("Это уже сохранено", show_alert=False)
            return
        data = await state.get_data()
        draft, edit_id = data.get("draft") or {}, data.get("edit_id")
        required = ("title", "recurrence", "start_time", "duration_min", "remind_before_min")
        if any(not draft.get(k) for k in required):
            await callback.answer("Не все данные заполнены — нажми «Изменить»", show_alert=True)
            return
        fields = _build_fields(draft, user)
        try:
            rule = rule_from_fields(fields, user.timezone)
        except (ValueError, KeyError):
            await callback.answer("Не все данные заполнены — нажми «Изменить»", show_alert=True)
            return

        note = ""
        if fields["recurrence"] == "none":
            if rule.anchor_date is None:
                await callback.answer("Укажи дату — нажми «Изменить» → «Когда»", show_alert=True)
                return
            start = start_datetime(rule, rule.anchor_date)
            if start.timestamp() <= time.time():
                await callback.answer("Это время уже прошло — измени дату или время", show_alert=True)
                return
            if start.timestamp() - fields["remind_before_min"] * 60 < time.time():
                note = ("⚠️ До начала осталось меньше, чем указано в напоминании, — "
                        "напомнить заранее уже не получится.")

        try:
            if edit_id is None:
                event = await user_events.create(user.telegram_id, fields, max_active=settings.max_user_events)
                notice = "✅ Событие сохранено."
            else:
                if not await user_events.update(user.telegram_id, edit_id, fields):
                    await state.clear()
                    await callback.answer("Событие уже удалено", show_alert=True)
                    return
                event = await user_events.get(user.telegram_id, edit_id)
                notice = "✅ Изменения сохранены."
        except EventLimitError as exc:
            await state.clear()
            await callback.answer()
            active = await user_events.count_active(user.telegram_id)
            await edit_or_reply(callback, _limit_text(active, exc.limit), reply_markup=events_menu_keyboard())
            return
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        await state.clear()
        await callback.answer("Сохранено ✅")
        text, kb = await _card_view(user, user_events, event.id, 0)
        await edit_or_reply(callback, notice + ("\n" + note if note else "") + "\n\n" + (text or ""), reply_markup=kb)
    finally:
        guard.release(user.telegram_id, "event-save")
