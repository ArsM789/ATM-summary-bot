"""📅 Моё расписание: уроки по предметам на каждый учебный день (Пн–Пт).

Принципы:
- в БД ничего не пишется, пока пользователь явно не нажал «Сохранить/Заменить/Убрать/Очистить»;
- корректные строки не теряются при ошибке в других строках (копятся в состоянии диалога);
- каждый запрос идёт с user_id из самого апдейта, изменение одного дня не трогает остальные;
- предмет без числа — один урок; «Геометрия — 2» — два отдельных урока (нумеруются по очереди);
- расписание показывается в цитате (<blockquote>)."""
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
from database.models import LessonItem, User, expand_lessons
from database.repositories import LessonsLimitError, WeeklyScheduleRepository
from services.schedule_parser import (
    day_total_error, format_day_editable, format_day_quote, parse_lessons, parse_numbers, parse_week,
    remove_lessons, total_lessons,
)
from services.time_service import WEEKDAYS
from utils.formatting import capitalize_first, esc, lessons_phrase

router = Router(name="lessons")
router.message.filter(F.chat.type == ChatType.PRIVATE)

SCHOOL_DAYS = range(5)

FORMAT_HELP = (
    "Каждый предмет — с новой строки (или через запятую):\n"
    "<code>Математика — 2\nФизика\nРусский язык</code>\n"
    "Без числа — один урок. «Математика — 2» — это два урока подряд, они пронумеруются по очереди. "
    "Подойдёт и обычный дефис, и «Математика 2»."
)


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
            lines.append(format_day_quote(items))
        else:
            lines.append(f"<b>{_day_name(d)}</b> — не заполнено")
    lines.append("\n<i>Суббота и воскресенье — выходные. Выбери день, чтобы изменить его.</i>")
    return "\n".join(lines)


async def _day_text(weekly: WeeklyScheduleRepository, user_id: int, weekday: int, notice: str = "") -> tuple[str, bool]:
    items = await weekly.get_day(user_id, weekday)
    prefix = f"{notice}\n\n" if notice else ""
    if items:
        return f"{prefix}📅 <b>{_day_name(weekday)}</b> — {lessons_phrase(total_lessons(items))}:\n{format_day_quote(items)}", True
    return f"{prefix}📅 <b>{_day_name(weekday)}</b> — пока не заполнено", False


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


# ----------------------------------------------------------------- начало ввода

def _prompt(weekday: int, mode: str, items: list[LessonItem]) -> str:
    if mode == "replace":
        text = f"✏️ <b>{_day_name(weekday)}</b> — заменяю весь день.\n\n{FORMAT_HELP}"
        if items:
            text += (f"\n\nТекущее расписание — можно скопировать, поправить и отправить:\n"
                     f"<code>{esc(format_day_editable(items))}</code>")
        return text
    text = f"✏️ <b>{_day_name(weekday)}</b> — добавляю предметы в конец.\n\n{FORMAT_HELP}"
    if items:
        text += f"\n\nСейчас:\n{format_day_quote(items)}"
    return text


async def _start_input(event: CallbackQuery, state: FSMContext, weekly: WeeklyScheduleRepository,
                       user_id: int, weekday: int, mode: str, wizard: bool = False) -> None:
    items = await weekly.get_day(user_id, weekday)
    await state.set_state(LessonsInput.text)
    await state.update_data(weekday=weekday, mode=mode, origin=None, wizard=wizard, pending=[], allow_empty=False)
    await edit_or_reply(event, _prompt(weekday, mode, items), reply_markup=input_keyboard(wizard))


