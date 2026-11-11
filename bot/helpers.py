"""Мелкие помощники хендлеров."""
from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)

MAX_TEXT = 3900  # запас до лимита Telegram в 4096 символов


def clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def reply(event: Message | CallbackQuery, text: str, **kwargs: Any) -> None:
    """Новое сообщение в личный чат того, кто совершил действие (и для сообщения, и для кнопки)."""
    if isinstance(event, Message):
        await event.answer(clip(text), **kwargs)
    else:
        await event.bot.send_message(event.from_user.id, clip(text), **kwargs)


async def edit_or_reply(event: CallbackQuery, text: str, **kwargs: Any) -> None:
    """Редактирует сообщение с кнопкой; если нельзя (старое/недоступное) — присылает новое."""
    message = event.message
    if isinstance(message, Message):
        try:
            await message.edit_text(clip(text), **kwargs)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
            logger.debug("edit_text не удался (%s), отправляю новое сообщение", exc)
    await event.bot.send_message(event.from_user.id, clip(text), **kwargs)
