"""Главное меню (постоянная reply-клавиатура) и кнопка под сводкой."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.keyboards.common import button

BTN_SUMMARY = "🌤 Сводка"
BTN_SETTINGS = "⚙️ Настройки"
BTN_SCHEDULE = "📅 Расписание"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SUMMARY)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_SCHEDULE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие",
    )


def summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button("🔄 Обновить", "summary:refresh", style="primary")]])
