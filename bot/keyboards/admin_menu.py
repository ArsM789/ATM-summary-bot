"""Клавиатуры админ-панели."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.keyboards.common import button, markup
from services.events_format import FILTERS


def admin_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button("📢 Сделать рассылку", "adm:bc")],
        [button("👥 Пользователи", "adm:users")],
        [button("✉️ Сообщения и отзывы", "adm:fb")],
        [button("📜 Журнал событий", "adm:lg:all:0")],
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


def logs_keyboard(current: str, page: int, pages: int) -> InlineKeyboardMarkup:
    filters = [
        button(("• " if code == current else "") + name, f"adm:lg:{code}:0")
        for code, (_, _, name) in FILTERS.items()
    ]
    rows = [filters[:3], filters[3:]]
    nav = []
    if page > 0:
        nav.append(button("‹ Новее", f"adm:lg:{current}:{page - 1}"))
    if page + 1 < pages:
        nav.append(button("Старее ›", f"adm:lg:{current}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([button("🔄 Обновить", f"adm:lg:{current}:{page}"), button("‹ Админка", "adm:home")])
    return markup(*rows)
