"""Сводки v2: компактный формат, ближайшая задача, вечерняя сводка (на завтра) в 18:00, миграция v5."""
from __future__ import annotations

import functools
import random
import re
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import _shims  # noqa: F401

import database.migrations as migrations
import test_scheduler as tsch
from database.database import Database
from database.models import LessonItem, weekdays_to_mask
from database.repositories import UserEventRepository
from database.schema import SCHEMA
from services.delivery import Outcome, SummaryDispatcher
from services.summary_generator import SummaryContext, SummaryGenerator
from services.summary_service import SummaryService
from services.telegram_send import send_message_safe
from services.weather_models import WeatherUnavailableError
from test_core import SCHEDULE, make_report, make_user
from test_database import DbCase, make_settings

MSK = "Europe/Moscow"


def at(tz: str, y, mo, d, h, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(tz)).astimezone(tsch.UTC)


class AnchoredWeather:
    """Синтетический прогноз вокруг заданной локальной даты (в отличие от FakeWeather — не привязан к реальному «сегодня»)."""

    def __init__(self, anchor: date, temp: float = 3.0, fail: bool = False):
        self.anchor, self.temp, self.fail = anchor, temp, fail

    async def get_report(self, city, lat, lon, tz_name):
        if self.fail:
            raise WeatherUnavailableError("down")
        now_local = datetime(self.anchor.year, self.anchor.month, self.anchor.day, 12, tzinfo=ZoneInfo(tz_name))
        return tsch.synthetic_report(city, tz_name, now_local, self.temp)


def ev_fields(**kw) -> dict:
    base = dict(title="Урок английского", recurrence="daily", weekdays_mask=0, month_day=None, event_date="2026-11-01",
                start_time="16:00", end_time="17:00", duration_min=60, remind_before_min=15, timezone=MSK)
    base.update(kw)
    return base


class CompactFormatTests(unittest.TestCase):
    def gen(self, now, report, seed=1, **ctx):
        g = SummaryGenerator(rng=random.Random(seed))
        return g.generate(SummaryContext(make_user(), SCHEDULE, now, report, address="Сеня", **ctx))

    def test_weather_and_clothes_in_one_compact_card(self):
        now = datetime(2026, 11, 10, 7, 30, tzinfo=ZoneInfo(MSK))
        text = self.gen(now, make_report(now.date(), temp=3, feels=0, swing=3, code=63, prob=70, precip=0.6, gust=9))
        card = re.search(r"<blockquote>(.*?)</blockquote>", text, re.S).group(1)
        self.assertIn("<b>Дождь, ", card)                        # заголовок: небо + диапазон
        self.assertIn("сейчас ", card); self.assertIn("☔ до 70%", card)
        self.assertIn("Надеть: <b>", card); self.assertIn("🎒 Взять: <b>зонт</b>", card)
        self.assertLessEqual(len(card.splitlines()), 6)           # карточка короткая
        for old in ("Через", "Что с погодой", "На улице", "Не забудь", "Рекомендую", "Вероятность осадков"):
            self.assertNotIn(old, text)
        # короткая одежда: не перечисляем кофты и штаны
        self.assertNotIn("плотные штаны", text); self.assertNotIn("кофту", card)

    def test_precipitation_not_repeated_when_headline_says_it(self):
        now = datetime(2026, 11, 13, 18, 0, tzinfo=ZoneInfo(MSK))
        text = self.gen(now, make_report(now.date(), temp=-8, swing=2, code=73, prob=60, snow=0.4))
        self.assertIn("Снег, ", text)
        self.assertIn("❄️ до 60%", text)
        self.assertNotIn("снег до", text)

    def test_evening_is_about_tomorrow_without_current_conditions(self):
        now = datetime(2026, 11, 8, 18, 0, tzinfo=ZoneInfo(MSK))      # воскресенье, 18:00
        text = self.gen(now, make_report(now.date(), temp=4, code=3))
        self.assertIn("🗓 Завтра: понедельник, 9 ноября", text)
        self.assertNotIn("сейчас", text)                           # «сейчас» вечером не нужно — смотрим на завтра
        self.assertRegex(text, r"Завтра по расписанию <b>7 уроков</b>")
        self.assertIn("1. Математика\n2. Математика", text)

    def test_friday_evening_tomorrow_is_weekend_and_no_lessons_list(self):
        now = datetime(2026, 11, 13, 18, 0, tzinfo=ZoneInfo(MSK))
        text = self.gen(now, make_report(now.date(), temp=3))
        self.assertIn("Завтра: суббота", text)
        self.assertIn("уроков нет", text)
        self.assertNotIn("1.", text)

    def test_weather_unavailable_is_one_short_line(self):
        now = datetime(2026, 11, 10, 7, 30, tzinfo=ZoneInfo(MSK))
        text = self.gen(now, None)
        self.assertIn("Погода сейчас недоступна", text)
        self.assertNotIn("<blockquote>☁️", text)
        self.assertIn("6 уроков", text)

    def test_total_message_is_short(self):
        now = datetime(2026, 11, 10, 7, 30, tzinfo=ZoneInfo(MSK))
        for seed in range(20):
            text = self.gen(now, make_report(now.date(), temp=5, prob=30, gust=8), seed=seed)
            self.assertLess(len(text), 900, seed)


class NextTaskBlockTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.events = UserEventRepository(self.db)
        self.weather = AnchoredWeather(date(2026, 11, 10))
        self.service = SummaryService(
            __import__("database.repositories", fromlist=["x"]).WeeklyScheduleRepository(self.db), self.users,
            self.weather, SummaryGenerator(rng=random.Random(2)), user_events=self.events)
        await self.users.get_or_create(1, "Анна", None); await self.users.get_or_create(2, "Борис", None)
        for uid, day in ((1, 1), (2, 1)):
            await __import__("database.repositories", fromlist=["x"]).WeeklyScheduleRepository(self.db).replace_day(
                uid, day, [LessonItem("Алгебра", 1), LessonItem("Физика", 1)])

    async def build(self, uid, h, mi=0, y=2026, mo=11, d=10, tz=MSK):
        user = await self.users.get(uid)
        return (await self.service.build(user, now=datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)))).text

    async def add(self, uid, **kw):
        return await self.events.create(uid, ev_fields(**kw), max_active=20)

    async def test_nearest_task_shown_right_after_lessons(self):
        await self.add(1, title="Секция", start_time="18:30")
        await self.add(1, title="Урок английского", start_time="16:00")
        await self.add(1, title="Зарядка", start_time="09:30")
        text = await self.build(1, 7, 30)
        self.assertIn("⏰ Ближайшая задача: <b>Зарядка</b> — 09:30", text)       # из нескольких — ближайшая
        self.assertNotIn("Секция", text); self.assertNotIn("Урок английского", text)
        lessons_end = text.index("</blockquote>", text.index("📚"))
        task_pos = text.index("⏰ Ближайшая задача")
        closing_pos = text.rindex("<i>")                                              # концовка — последний курсив
        self.assertLess(lessons_end, task_pos)                                  # сразу после расписания
        self.assertLess(task_pos, closing_pos)
        self.assertEqual(text[lessons_end + len("</blockquote>"):task_pos], "\n\n")   # без лишних блоков между ними

    async def test_next_task_moves_forward_during_the_day(self):
        await self.add(1, title="Зарядка", start_time="09:30")
        await self.add(1, title="Урок английского", start_time="16:00")
        self.assertIn("<b>Урок английского</b> — 16:00", await self.build(1, 12, 0))   # 09:30 уже прошло
        self.assertIn("<b>Урок английского</b> — 16:00", await self.build(1, 15, 30))
        self.assertNotIn("⏰", await self.build(1, 16, 1))                                # сегодня больше задач нет — блока нет

    async def test_no_block_when_no_tasks_or_nothing_today(self):
        self.assertNotIn("⏰", await self.build(1, 7, 30))                               # задач нет вообще
        await self.add(1, title="Завтрашнее", recurrence="none", event_date="2026-11-11", start_time="08:00")
        await self.add(1, title="Отключённое", start_time="12:00")
        off = (await self.events.list_for_user(1))[-1]
        await self.events.set_status(1, off.id, "disabled")
        text = await self.build(1, 7, 30)
        self.assertNotIn("⏰", text)                                                      # завтрашняя и отключённая не показываются
        self.assertNotIn("Завтрашнее", text); self.assertNotIn("Отключённое", text)

    async def test_weekly_task_only_on_its_weekday(self):
        await self.add(1, title="Секция", recurrence="weekly", weekdays_mask=weekdays_to_mask([3]), start_time="17:00")   # чт
        self.assertNotIn("⏰", await self.build(1, 7, 30))                                # 10.11.2026 — вторник
        thursday = await self.build(1, 7, 30, d=12)
        self.assertIn("<b>Секция</b> — 17:00", thursday)

    async def test_evening_summary_does_not_show_tasks(self):
        await self.add(1, title="Секция", start_time="19:30")
        self.assertNotIn("⏰", await self.build(1, 18, 0))                                # вечером — про завтра, блок только утром/днём

    async def test_tasks_are_per_user(self):
        await self.add(1, title="Задача Анны", start_time="10:00")
        await self.add(2, title="Задача Бориса", start_time="11:00")
        a, b = await self.build(1, 7, 30), await self.build(2, 7, 30)
        self.assertIn("Задача Анны", a); self.assertNotIn("Бориса", a)
        self.assertIn("Задача Бориса", b); self.assertNotIn("Анны", b)

    async def test_timezone_of_the_event_owner_is_used(self):
        await self.users.update_timezone(1, "Asia/Vladivostok")
        await self.add(1, title="Утренняя", start_time="08:30", timezone="Asia/Vladivostok")
        text = await self.build(1, 7, 30, tz="Asia/Vladivostok")
        self.assertIn("<b>Утренняя</b> — 08:30", text)

    async def test_html_in_task_title_is_escaped(self):
        await self.add(1, title="<b>Хак</b> & Ко", start_time="10:00")
        text = await self.build(1, 7, 30)
        self.assertIn("&lt;b&gt;Хак&lt;/b&gt; &amp; Ко", text)

    async def test_service_without_events_repo_still_works(self):
        plain = SummaryService(__import__("database.repositories", fromlist=["x"]).WeeklyScheduleRepository(self.db), self.users,
                               self.weather, SummaryGenerator(rng=random.Random(2)))
        text = (await plain.build(await self.users.get(1), now=datetime(2026, 11, 10, 7, 30, tzinfo=ZoneInfo(MSK)))).text
        self.assertNotIn("⏰", text)


class EveningSummaryDeliveryTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.weekly = __import__("database.repositories", fromlist=["x"]).WeeklyScheduleRepository(self.db)
        self.schedules = __import__("database.repositories", fromlist=["x"]).ScheduleRepository(self.db)
        self.log = __import__("database.repositories", fromlist=["x"]).NotificationLogRepository(self.db)
        self.weather = AnchoredWeather(date(2026, 11, 10))
        self.summaries = SummaryService(self.weekly, self.users, self.weather, SummaryGenerator(rng=random.Random(3)),
                                        user_events=UserEventRepository(self.db))

    def dispatcher(self, bot):
        return SummaryDispatcher(bot, self.users, self.schedules, self.log, self.summaries,
                                 send=functools.partial(send_message_safe, sleep=tsch.fast_sleep))

    async def make(self, uid, tz=MSK, lessons=None):
        await self.users.get_or_create(uid, f"U{uid}", None)
        await self.users.update_timezone(uid, tz)
        await self.db.execute("UPDATE schedules SET effective_from = 0 WHERE user_id = ?", (uid,))
        for wd, items in (lessons or {}).items():
            await self.weekly.replace_day(uid, wd, items)

    async def test_default_user_gets_morning_today_and_evening_tomorrow(self):
        await self.make(1, lessons={1: [LessonItem("Вторничный", 1)], 2: [LessonItem("Средовый", 1)]})   # вт и ср
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        self.assertEqual((await d.run_once(at(MSK, 2026, 11, 10, 7, 0, 20)))[Outcome.SENT], 1)           # утро вторника
        self.assertEqual((await d.run_once(at(MSK, 2026, 11, 10, 18, 0, 20)))[Outcome.SENT], 1)          # 18:00 вторника
        morning, evening = bot.sent[0][1], bot.sent[1][1]
        self.assertIn("Вторничный", morning); self.assertNotIn("Средовый", morning)
        self.assertNotIn("Завтра:", morning)
        self.assertIn("Завтра: среда", evening); self.assertIn("Средовый", evening)                     # вечером — на следующий день
        self.assertNotIn("Вторничный", evening)
        for minute in (1, 3, 20):                                                                       # повторные проходы не дублируют
            await d.run_once(at(MSK, 2026, 11, 10, 18, minute))
        self.assertEqual(len(bot.sent), 2)

    async def test_each_user_gets_evening_at_own_local_18(self):
        await self.make(1, MSK, {2: [LessonItem("МСК-среда", 1)]})
        await self.make(2, "Asia/Vladivostok", {2: [LessonItem("ВЛАД-среда", 1)]})
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at("Asia/Vladivostok", 2026, 11, 10, 18, 0, 20))              # во Владивостоке 18:00 (в Москве 11:00)
        self.assertEqual([(c, "ВЛАД-среда" in t) for c, t in bot.sent], [(2, True)])
        await d.run_once(at(MSK, 2026, 11, 10, 18, 0, 20))
        self.assertEqual(sorted(c for c, _ in bot.sent), [1, 2])
        self.assertNotIn("ВЛАД", dict(bot.sent)[1]); self.assertNotIn("МСК", dict(bot.sent)[2])

    async def test_evening_friday_and_sunday(self):
        await self.make(1, lessons={0: [LessonItem("Понедельничный", 2)]})
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at(MSK, 2026, 11, 13, 18, 0, 20))                              # пятница -> суббота
        self.assertIn("уроков нет", bot.sent[-1][1])
        await d.run_once(at(MSK, 2026, 11, 15, 18, 0, 20))                              # воскресенье -> понедельник
        self.assertIn("1. Понедельничный\n2. Понедельничный", bot.sent[-1][1])
        self.assertIn("Завтра: понедельник", bot.sent[-1][1])

    async def test_evening_with_weather_api_down(self):
        await self.make(1, lessons={2: [LessonItem("Средовый", 1)]})
        self.weather.fail = True
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        t0 = at(MSK, 2026, 11, 10, 18, 0, 20)
        self.assertEqual((await d.run_once(t0))[Outcome.DEFERRED], 1)                   # не выдумываем погоду, пробуем позже
        self.assertEqual((await d.run_once(t0 + timedelta(minutes=4)))[Outcome.DEFERRED], 1)
        self.assertEqual((await d.run_once(t0 + timedelta(minutes=8)))[Outcome.SENT], 1)
        text = bot.sent[0][1]
        self.assertIn("Погода сейчас недоступна", text); self.assertIn("Средовый", text); self.assertNotIn("°", text)

    async def test_notifications_switch_disables_both_summaries(self):
        await self.make(1)
        await self.users.set_notifications_enabled(1, False)                            # существующая настройка уведомлений
        await self.db.execute("UPDATE schedules SET effective_from = 0 WHERE user_id = 1")
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at(MSK, 2026, 11, 10, 7, 0, 20)); await d.run_once(at(MSK, 2026, 11, 10, 18, 0, 20))
        self.assertEqual(bot.sent, [])

    async def test_user_can_remove_evening_and_it_stays_removed(self):
        await self.make(1)
        evening = next(e for e in await self.schedules.list_for_user(1) if e.send_time == "18:00")
        await self.schedules.delete(1, evening.id)
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at(MSK, 2026, 11, 10, 18, 0, 20))
        self.assertEqual(bot.sent, [])
        await d.run_once(at(MSK, 2026, 11, 10, 7, 0, 20))
        self.assertEqual(len(bot.sent), 1)                                              # утренняя осталась


