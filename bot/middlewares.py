"""Middleware: гарантирует, что пользователь есть в БД, и кладёт его в данные хендлера."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from database.repositories import UserRepository


class UserMiddleware(BaseMiddleware):
    """Каждый хендлер получает `user` — запись именно того, кто написал боту.
    Все операции дальше идут по user.telegram_id из самого Telegram-апдейта, поэтому
    управлять чужими настройками невозможно."""

    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is not None and not tg_user.is_bot:
            data["user"] = await self._users.get_or_create(tg_user.id, tg_user.first_name)
        return await handler(event, data)
