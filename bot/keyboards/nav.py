"""Навигация, общая для всех экранов: «Назад», «В главное меню», главное меню-хаб и тур по возможностям."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.common import button, markup
from bot.texts import TOUR_PAGES

HOME = "nav:home"


def nav_row(back: str | None = None, *, home: bool = True) -> list[InlineKeyboardButton]:
    """Строка «‹ Назад · 🏠 В меню». back — callback_data экрана, на который ведёт «Назад»."""
    row: list[InlineKeyboardButton] = []
    if back:
        row.append(button("‹ Назад", back))
    if home:
        row.append(button("🏠 В меню", HOME))
    return row


def with_nav(keyboard: InlineKeyboardMarkup | None, back: str | None = None, *, home: bool = True) -> InlineKeyboardMarkup:
    """Добавляет к клавиатуре строку навигации (для экранов, где нет собственных «Назад / В меню»)."""
    rows = [list(r) for r in (keyboard.inline_keyboard if keyboard else [])]
    row = nav_row(back, home=home)
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hub_keyboard() -> InlineKeyboardMarkup:
    """Главное меню в виде inline-кнопок: открывается на месте, без новых сообщений."""
    return markup(
        [button("🌤 Сводка", "nav:sum", style="primary")],
        [button("📅 Моё расписание", "nav:les"), button("📅 Мои события", "nav:evt")],
        [button("⚙️ Настройки", "nav:set")],
        [button("💬 Сообщить об ошибке", "nav:fb"), button("📖 Как пользоваться", "tour:0")],
    )


def tour_keyboard(page: int) -> InlineKeyboardMarkup:
    last = len(TOUR_PAGES) - 1
    page = max(0, min(page, last))
    if page == last:
        return markup([button("✅ Понятно, начнём", "tour:done", style="success")], [button("‹ Назад", f"tour:{page - 1}")])
    row = []
    if page > 0:
        row.append(button("‹ Назад", f"tour:{page - 1}"))
    row.append(button("Далее ›", f"tour:{page + 1}", style="primary"))
    return markup(row, [button("Пропустить", "tour:done")])


def home_keyboard() -> InlineKeyboardMarkup:
    """Экран-итог: единственная кнопка — «🏠 В меню»."""
    return markup(nav_row())
