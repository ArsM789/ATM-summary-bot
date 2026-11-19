
"""Страховочные обработчики. Подключаются последними."""
from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji

from bot.keyboards.main_menu import main_menu
from database.models import User
from utils.formatting import esc

router = Router(name="fallback")


@router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    """Кнопка устарела (диалог завершён, бот перезапускался) или недоступна этому пользователю."""
    await callback.answer("Эта кнопка уже неактуальна — открой нужный раздел заново.", show_alert=False)


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text,
    StateFilter(None),
)
async def greet_user(message: Message, user: User) -> None:
    """Отвечает на простое приветствие пользователя."""

    # Нормализуем текст: убираем пунктуацию и приводим к нижнему регистру.
    text = re.sub(r"[^\w\s]", " ", message.text.casefold())
    text = " ".join(text.split())

    if text != "привет как дела":
        return

    # Отправляем персональный ответ.
    await message.answer(
        f"Привет, {esc(user.name)}, у меня всё хорошо)"
    )

    # Ставим сердечко на исходное сообщение.
    await message.bot.set_message_reaction(
        chat_id=message.chat.id,
        message_id=message.message_id,
        reaction=[ReactionTypeEmoji(emoji="❤️")],
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text, StateFilter(None))
async def unknown_text(message: Message) -> None:
    await message.answer(
        "Не совсем понял 🙂 Выбери действие в меню ниже.",
        reply_markup=main_menu(),
    )
