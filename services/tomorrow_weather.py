"""Погода на завтра для inline-режима. Чистая логика: прогноз на вход -> HTML на выход."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from services.clothing_advisor import build_facts
from services.summary_generator import _PRECIP_OF_KIND, _PRECIP_WORDS
from services.time_service import format_date, weekday_name
from services.weather_models import WeatherReport, describe_code
from utils.formatting import capitalize_first, esc, fmt_span, round_half_up

_TRIGGERS = ("погод", "завтр", "weather", "tomorrow")
_WORDS = ("погода", "завтра")


@dataclass(frozen=True, slots=True)
class TomorrowForecast:
    title: str        # «Погода завтра · Казань»
    description: str  # короткая строка для списка результатов
    html: str         # текст сообщения, которое уйдёт в чат


def matches_query(query: str) -> bool:
    """Показываем результат на пустой запрос и на всё, что похоже на «погода завтра» (в т.ч. при наборе)."""
    q = (query or "").strip().lower()
    if not q:
        return True
    if any(t in q for t in _TRIGGERS):
        return True
    last = q.split()[-1]
    return len(last) >= 2 and any(w.startswith(last) for w in _WORDS)


def build_tomorrow(report: WeatherReport, now_local: datetime) -> TomorrowForecast | None:
    """None, если в прогнозе нет данных на завтра (ничего не выдумываем)."""
    tomorrow = now_local.date() + timedelta(days=1)
    day = report.day(tomorrow)
    if day is None:
        return None

    start = datetime.combine(tomorrow, time(6), tzinfo=now_local.tzinfo)
    end = datetime.combine(tomorrow, time(23), tzinfo=now_local.tzinfo)
    hours = report.hours_between(start, end)
    facts = build_facts(hours, report.current) if hours else None

    code = facts.dominant_code if facts and facts.dominant_code is not None else day.code
    sky = describe_code(code)
    sentence = capitalize_first(sky.text)
    if facts and facts.precip_kind and _PRECIP_OF_KIND.get(sky.kind) != facts.precip_kind:
        word = _PRECIP_WORDS[facts.precip_kind][0]
        sentence += f", возможна {word}" if facts.precip_kind == "thunder" else f", возможен {word}"
    sentence += "."

    city = report.city
    title = f"Погода завтра · {city}"
    lines = [
        f"<b>{esc(title)}</b>",
        f"<i>{weekday_name(tomorrow)}, {format_date(tomorrow)}</i>",
        "",
        f"{sky.emoji} {esc(sentence)}",
        "",
        f"Температура: <b>{fmt_span(day.temp_min, day.temp_max)}</b>",
    ]
    if day.apparent_min is not None and day.apparent_max is not None:
        lines.append(f"Ощущается как <b>{fmt_span(day.apparent_min, day.apparent_max)}</b>")
    prob = day.precip_prob_max
    if prob is not None:
        if prob < 5:
            lines.append("Осадки: <b>не ожидаются</b>")
        else:
            extra = ""
            if day.precip_sum >= 0.5:
                extra = f", около {day.precip_sum:.1f} мм".replace(".", ",")
            elif day.snowfall_sum >= 0.5:
                extra = f", около {day.snowfall_sum:.1f} см снега".replace(".", ",")
            lines.append(f"Осадки: до <b>{round_half_up(prob)}%</b>{extra}")

    description = f"{sentence.rstrip('.')} · {fmt_span(day.temp_min, day.temp_max)}"
    return TomorrowForecast(title=title, description=description, html="\n".join(lines))
