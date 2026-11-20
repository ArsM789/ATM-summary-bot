"""Middleware: проверяет бан и передаёт данные пользователя обработчикам."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineQuery, Message, TelegramObject

from database.repositories import UserRepository

BAN_MESSAGE = (
    "Упс!\n"
    "⛔ Доступ к Deylo ограничен.\n\n"
    "Если вы считаете, что это ошибка, свяжитесь с админом бота.\n\n"
    "@rmdurov or @XzM90"
)


class UserMiddleware(BaseMiddleware):
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
            if await self._users.is_banned(tg_user.id):
                if isinstance(event, Message):
                    await event.answer(BAN_MESSAGE)
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        "Доступ к Deylo ограничен.",
                        show_alert=True,
                    )
                elif isinstance(event, InlineQuery):
                    await event.answer([], cache_time=0)

                return None

            data["user"] = await self._users.get_or_create(
                tg_user.id,
                tg_user.first_name,
                tg_user.username,
            )

        return await handler(event, data)