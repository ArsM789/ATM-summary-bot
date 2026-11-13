"""Клавиатуры раздела «📅 Мои события»."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.keyboards.common import button, markup
from database.models import UserEvent
from services.event_text import fields_from_event, format_remind, short_label
from services.time_service import WEEKDAYS_SHORT

PAGE_SIZE = 6
REMIND_CHOICES = (5, 10, 15, 20, 30, 60, 120)


def events_menu_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button("➕ Добавить событие", "ev:add", style="primary")],
        [button("📋 Мои события", "ev:list:0")],
        [button("⚙️ Настройки уведомлений", "ev:set")],
        [button("‹ В главное меню", "ev:exit")],
    )


def list_keyboard(events: list[UserEvent], page: int) -> InlineKeyboardMarkup:
    pages = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = [
        [button(short_label(fields_from_event(e), e.status), f"ev:o:{e.id}:{page}")]
        for e in events[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    ]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(button("‹", f"ev:list:{page - 1}"))
        nav.append(button(f"{page + 1}/{pages}", f"ev:list:{page}"))
        if page + 1 < pages:
            nav.append(button("›", f"ev:list:{page + 1}"))
        rows.append(nav)
    rows.append([button("➕ Добавить", "ev:add"), button("‹ Назад", "ev:menu")])
    return markup(*rows)


def card_keyboard(event: UserEvent, page: int) -> InlineKeyboardMarkup:
    toggle = (button("⏸ Отключить", f"ev:off:{event.id}") if event.status == "active"
              else button("▶️ Включить", f"ev:on:{event.id}"))
    return markup(
        [toggle, button("✏️ Изменить", f"ev:edit:{event.id}")],
        [button("🗑 Удалить", f"ev:del:{event.id}", style="danger")],
        [button("‹ К списку", f"ev:list:{page}")],
    )


def delete_confirm_keyboard(event_id: int, page: int = 0) -> InlineKeyboardMarkup:
    return markup([button("Да, удалить", f"ev:delok:{event_id}", style="danger"),
                   button("Отмена", f"ev:o:{event_id}:{page}")])


# ----------------------------------------------------------------- диалог создания / изменения

def cancel_row():
    return [button("❌ Отмена", "ef:cancel")]


def cancel_keyboard() -> InlineKeyboardMarkup:
    return markup(cancel_row())


def repeat_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button("Без повторения", "ef:rep:none")],
        [button("Каждый день", "ef:rep:daily"), button("Каждую неделю", "ef:rep:weekly")],
        [button("Каждый месяц", "ef:rep:monthly")],
        cancel_row(),
    )


def weekdays_keyboard(mask: int) -> InlineKeyboardMarkup:
    days = [button(("✅ " if mask & (1 << d) else "") + WEEKDAYS_SHORT[d], f"ef:wd:{d}") for d in range(7)]
    return markup(days[:4], days[4:], [button("Готово", "ef:wddone", style="success")], cancel_row())


def date_keyboard() -> InlineKeyboardMarkup:
    return markup([button("Сегодня", "ef:date:today"), button("Завтра", "ef:date:tomorrow")], cancel_row())


def month_day_keyboard() -> InlineKeyboardMarkup:
    days = [button(str(d), f"ef:md:{d}") for d in (1, 5, 10, 15, 20, 25)]
    return markup(days[:3], days[3:], [button("Последний день месяца", "ef:md:last")], cancel_row())


def duration_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button("30 мин", "ef:dur:30"), button("1 час", "ef:dur:60"), button("1,5 часа", "ef:dur:90")],
        [button("2 часа", "ef:dur:120"), button("3 часа", "ef:dur:180")],
        cancel_row(),
    )


def remind_keyboard(current: int | None = None) -> InlineKeyboardMarkup:
    def label(m: int) -> str:
        text = format_remind(m).replace("За ", "")
        return ("✓ " if m == current else "") + text

    buttons = [button(label(m), f"ef:rem:{m}") for m in REMIND_CHOICES]
    return markup(buttons[:3], buttons[3:6], [buttons[6], button("Свой вариант", "ef:remc")], cancel_row())


def preview_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button("✅ Сохранить", "ef:save", style="success")],
        [button("✏️ Изменить", "ef:edit"), button("❌ Отмена", "ef:cancel")],
    )


def edit_fields_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button("Название", "ef:f:title"), button("Когда", "ef:f:when")],
        [button("Время", "ef:f:time"), button("Длительность", "ef:f:dur")],
        [button("Уведомление", "ef:f:rem")],
        [button("‹ К карточке", "ef:preview")],
    )


# ----------------------------------------------------------------- настройки уведомлений

def event_settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle = "🔕 Выключить напоминания" if enabled else "🔔 Включить напоминания"
    return markup(
        [button(toggle, "es:tog")],
        [button("⏱ Время по умолчанию", "es:def")],
        [button("‹ Назад", "ev:menu")],
    )


def default_remind_keyboard(current: int) -> InlineKeyboardMarkup:
    buttons = [button(("✓ " if m == current else "") + format_remind(m).replace("За ", ""), f"es:set:{m}")
               for m in REMIND_CHOICES]
    return markup(buttons[:3], buttons[3:6], [buttons[6]], [button("‹ Назад", "ev:set")])
