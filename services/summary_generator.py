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
    EmojiPack, capitalize_first, esc, fmt_range, fmt_speed, fmt_temp,
    join_ru, lessons_phrase, round_half_up,
)

# Слова, описывающие тепловой уровень. Один и тот же уровень используется и в описании
# погоды, и в советах по одежде — поэтому противоречий («жарко» + «пуховик») быть не может.
_LEVEL_WORDS: dict[Thermal, tuple[str, ...]] = {
    Thermal.EXTREME: ("лютый мороз", "очень сильный мороз"),
    Thermal.HARD_FROST: ("сильный мороз", "крепкий мороз"),
    Thermal.FROST: ("морозно", "подмораживает"),
    Thermal.COLD: ("холодно", "довольно холодно"),
    Thermal.COOL: ("прохладно", "довольно прохладно"),
    Thermal.FRESH: ("свежо", "немного прохладно"),
    Thermal.COMFORT: ("комфортно", "приятно", "тепло, но не жарко"),
    Thermal.WARM: ("тепло", "по-настоящему тепло"),
    Thermal.HOT: ("жарко", "очень жарко"),
}

_PRECIP_OF_KIND = {"drizzle": "rain", "rain": "rain", "snow": "snow", "freezing": "freezing", "thunder": "thunder"}
_PRECIP_WORDS = {
    "rain": ("дождь", "дождя"),
    "snow": ("снег", "снега"),
    "freezing": ("ледяной дождь", "ледяного дождя"),
    "mixed": ("дождь и снег", "дождя и снега"),
    "thunder": ("гроза", "грозы"),
}


