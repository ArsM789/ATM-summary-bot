"""Форматирование текста для Telegram (HTML) и русская грамматика."""
from __future__ import annotations

import html
import math
from collections.abc import Iterable, Mapping

MINUS = "\u2212"  # типографский минус «−», а не дефис
DIVIDER = "┄" * 14

# Запасные Unicode-эмодзи. Premium Custom Emoji подключаются только через CUSTOM_EMOJI_IDS.
UNICODE_EMOJI: dict[str, str] = {
    "sun": "☀️",
    "sunrise": "🌅",
    "moon": "🌙",
    "calendar": "🗓",
    "thermo": "🌡",
    "jacket": "🧥",
    "books": "📚",
    "cloud": "☁️",
    "wind": "💨",
    "rain": "🌧",
    "party": "🎉",
    "gear": "⚙️",
    "clock": "⏰",
}


class EmojiPack:
    """Возвращает эмодзи по ключу: Premium Custom Emoji, если id задан, иначе обычный Unicode.

    Custom emoji в Bot API отправляются тегом <tg-emoji emoji-id="...">. Показываются они
    только если владелец бота имеет Telegram Premium (или бот купил доп. юзернеймы на Fragment);
    иначе клиент просто покажет запасной Unicode-символ внутри тега — ничего не ломается.
    """

    def __init__(self, custom_ids: Mapping[str, str] | None = None) -> None:
        self._custom = {k: v for k, v in (custom_ids or {}).items() if k in UNICODE_EMOJI and v.isdigit()}

    def __call__(self, key: str) -> str:
        base = UNICODE_EMOJI[key]
        custom_id = self._custom.get(key)
        if custom_id:
            return f'<tg-emoji emoji-id="{custom_id}">{base}</tg-emoji>'
        return base


def esc(text: object) -> str:
    """Экранирование пользовательских данных для parse_mode=HTML."""
    return html.escape(str(text), quote=False)


def round_half_up(value: float) -> int:
    """Школьное округление (round() в Python округляет .5 к чётному)."""
    return math.floor(value + 0.5)


def fmt_temp(value: float | None, unit: str = "°") -> str:
    """−3 -> «−3°», 2 -> «+2°», 0.3 -> «0°»."""
    if value is None:
        return "—"
    rounded = round_half_up(value)
    if rounded == 0:
        sign_value = "0"
    elif rounded > 0:
        sign_value = f"+{rounded}"
    else:
        sign_value = f"{MINUS}{abs(rounded)}"
    return f"{sign_value}{unit}"


def fmt_range(low: float, high: float, unit: str = "°C") -> str:
    """«от −3 до +2°C» (или «около +2°C», если границы совпали после округления)."""
    if round_half_up(low) == round_half_up(high):
        return f"около {fmt_temp(high, unit)}"
    return f"от {fmt_temp(low, '')} до {fmt_temp(high, unit)}"


def fmt_speed(ms: float) -> str:
    return f"{round_half_up(ms)} м/с"


def join_ru(items: Iterable[str]) -> str:
    """['а', 'б', 'в'] -> «а, б и в»."""
    parts = [item for item in items if item]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " и " + parts[-1]


def plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n)
    if 11 <= n_abs % 100 <= 14:
        return many
    last = n_abs % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def lessons_phrase(n: int) -> str:
    return f"{n} {plural(n, 'урок', 'урока', 'уроков')}"


def capitalize_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def fmt_span(low: float, high: float) -> str:
    """«+8…+11 °C» (или «+8 °C», если границы совпали после округления)."""
    if round_half_up(low) == round_half_up(high):
        return f"{fmt_temp(high, '')} °C"
    return f"{fmt_temp(low, '')}…{fmt_temp(high, '')} °C"
