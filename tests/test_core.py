"""Тесты чистой логики (без сети, aiogram и aiosqlite): запуск — python -m unittest discover -s tests -v"""
from __future__ import annotations

import random
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.models import User, mask_to_weekdays, weekdays_to_mask  # noqa: E402
from database.schema import SCHEMA  # noqa: E402
from services.clothing_advisor import (  # noqa: E402
    CORE, EXTRAS, FEET, HANDS, HEAD, LEGS, NECK, OUTER, Thermal, advise, build_facts, thermal_level,
)
from services.summary_generator import SummaryContext, SummaryGenerator, resolve_focus  # noqa: E402
from services.time_service import DayPart, day_part, parse_hhmm, parse_timezone  # noqa: E402
from services.weather_models import (  # noqa: E402
    CurrentConditions, DayForecast, HourPoint, WeatherParseError, WeatherReport, parse_report,
)
from utils.formatting import fmt_range, fmt_temp, join_ru, lessons_phrase, plural  # noqa: E402

TZ = ZoneInfo("Europe/Moscow")
LESSONS = {0: 7, 1: 6, 2: 6, 3: 7, 4: 7, 5: 0, 6: 0}


def make_report(day: date, *, temp: float, feels: float | None = None, code: int = 3,
                prob: float | None = 10, precip: float = 0.0, snow: float = 0.0,
                gust: float = 4.0, swing: float = 0.0) -> WeatherReport:
    """Синтетический отчёт для проверки логики: 3 суток, температура плавно меняется на ±swing."""
    feels = temp if feels is None else feels
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    hours = []
    for i in range(72):
        hour = i % 24
        shift = swing * (-1 + 2 * (hour / 23))  # от −swing к +swing за сутки
        hours.append(HourPoint(
            time=start + timedelta(hours=i), temp=temp + shift, apparent=feels + shift,
            precip_prob=prob, precip=precip, snowfall=snow, wind=min(gust, 5.0), gusts=gust, code=code,
        ))
    days = tuple(
        DayForecast(date=day + timedelta(days=d), code=code, temp_max=temp + swing, temp_min=temp - swing,
                    apparent_max=feels + swing, apparent_min=feels - swing, precip_prob_max=prob,
                    precip_sum=precip * 24, snowfall_sum=snow * 24, wind_max=5.0, gusts_max=gust)
        for d in range(3)
    )
    current = CurrentConditions(time=start, temp=temp, apparent=feels, wind=3.0, gusts=gust, precip=precip, code=code)
    return WeatherReport("Казань", "Europe/Moscow", current, days, tuple(hours), datetime.now(TZ))


def make_user(name: str = "Арсений") -> User:
    return User(1, name, "Казань", 55.79, 49.12, "Europe/Moscow", True)


class FormattingTests(unittest.TestCase):
    def test_temperature(self):
        self.assertEqual(fmt_temp(-3), "\u22123°")
        self.assertEqual(fmt_temp(2), "+2°")
        self.assertEqual(fmt_temp(0.3), "0°")
        self.assertEqual(fmt_temp(-0.2), "0°")
        self.assertEqual(fmt_temp(2.5), "+3°")

    def test_range(self):
        self.assertEqual(fmt_range(-3, 2), "от \u22123 до +2°C")
        self.assertEqual(fmt_range(1.6, 2.2), "около +2°C")

    def test_grammar(self):
        self.assertEqual([lessons_phrase(n) for n in (1, 2, 5, 7, 11, 21)],
                         ["1 урок", "2 урока", "5 уроков", "7 уроков", "11 уроков", "21 урок"])
        self.assertEqual(plural(12, "а", "б", "в"), "в")
        self.assertEqual(join_ru(["а", "б", "в"]), "а, б и в")
        self.assertEqual(join_ru(["а", "б"]), "а и б")


