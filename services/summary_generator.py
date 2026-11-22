"""Генератор персональных сводок.

Чистая логика без сети и БД: на вход — пользователь, расписание, время и прогноз,
на выход — готовый HTML для Telegram. Фразы собираются из вариантов (приветствие, погода,
одежда, уроки, концовка) и комбинируются так, чтобы части не противоречили друг другу:
и описание погоды, и советы по одежде строятся от одних и тех же «фактов» и одного
теплового уровня.
"""
from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from database.models import LessonItem, User
from services.clothing_advisor import (
    CORE, EXTRAS, FEET, HANDS, HEAD, LEGS, NECK, OUTER,
    ClothingAdvice, Thermal, WeatherFacts, advise, build_facts,
)
from services.time_service import DayPart, WEEKDAYS_ACC, day_part, format_date, weekday_name
from services.weather_models import WeatherReport, describe_code
from services.schedule_parser import format_day_quote, total_lessons
from utils.formatting import (
    EmojiPack, capitalize_first, esc, fmt_span, fmt_speed, fmt_temp,
    join_ru, lessons_phrase, round_half_up,
)

_PRECIP_OF_KIND = {"drizzle": "rain", "rain": "rain", "snow": "snow", "freezing": "freezing", "thunder": "thunder"}
_PRECIP_WORDS = {
    "rain": ("дождь", "дождя"),
    "snow": ("снег", "снега"),
    "freezing": ("ледяной дождь", "ледяного дождя"),
    "mixed": ("дождь и снег", "дождя и снега"),
    "thunder": ("гроза", "грозы"),
}


@dataclass(frozen=True)
class NextEvent:
    """Ближайшая задача пользователя (из «Мои события»): название и локальное время начала."""

    title: str
    start: datetime


@dataclass(frozen=True)
class SummaryContext:
    user: User
    schedule: Mapping[int, Sequence[LessonItem]]  # расписание ЭТОГО пользователя: 0 = понедельник ... 4 = пятница
    now: datetime                                 # aware, в часовом поясе пользователя
    report: WeatherReport | None                  # None — погоду получить не удалось
    address: str | None = None                    # выбранный вариант обращения (None -> основное имя)
    next_event: NextEvent | None = None           # ближайшая задача на сегодня (показывается утром и днём)


@dataclass(frozen=True)
class Focus:
    """О каком дне и каком окне времени говорит сводка."""

    part: DayPart
    focus_date: date
    is_tomorrow: bool
    window_start: datetime
    window_end: datetime


def _at(d: date, hour: int, like: datetime) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=like.tzinfo)


def resolve_focus(now: datetime) -> Focus:
    part = day_part(now)
    today = now.date()
    midnight = _at(today + timedelta(days=1), 0, now)
    hour_start = now.replace(minute=0, second=0, microsecond=0)

    if part is DayPart.MORNING:
        return Focus(part, today, False, hour_start, min(hour_start + timedelta(hours=10), midnight))
    if part is DayPart.DAY:
        return Focus(part, today, False, hour_start, min(hour_start + timedelta(hours=7), midnight))
    if now.hour < 5:  # глубокая ночь: «завтра» — это уже сегодняшний день
        return Focus(part, today, False, _at(today, 7, now), _at(today, 17, now))
    tomorrow = today + timedelta(days=1)
    return Focus(part, tomorrow, True, _at(tomorrow, 7, now), _at(tomorrow, 17, now))


