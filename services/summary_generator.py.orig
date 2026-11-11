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

from database.models import User
from services.clothing_advisor import (
    CORE, EXTRAS, FEET, HANDS, HEAD, LEGS, NECK, OUTER,
    ClothingAdvice, Thermal, WeatherFacts, advise, build_facts,
)
from services.time_service import DayPart, day_part, format_date, weekday_name
from services.weather_models import DayForecast, WeatherReport, describe_code
from utils.formatting import (
    DIVIDER, EmojiPack, capitalize_first, esc, fmt_range, fmt_speed, fmt_temp,
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
    lessons: Mapping[int, int]   # 0 = понедельник ... 6 = воскресенье
    now: datetime                # aware, в часовом поясе пользователя
    report: WeatherReport | None  # None — погоду получить не удалось


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
        name = esc(ctx.user.name)
        today = ctx.now.date()

        lessons_count = int(ctx.lessons.get(focus.focus_date.weekday(), 0))
        is_free_day = lessons_count == 0

        blocks: list[str] = []
        blocks.append(f"<b>{self._greeting(uid, focus.part, name)}</b>")
        blocks.append(
            f"{self._emo('calendar')} <b>{capitalize_first(weekday_name(today))}, {format_date(today)}</b>"
            f" · {esc(ctx.user.city)}"
        )

        advice: ClothingAdvice | None = None
        if ctx.report is not None:
            facts = self._facts(ctx.report, focus)
            advice = advise(facts)
            blocks.append(self._weather_prose(uid, ctx, focus, advice))
            blocks.append(self._data_block(ctx, focus, facts))
            blocks.append(f"{self._emo('jacket')} {self._clothing_text(uid, focus, advice)}")
        else:
            blocks.append(
                f"{self._emo('cloud')} Погоду сейчас получить не удалось, а выдумывать её я не буду. "
                f"Попробуй нажать «Сводка» ещё раз через пару минут."
            )

        blocks.append(DIVIDER)
        blocks.append(f"{self._emo('books')} {self._lessons_text(uid, focus, lessons_count)}")
        blocks.append(f"<i>{self._closing(uid, focus, lessons_count, is_free_day)}</i>")
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
                f"{name}, привет! Небольшая сводка на остаток дня 🌤",
            ],
            DayPart.EVENING: [
                f"{name}, добрый вечер!",
                f"Привет, {name}! Подведём итоги дня?) {e('moon')}",
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

    def _weather_prose(self, uid: int, ctx: SummaryContext, focus: Focus, advice: ClothingAdvice) -> str:
        facts = advice.facts
        report = ctx.report
        assert report is not None
        day = self._day_word(focus)
        adjective = self._pick(uid, f"level:{advice.level.name}", _LEVEL_WORDS[advice.level])

        sky_condition = describe_code(facts.dominant_code)
        sky = sky_condition.text
        city = esc(ctx.user.city)

        templates = [
            f"{day} {sky}, на улице {adjective}.",
            f"{day} {sky} и {adjective}.",
            f"{day} в городе {city}: {sky}, {adjective}.",
            f"За окном {sky} — на улице {adjective}.",
        ]
        if advice.level <= Thermal.COLD:
            templates.append(f"{day} {sky}, и {adjective} — одевайся с умом.")
        sentence = self._pick(uid, "weather", templates)
        parts = [f"{sky_condition.emoji} {sentence}"]

        # Осадки: отдельная фраза, если небо уже не описано как этот тип осадков.
        precip = facts.precip_kind
        if precip and _PRECIP_OF_KIND.get(sky_condition.kind) != precip:
            word, genitive = _PRECIP_WORDS[precip]
            prob = facts.max_precip_prob
            prob_part = f" (вероятность до {round_half_up(prob)}%)" if prob is not None else ""
            strong = prob is not None and prob >= 60
            if precip == "thunder":
                parts.append(f"Возможна {word}{prob_part}.")
            elif strong:
                parts.append(f"Ожидается {word}{prob_part}.")
            else:
                parts.append(f"Возможен {word}{prob_part}." if precip != "snow" else f"Возможен снег{prob_part}.")
        return " ".join(parts)

    def _data_block(self, ctx: SummaryContext, focus: Focus, facts: WeatherFacts) -> str:
        report = ctx.report
        assert report is not None
        cur = report.current
        lines: list[str] = []

        cur_emoji = describe_code(cur.code).emoji
        now_line = f"{cur_emoji} Сейчас: <b>{fmt_temp(cur.temp, '°C')}</b>"
        if abs(cur.apparent - cur.temp) >= 2:
            now_line += f", ощущается как <b>{fmt_temp(cur.apparent)}</b>"
        lines.append(now_line)

        day_forecast: DayForecast | None = report.day(focus.focus_date)
        label = self._day_word(focus)
        if day_forecast is not None:
            lines.append(
                f"📈 {label}: <b>{fmt_range(day_forecast.temp_min, day_forecast.temp_max)}</b>"
            )

        if facts.hours_count:
            wind_line = f"{self._emo('wind')} Ветер: <b>{fmt_speed(facts.max_wind)}</b>"
            if facts.max_gust - facts.max_wind >= 3:
                wind_line += f", порывы до <b>{fmt_speed(facts.max_gust)}</b>"
            lines.append(wind_line)

        prob = facts.max_precip_prob
        if prob is None and day_forecast is not None:
            prob = day_forecast.precip_prob_max
        if prob is not None:
            icon = "❄️" if facts.precip_kind == "snow" else "☔"
            if prob < 5:
                lines.append(f"{icon} Осадки: <b>не ожидаются</b>")
            else:
                lines.append(f"{icon} Осадки: до <b>{round_half_up(prob)}%</b>")

        return "<blockquote>" + "\n".join(lines) + "</blockquote>"

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
            sentences.append(f"<i>По желанию можно добавить: {join_ru(optional_items)}.</i>")
        if extras_optional:
            sentences.append(f"<i>На всякий случай можно взять {join_ru(extras_optional)}.</i>")

        for warning in advice.outfit.warnings[:3]:
            sentences.append(f"<i>⚠️ {capitalize_first(warning)}.</i>")

        text = " ".join(s for s in sentences if s)
        return text or "Особых рекомендаций по одежде нет — одевайся по привычке."

    # ----------------------------- уроки -----------------------------

    def _lessons_text(self, uid: int, focus: Focus, count: int) -> str:
        when = self._day_word(focus)
        weekday = weekday_name(focus.focus_date)
        wd_index = focus.focus_date.weekday()

        if count == 0:
            if wd_index >= 5:
                head = self._pick(uid, "free", [
                    f"{when} {weekday} — выходной, уроков нет {self._emo('party')}",
                    f"{when} {weekday}, уроков нет — можно выдохнуть {self._emo('party')}",
                ])
                tail = self._pick(uid, "free_tail", ["Хорошего отдыха!", "Наслаждайся выходным!"])
            else:
                head = f"{when} {weekday}, по расписанию уроков нет."
                tail = "Можно заняться своими делами."
            return f"{head} {tail}"

        head = self._pick(uid, "lessons", [
            f"{when} {weekday}, у тебя <b>{lessons_phrase(count)}</b>.",
            f"{when} {weekday} — по расписанию <b>{lessons_phrase(count)}</b>.",
            f"{when} {weekday}: <b>{lessons_phrase(count)}</b>.",
        ])
        if count <= 4:
            tail = self._pick(uid, "tail_light", ["День обещает быть лёгким.", "Не так уж и много!"])
        elif count <= 6:
            tail = self._pick(uid, "tail_mid", ["Надеюсь, день пройдёт быстро!", "Обычный учебный день."])
        else:
            tail = self._pick(uid, "tail_heavy", ["День насыщенный, но ты справишься!", "Денёк непростой — держись!"])
        return f"{head} {tail}"

    # ----------------------------- концовка -----------------------------

    def _closing(self, uid: int, focus: Focus, count: int, is_free: bool) -> str:
        part = focus.part
        if part is DayPart.MORNING:
            options = (
                ["Отличного выходного! 🌿", "Пусть день будет спокойным и приятным! 🌿"]
                if is_free
                else ["Продуктивного дня! 📚", "Удачи на уроках! ✨", "Хорошего настроения на весь день! 🙂", "Пусть день пройдёт легко! 💪"]
            )
        elif part is DayPart.DAY:
            options = ["Хорошего продолжения дня! ✨", "Лёгкой оставшейся части дня! 🙂"]
        elif part is DayPart.EVENING:
            options = (
                ["Впереди выходной — наслаждайся! 🎉", "Хорошего вечера, а завтра — отдыхать! 🌿"]
                if is_free
                else ["Хорошего вечера! 🌙", "Отдыхай и набирайся сил на завтра! 🌙", "Спокойного вечера! ✨"]
            )
        else:
            options = ["Спокойной ночи! 🌙", "Сладких снов! 😴"]
        return self._pick(uid, f"close:{part.value}:{is_free}", options)