class TimeTests(unittest.TestCase):
    def test_day_part(self):
        parts = {h: day_part(datetime(2026, 11, 9, h, 0, tzinfo=TZ)) for h in (4, 5, 11, 12, 16, 17, 22, 23, 0)}
        self.assertEqual(parts[4], DayPart.NIGHT)
        self.assertEqual(parts[5], DayPart.MORNING)
        self.assertEqual(parts[11], DayPart.MORNING)
        self.assertEqual(parts[12], DayPart.DAY)
        self.assertEqual(parts[16], DayPart.DAY)
        self.assertEqual(parts[17], DayPart.EVENING)
        self.assertEqual(parts[22], DayPart.EVENING)
        self.assertEqual(parts[23], DayPart.NIGHT)
        self.assertEqual(parts[0], DayPart.NIGHT)

    def test_parse_hhmm(self):
        self.assertEqual(parse_hhmm("7:05"), "07:05")
        self.assertEqual(parse_hhmm("07.05"), "07:05")
        self.assertEqual(parse_hhmm(" 23:59 "), "23:59")
        for bad in ("24:00", "7:5", "abc", "", "12:60", "7"):
            self.assertIsNone(parse_hhmm(bad), bad)

    def test_parse_timezone(self):
        self.assertEqual(parse_timezone("Europe/Moscow"), "Europe/Moscow")
        self.assertEqual(parse_timezone("europe/samara"), "Europe/Samara")
        self.assertEqual(parse_timezone("МСК"), "Europe/Moscow")
        self.assertEqual(parse_timezone("+3"), "Etc/GMT-3")
        self.assertEqual(parse_timezone("UTC-5"), "Etc/GMT+5")
        self.assertEqual(parse_timezone("+0"), "UTC")
        self.assertIsNone(parse_timezone("Mars/Olympus"))
        self.assertIsNone(parse_timezone("+30"))

    def test_focus_windows(self):
        mon_morning = resolve_focus(datetime(2026, 11, 9, 7, 30, tzinfo=TZ))
        self.assertFalse(mon_morning.is_tomorrow)
        self.assertEqual(mon_morning.focus_date, date(2026, 11, 9))
        evening = resolve_focus(datetime(2026, 11, 9, 20, 0, tzinfo=TZ))
        self.assertTrue(evening.is_tomorrow)
        self.assertEqual(evening.focus_date, date(2026, 11, 10))
        deep_night = resolve_focus(datetime(2026, 11, 9, 2, 0, tzinfo=TZ))
        self.assertFalse(deep_night.is_tomorrow)
        self.assertEqual(deep_night.focus_date, date(2026, 11, 9))
        late_night = resolve_focus(datetime(2026, 11, 9, 23, 30, tzinfo=TZ))
        self.assertTrue(late_night.is_tomorrow)


class ParserTests(unittest.TestCase):
    """Фикстура повторяет формат ответа Open-Meteo, но значения придуманы для теста парсера."""

    def payload(self):
        times = [f"2026-11-09T{h:02d}:00" for h in range(24)]
        n = len(times)
        return {
            "current": {"time": "2026-11-09T07:00", "temperature_2m": -1.0, "apparent_temperature": -5.0,
                        "wind_speed_10m": 4.0, "wind_gusts_10m": 9.0, "precipitation": 0.0, "weather_code": 3},
            "hourly": {"time": times, "temperature_2m": [0.0] * n, "apparent_temperature": [-2.0] * n,
                       "precipitation_probability": [None] * n, "precipitation": [0.0] * n,
                       "snowfall": [0.0] * n, "weather_code": [3] * n,
                       "wind_speed_10m": [3.0] * n, "wind_gusts_10m": [7.0] * n},
            "daily": {"time": ["2026-11-09"], "weather_code": [3], "temperature_2m_max": [2.0],
                      "temperature_2m_min": [-3.0], "apparent_temperature_max": [0.0],
                      "apparent_temperature_min": [-6.0], "precipitation_probability_max": [None],
                      "precipitation_sum": [0.0], "snowfall_sum": [0.0],
                      "wind_speed_10m_max": [5.0], "wind_gusts_10m_max": [9.0]},
        }

    def test_parse_ok(self):
        report = parse_report(self.payload(), "Казань", "Europe/Moscow")
        self.assertEqual(len(report.hours), 24)
        self.assertIsNone(report.hours[0].precip_prob)
        self.assertEqual(report.current.temp, -1.0)
        self.assertEqual(report.day(date(2026, 11, 9)).temp_min, -3.0)
        window = report.hours_between(datetime(2026, 11, 9, 7, tzinfo=TZ), datetime(2026, 11, 9, 10, tzinfo=TZ))
        self.assertEqual(len(window), 3)

    def test_parse_bad(self):
        bad = self.payload()
        del bad["hourly"]
        with self.assertRaises(WeatherParseError):
            parse_report(bad, "Казань", "Europe/Moscow")


