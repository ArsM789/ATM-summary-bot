"""Оформление журнала событий для админ-панели (чистые функции)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from database.models import EventRecord
from utils.formatting import esc

ICONS = {
    "user_new": "🆕", "user_blocked": "🚫", "error": "🔴", "delay": "⏱", "weather": "☁️",
    "delivery": "📤", "broadcast": "📢", "feedback": "💬", "system": "⚙️",
}

# код фильтра -> (категории, уровни, название кнопки)
FILTERS: dict[str, tuple[tuple[str, ...] | None, tuple[str, ...] | None, str]] = {
    "all": (None, None, "Все"),
    "usr": (("user_new", "user_blocked"), None, "🆕 Люди"),
    "err": (None, ("error",), "🔴 Ошибки"),
    "dly": (("delay", "weather"), None, "⏱ Задержки"),
    "dlv": (("delivery", "broadcast", "feedback"), None, "📤 Доставка"),
}
PAGE_SIZE = 10


def format_event(record: EventRecord, tz: ZoneInfo) -> str:
    icon = "🔴" if record.level == "error" else ICONS.get(record.category, "•")
    when = datetime.fromtimestamp(record.ts, tz).strftime("%d.%m %H:%M")
    user = f" · <code>{record.user_id}</code>" if record.user_id else ""
    return f"{icon} <code>{when}</code> {esc(record.message[:220])}{user}"


def format_events_page(records: list[EventRecord], tz: ZoneInfo, title: str, total: int, page: int) -> str:
    if not records:
        return f"📜 <b>Журнал — {esc(title)}</b>\n\nПока пусто."
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"📜 <b>Журнал — {esc(title)}</b> <i>(стр. {page + 1} из {pages}, всего {total})</i>\n"]
    lines.extend(format_event(r, tz) for r in records)
    return "\n".join(lines)
