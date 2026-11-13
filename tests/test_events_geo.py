"""Геокодинг (разбор запроса и ответа), журнал событий для админа и миграция v2 -> v3."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import _shims  # noqa: F401

import database.migrations as migrations
import test_scheduler as ts
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from database.database import Database
from database.models import EventRecord, LessonItem
from database.repositories import BroadcastRepository, EventLogRepository, UserRepository
from database.schema import SCHEMA
from services.broadcast import BroadcastService
from services.events import EventLog, NullEventLog
from services.events_format import FILTERS, PAGE_SIZE, format_event, format_events_page
from services.weather_models import GeoPlace, parse_geocoding, search_queries
from test_database import DbCase, make_settings


# ------------------------------------------------------------------ геокодинг

def doc_payload() -> dict:
    """Ответ в формате документации Open-Meteo geocoding (значения придуманы для проверки парсера)."""
    return {"results": [
        {"id": 1, "name": "Казань", "latitude": 55.78874, "longitude": 49.12214, "country_code": "RU",
         "timezone": "Europe/Moscow", "population": 1243500, "country": "Россия", "admin1": "Татарстан"},
        {"id": 2, "name": "Казань", "latitude": 55.78874, "longitude": 49.12214, "country_code": "RU",
         "timezone": "Europe/Moscow", "country": "Россия", "admin1": "Татарстан"},                    # дубль
        {"id": 3, "name": "Казань", "latitude": 38.1, "longitude": 28.0, "country_code": "TR",
         "country": "Турция", "admin1": "Анкара"},                                                    # нет timezone
        {"id": 4, "name": "Казань", "latitude": 999, "longitude": 28.0, "country": "Мусор"},         # невалидные координаты
        {"id": 5, "latitude": 1.0, "longitude": 1.0},                                                 # нет имени
        {"id": 6, "name": "Казань", "latitude": "abc", "longitude": 1.0},                             # не число
    ]}


class GeoParsingTests(unittest.TestCase):
    def test_parses_dedupes_and_skips_broken(self):
        places = parse_geocoding(doc_payload())
        self.assertEqual([(p.name, p.country_code) for p in places], [("Казань", "RU"), ("Казань", "TR")])
        self.assertEqual(places[0].timezone, "Europe/Moscow")
        self.assertIsNone(places[1].timezone)                          # отсутствие пояса не выдумываем
        self.assertEqual(places[0].label, "Казань, Татарстан, Россия")

    def test_qualifier_filters_but_never_empties(self):
        self.assertEqual([p.country_code for p in parse_geocoding(doc_payload(), "Россия")], ["RU"])
        self.assertEqual([p.country_code for p in parse_geocoding(doc_payload(), "tr")], ["TR"])       # код страны
        self.assertEqual([p.country_code for p in parse_geocoding(doc_payload(), "татар")], ["RU"])    # регион
        self.assertEqual(len(parse_geocoding(doc_payload(), "Атлантида")), 2)                          # ничего не подошло -> без фильтра

    def test_empty_and_garbage_payloads(self):
        for payload in ({}, {"results": None}, {"results": []}, {"results": "x"}, None, {"generationtime_ms": 0.5}):
            self.assertEqual(parse_geocoding(payload), [], payload)   # API не отдаёт results, если ничего не найдено

    def test_limit(self):
        many = {"results": [{"name": f"Город{i}", "latitude": i, "longitude": i, "country": "X"} for i in range(1, 9)]}
        self.assertEqual(len(parse_geocoding(many, limit=5)), 5)

    def test_label_not_duplicated_for_city_equal_region(self):
        self.assertEqual(GeoPlace("Москва", 1, 1, "Россия", "Москва", "Europe/Moscow").label, "Москва, Россия")


class SearchQueryTests(unittest.TestCase):
    def test_normalization(self):
        cases = {
            "Казань": (["Казань"], None),
            "  казань  ": (["казань"], None),
            "г. Казань": (["Казань"], None),
            "город Казань": (["Казань"], None),
            "Казань, Россия": (["Казань, Россия", "Казань"], "Россия"),
            "СПб": (["Санкт-Петербург"], None),
            "питер": (["Санкт-Петербург"], None),
            "Санкт Петербург": (["Санкт-Петербург"], None),
            "Ростов на Дону": (["Ростов на Дону", "Ростов-на-Дону"], None),
            "н. новгород": (["Нижний Новгород", "Нижний-Новгород"], None),
            "Paris, Texas": (["Paris, Texas", "Paris"], "Texas"),
        }
        for text, expected in cases.items():
            self.assertEqual(search_queries(text), expected, text)

    def test_unusable_input(self):
        for text in ("", " ", "а", ",", ", ,", "г."):
            self.assertEqual(search_queries(text), ([], None), repr(text))


# ------------------------------------------------------------------ журнал событий

class EventRepoTests(DbCase):
    async def test_add_list_filter_count_prune(self):
        repo = EventLogRepository(self.db)
        base = 1_700_000_000
        await repo.add("info", "user_new", "Новый: А", 1, ts=base)
        await repo.add("error", "error", "Ошибка", None, ts=base + 10)
        await repo.add("warning", "delay", "Медленно", None, ts=base + 20)
        await repo.add("warning", "weather", "Погода", None, ts=base + 30)
        await repo.add("info", "broadcast", "Рассылка", None, ts=base + 40)
        self.assertEqual([e.message for e in await repo.list(limit=10)], ["Рассылка", "Погода", "Медленно", "Ошибка", "Новый: А"])
        self.assertEqual([e.message for e in await repo.list(limit=2, offset=1)], ["Погода", "Медленно"])
        self.assertEqual([e.message for e in await repo.list(("delay", "weather"))], ["Погода", "Медленно"])
        self.assertEqual([e.message for e in await repo.list(levels=("error",))], ["Ошибка"])
        self.assertEqual(await repo.count(), 5)
        self.assertEqual(await repo.count(("delay", "weather")), 2)
        self.assertEqual(await repo.counts_since(base + 15), {"new_users": 0, "errors": 0, "delays": 2})
        self.assertEqual(await repo.counts_since(base), {"new_users": 1, "errors": 1, "delays": 2})
        removed = await repo.prune(keep_last=2)
        self.assertEqual(removed, 3)
        self.assertEqual([e.message for e in await repo.list()], ["Рассылка", "Погода"])
        self.assertEqual(await repo.prune(keep_last=10, older_than_ts=base + 35), 1)   # и по возрасту

    async def test_long_message_is_truncated_and_level_checked(self):
        repo = EventLogRepository(self.db)
        await repo.add("info", "system", "x" * 1000)
        self.assertEqual(len((await repo.list())[0].message), 500)
        with self.assertRaises(sqlite3.IntegrityError):
            await repo.add("fatal", "system", "bad level")


class EventLogServiceTests(DbCase):
    async def test_throttle_and_clock(self):
        clock = {"t": 1000.0}
        repo = EventLogRepository(self.db)
        events = EventLog(repo, clock=lambda: clock["t"])
        for _ in range(5):
            await events.log("weather", "Open-Meteo недоступен", level="warning", throttle_key="weather-down")
        self.assertEqual(await repo.count(), 1)                          # повторы подавлены
        await events.log("weather", "Другое", level="warning", throttle_key="other")
        self.assertEqual(await repo.count(), 2)
        clock["t"] += 301
        await events.log("weather", "Open-Meteo недоступен", level="warning", throttle_key="weather-down")
        self.assertEqual(await repo.count(), 3)                          # окно прошло
        await events.log("system", "без ключа")
        await events.log("system", "без ключа")
        self.assertEqual(await repo.count(), 5)                          # без throttle_key пишется всегда
        await events.log("system", "странный уровень", level="catastrophic")
        self.assertEqual((await repo.list(limit=1))[0].level, "info")

    async def test_logging_failure_never_raises(self):
        class Broken:
            async def add(self, *a, **k):
                raise RuntimeError("БД упала")
        await EventLog(Broken()).log("error", "x", level="error")          # не бросает
        await NullEventLog().log("error", "x")

    async def test_auto_prune(self):
        repo = EventLogRepository(self.db)
        events = EventLog(repo, keep_last=50)
        for i in range(120):
            await events.log("system", f"e{i}")
        self.assertLessEqual(await repo.count(), 70)                      # чистка срабатывает на 100-й записи


class EventFormatTests(unittest.TestCase):
    def test_format_event(self):
        tz = ZoneInfo("Europe/Moscow")
        ts = int(datetime(2026, 11, 10, 4, 5, tzinfo=ZoneInfo("UTC")).timestamp())
        line = format_event(EventRecord(1, ts, "info", "user_new", "Новый пользователь: <Арсений> & Ко", 123), tz)
        self.assertIn("🆕", line); self.assertIn("<code>10.11 07:05</code>", line)       # время — в часовом поясе админа
        self.assertIn("&lt;Арсений&gt; &amp; Ко", line); self.assertIn("<code>123</code>", line)
        self.assertNotIn("<Арсений>", line)
        self.assertTrue(format_event(EventRecord(2, ts, "error", "delivery", "x"), tz).startswith("🔴"))
        self.assertTrue(format_event(EventRecord(3, ts, "warning", "delay", "x"), tz).startswith("⏱"))

    def test_page_text_and_filters(self):
        tz = ZoneInfo("UTC")
        self.assertIn("Пока пусто", format_events_page([], tz, "Все", 0, 0))
        records = [EventRecord(i, 1_700_000_000 + i, "info", "system", f"событие {i}") for i in range(PAGE_SIZE)]
        text = format_events_page(records, tz, "Все", 25, 1)
        self.assertIn("стр. 2 из 3", text); self.assertIn("всего 25", text)
        self.assertEqual(set(FILTERS), {"all", "usr", "err", "dly", "dlv"})
        self.assertLess(len(text), 4096)
        worst = [EventRecord(i, 1_700_000_000, "error", "error", "я" * 500, 10**9) for i in range(PAGE_SIZE)]
        self.assertLess(len(format_events_page(worst, tz, "Ошибки", 999, 0)), 4096)      # даже худший случай влезает в сообщение


class EventIntegrationTests(ts.DeliveryCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.event_repo = EventLogRepository(self.db)
        self.events = EventLog(self.event_repo)
        self.summaries = ts.SummaryService(self.weekly, self.users, self.weather, ts.SummaryGenerator(), events=self.events)

    async def messages(self, **kw):
        return [(e.category, e.level, e.message, e.user_id) for e in await self.event_repo.list(limit=50, **kw)]

    async def test_new_user_event_once_and_hook_failure_is_harmless(self):
        calls = []

        async def hook(user):
            calls.append(user.telegram_id)
            await self.events.log("user_new", f"Новый пользователь: {user.name}, @{user.username}", user_id=user.telegram_id)

        users = UserRepository(self.db, self.settings, on_created=hook)
        await users.get_or_create(77, "Арсений", "arsen")
        await users.get_or_create(77, "Арсений", "arsen")            # повторное обращение — не «новый»
        await users.get_or_create(78, "Анна", None)
        self.assertEqual(calls, [77, 78])
        events = await self.messages(categories=("user_new",))
        self.assertEqual([m for _, _, m, _ in events], ["Новый пользователь: Анна, @None", "Новый пользователь: Арсений, @arsen"])

        async def broken(user):
            raise RuntimeError("хук упал")

        safe = UserRepository(self.db, self.settings, on_created=broken)
        created = await safe.get_or_create(79, "Борис", None)         # регистрация не ломается
        self.assertEqual(created.telegram_id, 79)

    async def test_blocked_and_failed_delivery_events(self):
        await self.make_user(1, "Блок", "Г", "Europe/Moscow")
        await self.make_user(2, "Сеть", "Г", "Europe/Moscow")
        await self.make_user(3, "Норм", "Г", "Europe/Moscow")
        bot = ts.FakeBot({1: TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user"),
                          2: TelegramNetworkError(method=None, message="timeout")})
        await self.dispatcher(bot, events=self.events).run_once(ts.utc(2026, 11, 10, 4, 0, 30))
        events = await self.messages()
        self.assertIn(("user_blocked", "info", "Пользователь заблокировал бота (сводка не доставлена)", 1), events)
        self.assertIn(("delivery", "error", "Не удалось отправить сводку (07:00)", 2), events)
        self.assertFalse([e for e in events if e[3] == 3])           # у успешного пользователя событий нет

    async def test_late_summaries_logged_once_aggregated(self):
        for uid in (1, 2, 3):
            await self.make_user(uid, f"U{uid}", "Г", "Europe/Moscow")
        await self.dispatcher(ts.FakeBot(), events=self.events).run_once(ts.utc(2026, 11, 10, 4, 40))   # бот проснулся в 07:40
        delays = await self.messages(categories=("delay",))
        self.assertEqual(len(delays), 1)                              # одна строка на всех, а не по записи на человека
        self.assertIn("с опозданием: 3", delays[0][2]); self.assertIn("40 мин", delays[0][2])

    async def test_on_time_summary_is_not_a_delay(self):
        await self.make_user(1, "А", "Г", "Europe/Moscow")
        await self.dispatcher(ts.FakeBot(), events=self.events).run_once(ts.utc(2026, 11, 10, 4, 0, 40))
        self.assertEqual(await self.messages(), [])

    async def test_weather_outage_event_is_throttled(self):
        await self.make_user(1, "А", "Г", "Europe/Moscow")
        await self.make_user(2, "Б", "Г", "Europe/Moscow")
        self.weather.fail = True
        user1, user2 = await self.users.get(1), await self.users.get(2)
        for u in (user1, user2, user1):
            await self.summaries.build(u, now=datetime(2026, 11, 10, 7, 0, tzinfo=ZoneInfo("Europe/Moscow")))
        events = await self.messages(categories=("weather",))
        self.assertEqual(len(events), 1)                              # три сбоя — одна запись
        self.assertIn("Open-Meteo недоступен", events[0][2])

    async def test_slow_weather_is_a_delay_event(self):
        await self.make_user(1, "А", "Г", "Europe/Moscow")
        with mock.patch("services.summary_service.SLOW_WEATHER_SECONDS", -1.0):
            await self.summaries.build(await self.users.get(1), now=datetime(2026, 11, 10, 7, 0, tzinfo=ZoneInfo("Europe/Moscow")))
        delays = await self.messages(categories=("delay",))
        self.assertEqual(len(delays), 1)
        self.assertIn("отвечает медленно", delays[0][2])

    async def test_broadcast_start_and_finish_events(self):
        for uid in (1, 2, 3):
            await self.users.get_or_create(uid, f"U{uid}", None)
        bot = ts.FakeBot({2: TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")})

        async def sleep(_):
            return None

        svc = BroadcastService(bot, self.users, BroadcastRepository(self.db), 1000.0,
                               send=lambda b, uid, text, **kw: __import__("services.telegram_send", fromlist=["x"]).send_message_safe(b, uid, text, sleep=sleep, **kw),
                               sleep=sleep, events=self.events)
        bid = await svc.create_draft(999, "Привет")
        await svc.run(bid)
        events = await self.messages(categories=("broadcast",))
        texts = [m for _, _, m, _ in events]
        self.assertTrue(any("запущена: получателей 3" in t for t in texts))
        self.assertTrue(any("отправлено 2 из 3" in t and "недоступны 1" in t for t in texts))


# ------------------------------------------------------------------ миграция v2 -> v3

class MigrationV3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_database_upgrades_to_v3_keeping_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "db.sqlite"
            conn = sqlite3.connect(path)
            conn.executescript(SCHEMA); conn.execute("PRAGMA user_version=1")
            conn.execute("INSERT INTO users (telegram_id,name,city,latitude,longitude,timezone) VALUES (5,'Анна','Самара',1,1,'Europe/Samara')")
            conn.commit(); conn.close()

            with mock.patch.object(migrations, "LATEST_VERSION", 2):          # «версия бота v2»
                db = Database(path); await db.connect()
                self.assertEqual((await db.fetchone("PRAGMA user_version"))[0], 2)
                tables = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("event_log", tables)
                weekly = __import__("database.repositories", fromlist=["x"]).WeeklyScheduleRepository(db)
                await weekly.replace_day(5, 0, [LessonItem("Математика", 2)])
                await db.close()

            db = Database(path); await db.connect()                           # обновление до v3
            try:
                self.assertEqual((await db.fetchone("PRAGMA user_version"))[0], migrations.LATEST_VERSION)   # дошла до актуальной
                self.assertEqual((await db.fetchone("SELECT name FROM users WHERE telegram_id = 5"))["name"], "Анна")
                self.assertEqual((await db.fetchone("SELECT COUNT(*) n FROM weekly_schedule"))["n"], 1)
                self.assertEqual((await db.fetchone("SELECT COUNT(*) n FROM event_log"))["n"], 0)
                idx = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertTrue({"idx_event_log_ts", "idx_event_log_category_ts"} <= idx)
            finally:
                await db.close()
            self.assertTrue((Path(d) / "db.sqlite.v2.bak").exists())          # копия перед миграцией 2 -> 3


if __name__ == "__main__":
    unittest.main()
