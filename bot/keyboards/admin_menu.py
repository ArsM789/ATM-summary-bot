"""Клавиатуры админ-панели."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.keyboards.common import button, markup
from bot.keyboards.nav import nav_row
from services.events_format import FILTERS


def admin_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button("📢 Сделать рассылку", "adm:bc")],
        [button("🛠 Сообщить о недоступности", "adm:down")],
        [button("👥 Пользователи", "adm:users")],
        [button("✉️ Сообщения и отзывы", "adm:fb")],
        [button("📜 Журнал событий", "adm:lg:all:0")],
        nav_row(),
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return markup(nav_row("adm:home"))


def cancel_keyboard() -> InlineKeyboardMarkup:
    return markup([button("❌ Отмена", "adm:cancel")])


def broadcast_preview_keyboard(broadcast_id: int, recipients: int, has_photo: bool) -> InlineKeyboardMarkup:
    """Под предпросмотром: изменить картинку / текст, отправить, отменить. «Отправить» ведёт на подтверждение."""
    image = "🖼 Сменить картинку" if has_photo else "🖼 Добавить картинку"
    return markup(
        [button(image, f"bc:img:{broadcast_id}"), button("✏️ Текст", f"bc:txt:{broadcast_id}")],
        [button(f"🚀 Отправить всем ({recipients})", f"bc:ask:{broadcast_id}", style="success")],
        [button("❌ Отменить рассылку", f"bc:cancel:{broadcast_id}", style="danger")],
    )


def broadcast_confirm_keyboard(broadcast_id: int, recipients: int, duplicate: bool = False) -> InlineKeyboardMarkup:
    label = f"⚠️ Всё равно отправить ({recipients})" if duplicate else f"✅ Да, отправить ({recipients})"
    return markup(
        [button(label, f"bc:send:{broadcast_id}", style="danger" if duplicate else "success")],
        [button("‹ Назад к предпросмотру", f"bc:back:{broadcast_id}")],
    )


def broadcast_input_keyboard(broadcast_id: int | None, has_photo: bool = False) -> InlineKeyboardMarkup:
    """Экран ввода текста/картинки для черновика: «Назад» возвращает к предпросмотру."""
    rows = []
    if broadcast_id is None:
        rows.append([button("❌ Отмена", "adm:cancel")])
    else:
        if has_photo:
            rows.append([button("🗑 Убрать картинку", f"bc:rmimg:{broadcast_id}")])
        rows.append([button("‹ Назад к предпросмотру", f"bc:back:{broadcast_id}")])
    return markup(*rows)


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
