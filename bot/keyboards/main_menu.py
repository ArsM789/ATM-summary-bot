"""Главное меню (постоянная reply-клавиатура) и кнопка под сводкой."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.keyboards.common import button

BTN_SUMMARY = "🌤 Сводка"
BTN_LESSONS = "📅 Моё расписание"
BTN_SETTINGS = "⚙️ Настройки"
BTN_FEEDBACK = "💬 Сообщить об ошибке / предложить улучшение"
# Старая кнопка из прошлой версии: у существующих пользователей она может оставаться на клавиатуре.
BTN_LEGACY_SCHEDULE = "📅 Расписание"

MENU_TEXTS = frozenset({BTN_SUMMARY, BTN_LESSONS, BTN_SETTINGS, BTN_FEEDBACK, BTN_LEGACY_SCHEDULE})


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SUMMARY)],
            [KeyboardButton(text=BTN_LESSONS), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_FEEDBACK)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие",
    )


def summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button("🔄 Обновить", "summary:refresh", style="primary")]])
