"""Клавиатуры настроек и времени автосводок."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from bot.keyboards.common import button, markup
from database.models import ScheduleEntry


def settings_keyboard(notifications_enabled: bool) -> InlineKeyboardMarkup:
    toggle = "🔕 Выключить автосводки" if notifications_enabled else "🔔 Включить автосводки"
    return markup(
        [button("👤 Имя и обращения", "st:name")],
        [button("🏙 Город", "st:city"), button("🕒 Часовой пояс", "st:tz")],
        [button("⏰ Время сводок", "st:times"), button(toggle, "st:notif")],
        [button("🧪 Тестовая сводка", "st:test")],
    )


def cancel_keyboard(data: str = "st:cancel") -> InlineKeyboardMarkup:
    return markup([button("Отмена", data)])


def cities_keyboard(labels: list[str]) -> InlineKeyboardMarkup:
    rows = [[button(label, f"ct:{i}")] for i, label in enumerate(labels)]
    rows.append([button("Отмена", "st:cancel")])
    return markup(*rows)


def times_keyboard(entries: list[ScheduleEntry]) -> InlineKeyboardMarkup:
    rows = [[button(f"{'✅' if e.enabled else '⏸'} {e.send_time}", f"tm:open:{e.id}")] for e in entries]
    rows.append([button("➕ Добавить время", "tm:add")])
    rows.append([button("‹ Назад", "st:home")])
    return markup(*rows)


def time_entry_keyboard(entry: ScheduleEntry) -> InlineKeyboardMarkup:
    toggle = "⏸ Отключить" if entry.enabled else "▶️ Включить"
    return markup(
        [button(toggle, f"tm:tog:{entry.id}"), button("✏️ Изменить время", f"tm:edit:{entry.id}")],
        [button("🗑 Удалить", f"tm:del:{entry.id}", style="danger")],
        [button("‹ К списку", "st:times")],
    )


def time_delete_confirm_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return markup(
        [button("Да, удалить", f"tm:delok:{entry_id}", style="danger"), button("Отмена", f"tm:open:{entry_id}")]
    )
