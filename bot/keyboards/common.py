"""Общие помощники для клавиатур."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Bot API 9.4 добавил цвет кнопки (style: primary / success / danger). Старые клиенты
# показывают такие кнопки обычными, так что это безопасное украшение.
# Проверяем поддержку на стороне aiogram, чтобы не упасть на старой версии библиотеки.
_STYLE_SUPPORTED = "style" in InlineKeyboardButton.model_fields


def button(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    if style and _STYLE_SUPPORTED:
        return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def markup(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])
