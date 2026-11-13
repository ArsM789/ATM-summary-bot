"""Inline «погода завтра», формат обращения админу, конфигурация."""
from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import _shims  # noqa: F401

import config
from database.models import User
from services.feedback_format import format_feedback_for_admin
from services.tomorrow_weather import build_tomorrow, matches_query
from services.weather_models import CurrentConditions, DayForecast, HourPoint, WeatherReport


def report_for(tz_name: str, today: date, *, code=3, prob=10, precip=0.0, tmin=8.0, tmax=11.0, amin=6.0, amax=9.0,
               days=3, hour_code=None, hour_prob=None, hour_precip=0.0) -> WeatherReport:
    tz = ZoneInfo(tz_name)
    start = datetime(today.year, today.month, today.day, tzinfo=tz)
    hours = tuple(
        HourPoint(start + timedelta(hours=i), (tmin + tmax) / 2, (amin + amax) / 2,
                  prob if hour_prob is None else hour_prob, hour_precip, 0.0, 3.0, 5.0,
                  code if hour_code is None else hour_code)
        for i in range(24 * days)
    )
    forecast = tuple(
        DayForecast(today + timedelta(days=d), code, tmax, tmin, amax, amin, prob, precip, 0.0, 4.0, 6.0)
        for d in range(days)
    )
    cur = CurrentConditions(start, tmin, tmin, 2.0, 4.0, 0.0, code)
    return WeatherReport("Казань", tz_name, cur, forecast, hours, datetime.now(tz))


