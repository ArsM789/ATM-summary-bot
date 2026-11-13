"""Миграции и репозитории на настоящем SQL (sqlite3; без aiosqlite используется тестовый шим)."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _shims  # noqa: F401

from config import Settings
from database.database import Database
from database.migrations import LATEST_VERSION
from database.models import LessonItem
from database.repositories import (
    BroadcastRepository, DuplicateTimeError, FeedbackRepository, LessonsLimitError, NotificationLogRepository,
    ScheduleLimitError, ScheduleRepository, UserRepository, WeeklyScheduleRepository,
)
from database.schema import SCHEMA


def make_settings(tmp: Path) -> Settings:
    return Settings(
        bot_token="1:x", bot_name="T", default_city="Казань", default_latitude=55.79, default_longitude=49.12,
        default_timezone="Europe/Moscow", default_send_time="07:00", admin_id=1, broadcast_rate=15.0, max_user_events=10,
        database_path=tmp / "t.db", weather_cache_ttl=0, weather_timeout=5, weather_retries=0,
        log_level="INFO", custom_emoji={},
    )


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = make_settings(self.tmp)
        self.db = Database(self.settings.database_path)
        await self.db.connect()
        self.users = UserRepository(self.db, self.settings)
        self.weekly = WeeklyScheduleRepository(self.db)
        self.schedules = ScheduleRepository(self.db)
        self.log = NotificationLogRepository(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def make_v1_db(self, path: Path) -> None:
        """База ровно в том виде, как её создавала предыдущая версия бота (схема v1 + данные)."""
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.executemany(
            "INSERT INTO users (telegram_id,name,city,latitude,longitude,timezone,notifications_enabled) VALUES (?,?,?,?,?,?,?)",
            [(10, "Арсений", "Казань", 55.79, 49.12, "Europe/Moscow", 1),
             (20, "Анна", "Самара", 53.2, 50.1, "Europe/Samara", 0)],
        )
        conn.executemany("INSERT INTO lessons VALUES (?,?,?)", [(10, 0, 7), (10, 1, 6), (20, 0, 5)])
        conn.execute("INSERT INTO schedules (user_id,send_time,enabled,weekdays_mask) VALUES (10,'07:30',1,127)")
        conn.execute("INSERT INTO schedules (user_id,send_time,enabled,weekdays_mask,last_sent_on) VALUES (20,'08:00',0,31,'2026-11-01')")
        conn.commit()
        conn.close()

    async def test_v1_to_latest_keeps_all_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            self.make_v1_db(path)
            db = Database(path)
            await db.connect()
            try:
                self.assertEqual((await db.fetchone("PRAGMA user_version"))[0], LATEST_VERSION)
                # старые данные на месте
                u = await db.fetchone("SELECT * FROM users WHERE telegram_id = 10")
                self.assertEqual((u["name"], u["city"], u["timezone"], u["notifications_enabled"]),
                                 ("Арсений", "Казань", "Europe/Moscow", 1))
                self.assertEqual((await db.fetchone("SELECT COUNT(*) n FROM users"))["n"], 2)
                self.assertEqual((await db.fetchone("SELECT COUNT(*) n FROM lessons"))["n"], 3)  # старая таблица не тронута
                s = await db.fetchone("SELECT * FROM schedules WHERE user_id = 20")
                self.assertEqual((s["send_time"], s["enabled"], s["weekdays_mask"]), ("08:00", 0, 31))
                # новые поля: безопасные значения по умолчанию
                self.assertEqual((u["name_confirmed"], u["bot_blocked"], u["name_variants"]), (0, 0, "[]"))
                self.assertEqual(u["first_name"], "Арсений")
                self.assertEqual(s["effective_from"], 0)
                # новые таблицы
                tables = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"weekly_schedule", "notification_log", "feedback", "broadcasts",
                                 "broadcast_deliveries", "calendar_events"} <= tables)
                # индексы
                idx = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertTrue({"idx_weekly_schedule_user_day", "idx_notification_log_user_date", "idx_feedback_user"} <= idx)
            finally:
                await db.close()
            self.assertTrue((Path(d) / "old.db.v1.bak").exists(), "перед миграцией должна создаваться копия")

    async def test_migration_is_idempotent_and_old_user_flow(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            self.make_v1_db(path)
            for _ in range(3):  # повторные запуски ничего не ломают
                db = Database(path)
                await db.connect()
                self.assertEqual((await db.fetchone("SELECT COUNT(*) n FROM users"))["n"], 2)
                await db.close()
            db = Database(path)
            await db.connect()
            users = UserRepository(db, make_settings(Path(d)))
            old = await users.get_or_create(10, "ArseniyTG", "arsen")
            self.assertFalse(old.name_confirmed)           # старому пользователю предложат заполнить имя
            self.assertEqual(old.name, "Арсений")          # имя и настройки не потеряны
            self.assertEqual(old.city, "Казань")
            self.assertEqual(old.username, "arsen")        # username подтянулся при обращении
            await users.set_names(10, "Арсений", ["Арсений", "Сеня"])
            self.assertTrue((await users.get(10)).name_confirmed)
            await db.close()

    async def test_fresh_db(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "new.db")
            await db.connect()
            self.assertEqual((await db.fetchone("PRAGMA user_version"))[0], LATEST_VERSION)
            self.assertFalse((Path(d) / "new.db.v1.bak").exists())
            await db.close()


class UserAndNameRepoTests(DbCase):
    async def test_new_user_defaults(self):
        u = await self.users.get_or_create(1, "Арсений", "arsen")
        self.assertEqual((u.name, u.city, u.timezone, u.username, u.name_confirmed), ("Арсений", "Казань", "Europe/Moscow", "arsen", False))
        sch = await self.schedules.list_for_user(1)
        self.assertEqual([(e.send_time, e.enabled) for e in sch], [("07:00", True)])
        self.assertEqual(await self.weekly.get_week(1), {})  # выдуманных уроков нет
        again = await self.users.get_or_create(1, "Арсений", "arsen")
        self.assertEqual(len(await self.schedules.list_for_user(1)), 1)  # повторный вызов не дублирует
        self.assertEqual(again.telegram_id, 1)

    async def test_names_roundtrip_and_blocked_reset(self):
        await self.users.get_or_create(1, "Арсений", None)
        await self.users.set_names(1, "Арсений", ["Арсений", "Сеня", "Арсен"])
        await self.users.set_last_used_name(1, "Сеня")
        u = await self.users.get(1)
        self.assertEqual(u.name_variants, ("Арсений", "Сеня", "Арсен"))
        self.assertEqual((u.last_used_name, u.name_confirmed), ("Сеня", True))
        await self.users.set_bot_blocked(1, True)
        self.assertTrue((await self.users.get(1)).bot_blocked)
        u2 = await self.users.get_or_create(1, "Арсений", "newnick")  # пользователь снова написал боту
        self.assertFalse(u2.bot_blocked)
        self.assertEqual(u2.username, "newnick")
        self.assertEqual(u2.name, "Арсений")  # имя из Telegram не затирает введённое

    async def test_timezone_change_resets_effective_from(self):
        await self.users.get_or_create(1, "A", None)
        await self.db.execute("UPDATE schedules SET effective_from = 5 WHERE user_id = 1")
        await self.users.update_timezone(1, "Asia/Yekaterinburg")
        e = (await self.schedules.list_for_user(1))[0]
        self.assertGreater(e.effective_from, 1_700_000_000)
        self.assertEqual((await self.users.get(1)).timezone, "Asia/Yekaterinburg")


class AdminQueriesTests(DbCase):
    async def test_stats_recent_and_broadcast_targets(self):
        for uid in (1, 2, 3):
            await self.users.get_or_create(uid, f"U{uid}", f"nick{uid}")
        await self.users.set_names(1, "U1", ["U1"])
        await self.users.set_bot_blocked(2, True)
        await self.users.set_notifications_enabled(3, False)
        stats = await self.users.stats()
        self.assertEqual(stats, {"total": 3, "blocked": 1, "notifications": 2, "named": 1})
        self.assertEqual({u.telegram_id for u in await self.users.list_recent(10)}, {1, 2, 3})
        self.assertEqual(await self.users.list_broadcast_targets(exclude_id=1), [(2, True), (3, False)])
        self.assertEqual(len(await self.users.list_broadcast_targets(exclude_id=None)), 3)


class WeeklyScheduleTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for uid in (1, 2):
            await self.users.get_or_create(uid, f"U{uid}", None)

    async def test_replace_keeps_order_and_isolated_days_and_users(self):
        a = [LessonItem("Математика", 2), LessonItem("Физика", 1), LessonItem("Русский язык", 1)]
        await self.weekly.replace_day(1, 0, a)
        await self.weekly.replace_day(1, 1, [LessonItem("Алгебра", 1)])
        await self.weekly.replace_day(2, 0, [LessonItem("Чужой предмет", 3)])
        self.assertEqual(await self.weekly.get_day(1, 0), a)                       # порядок как введён
        await self.weekly.replace_day(1, 0, [LessonItem("История", 1)])           # замена одного дня…
        self.assertEqual(await self.weekly.get_day(1, 0), [LessonItem("История", 1)])
        self.assertEqual(await self.weekly.get_day(1, 1), [LessonItem("Алгебра", 1)])    # …не трогает остальные
        self.assertEqual(await self.weekly.get_day(2, 0), [LessonItem("Чужой предмет", 3)])  # …и других пользователей

    async def test_append_and_clear(self):
        await self.weekly.replace_day(1, 2, [LessonItem("А", 1), LessonItem("Б", 2)])
        combined = await self.weekly.append_to_day(1, 2, [LessonItem("В", 1)])
        self.assertEqual([i.subject for i in combined], ["А", "Б", "В"])
        self.assertEqual([i.subject for i in await self.weekly.get_day(1, 2)], ["А", "Б", "В"])
        self.assertEqual(await self.weekly.clear_day(1, 2), 3)
        self.assertEqual(await self.weekly.get_day(1, 2), [])
        self.assertEqual(await self.weekly.clear_day(1, 2), 0)  # повторная очистка безопасна
        await self.weekly.append_to_day(1, 2, [LessonItem("Г", 1)])  # после очистки позиции начинаются заново
        self.assertEqual(await self.weekly.get_day(1, 2), [LessonItem("Г", 1)])

    async def test_limits_and_atomicity(self):
        await self.weekly.replace_day(1, 3, [LessonItem("А", 6), LessonItem("Б", 6)])
        with self.assertRaises(LessonsLimitError):
            await self.weekly.append_to_day(1, 3, [LessonItem("В", 1)])
        self.assertEqual(len(await self.weekly.get_day(1, 3)), 2)   # ничего не записалось
        with self.assertRaises(LessonsLimitError):
            await self.weekly.replace_day(1, 3, [LessonItem("А", 12), LessonItem("Б", 1)])
        self.assertEqual(await self.weekly.get_day(1, 3), [LessonItem("А", 6), LessonItem("Б", 6)])  # старое цело
        with self.assertRaises(ValueError):
            await self.weekly.replace_day(1, 3, [LessonItem("  ", 1)])

    async def test_week_for_summary_and_empty(self):
        self.assertEqual(await self.weekly.get_week(1), {})
        await self.weekly.replace_day(1, 4, [LessonItem("Физкультура", 1)])
        self.assertEqual(await self.weekly.get_week(1), {4: [LessonItem("Физкультура", 1)]})

    async def test_cascade_on_user_delete(self):
        await self.weekly.replace_day(1, 0, [LessonItem("А", 1)])
        await self.db.execute("DELETE FROM users WHERE telegram_id = 1")
        self.assertEqual(await self.weekly.get_week(1), {})


class ScheduleRepoTests(DbCase):
    async def test_add_duplicate_limit_ownership(self):
        await self.users.get_or_create(1, "A", None)
        await self.users.get_or_create(2, "B", None)
        e = await self.schedules.add(1, "13:30")
        with self.assertRaises(DuplicateTimeError):
            await self.schedules.add(1, "13:30")
        await self.schedules.add(2, "13:30")  # у другого пользователя то же время — можно
        with self.assertRaises(DuplicateTimeError):
            await self.schedules.update_time(1, e.id, "07:00")
        for i in range(8):  # уже есть 07:00 и 13:30 -> ещё 8 = лимит 10
            await self.schedules.add(1, f"1{i}:15")
        with self.assertRaises(ScheduleLimitError):
            await self.schedules.add(1, "23:59")
        # чужое не трогаем
        self.assertFalse(await self.schedules.delete(2, e.id))
        self.assertFalse(await self.schedules.set_enabled(2, e.id, False))
        self.assertIsNone(await self.schedules.get(2, e.id))
        self.assertTrue(await self.schedules.set_enabled(1, e.id, False))
        self.assertTrue(await self.schedules.delete(1, e.id))

    async def test_list_all_active_filters(self):
        for uid in (1, 2, 3, 4):
            await self.users.get_or_create(uid, f"U{uid}", None)
        await self.users.set_notifications_enabled(2, False)
        await self.users.set_bot_blocked(3, True)
        e4 = (await self.schedules.list_for_user(4))[0]
        await self.schedules.set_enabled(4, e4.id, False)
        active = await self.schedules.list_all_active()
        self.assertEqual([(s.user_id, u.telegram_id) for s, u in active], [(1, 1)])


class NotificationLogTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.users.get_or_create(1, "A", None)
        await self.users.get_or_create(2, "B", None)

    async def test_single_successful_send_per_slot_and_date(self):
        c = await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=1000)
        self.assertEqual(c.attempt, 1)
        self.assertIsNone(await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=1001))  # идёт сейчас
        await self.log.finish(c.log_id, "sent", now_ts=1002)
        self.assertIsNone(await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=99999))  # уже отправлено
        self.assertIsNotNone(await self.log.claim(1, "summary", "07:00", "2026-11-11", now_ts=99999))  # другая дата
        self.assertIsNotNone(await self.log.claim(1, "summary", "13:30", "2026-11-10", now_ts=99999))   # другой слот
        self.assertIsNotNone(await self.log.claim(2, "summary", "07:00", "2026-11-10", now_ts=99999))   # другой пользователь

    async def test_failed_is_retried_after_gap_up_to_max(self):
        c1 = await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=1000, retry_gap=180, max_attempts=3)
        await self.log.finish(c1.log_id, "failed", "net", now_ts=1000)
        self.assertIsNone(await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=1100, retry_gap=180))   # рано
        c2 = await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=1200, retry_gap=180)
        self.assertEqual(c2.attempt, 2)
        await self.log.finish(c2.log_id, "failed", "net", now_ts=1200)
        c3 = await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=1500, retry_gap=180)
        self.assertEqual(c3.attempt, 3)
        await self.log.finish(c3.log_id, "failed", "net", now_ts=1500)
        self.assertIsNone(await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=99999, retry_gap=180))  # попытки кончились

    async def test_blocked_is_final_and_crash_recovery(self):
        c = await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=1000)
        await self.log.finish(c.log_id, "blocked", "Forbidden", now_ts=1001)
        self.assertIsNone(await self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=99999))
        # процесс «упал» посреди отправки: запись осталась sending — через stale_after её можно перехватить
        c = await self.log.claim(2, "summary", "07:00", "2026-11-10", now_ts=1000)
        self.assertIsNone(await self.log.claim(2, "summary", "07:00", "2026-11-10", now_ts=1100, stale_after=600))
        again = await self.log.claim(2, "summary", "07:00", "2026-11-10", now_ts=1700, stale_after=600)
        self.assertEqual(again.attempt, 2)

    async def test_parallel_claims_have_exactly_one_winner(self):
        results = await asyncio.gather(*(self.log.claim(1, "summary", "07:00", "2026-11-10", now_ts=1000) for _ in range(25)))
        self.assertEqual(sum(r is not None for r in results), 1)


class FeedbackAndBroadcastRepoTests(DbCase):
    async def test_feedback(self):
        await self.users.get_or_create(1, "Арсений", "arsen")
        fb = FeedbackRepository(self.db)
        fid = await fb.create(1, "Бот не отвечает")
        await fb.set_status(fid, "delivered")
        items = await fb.list_recent()
        self.assertEqual((items[0].message, items[0].status, items[0].user_name, items[0].username),
                         ("Бот не отвечает", "delivered", "Арсений", "arsen"))
        self.assertEqual(await fb.count_since(1, 0), 1)
        self.assertEqual(await fb.count_since(1, 9_999_999_999), 0)
        with self.assertRaises(sqlite3.IntegrityError):  # пустое обращение БД не принимает
            await fb.create(1, "   ")

    async def test_broadcast_state_machine(self):
        repo = BroadcastRepository(self.db)
        bid = await repo.create_draft(99, "Привет")
        targets = [(1, False), (2, True), (3, False)]
        self.assertTrue(await repo.claim_start(bid, targets))
        self.assertFalse(await repo.claim_start(bid, targets))           # повторный запуск невозможен
        self.assertFalse(await repo.cancel(bid))                          # запущенную отменить нельзя
        other = await repo.create_draft(99, "Другая")
        self.assertFalse(await repo.claim_start(other, targets))         # пока идёт одна, вторая не стартует
        self.assertEqual(await repo.next_pending(bid), [1, 3])            # заблокированный известен заранее
        await repo.mark_delivery(bid, 1, "sent")
        await repo.mark_delivery(bid, 1, "failed", "x")                   # повторная отметка не меняет результат
        counts = await repo.counts(bid)
        self.assertEqual((counts["sent"], counts["failed"], counts["blocked"], counts["pending"]), (1, 0, 1, 1))
        await repo.mark_delivery(bid, 3, "failed", "boom")
        final = await repo.finish(bid, "done")
        self.assertEqual((final.total_count, final.success_count, final.failed_count, final.blocked_count), (3, 1, 1, 1))
        self.assertTrue(await repo.cancel(other))
        self.assertEqual(await repo.mark_interrupted(), 0)


if __name__ == "__main__":
    unittest.main()
