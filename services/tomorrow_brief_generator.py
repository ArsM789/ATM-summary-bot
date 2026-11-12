from __future__ import annotations

import random
from datetime import date
from collections.abc import Sequence

from database.models import LessonItem, User
from services.time_service import weekday_name, format_date
from services.weather_models import DayForecast, describe_code
from services.schedule_parser import format_day_quote, total_lessons
from utils.formatting import esc, fmt_range, lessons_phrase
from utils.formatting import EmojiPack

class TomorrowBriefGenerator:
    def __init__(
            self,
            emoji: EmojiPack | None = None,
            rng: random.Random | None = None,
    ) -> None:
        self._emo = emoji or EmojiPack()
        self._rng = rng or random.Random()

    def generate(
            self,
            user: User,
            target_date: date,
            forecast: DayForecast | None,
            lessons: Sequence[LessonItem],
            address: str | None = None,
    ) -> str:
        name = esc(address or user.name)

        greeting = self._rng.choice([
            f"Добрый вечер, {name}! 🌙",
            f"Привет, {name}! Вот твой план на завтра.",
            f"{name}, пора подготовиться к завтрашнему дню!",
        ])

        blocks = [
            f"<b>{greeting}</b>",
            (
                f"📅 <b>План на завтра — "
                f"{weekday_name(target_date)}, "
                f"{format_date(target_date)}</b>"
            ),
        ]


        if forecast is not None:
            condition = describe_code(forecast.code)

            weather_text = (
                f"{condition.emoji} {condition.text.capitalize()}."
                f"Температура: {fmt_range(forecast.temp_min, forecast.temp_max)}"
            )

            if forecast.precip_prob_max is not None:
                probability = round(forecast.precip_prob_max)

                if probability >= 20:
                    weather_text += (
                        f" Вероятность осадков - до {probability}%"
                    )

                blocks.append(
                    f"{self._emo("sun")} <b>Погода в {esc(user.city)}</b>\n{weather_text}"
                )

            else:
                blocks.append(
                f"{self._emo("sun")} <b>Погода</b>\n"
                "Не удалось получить прогноз на завтра "
                "Попробуйте позднее."
                )



        if target_date.weekday() >= 5:
            lessons_text = "🎉 Завтра выходной — уроков нет."

        elif not lessons:
            lessons_text = (
                "📚 Расписание на завтра пока не заполнено. "
                "Его можно добавить в «📅 Моё расписание»."
            )

        else:
            total = total_lessons(lessons)
            phrase = lessons_phrase(total)

            lessons_text = (
                f"📚 <b>Завтра у тебя {phrase}:</b>\n"
                f"{format_day_quote(lessons)}"
            )

        blocks.append(lessons_text)

        closing = self._rng.choice([
            "Хорошего вечера и удачного завтрашнего дня!",
            "Подготовься к завтрашнему дню и хорошо отдохни.",
            "Пусть завтрашний день пройдёт отлично!",
        ])

        blocks.append(f"<i>{closing}</i>")

        return "\n\n".join(blocks)