class TomorrowTests(unittest.TestCase):
    def test_format_like_spec_mostly_cloudy_with_possible_rain(self):
        from dataclasses import replace
        now = datetime(2026, 11, 9, 20, 0, tzinfo=ZoneInfo("Europe/Moscow"))   # понедельник вечером
        r = report_for("Europe/Moscow", now.date(), code=3, prob=40, precip=0.8)
        tomorrow = now.date() + timedelta(days=1)
        wet = {h.time: replace(h, code=61, precip=0.3, precip_prob=40) for h in r.hours
               if h.time.date() == tomorrow and 10 <= h.time.hour < 13}      # дождь лишь в 3 часа из 17
        r = WeatherReport(r.city, r.timezone, r.current, r.days, tuple(wet.get(h.time, h) for h in r.hours), r.fetched_at)
        f = build_tomorrow(r, now)
        self.assertEqual(f.title, "Погода завтра · Казань")
        self.assertIn("<b>Погода завтра · Казань</b>", f.html)
        self.assertIn("вторник, 10 ноября", f.html)                          # именно ЗАВТРА
        self.assertIn("Пасмурно, возможен дождь.", f.html)                   # небо + осадки
        self.assertIn("Температура: <b>+8…+11 °C</b>", f.html)
        self.assertIn("Ощущается как <b>+6…+9 °C</b>", f.html)
        self.assertIn("Осадки: до <b>40%</b>", f.html)
        self.assertIn("+8…+11 °C", f.description)

    def test_rain_all_day_is_not_repeated(self):
        now = datetime(2026, 11, 9, 20, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        r = report_for("Europe/Moscow", now.date(), code=61, prob=80, precip=2.0, hour_code=61, hour_prob=80, hour_precip=0.2)
        f = build_tomorrow(r, now)
        self.assertIn("Небольшой дождь.", f.html)
        self.assertNotIn("возможен", f.html)

    def test_tomorrow_uses_users_timezone_not_utc(self):
        # 23:30 в Москве — в UTC ещё 20:30 того же дня; «завтра» считаем по местной дате пользователя
        now = datetime(2026, 11, 9, 23, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        f = build_tomorrow(report_for("Europe/Moscow", now.date()), now)
        self.assertIn("10 ноября", f.html)
        # а для Владивостока (UTC+10) тот же момент — уже 10 ноября 06:30 -> завтра 11-е
        now_v = now.astimezone(ZoneInfo("Asia/Vladivostok"))
        f2 = build_tomorrow(report_for("Asia/Vladivostok", now_v.date()), now_v)
        self.assertIn("11 ноября", f2.html)

    def test_no_data_for_tomorrow_returns_none(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        r = report_for("Europe/Moscow", now.date() - timedelta(days=5))     # прогноз устарел и «завтра» в нём нет
        self.assertIsNone(build_tomorrow(r, now))

    def test_negative_temperature_and_no_precip(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        f = build_tomorrow(report_for("Europe/Moscow", now.date(), tmin=-5, tmax=-1, amin=-9, amax=-4, prob=0), now)
        self.assertIn("\u22125…\u22121 °C", f.html)
        self.assertIn("Осадки: <b>не ожидаются</b>", f.html)

    def test_missing_optional_fields_not_invented(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        r = report_for("Europe/Moscow", now.date(), prob=None, hour_prob=None)
        day = r.days[1]
        r = WeatherReport(r.city, r.timezone, r.current,
                          (r.days[0], DayForecast(day.date, day.code, day.temp_max, day.temp_min, None, None, None, 0.0, 0.0, None, None), r.days[2]),
                          r.hours, r.fetched_at)
        f = build_tomorrow(r, now)
        self.assertNotIn("Ощущается", f.html)
        self.assertNotIn("Осадки", f.html)

    def test_html_is_escaped(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        r = report_for("Europe/Moscow", now.date())
        r = WeatherReport("<i>Город</i> & Ко", r.timezone, r.current, r.days, r.hours, r.fetched_at)
        self.assertNotIn("<i>Город</i>", build_tomorrow(r, now).html)

    def test_query_matching(self):
        for q in ("", "погода завтра", "Погода Завтра", "завтра", "пого", "weather", "tomorrow weather", "что там завт"):
            self.assertTrue(matches_query(q), q)
        for q in ("курс доллара", "привет", "расписание", "х"):
            self.assertFalse(matches_query(q), q)


class FeedbackFormatTests(unittest.TestCase):
    def test_contains_required_fields(self):
        u = User(123456789, "Арсений", "Казань", 1.0, 1.0, "UTC", True, username="arsen", first_name="Arseniy")
        text = format_feedback_for_admin(u, "Не приходит сводка", datetime(2026, 11, 10, 7, 5))
        for part in ("<b>Новое обращение в ATM</b>", "Тип: Обратная связь", "Пользователь: Арсений",
                     "Username: @arsen", "Telegram ID: <code>123456789</code>", "«Не приходит сводка»",
                     "Дата и время: 10.11.2026 07:05"):
            self.assertIn(part, text)

    def test_no_username_and_escaping(self):
        u = User(5, "A<b>", "К", 1.0, 1.0, "UTC", True)
        text = format_feedback_for_admin(u, "a < b & <script>", datetime(2026, 1, 1))
        self.assertIn("Username: —", text)
        self.assertIn("A&lt;b&gt;", text)
        self.assertIn("a &lt; b &amp; &lt;script&gt;", text)
        self.assertNotIn("<script>", text)

    def test_fallback_to_first_name_when_name_empty(self):
        u = User(5, "", "К", 1.0, 1.0, "UTC", True, first_name="Tg")
        self.assertIn("Пользователь: Tg", format_feedback_for_admin(u, "x", datetime(2026, 1, 1)))


class ConfigTests(unittest.TestCase):
    TOKEN = "123456789:" + "A" * 35

    def load(self, **env):
        with mock.patch.dict(os.environ, {k: v for k, v in env.items()}, clear=True), \
             mock.patch.object(config, "load_dotenv", lambda *a, **k: False):
            return config.load_settings()

    def test_defaults_and_admin(self):
        s = self.load(BOT_TOKEN=self.TOKEN, ADMIN_ID="42")
        self.assertEqual((s.default_city, s.default_timezone, s.admin_id, s.broadcast_rate), ("Казань", "Europe/Moscow", 42, 15.0))
        self.assertIsNone(self.load(BOT_TOKEN=self.TOKEN).admin_id)         # ADMIN_ID необязателен

    def test_validation(self):
        with self.assertRaises(config.ConfigError):
            self.load()                                                    # нет токена
        for bad in ({"BOT_TOKEN": "abc"}, {"ADMIN_ID": "abc"}, {"ADMIN_ID": "-5"}, {"ADMIN_ID": "0"},
                    {"DEFAULT_TIMEZONE": "Mars/X"}, {"DEFAULT_SEND_TIME": "25:00"}, {"BROADCAST_RATE": "100"},
                    {"CUSTOM_EMOJI_IDS": "{bad"}):
            env = {"BOT_TOKEN": self.TOKEN, **bad}
            with self.assertRaises(config.ConfigError, msg=str(bad)):
                self.load(**env)

    def test_max_user_events(self):
        self.assertEqual(self.load(BOT_TOKEN=self.TOKEN).max_user_events, 10)                       # по умолчанию
        self.assertEqual(self.load(BOT_TOKEN=self.TOKEN, MAX_USER_EVENTS="").max_user_events, 10)   # пустое значение
        self.assertEqual(self.load(BOT_TOKEN=self.TOKEN, MAX_USER_EVENTS=" 25 ").max_user_events, 25)
        self.assertEqual(self.load(BOT_TOKEN=self.TOKEN, MAX_USER_EVENTS="1").max_user_events, 1)
        for bad in ("0", "-3", "abc", "2.5", "10 событий", "١٠"):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.load(BOT_TOKEN=self.TOKEN, MAX_USER_EVENTS=bad)

    def test_default_lessons_removed(self):
        self.assertFalse(hasattr(self.load(BOT_TOKEN=self.TOKEN), "default_lessons"))


if __name__ == "__main__":
    unittest.main()