class AdvisorTests(unittest.TestCase):
    def advice_for(self, temp, **kw):
        report = make_report(date(2026, 11, 9), temp=temp, **kw)
        facts = build_facts(report.hours_between(datetime(2026, 11, 9, 7, tzinfo=TZ), datetime(2026, 11, 9, 17, tzinfo=TZ)))
        return advise(facts)

    def test_levels(self):
        self.assertEqual(thermal_level(-20), Thermal.EXTREME)
        self.assertEqual(thermal_level(0), Thermal.COLD)
        self.assertEqual(thermal_level(20), Thermal.COMFORT)
        self.assertEqual(thermal_level(30), Thermal.HOT)

    def test_around_zero(self):
        a = self.advice_for(-1)
        self.assertIn("тёплую куртку", a.outfit.texts((OUTER,), optional=False))
        self.assertIn("перчатки", a.outfit.texts((HANDS,), optional=False))
        self.assertIn("шапку", a.outfit.texts((HEAD,), optional=False))

    def test_plus_ten_and_twenty(self):
        ten = self.advice_for(11)
        self.assertTrue(any("куртк" in t or "ветровк" in t for t in ten.outfit.texts((OUTER,), optional=False)))
        self.assertEqual(ten.outfit.texts((HEAD, HANDS, NECK), optional=False), [])
        twenty = self.advice_for(20)
        self.assertEqual(twenty.outfit.texts((OUTER,), optional=False), [])
        self.assertTrue(any("футболк" in t for t in twenty.outfit.texts((CORE,), optional=False)))

    def test_rain_and_wind(self):
        calm = self.advice_for(8, code=63, prob=80, precip=0.8, gust=5)
        self.assertIn("зонт", calm.outfit.texts((EXTRAS,), optional=False))
        windy = self.advice_for(8, code=63, prob=80, precip=0.8, gust=16)
        self.assertNotIn("зонт", windy.outfit.all_texts())
        self.assertTrue(any("капюшон" in t for t in windy.outfit.all_texts()))
        self.assertTrue(any("ветр" in w for w in windy.outfit.warnings))

    def test_low_probability_rain_is_optional(self):
        a = self.advice_for(10, code=61, prob=35, precip=0.05, gust=4)  # ~0.5 мм за 10 ч — слабый дождь
        self.assertEqual(a.outfit.texts((EXTRAS,), optional=False), [])
        self.assertIn("зонт", a.outfit.texts((EXTRAS,), optional=True))

    def test_snow_and_ice(self):
        snow = self.advice_for(-6, code=73, prob=80, snow=0.4)
        self.assertTrue(any("непромокаем" in t for t in snow.outfit.texts((FEET,), optional=False)))
        freezing = self.advice_for(-1, code=66, prob=70, precip=0.5)
        self.assertTrue(any("наледь" in w for w in freezing.outfit.warnings))

    def test_no_contradictions_over_sweep(self):
        """Свипим температуры, ветер, осадки: тёплое и холодное не должно смешиваться."""
        for temp in range(-30, 38, 2):
            for gust in (3, 12, 18):
                for code, prob, precip, snow in ((3, 5, 0, 0), (63, 80, 1.2, 0), (73, 80, 0, 0.5), (0, 0, 0, 0), (95, 70, 2, 0)):
                    a = self.advice_for(temp, code=code, prob=prob, precip=precip, snow=snow, gust=gust, swing=3)
                    texts = " | ".join(a.outfit.all_texts())
                    lvl = a.level
                    ctx = f"temp={temp} gust={gust} code={code} lvl={lvl.name}: {texts}"
                    if lvl >= Thermal.FRESH:
                        for word in ("шапк", "перчатк", "варежк", "шарф", "термобельё", "пуховик", "зимн"):
                            self.assertNotIn(word, texts.replace("шапку или капюшон", ""), ctx)
                    if lvl <= Thermal.COLD:
                        for word in ("шорты", "кепк", "панам", "солнцезащит", "открытую обувь"):
                            self.assertNotIn(word, texts, ctx)
                    if lvl <= Thermal.FROST:
                        self.assertNotIn("лёгкую футболку", texts, ctx)


