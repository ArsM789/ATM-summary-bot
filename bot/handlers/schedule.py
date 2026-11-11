"""⏰ Время автосводок (из «⚙️ Настройки»): несколько независимых уведомлений в день.
Список / добавить / изменить время / включить-отключить / удалить. Все операции — по user_id из апдейта."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.filters import TEXT_INPUT
from bot.helpers import edit_or_reply, reply
from bot.keyboards.settings_menu import (
    cancel_keyboard, time_delete_confirm_keyboard, time_entry_keyboard, times_keyboard,
)
from bot.states.settings_states import SendTimeInput
from database.models import ScheduleEntry, User
from database.repositories import DuplicateTimeError, ScheduleLimitError, ScheduleRepository
from services.time_service import describe_weekdays, parse_hhmm

router = Router(name="schedule")
router.message.filter(F.chat.type == ChatType.PRIVATE)


def _entry_id(data: str | None) -> int | None:
    try:
        return int((data or "").rsplit(":", 1)[-1])
    except ValueError:
        return None


async def _list_text(user: User, schedules: ScheduleRepository) -> tuple[str, list[ScheduleEntry]]:
    entries = await schedules.list_for_user(user.telegram_id)
    lines = ["⏰ <b>Время автосводок</b>\n", f"Часовой пояс: <b>{user.timezone}</b>"]
    lines.append(f"Автосводки: <b>{'включены ✅' if user.notifications_enabled else 'выключены ⏸ (включить можно в настройках)'}</b>\n")
    if entries:
        for e in entries:
            lines.append(f"{'✅' if e.enabled else '⏸'} <b>{e.send_time}</b> — {describe_weekdays(e.weekdays)}")
    else:
        lines.append("Времени отправки пока нет — добавь первое.")
    lines.append("\n<i>Сводка приходит в это время по твоим часам. Можно задать несколько времён в день.</i>")
    return "\n".join(lines), entries


async def show_times(event: Message | CallbackQuery, user: User, schedules: ScheduleRepository, *, edit: bool) -> None:
    text, entries = await _list_text(user, schedules)
    if edit and isinstance(event, CallbackQuery):
        await edit_or_reply(event, text, reply_markup=times_keyboard(entries))
    else:
        await reply(event, text, reply_markup=times_keyboard(entries))


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, user: User, schedules: ScheduleRepository, state: FSMContext) -> None:
    await state.clear()
    await show_times(message, user, schedules, edit=False)


@router.callback_query(F.data == "st:times")
async def open_times(callback: CallbackQuery, user: User, schedules: ScheduleRepository, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await show_times(callback, user, schedules, edit=True)


async def _show_entry(callback: CallbackQuery, user: User, schedules: ScheduleRepository, entry_id: int | None) -> None:
    entry = await schedules.get(user.telegram_id, entry_id) if entry_id is not None else None
    if entry is None:
        await callback.answer("Это время уже удалено", show_alert=True)
        await show_times(callback, user, schedules, edit=True)
        return
    await edit_or_reply(
        callback,
        f"⏰ <b>{entry.send_time}</b> — {describe_weekdays(entry.weekdays)}\n"
        f"Статус: <b>{'включено ✅' if entry.enabled else 'отключено ⏸'}</b>",
        reply_markup=time_entry_keyboard(entry),
    )


@router.callback_query(F.data.startswith("tm:open:"))
async def open_entry(callback: CallbackQuery, user: User, schedules: ScheduleRepository, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_entry(callback, user, schedules, _entry_id(callback.data))


@router.callback_query(F.data.startswith("tm:tog:"))
async def toggle_entry(callback: CallbackQuery, user: User, schedules: ScheduleRepository) -> None:
    entry_id = _entry_id(callback.data)
    entry = await schedules.get(user.telegram_id, entry_id) if entry_id is not None else None
    if entry is None:
        await callback.answer("Это время уже удалено", show_alert=True)
        return
    await schedules.set_enabled(user.telegram_id, entry.id, not entry.enabled)
    await callback.answer("Включено" if not entry.enabled else "Отключено")
    await _show_entry(callback, user, schedules, entry.id)


@router.callback_query(F.data.startswith("tm:del:"))
async def ask_delete(callback: CallbackQuery, user: User, schedules: ScheduleRepository) -> None:
    entry_id = _entry_id(callback.data)
    entry = await schedules.get(user.telegram_id, entry_id) if entry_id is not None else None
    if entry is None:
        await callback.answer("Это время уже удалено", show_alert=True)
        return
    await callback.answer()
    await edit_or_reply(callback, f"Удалить время <b>{entry.send_time}</b>?",
                        reply_markup=time_delete_confirm_keyboard(entry.id))


@router.callback_query(F.data.startswith("tm:delok:"))
async def do_delete(callback: CallbackQuery, user: User, schedules: ScheduleRepository) -> None:
    entry_id = _entry_id(callback.data)
    deleted = await schedules.delete(user.telegram_id, entry_id) if entry_id is not None else False
    await callback.answer("Удалено" if deleted else "Уже удалено")
    await show_times(callback, user, schedules, edit=True)


# ----------------------------------------------------------------- добавить / изменить время

@router.callback_query(F.data == "tm:add")
async def ask_new_time(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SendTimeInput.value)
    await state.update_data(entry_id=None)
    await callback.answer()
    await edit_or_reply(callback, "➕ Во сколько присылать сводку? Напиши время в формате <b>ЧЧ:ММ</b>, например <code>07:30</code>.",
                        reply_markup=cancel_keyboard("st:times"))


@router.callback_query(F.data.startswith("tm:edit:"))
async def ask_edit_time(callback: CallbackQuery, user: User, schedules: ScheduleRepository, state: FSMContext) -> None:
    entry_id = _entry_id(callback.data)
    entry = await schedules.get(user.telegram_id, entry_id) if entry_id is not None else None
    if entry is None:
        await callback.answer("Это время уже удалено", show_alert=True)
        return
    await state.clear()
    await state.set_state(SendTimeInput.value)
    await state.update_data(entry_id=entry.id)
    await callback.answer()
    await edit_or_reply(callback, f"✏️ Сейчас <b>{entry.send_time}</b>. Напиши новое время в формате <b>ЧЧ:ММ</b>.",
                        reply_markup=cancel_keyboard(f"tm:open:{entry.id}"))


@router.message(StateFilter(SendTimeInput.value), TEXT_INPUT)
async def got_time(message: Message, user: User, schedules: ScheduleRepository, state: FSMContext) -> None:
    value = parse_hhmm(message.text or "")
    if value is None:
        await message.answer("Не похоже на время. Нужен формат <b>ЧЧ:ММ</b>, например <code>07:30</code> или <code>21:05</code>.")
        return
    data = await state.get_data()
    entry_id = data.get("entry_id")
    try:
        if entry_id is None:
            await schedules.add(user.telegram_id, value)
            note = f"✅ Добавил время <b>{value}</b>."
        else:
            changed = await schedules.update_time(user.telegram_id, entry_id, value)
            note = f"✅ Время изменено на <b>{value}</b>." if changed else "Это время уже удалено."
    except DuplicateTimeError:
        await message.answer(f"Время <b>{value}</b> уже есть в списке. Напиши другое.")
        return
    except ScheduleLimitError:
        await state.clear()
        await message.answer("Достигнут лимит: не больше 10 времён отправки. Удали ненужные.")
        return
    await state.clear()
    await message.answer(note)
    await show_times(message, user, schedules, edit=False)
