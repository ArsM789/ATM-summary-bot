"""Мелкие помощники хендлеров. Все «ответы пользователю» идут через bot/ui.py: редактирование живого экрана
вместо новых сообщений, удаление ввода пользователя, чистка устаревших кнопок."""
from __future__ import annotations

from typing import Any

from aiogram.types import CallbackQuery, Message

from bot.ui import ui
from utils.formatting import MAX_TEXT, clip  # noqa: F401  (реэкспорт для обратной совместимости)


async def reply(event: Message | CallbackQuery, text: str, *, reply_markup: Any = None, fresh: bool = False) -> None:
    """Показывает экран: нажатие кнопки -> правим то же сообщение; ввод текста -> правим экран и удаляем ввод;
    fresh=True (вход из меню/команды) -> убираем прошлый экран и присылаем новый внизу чата."""
    await ui.show(event, text, reply_markup, fresh=fresh)


async def edit_or_reply(event: CallbackQuery | Message, text: str, *, reply_markup: Any = None) -> None:
    """Совместимость: то же, что reply, для обработчиков кнопок."""
    await ui.show(event, text, reply_markup)
