"""Оформление обращения пользователя для администратора (чистая функция)."""
from __future__ import annotations

from datetime import datetime

from database.models import User
from utils.formatting import esc


def format_feedback_for_admin(user: User, text: str, when: datetime) -> str:
    """HTML-текст обращения. Имя — из БД, при отсутствии — first_name из Telegram."""
    name = user.name or user.first_name or "—"
    username = f"@{esc(user.username)}" if user.username else "—"
    return (
        "<b>Новое обращение в ATM</b>\n\n"
        "Тип: Обратная связь\n\n"
        f"Пользователь: {esc(name)}\n"
        f"Username: {username}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n\n"
        f"Сообщение:\n«{esc(text)}»\n\n"
        f"Дата и время: {when.strftime('%d.%m.%Y %H:%M')}"
    )