class MigrationV5Tests(unittest.IsolatedAsyncioTestCase):
    async def test_v4_database_gets_evening_schedule_without_touching_existing_choices(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "db.sqlite"
            conn = sqlite3.connect(path); conn.executescript(SCHEMA); conn.execute("PRAGMA user_version=1")
            for uid, name in ((1, "Анна"), (2, "Борис"), (3, "Вика")):
                conn.execute("INSERT INTO users (telegram_id,name,city,latitude,longitude,timezone) VALUES (?,?,?,?,?,?)",
                             (uid, name, "Казань", 1, 1, "Europe/Moscow"))
            conn.execute("INSERT INTO schedules (user_id, send_time, enabled) VALUES (1, '07:00', 1)")        # только утро
            conn.execute("INSERT INTO schedules (user_id, send_time, enabled) VALUES (2, '18:00', 0)")        # вечер уже есть, но ВЫКЛЮЧЕН
            conn.execute("INSERT INTO schedules (user_id, send_time, enabled) VALUES (2, '08:15', 1)")        # и своё время
            conn.commit(); conn.close()
            with mock.patch.object(migrations, "LATEST_VERSION", 4):
                db = Database(path); await db.connect()
                await db.execute("INSERT INTO broadcasts (admin_id, message, status) VALUES (9, 'старая рассылка', 'done')")
                self.assertEqual((await db.fetchone("PRAGMA user_version"))[0], 4)
                await db.close()
            db = Database(path); await db.connect()
            try:
                self.assertEqual((await db.fetchone("PRAGMA user_version"))[0], migrations.LATEST_VERSION)
                rows = await db.fetchall("SELECT user_id, send_time, enabled, effective_from FROM schedules ORDER BY user_id, send_time")
                by_user: dict[int, list] = {}
                for r in rows:
                    by_user.setdefault(r["user_id"], []).append((r["send_time"], r["enabled"]))
                self.assertEqual(by_user[1], [("07:00", 1), ("18:00", 1)])                    # добавился вечер
                self.assertEqual(by_user[2], [("08:15", 1), ("18:00", 0)])                    # выбор пользователя (вечер выключен) не тронут
                self.assertEqual(by_user[3], [("18:00", 1)])                                  # у кого не было записей — получил вечер
                new_rows = [r for r in rows if r["send_time"] == "18:00" and r["user_id"] in (1, 3)]
                self.assertTrue(all(r["effective_from"] > 1_700_000_000 for r in new_rows))   # «не задним числом»
                old = await db.fetchone("SELECT * FROM broadcasts WHERE message = 'старая рассылка'")
                self.assertEqual((old["status"], old["kind"], old["photo_file_id"], old["content_hash"]), ("done", "custom", None, None))
                idx = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertIn("idx_broadcasts_hash", idx)
            finally:
                await db.close()
            self.assertTrue((Path(d) / "db.sqlite.v4.bak").exists())

    async def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "db.sqlite"); await db.connect()
            await db.execute("INSERT INTO users (telegram_id,name,city,latitude,longitude,timezone) VALUES (1,'А','К',1,1,'UTC')")
            await db.close()
            for _ in range(3):
                db = Database(Path(d) / "db.sqlite"); await db.connect()
                await db.close()
            db = Database(Path(d) / "db.sqlite"); await db.connect()
            n = (await db.fetchone("SELECT COUNT(*) n FROM schedules WHERE user_id = 1 AND send_time = '18:00'"))["n"]
            await db.close()
            self.assertEqual(n, 0)      # миграция v5 уже применена и повторно не добавляет вечер (пользователь мог его удалить)


if __name__ == "__main__":
    unittest.main()
