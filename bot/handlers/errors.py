"""Глобальный обработчик ошибок: логируем и вежливо сообщаем пользователю."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)

router = Router(name="errors")


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    logger.error("Необработанная ошибка в обработчике", exc_info=event.exception)
    update = event.update
    try:
        if update.message:
            await update.message.answer("😔 Что-то пошло не так. Попробуй ещё раз чуть позже.")
        elif update.callback_query:
            await update.callback_query.answer("Что-то пошло не так, попробуй ещё раз", show_alert=False)
    except Exception:  # noqa: BLE001 — ответ об ошибке не должен порождать новую ошибку
        logger.debug("Не удалось сообщить пользователю об ошибке", exc_info=True)
    return True