class SummaryGenerator:
    def __init__(self, emoji: EmojiPack | None = None, rng: random.Random | None = None) -> None:
        self._emo = emoji or EmojiPack()
        self._rng = rng or random.Random()
        self._last: dict[tuple[int, str], int] = {}  # чтобы не повторять один вариант подряд

    # ----------------------------- публичный API -----------------------------

    def generate(self, ctx: SummaryContext) -> str:
        """Компактная сводка: приветствие, дата, «карточка» погоды и одежды, расписание, ближайшая задача, концовка.
        Утром и днём — про сегодня; вечером (с 17:00) — про завтра."""
        focus = resolve_focus(ctx.now)
        uid = ctx.user.telegram_id
        name = esc(ctx.address or ctx.user.name)

        greeting = self._greeting(uid, focus.part, name)
        name_used = name in greeting  # имя в сообщении появляется один раз, а не в каждой фразе

        day = focus.focus_date
        date_line = f"{capitalize_first(weekday_name(day))}, {format_date(day)}"
        if focus.is_tomorrow:
            date_line = f"Завтра: {weekday_name(day)}, {format_date(day)}"
        blocks: list[str] = [f"<b>{greeting}</b>", f"{self._emo('calendar')} {date_line} · {esc(ctx.user.city)}"]

        if ctx.report is not None:
            advice = advise(self._facts(ctx.report, focus))
            blocks.append(self._weather_card(uid, ctx, focus, advice))
        else:
            blocks.append(f"{self._emo('cloud')} Погода сейчас недоступна — не буду гадать. Загляни в «Сводку» чуть позже.")

        lessons_block, mode, total = self._lessons_block(uid, focus, ctx.schedule)
        blocks.append(lessons_block)
        if ctx.next_event is not None and not focus.is_tomorrow:
            blocks.append(self._event_block(ctx.next_event))
        blocks.append(f"<i>{self._closing(uid, focus, mode, total, None if name_used else name)}</i>")
        return "\n\n".join(blocks)

    # ----------------------------- выбор вариантов -----------------------------

    def _pick(self, uid: int, key: str, options: Sequence[str]) -> str:
        """Случайный вариант, не совпадающий с прошлым для этого пользователя и ключа."""
        if len(options) == 1:
            return options[0]
        last = self._last.get((uid, key))
        indices = [i for i in range(len(options)) if i != last]
        choice = self._rng.choice(indices)
        self._last[(uid, key)] = choice
        return options[choice]

    # ----------------------------- приветствие -----------------------------

    def _greeting(self, uid: int, part: DayPart, name: str) -> str:
        e = self._emo
        options = {
            DayPart.MORNING: [
                f"{name}, доброе утро! {e('sun')}",
                f"С добрым утром, {name}) {e('sun')}",
                f"Ну что, {name}, пора начинать новый день! {e('sunrise')}",
                f"Привет, {name}! Чувствую, что сегодня будет не забываемый день)",
            ],
            DayPart.DAY: [
                f"{name}, добрый день!",
                f"Привет, {name}! Как проходит день?",
                f"{name}, привет! Небольшая сводка на остаток дня",
            ],
            DayPart.EVENING: [
                f"{name}, добрый вечер!",
                f"Привет, {name}! {e('moon')}",
                f"Вечер добрый, {name}! {e('moon')}",
            ],
            DayPart.NIGHT: [
                f"{name}, доброй ночи! {e('moon')}",
                f"Привет, {name}! Уже поздно, но вот что ждёт дальше {e('moon')}",
            ],
        }[part]
        return self._pick(uid, f"greet:{part.value}", options)

    # ----------------------------- данные о погоде -----------------------------

    @staticmethod
    def _facts(report: WeatherReport, focus: Focus) -> WeatherFacts:
        hours = report.hours_between(focus.window_start, focus.window_end)
        return build_facts(hours, report.current)

    @staticmethod
    def _day_word(focus: Focus) -> str:
        return "Завтра" if focus.is_tomorrow else "Сегодня"

    def _weather_card(self, uid: int, ctx: SummaryContext, focus: Focus, advice: ClothingAdvice) -> str:
        """Погода и одежда одним блоком-цитатой, по строке на смысл:
            ☁️ Пасмурно, −3…+2 °C
            сейчас +1° (ощущ. −2°) · 💨 6 м/с · ☔ дождь до 40%
            👕 Надеть: тёплую куртку, шапку, перчатки
            🎒 Взять: зонт
            ⚠️ Возможна наледь …"""
        facts = advice.facts
        report = ctx.report
        assert report is not None
        sky = describe_code(facts.dominant_code)
        forecast = report.day(focus.focus_date)

        head = capitalize_first(sky.text)
        if forecast is not None:
            head += f", {fmt_span(forecast.temp_min, forecast.temp_max)}"
        lines = [f"{sky.emoji} <b>{head}</b>"]

        details: list[str] = []
        if not focus.is_tomorrow:
            cur = report.current
            now_part = f"сейчас {fmt_temp(cur.temp)}"
            if abs(cur.apparent - cur.temp) >= 2:
                now_part += f" (ощущ. {fmt_temp(cur.apparent)})"
            details.append(now_part)
        if facts.hours_count and facts.max_wind >= 6:
            wind = f"{self._emo('wind')} {fmt_speed(facts.max_wind)}"
            if facts.max_gust - facts.max_wind >= 4:
                wind += f" (порывы {fmt_speed(facts.max_gust)})"
            details.append(wind)
        precip, prob = facts.precip_kind, facts.max_precip_prob
        icon = "❄️" if precip in ("snow", "mixed") else "☔"
        if precip:
            already_in_headline = _PRECIP_OF_KIND.get(sky.kind) == precip      # «Снег, …» уже сказано заголовком
            word = "" if already_in_headline else f"{_PRECIP_WORDS[precip][0]} "
            details.append(f"{icon} {word}до {round_half_up(prob)}%" if prob is not None else f"{icon} {word}".strip())
        elif prob is not None and prob >= 20:
            details.append(f"☔ до {round_half_up(prob)}%")
        if details:
            lines.append(" · ".join(details))

        lines.extend(self._clothing_lines(uid, advice))
        return "<blockquote>" + "\n".join(lines) + "</blockquote>"

    def _clothing_lines(self, uid: int, advice: ClothingAdvice) -> list[str]:
        """Одежда короткими строками: что надеть (всё нужное в одной строке), что взять, по желанию, одно предупреждение."""
        o = advice.outfit
        wear = self._short_wear(o, advice.level)
        take = o.texts((EXTRAS,), optional=False)
        maybe = o.texts((OUTER, CORE, LEGS, FEET, HEAD, HANDS, NECK), optional=True)
        maybe_take = o.texts((EXTRAS,), optional=True)

        lines: list[str] = []
        if wear:
            label = self._pick(uid, "wear_label", ["Надеть", "Лучше надеть"])
            lines.append(f"{self._emo('jacket')} {label}: <b>{join_ru(wear[:4])}</b>")
        if take:
            lines.append(f"🎒 Взять: <b>{join_ru(take)}</b>")
        extra: list[str] = []
        if maybe:
            extra.append("можно добавить: " + join_ru(maybe[:2]))
        if maybe_take:
            extra.append("на всякий случай взять " + join_ru(maybe_take))
        if extra:
            lines.append("<i>" + "; ".join(extra) + "</i>")
        if o.warnings:
            lines.append(f"⚠️ {capitalize_first(o.warnings[0])}")
        if not lines:
            lines.append(f"{self._emo('jacket')} Одевайся по привычке")
        return lines

    @staticmethod
    def _short_wear(o, level: Thermal) -> list[str]:
        """Только главное: верхняя одежда, аксессуары и «особая» обувь. Базовые вещи под курткой (кофта, штаны)
        перечислять незачем; в тёплую погоду, когда куртки нет, — сама одежда (футболка, шорты)."""
        items = list(o.texts((OUTER,), optional=False))
        if items:
            items += [it.text for key, it in o.slots[CORE].items() if key == "base" and not it.optional]  # термобельё
        else:
            items += o.texts((CORE,), optional=False)
        if level >= Thermal.WARM:
            items += o.texts((LEGS,), optional=False)
        items += [t for t in o.texts((FEET,), optional=False) if any(w in t for w in ("непромокаем", "зимн", "утепл"))]
        items += o.texts((HEAD, HANDS, NECK), optional=False)
        return items

    @staticmethod
    def _event_block(event: NextEvent) -> str:
        """Ближайшая задача: название и время начала (сразу после расписания уроков)."""
        return f"⏰ Ближайшая задача: <b>{esc(event.title)}</b> — {event.start:%H:%M}"

    # ----------------------------- уроки -----------------------------

    def _lessons_block(
        self, uid: int, focus: Focus, schedule: Mapping[int, Sequence[LessonItem]]
    ) -> tuple[str, str, int]:
        """(текст блока, режим дня: school|weekend|empty, всего уроков)."""
        when = self._day_word(focus)
        weekday_index = focus.focus_date.weekday()
        weekday = weekday_name(focus.focus_date)

        if weekday_index >= 5:
            text = self._pick(uid, "weekend", [
                f"{self._emo('party')} {when} выходной — уроков нет.",
                f"{self._emo('party')} {when} {weekday}, уроков нет.",
            ])
            return text, "weekend", 0

        items = list(schedule.get(weekday_index, ()))
        if not items:
            return (
                f"{self._emo('books')} Расписание на {WEEKDAYS_ACC[weekday_index]} пока не заполнено — "
                f"его можно добавить в «📅 Моё расписание».",
                "empty", 0,
            )

        total = total_lessons(items)
        lp = lessons_phrase(total)
        head = self._pick(uid, "lessons", [
            f"{when} у тебя <b>{lp}</b>:",
            f"{when} по расписанию <b>{lp}</b>:",
            f"{when} {weekday} — <b>{lp}</b>:",
        ])
        return f"{self._emo('books')} {head}\n{format_day_quote(items)}", "school", total

    # ----------------------------- концовка -----------------------------

    def _closing(self, uid: int, focus: Focus, mode: str, total: int, name: str | None) -> str:
        part = focus.part
        if part is DayPart.MORNING:
            if mode == "weekend":
                options = ["Хорошего выходного! 🌿", "Пусть день будет спокойным."]
            elif mode == "school":
                options = ["Продуктивного дня! 📚", "Удачи на уроках!", "Пусть день пройдёт легко."]
                if total >= 7:
                    options.append("День насыщенный — держись!")
            else:
                options = ["Хорошего дня!", "Спокойного и удачного дня."]
            if name:
                options.append(f"Хорошего дня, {name}!")
        elif part is DayPart.DAY:
            options = ["Хорошего продолжения дня!", "Лёгкой оставшейся части дня."]
            if name:
                options.append(f"Хорошего дня, {name}!")
        elif part is DayPart.EVENING:
            if mode == "weekend":
                options = ["Впереди выходной — отдыхай! 🌿", "Хорошего вечера и спокойных выходных."]
            else:
                options = ["Хорошего вечера!", "Отдыхай, набирайся сил на завтра.", "Спокойного вечера! ✨"]
            if name:
                options.append(f"Хорошего вечера, {name}!")
        else:
            options = ["Спокойной ночи! 🌙", "Сладких снов."]
        return self._pick(uid, f"close:{part.value}:{mode}", options)
