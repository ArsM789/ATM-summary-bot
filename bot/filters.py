"""Фильтры aiogram."""
from __future__ import annotations

from aiogram import F
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from bot.keyboards.main_menu import MENU_TEXTS
from config import Settings, is_admin
from database.models import User


class IsAdmin(BaseFilter):
    """Серверная проверка прав: ID отправителя должен совпасть с ADMIN_ID из .env.
    Если ADMIN_ID не задан — админ-панели нет ни у кого."""

    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        sender = event.from_user
        return is_admin(sender.id if sender else None, settings.admin_id)


class NeedsOnboarding(BaseFilter):
    """Сообщение от пользователя, чьё имя ещё не подтверждено, вне какого-либо диалога (FSM)."""

    async def __call__(self, message: Message, user: User | None = None, raw_state: str | None = None) -> bool:
        return user is not None and not user.name_confirmed and raw_state is None


# Обычный текст, введённый пользователем в диалоге: не кнопка меню и не команда.
# Благодаря этому нажатие кнопки меню посреди диалога не принимается за «имя» или «предмет».
TEXT_INPUT = F.text & ~F.text.in_(MENU_TEXTS) & ~F.text.startswith("/")