async def _start_week(event: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(LessonsInput.text)
    await state.update_data(weekday=None, mode="week", origin=None, wizard=False, pending=[], week_pending=None, allow_empty=False)
    await edit_or_reply(
        event,
        "📋 Вставь расписание на неделю <b>одним сообщением</b>, с названиями дней:\n"
        "<code>Понедельник\nМатематика - 2\nФизика\n\nВторник: Алгебра, Геометрия, Биология - 2</code>\n"
        "Подойдут полные названия и сокращения (Пн, Вт…). Предмет без числа — один урок. "
        "Дни, которых нет в сообщении, не изменятся; суббота и воскресенье пропускаются.",
        reply_markup=input_keyboard(False),
    )


async def _start_remove(event: CallbackQuery, state: FSMContext, weekly: WeeklyScheduleRepository,
                        user_id: int, weekday: int) -> None:
    items = await weekly.get_day(user_id, weekday)
    if not items:
        text, filled = await _day_text(weekly, user_id, weekday)
        await edit_or_reply(event, text, reply_markup=day_keyboard(weekday, filled))
        return
    await state.set_state(LessonsInput.text)
    await state.update_data(weekday=weekday, mode="remove", origin=None, wizard=False, pending=[], allow_empty=False)
    await edit_or_reply(
        event,
        f"➖ <b>{_day_name(weekday)}</b> — какой урок убрать?\n{format_day_quote(items)}\n"
        "Напиши номер или несколько: <code>3</code>, <code>2, 4</code>, <code>3-5</code>.",
        reply_markup=input_keyboard(False),
    )


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


@router.callback_query(F.data.startswith("ls:del:"))
async def begin_remove(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    weekday = _parse_weekday(callback.data)
    if weekday is None:
        await callback.answer("Такого дня нет", show_alert=True)
        return
    await callback.answer()
    await _start_remove(callback, state, weekly, user.telegram_id, weekday)


@router.callback_query(F.data == "ls:fill")
async def begin_wizard(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    await callback.answer()
    await _start_input(callback, state, weekly, user.telegram_id, 0, "replace", wizard=True)


@router.callback_query(F.data == "ls:week")
async def begin_week(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_week(callback, state)


# ----------------------------------------------------------------- разбор введённого текста

@router.message(StateFilter(LessonsInput.text), TEXT_INPUT)
async def got_lessons_text(message: Message, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    data = await state.get_data()
    mode, weekday = data.get("mode"), data.get("weekday")
    if mode is None or (mode != "week" and weekday is None):
        await state.clear()
        await message.answer("Диалог устарел. Открой «📅 Моё расписание» заново.")
        return
    if mode == "week":
        await _handle_week(message, state)
    elif mode == "remove":
        await _handle_remove(message, user, weekly, state, weekday)
    else:
        await _handle_day(message, user, weekly, state, data, weekday, mode)


async def _handle_day(message: Message, user: User, weekly: WeeklyScheduleRepository, state: FSMContext,
                      data: dict, weekday: int, mode: str) -> None:
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
            text += (f"\nПринято (пока не сохранено):\n{format_day_quote(accepted)}\n"
                     "Пришли исправленные строки — добавлю к принятым, или сохрани то, что есть.")
            await message.answer(text, reply_markup=partial_keyboard())
        else:
            await message.answer(text + "\nИсправь и пришли ещё раз.", reply_markup=input_keyboard(bool(data.get("wizard"))))
        return

    if not accepted:
        await message.answer("Не нашёл ни одного предмета. Напиши название, например «Математика» или «Математика — 2».")
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
    n = total_lessons(accepted)
    if mode == "replace":
        text = f"<b>{_day_name(weekday)}</b> будет таким — {lessons_phrase(n)}:\n{format_day_quote(accepted)}"
        verb = "Заменить"
    else:
        text = f"Добавлю в конец дня <b>{_day_name(weekday).lower()}</b> — {lessons_phrase(n)}:\n{format_day_quote(accepted)}"
        verb = "Добавить"
    await message.answer(text, reply_markup=confirm_keyboard(verb))


async def _handle_week(message: Message, state: FSMContext) -> None:
    result = parse_week(message.text or "")
    problems: list[str] = [
        (f"• строка {e.line_no} «{esc(e.text)}» — {esc(e.reason)}" if e.line_no else f"• {esc(e.reason)}")
        for e in result.errors
    ]
    for day, parsed in sorted(result.days.items()):
        for e in parsed.errors:
            where = f"строка {e.line_no} «{esc(e.text)}» — " if e.line_no else ""
            problems.append(f"• {_day_name(day)}: {where}{esc(e.reason)}")
    if problems:
        await message.answer(
            "Не всё получилось разобрать:\n" + "\n".join(problems) +
            "\n\nНичего не сохранено. Исправь и пришли сообщение с неделей ещё раз целиком.",
            reply_markup=input_keyboard(False),
        )
        return
    days = {d: r.items for d, r in result.days.items() if r.items}
    if not days:
        await message.answer("Не нашёл ни одного предмета. Нужны заголовки дней и предметы под ними.")
        return
    await state.update_data(week_pending=[[d, _items_to_data(items)] for d, items in sorted(days.items())])
    blocks = [
        f"<b>{_day_name(d)}</b> — {lessons_phrase(total_lessons(items))}\n{format_day_quote(items)}"
        for d, items in sorted(days.items())
    ]
    notes = []
    if result.empty_days:
        notes.append("Без предметов, не изменятся: " + ", ".join(_day_name(d).lower() for d in result.empty_days) + ".")
    if result.weekend_skipped:
        notes.append("Суббота и воскресенье пропущены — это выходные.")
    untouched = [d for d in SCHOOL_DAYS if d not in days]
    if untouched:
        notes.append("Не будут затронуты: " + ", ".join(_day_name(d).lower() for d in untouched) + ".")
    text = "Заменю расписание этих дней:\n\n" + "\n".join(blocks)
    if notes:
        text += "\n\n<i>" + " ".join(notes) + "</i>"
    await message.answer(text, reply_markup=confirm_keyboard(f"Заменить дни ({len(days)})"))


async def _handle_remove(message: Message, user: User, weekly: WeeklyScheduleRepository,
                         state: FSMContext, weekday: int) -> None:
    existing = await weekly.get_day(user.telegram_id, weekday)
    total = total_lessons(existing)
    numbers, error = parse_numbers(message.text or "", total)
    if error:
        await message.answer(esc(error), reply_markup=input_keyboard(False))
        return
    lessons = expand_lessons(existing)
    removed = "\n".join(f"{n}. {esc(lessons[n - 1])}" for n in numbers)
    remaining = remove_lessons(existing, numbers)
    await state.update_data(mode="replace", origin="remove", pending=_items_to_data(remaining), allow_empty=True)
    tail = (f"Останется:\n{format_day_quote(remaining)}" if remaining else "Тогда день останется пустым.")
    await message.answer(
        f"Уберу из <b>{_day_name(weekday).lower()}</b>:\n<blockquote>{removed}</blockquote>\n{tail}",
        reply_markup=confirm_keyboard("Убрать"),
    )


# ----------------------------------------------------------------- кнопки диалога

@router.callback_query(StateFilter(LessonsInput.text), F.data == "ls:redo")
async def redo_input(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.answer()
    mode = data.get("origin") or data.get("mode")
    if mode == "week":
        await _start_week(callback, state)
    elif mode == "remove":
        await _start_remove(callback, state, weekly, user.telegram_id, data["weekday"])
    else:
        await _start_input(callback, state, weekly, user.telegram_id, data["weekday"], mode, bool(data.get("wizard")))


@router.callback_query(StateFilter(LessonsInput.text), F.data == "ls:save")
async def save_pending(callback: CallbackQuery, user: User, weekly: WeeklyScheduleRepository, state: FSMContext) -> None:
    data = await state.get_data()
    mode, weekday = data.get("mode"), data.get("weekday")

    if mode == "week":
        week_pending = data.get("week_pending") or []
        if not week_pending:
            await callback.answer("Нечего сохранять", show_alert=True)
            return
        try:
            for day, raw in week_pending:
                await weekly.replace_day(user.telegram_id, int(day), _items_from_data(raw))
        except LessonsLimitError:
            await callback.answer("В каком-то дне больше 12 уроков", show_alert=True)
            return
        await state.clear()
        await callback.answer("Сохранено ✅")
        await edit_or_reply(callback, f"✅ Сохранено дней: {len(week_pending)}.\n\n" + await _overview_text(weekly, user.telegram_id),
                            reply_markup=overview_keyboard())
        return

    pending = _items_from_data(data.get("pending", []))
    if weekday is None or (not pending and not data.get("allow_empty")):
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
        f"Очистить <b>{_day_name(weekday).lower()}</b>? Будет удалено:\n{format_day_quote(items)}",
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