@dataclass(frozen=True)
class SummaryContext:
    user: User
    schedule: Mapping[int, Sequence[LessonItem]]  # расписание ЭТОГО пользователя: 0 = понедельник ... 4 = пятница
    now: datetime                                 # aware, в часовом поясе пользователя
    report: WeatherReport | None                  # None — погоду получить не удалось
    address: str | None = None                    # выбранный вариант обращения (None -> основное имя)


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
        focus = resolve_focus(ctx.now)
        uid = ctx.user.telegram_id
        name = esc(ctx.address or ctx.user.name)
        today = ctx.now.date()

        greeting = self._greeting(uid, focus.part, name)
        name_used = name in greeting  # имя в сообщении появляется один раз, а не в каждой фразе

        blocks: list[str] = [
            f"<b>{greeting}</b>",
            f"{self._emo('calendar')} {capitalize_first(weekday_name(today))}, {format_date(today)} · {esc(ctx.user.city)}",
        ]

        if ctx.report is not None:
            advice = advise(self._facts(ctx.report, focus))
            blocks.append(self._weather_text(uid, ctx, focus, advice))
            blocks.append(f"{self._emo('jacket')} {self._clothing_text(uid, focus, advice)}")
        else:
            blocks.append(
                f"{self._emo('cloud')} Погоду сейчас получить не удалось — выдумывать её не буду. "
                f"Попробуй «Сводка» ещё раз чуть позже."
            )

        lessons_block, mode, total = self._lessons_block(uid, focus, ctx.schedule)
        blocks.append(lessons_block)
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

    def _weather_text(self, uid: int, ctx: SummaryContext, focus: Focus, advice: ClothingAdvice) -> str:
        """Короткий абзац: переход к погоде, описание неба, цифры (сейчас / диапазон дня / ветер), осадки."""
        facts = advice.facts
        report = ctx.report
        assert report is not None
        cur = report.current
        day = self._day_word(focus)
        adjective = self._pick(uid, f"level:{advice.level.name}", _LEVEL_WORDS[advice.level])
        sky_condition = describe_code(facts.dominant_code)
        sky = sky_condition.text

        intro = self._pick(uid, "weather_intro", [
            f"За окном {sky}, {adjective}.",
            f"{capitalize_first(sky)}, {adjective}.",
            f"Что с погодой: {sky}, {adjective}.",
            f"На улице {adjective}, {sky}.",
        ])

        now_part = f"сейчас {fmt_temp(cur.temp, '°')}"
        if abs(cur.apparent - cur.temp) >= 2:
            now_part += f" (ощущается как {fmt_temp(cur.apparent)})"
        forecast = report.day(focus.focus_date)
        range_part = f"{day.lower()} {fmt_range(forecast.temp_min, forecast.temp_max)}" if forecast else ""
        wind_part = ""
        if facts.hours_count and facts.max_wind >= 6:
            wind_part = f", ветер до {fmt_speed(facts.max_wind)}"
            if facts.max_gust - facts.max_wind >= 4:
                wind_part += f" (порывы до {fmt_speed(facts.max_gust)})"

        pieces = [now_part] + ([range_part] if range_part else [])
        numbers = self._pick(uid, "weather_numbers", [
            f"{capitalize_first(', '.join(pieces))}{wind_part}.",
            f"{capitalize_first(', '.join(reversed(pieces)))}{wind_part}.",
        ])
        parts = [f"{sky_condition.emoji} {intro}", numbers]

        precip = facts.precip_kind
        prob = facts.max_precip_prob
        if precip and _PRECIP_OF_KIND.get(sky_condition.kind) != precip:
            word, _ = _PRECIP_WORDS[precip]
            prob_part = f" (вероятность до {round_half_up(prob)}%)" if prob is not None else ""
            if precip == "thunder":
                parts.append(f"Возможна {word}{prob_part}.")
            elif prob is not None and prob >= 60:
                parts.append(f"Ожидается {word}{prob_part}.")
            else:
                parts.append(f"Возможен {word}{prob_part}.")
        elif not precip and prob is not None and prob >= 20:
            parts.append(f"Вероятность осадков — до {round_half_up(prob)}%.")
        return " ".join(parts)

    # ----------------------------- одежда -----------------------------

    def _clothing_text(self, uid: int, focus: Focus, advice: ClothingAdvice) -> str:
        o = advice.outfit
        wear_required = o.texts((OUTER, CORE, LEGS, FEET), optional=False)
        accessories_required = o.texts((HEAD, HANDS, NECK), optional=False)
        extras_required = o.texts((EXTRAS,), optional=False)
        optional_items = o.texts((OUTER, CORE, LEGS, FEET, HEAD, HANDS, NECK), optional=True)
        extras_optional = o.texts((EXTRAS,), optional=True)

        # Нужные «обычные» вещи; ограничиваем длину, чтобы фраза читалась.
        main_items = wear_required[:4]
        sentences: list[str] = []

        if focus.is_tomorrow:
            intros = [
                "На завтра советую надеть {items}.",
                "Завтра лучше надеть {items}.",
                "Завтра пригодится: надень {items}.",
            ]
        else:
            intros = [
                "Лучше надеть {items}.",
                "Я бы советовал надеть {items}.",
                "Стоит надеть {items}.",
                "Рекомендую надеть {items}.",
                "Надень {items} — так будет комфортно.",
            ]
        if main_items:
            sentences.append(self._pick(uid, "clothes", intros).format(items=f"<b>{join_ru(main_items)}</b>"))

        if accessories_required:
            template = self._pick(
                uid, "acc",
                ["Не забудь про {x}.", "Добавь к этому {x}.", "И возьми с собой {x}."],
            )
            sentences.append(template.format(x=f"<b>{join_ru(accessories_required)}</b>"))

        if extras_required:
            sentences.append(
                self._pick(uid, "extras", ["С собой лучше взять {x}.", "Захвати с собой {x}."]).format(
                    x=f"<b>{join_ru(extras_required)}</b>"
                )
            )

        if optional_items:
            sentences.append(f"<i>По желанию можно добавить: {join_ru(optional_items[:2])}.</i>")
        if extras_optional:
            sentences.append(f"<i>На всякий случай можно взять {join_ru(extras_optional)}.</i>")

        for warning in advice.outfit.warnings[:2]:
            sentences.append(f"<i>⚠️ {capitalize_first(warning)}.</i>")

        text = " ".join(s for s in sentences if s)
        return text or "Особых рекомендаций по одежде нет — одевайся по привычке."

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
