"""Страховочные обработчики. Подключаются последними."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from bot.keyboards.main_menu import main_menu

router = Router(name="fallback")


@router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    """Кнопка устарела (диалог завершён, бот перезапускался) или недоступна этому пользователю."""
    await callback.answer("Эта кнопка уже неактуальна — открой нужный раздел заново.", show_alert=False)


@router.message(F.chat.type == ChatType.PRIVATE, F.text, StateFilter(None))
async def unknown_text(message: Message) -> None:
    await message.answer("Не совсем понял 🙂 Выбери действие в меню ниже.", reply_markup=main_menu())
