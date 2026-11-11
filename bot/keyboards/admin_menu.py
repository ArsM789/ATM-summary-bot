"""Клавиатуры админ-панели."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.keyboards.common import button, markup


def admin_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button("📢 Сделать рассылку", "adm:bc")],
        [button("👥 Пользователи", "adm:users")],
        [button("✉️ Сообщения и отзывы", "adm:fb")],
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return markup([button("‹ Назад", "adm:home")])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return markup([button("Отмена", "adm:cancel")])


def broadcast_confirm_keyboard(broadcast_id: int, recipients: int) -> InlineKeyboardMarkup:
    return markup(
        [button(f"Отправить всем ({recipients})", f"bc:send:{broadcast_id}", style="success")],
        [button("Отменить", f"bc:cancel:{broadcast_id}", style="danger")],
    )
