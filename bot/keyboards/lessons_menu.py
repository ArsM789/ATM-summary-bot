"""Клавиатуры раздела «Моё расписание»."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.keyboards.common import button, markup
from services.time_service import WEEKDAYS_SHORT


def overview_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button(WEEKDAYS_SHORT[d], f"ls:day:{d}") for d in range(5)],
        [button("📝 Заполнить по дням", "ls:fill"), button("📋 Вставить неделю", "ls:week")],
    )


def day_keyboard(weekday: int, filled: bool) -> InlineKeyboardMarkup:
    rows = [[button("➕ Добавить", f"ls:add:{weekday}"), button("🔁 Заменить день", f"ls:rep:{weekday}")]]
    if filled:
        rows.append([button("➖ Убрать урок", f"ls:del:{weekday}"),
                     button("🗑 Очистить день", f"ls:clr:{weekday}", style="danger")])
    rows.append([button("‹ Назад", "ls:home")])
    return markup(*rows)


def input_keyboard(wizard: bool) -> InlineKeyboardMarkup:
    row = [button("Отмена", "ls:cancel")]
    if wizard:
        row.insert(0, button("Пропустить день", "ls:skip"))
    return markup(row)


def confirm_keyboard(verb: str) -> InlineKeyboardMarkup:
    return markup(
        [button(f"✅ {verb}", "ls:save", style="success")],
        [button("✏️ Ввести заново", "ls:redo"), button("Отмена", "ls:cancel")],
    )


def partial_keyboard() -> InlineKeyboardMarkup:
    return markup([button("💾 Сохранить принятое", "ls:save")], [button("Отмена", "ls:cancel")])


def clear_confirm_keyboard(weekday: int) -> InlineKeyboardMarkup:
    return markup(
        [button("Да, очистить", f"ls:clrok:{weekday}", style="danger"), button("Отмена", f"ls:day:{weekday}")]
    )
