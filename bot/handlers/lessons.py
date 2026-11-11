"""📅 Моё расписание: уроки по предметам на каждый учебный день (Пн–Пт).

Принципы:
- в БД ничего не пишется, пока пользователь явно не нажал «Сохранить/Заменить/Очистить»;
- корректные строки не теряются при ошибке в других строках (копятся в состоянии диалога);
- каждый запрос идёт с user_id из самого апдейта, изменение одного дня не трогает остальные."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.filters import TEXT_INPUT
from bot.helpers import edit_or_reply
from bot.keyboards.lessons_menu import (
    clear_confirm_keyboard, confirm_keyboard, day_keyboard, input_keyboard, overview_keyboard, partial_keyboard,
)
from bot.keyboards.main_menu import BTN_LEGACY_SCHEDULE, BTN_LESSONS, main_menu
from bot.states.settings_states import LessonsInput
from database.models import LessonItem, User
from database.repositories import LessonsLimitError, WeeklyScheduleRepository
from services.schedule_parser import (
    day_total_error, format_day_numbered, parse_lessons, total_lessons,
)
from services.time_service import WEEKDAYS
from utils.formatting import capitalize_first, esc, lessons_phrase

router = Router(name="lessons")
router.message.filter(F.chat.type == ChatType.PRIVATE)

SCHOOL_DAYS = range(5)


def _day_name(weekday: int) -> str:
    return capitalize_first(WEEKDAYS[weekday])


def _items_to_data(items: list[LessonItem]) -> list[list]:
    return [[i.subject, i.count] for i in items]


def _items_from_data(raw: list) -> list[LessonItem]:
    return [LessonItem(str(s), int(c)) for s, c in raw]


def _parse_weekday(data: str | None) -> int | None:
    try:
        value = int((data or "").rsplit(":", 1)[-1])
    except ValueError:
        return None
    return value if value in SCHOOL_DAYS else None


# ----------------------------------------------------------------- просмотр

async def _overview_text(weekly: WeeklyScheduleRepository, user_id: int) -> str:
    week = await weekly.get_week(user_id)
    lines = ["📅 <b>Моё расписание</b>\n"]
    for d in SCHOOL_DAYS:
        items = week.get(d, [])
        if items:
            lines.append(f"<b>{_day_name(d)}</b> — {lessons_phrase(total_lessons(items))}")
            lines.append(format_day_numbered(items))
        else:
            lines.append(f"<b>{_day_name(d)}</b> — не заполнено")
        lines.append("")
    lines.append("<i>Суббота и воскресенье — выходные. Выбери день, чтобы изменить его.</i>")
    return "\n".join(lines)


async def _day_text(weekly: WeeklyScheduleRepository, user_id: int, weekday: int, notice: str = "") -> tuple[str, bool]:
    items = await weekly.get_day(user_id, weekday)
    if items:
        body = f"{lessons_phrase(total_lessons(items))}:\n{format_day_numbered(items)}"
    else:
        body = "пока не заполнено"
    prefix = f"{notice}\n\n" if notice else ""
    return f"{prefix}📅 <b>{_day_name(weekday)}</b> — {body}", bool(items)


@router.message(F.text.in_({BTN_LESSONS, BTN_LEGACY_SCHEDULE}))
@router.message(Command("lessons"))
async def open_lessons(message: Message, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    await state.clear()
    if message.text == BTN_LEGACY_SCHEDULE:  # старая кнопка: обновляем клавиатуру
        await message.answer("Кнопка «Расписание» теперь называется «📅 Моё расписание».", reply_markup=main_menu())
    await message.answer(await _overview_text(weekly, user.telegram_id), reply_markup=overview_keyboard())


@router.callback_query(F.data == "ls:home")
async def back_home(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await edit_or_reply(callback, await _overview_text(weekly, user.telegram_id), reply_markup=overview_keyboard())


@router.callback_query(F.data.startswith("ls:day:"))
async def show_day(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    weekday = _parse_weekday(callback.data)
    if weekday is None:
        await callback.answer("Такого дня нет", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    text, filled = await _day_text(weekly, user.telegram_id, weekday)
    await edit_or_reply(callback, text, reply_markup=day_keyboard(weekday, filled))


# ----------------------------------------------------------------- ввод (добавить / заменить)

def _prompt(weekday: int, mode: str, items: list[LessonItem]) -> str:
    action = "Заменяю весь день" if mode == "replace" else "Добавляю предметы"
    current = f"\n\nСейчас:\n{format_day_numbered(items)}" if items else ""
    return (
        f"✏️ <b>{_day_name(weekday)}</b> — {action}.\n\n"
        f"Пришли предметы по одному в строке в формате «название — количество уроков»:\n"
        f"<code>Математика — 2\nФизика — 1\nРусский язык — 1</code>\n"
        f"Порядок строк сохранится так, как ты напишешь. Подойдёт и обычный дефис.{current}"
    )


async def _start_input(event: CallbackQuery, state: FSMContext, weekly: WeeklyScheduleRepository,
                       user_id: int, weekday: int, mode: str, wizard: bool = False) -> None:
    items = await weekly.get_day(user_id, weekday)
    await state.set_state(LessonsInput.text)
    await state.update_data(weekday=weekday, mode=mode, wizard=wizard, pending=[])
    await edit_or_reply(event, _prompt(weekday, mode, items), reply_markup=input_keyboard(wizard))


@router.callback_query(F.data.startswith("ls:add:"))
@router.callback_query(F.data.startswith("ls:rep:"))
async def begin_input(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    weekday = _parse_weekday(callback.data)
    if weekday is None:
        await callback.answer("Такого дня нет", show_alert=True)
        return
    mode = "add" if (callback.data or "").startswith("ls:add:") else "replace"
    await callback.answer()
    await _start_input(callback, state, weekly, user.telegram_id, weekday, mode)


@router.callback_query(F.data == "ls:fill")
async def begin_wizard(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    await callback.answer()
    await _start_input(callback, state, weekly, user.telegram_id, 0, "replace", wizard=True)


@router.message(StateFilter(LessonsInput.text), TEXT_INPUT)
async def got_lessons_text(message: Message, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    data = await state.get_data()
    weekday, mode = data.get("weekday"), data.get("mode")
    if weekday is None or mode is None:
        await state.clear()
        await message.answer("Диалог устарел. Открой «📅 Моё расписание» заново.")
        return

    pending = _items_from_data(data.get("pending", []))
    result = parse_lessons(message.text or "")
    accepted = pending + result.items

    if result.errors:
        # Ничего не пишем в БД. Корректные строки запоминаем, об ошибках говорим конкретно.
        await state.update_data(pending=_items_to_data(accepted))
        problems = "\n".join(
            (f"• строка {e.line_no} «{esc(e.text)}» — {esc(e.reason)}" if e.line_no else f"• {esc(e.reason)}")
            for e in result.errors
        )
        text = f"Не всё получилось разобрать:\n{problems}\n"
        if accepted:
            text += f"\nПринято (пока не сохранено):\n{format_day_numbered(accepted)}\n\nПришли исправленные строки — добавлю к принятым, или сохрани то, что есть."
            await message.answer(text, reply_markup=partial_keyboard())
        else:
            await message.answer(text + "\nИсправь и пришли ещё раз.", reply_markup=input_keyboard(bool(data.get("wizard"))))
        return

    if not accepted:
        await message.answer("Не нашёл ни одной строки. Формат: «Математика — 2».")
        return

    existing = await weekly.get_day(user.telegram_id, weekday) if mode == "add" else []
    total_error = day_total_error(existing + accepted)
    if total_error:
        await state.update_data(pending=[])
        await message.answer(
            f"Так не получится: {esc(total_error)}. Пришли список заново, покороче.",
            reply_markup=input_keyboard(bool(data.get("wizard"))),
        )
        return

    await state.update_data(pending=_items_to_data(accepted))
    if mode == "replace":
        text = f"<b>{_day_name(weekday)}</b> будет заменён на:\n{format_day_numbered(accepted)}"
        verb = "Заменить"
    else:
        text = f"К <b>{_day_name(weekday).lower()}</b> добавится:\n{format_day_numbered(accepted)}"
        verb = "Добавить"
    await message.answer(text, reply_markup=confirm_keyboard(verb))


@router.callback_query(StateFilter(LessonsInput.text), F.data == "ls:redo")
async def redo_input(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.answer()
    await _start_input(callback, state, weekly, user.telegram_id, data["weekday"], data["mode"], bool(data.get("wizard")))


@router.callback_query(StateFilter(LessonsInput.text), F.data == "ls:save")
async def save_pending(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    data = await state.get_data()
    weekday, mode = data.get("weekday"), data.get("mode")
    pending = _items_from_data(data.get("pending", []))
    if weekday is None or not pending:
        await callback.answer("Нечего сохранять", show_alert=True)
        return
    try:
        if mode == "replace":
            await weekly.replace_day(user.telegram_id, weekday, pending)
        else:
            await weekly.append_to_day(user.telegram_id, weekday, pending)
    except LessonsLimitError:
        await callback.answer("В сумме больше 12 уроков в день", show_alert=True)
        return
    await callback.answer("Сохранено ✅")

    if data.get("wizard") and weekday < 4:
        await _start_input(callback, state, weekly, user.telegram_id, weekday + 1, "replace", wizard=True)
        return
    await state.clear()
    if data.get("wizard"):
        await edit_or_reply(callback, "Готово! Расписание на неделю заполнено.\n\n" + await _overview_text(weekly, user.telegram_id),
                            reply_markup=overview_keyboard())
        return
    text, filled = await _day_text(weekly, user.telegram_id, weekday, notice="✅ Сохранено.")
    await edit_or_reply(callback, text, reply_markup=day_keyboard(weekday, filled))


@router.callback_query(StateFilter(LessonsInput.text), F.data == "ls:skip")
async def skip_day(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    data = await state.get_data()
    weekday = data.get("weekday")
    await callback.answer()
    if data.get("wizard") and weekday is not None and weekday < 4:
        await _start_input(callback, state, weekly, user.telegram_id, weekday + 1, "replace", wizard=True)
        return
    await state.clear()
    await edit_or_reply(callback, await _overview_text(weekly, user.telegram_id), reply_markup=overview_keyboard())


@router.callback_query(StateFilter(LessonsInput.text), F.data == "ls:cancel")
async def cancel_input(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    data = await state.get_data()
    weekday = data.get("weekday")
    await state.clear()
    await callback.answer("Отменено")
    if weekday is None:
        await edit_or_reply(callback, await _overview_text(weekly, user.telegram_id), reply_markup=overview_keyboard())
        return
    text, filled = await _day_text(weekly, user.telegram_id, weekday)
    await edit_or_reply(callback, text, reply_markup=day_keyboard(weekday, filled))


# ----------------------------------------------------------------- очистка

@router.callback_query(F.data.startswith("ls:clr:"))
async def ask_clear(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository) -> None:
    weekday = _parse_weekday(callback.data)
    if weekday is None:
        await callback.answer("Такого дня нет", show_alert=True)
        return
    await callback.answer()
    items = await weekly.get_day(user.telegram_id, weekday)
    if not items:
        text, filled = await _day_text(weekly, user.telegram_id, weekday)
        await edit_or_reply(callback, text, reply_markup=day_keyboard(weekday, filled))
        return
    await edit_or_reply(
        callback,
        f"Очистить <b>{_day_name(weekday).lower()}</b>? Будет удалено:\n{format_day_numbered(items)}",
        reply_markup=clear_confirm_keyboard(weekday),
    )


@router.callback_query(F.data.startswith("ls:clrok:"))
async def do_clear(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository) -> None:
    weekday = _parse_weekday(callback.data)
    if weekday is None:
        await callback.answer("Такого дня нет", show_alert=True)
        return
    await weekly.clear_day(user.telegram_id, weekday)
    await callback.answer("Очищено")
    text, filled = await _day_text(weekly, user.telegram_id, weekday, notice="🗑 День очищен.")
    await edit_or_reply(callback, text, reply_markup=day_keyboard(weekday, filled))