class GeneratorTests(unittest.TestCase):
    def gen(self, now, report, user=None, seed=1):
        g = SummaryGenerator(rng=random.Random(seed))
        return g.generate(SummaryContext(user or make_user(), LESSONS, now, report))

    def test_monday_morning_cold(self):
        now = datetime(2026, 11, 9, 7, 30, tzinfo=TZ)  # понедельник
        text = self.gen(now, make_report(now.date(), temp=-1, feels=-4, swing=2, code=3))
        self.assertIn("Арсений", text)
        self.assertIn("Понедельник, 9 ноября", text)
        self.assertIn("7 уроков", text)
        self.assertIn("<blockquote>", text)
        self.assertIn("куртк", text)
        self.assertIn("шапку", text)
        self.assertNotIn("футболк", text)

    def test_weekend_has_no_school_talk(self):
        now = datetime(2026, 11, 7, 8, 0, tzinfo=TZ)  # суббота
        text = self.gen(now, make_report(now.date(), temp=5))
        self.assertIn("уроков нет", text)
        self.assertNotIn("Продуктивного", text)
        self.assertNotIn("урок", text.replace("уроков нет", ""))

    def test_evening_talks_about_tomorrow(self):
        now = datetime(2026, 11, 8, 20, 0, tzinfo=TZ)  # воскресенье вечер -> завтра понедельник
        text = self.gen(now, make_report(now.date(), temp=3))
        self.assertIn("Завтра понедельник", text)
        self.assertIn("7 уроков", text)
        self.assertNotIn("доброе утро", text.lower())

    def test_friday_evening_tomorrow_is_weekend(self):
        now = datetime(2026, 11, 13, 19, 0, tzinfo=TZ)  # пятница вечер
        text = self.gen(now, make_report(now.date(), temp=3))
        self.assertIn("Завтра суббота", text)
        self.assertIn("уроков нет", text)

    def test_weather_unavailable_does_not_invent(self):
        now = datetime(2026, 11, 9, 7, 30, tzinfo=TZ)
        text = self.gen(now, None)
        self.assertIn("не удалось", text)
        self.assertNotIn("°", text)
        self.assertIn("7 уроков", text)

    def test_html_escaping(self):
        now = datetime(2026, 11, 9, 7, 30, tzinfo=TZ)
        text = self.gen(now, make_report(now.date(), temp=5), user=make_user("<b>Хакер</b> & Ко"))
        self.assertNotIn("<b>Хакер</b>", text)
        self.assertIn("&lt;b&gt;Хакер&lt;/b&gt; &amp; Ко", text)

    def test_variety_and_no_immediate_repeat(self):
        now = datetime(2026, 11, 9, 7, 30, tzinfo=TZ)
        report = make_report(now.date(), temp=5)
        g = SummaryGenerator(rng=random.Random(7))
        ctx = SummaryContext(make_user(), LESSONS, now, report)
        texts = [g.generate(ctx) for _ in range(12)]
        self.assertGreater(len(set(texts)), 6)
        greetings = [t.split("\n")[0] for t in texts]
        for a, b in zip(greetings, greetings[1:]):
            self.assertNotEqual(a, b)

    def test_no_contradictions_in_messages(self):
        """Тексты целиком: холод не сочетается с «лёгкой одеждой», жара — с «пуховиком»."""
        for seed in range(25):
            for hour in (7, 14, 20, 23, 2):
                for temp in (-25, -3, 4, 12, 19, 26, 33):
                    now = datetime(2026, 11, 9, hour, 15, tzinfo=TZ)
                    text = self.gen(now, make_report(now.date(), temp=temp, swing=2), seed=seed)
                    low = text.lower()
                    if temp <= 0:
                        for word in ("шорты", "лёгкую футболку", "кепку"):
                            self.assertNotIn(word, low, (seed, hour, temp))
                    if temp >= 22:
                        for word in ("пуховик", "шапку", "перчатки", "морозно", "мороз"):
                            self.assertNotIn(word, low, (seed, hour, temp))


class DatabaseSchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO users (telegram_id,name,city,latitude,longitude,timezone) VALUES (1,'A','К',1,1,'UTC')")

    def test_unique_time_per_user(self):
        self.conn.execute("INSERT INTO schedules (user_id,send_time) VALUES (1,'07:00')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO schedules (user_id,send_time) VALUES (1,'07:00')")
        self.conn.execute("INSERT INTO schedules (user_id,send_time) VALUES (1,'13:30')")

    def test_claim_send_is_idempotent(self):
        self.conn.execute("INSERT INTO schedules (user_id,send_time) VALUES (1,'07:00')")
        sql = "UPDATE schedules SET last_sent_on=? WHERE id=1 AND (last_sent_on IS NULL OR last_sent_on <> ?)"
        self.assertEqual(self.conn.execute(sql, ("2026-11-09", "2026-11-09")).rowcount, 1)
        self.assertEqual(self.conn.execute(sql, ("2026-11-09", "2026-11-09")).rowcount, 0)
        self.assertEqual(self.conn.execute(sql, ("2026-11-10", "2026-11-10")).rowcount, 1)

    def test_cascade_and_checks(self):
        self.conn.execute("INSERT INTO lessons VALUES (1,0,7)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO lessons VALUES (1,9,7)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO schedules (user_id,send_time,weekdays_mask) VALUES (1,'08:00',0)")
        self.conn.execute("DELETE FROM users WHERE telegram_id=1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0], 0)

    def test_weekday_mask(self):
        self.assertEqual(weekdays_to_mask([0, 2, 4]), 0b10101)
        self.assertEqual(mask_to_weekdays(0b1100000), (5, 6))
        with self.assertRaises(ValueError):
            weekdays_to_mask([7])


if __name__ == "__main__":
    unittest.main()
