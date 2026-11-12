"""Глобальный обработчик ошибок: логируем, пишем в журнал админа и вежливо сообщаем пользователю."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import ErrorEvent

from services.events import EventLog

logger = logging.getLogger(__name__)

router = Router(name="errors")


@router.errors()
async def on_error(event: ErrorEvent, events: EventLog) -> bool:
    exc = event.exception
    logger.error("Необработанная ошибка в обработчике", exc_info=exc)
    update = event.update

    kind, user_id = "update", None
    if update.message:
        kind, user_id = "message", update.message.from_user.id if update.message.from_user else None
    elif update.callback_query:
        kind, user_id = "callback", update.callback_query.from_user.id
    elif update.inline_query:
        kind, user_id = "inline", update.inline_query.from_user.id
    summary = f"{type(exc).__name__}: {str(exc)[:160]}"
    await events.log(
        "error", f"Необработанная ошибка ({kind}): {summary}", level="error", user_id=user_id,
        throttle_key=f"handler-error-{summary[:80]}", throttle_seconds=60,
    )

    try:
        if update.message:
            await update.message.answer("😔 Что-то пошло не так. Попробуй ещё раз чуть позже.")
        elif update.callback_query:
            await update.callback_query.answer("Что-то пошло не так, попробуй ещё раз", show_alert=False)
    except Exception:  # noqa: BLE001 — ответ об ошибке не должен порождать новую ошибку
        logger.debug("Не удалось сообщить пользователю об ошибке", exc_info=True)
    return True
