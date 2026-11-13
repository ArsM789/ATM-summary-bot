"""События пользователей: репозиторий (лимит, изоляция, транзакции), миграция v3 -> v4, диспетчер напоминаний."""
from __future__ import annotations

import asyncio
import functools
import random
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import _shims  # noqa: F401

import database.migrations as migrations
import test_scheduler as tsch
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from database.database import Database
from database.models import weekdays_to_mask
from database.repositories import (
    EventLimitError, EventLogRepository, NotificationLogRepository, UserEventRepository, UserRepository,
)
from database.schema import SCHEMA
from services.event_reminders import EventReminderDispatcher
from services.event_schedule import find_due_reminders
from services.delivery import Outcome
from services.events import EventLog
from services.telegram_send import send_message_safe
from test_database import DbCase, make_settings

MSK = "Europe/Moscow"
THU = (2026, 11, 12)   # четверг


def at(tz, y, mo, d, h, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(tz)).astimezone(tsch.UTC)


def fields(**kw) -> dict:
    base = dict(title="Урок английского", recurrence="weekly", weekdays_mask=weekdays_to_mask([3]), month_day=None,
                event_date="2026-11-01", start_time="16:00", end_time="18:00", duration_min=120,
                remind_before_min=20, timezone=MSK)
    base.update(kw)
    return base


class EventCase(DbCase):
    LIMIT = 3

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.events = UserEventRepository(self.db)

    async def user(self, uid, tz=MSK, name=None, variants=None):
        await self.users.get_or_create(uid, name or f"U{uid}", None)
        await self.users.set_names(uid, name or f"U{uid}", variants or [name or f"U{uid}"])
        if tz != self.settings.default_timezone:
            await self.users.update_timezone(uid, tz)

    async def make(self, uid, eff=0, **kw):
        """Создаёт событие и «состаривает» effective_from (репозиторий ставит реальное «сейчас»)."""
        kw.setdefault("timezone", (await self.users.get(uid)).timezone)
        ev = await self.events.create(uid, fields(**kw), max_active=100)
        await self.db.execute("UPDATE calendar_events SET effective_from = ? WHERE id = ?", (eff, ev.id))
        return await self.events.get(uid, ev.id)


class EventRepoTests(EventCase):
    async def test_roundtrip_all_recurrence_kinds(self):
        await self.user(1)
        cases = [
            dict(recurrence="none", event_date="2026-11-12", weekdays_mask=0),
            dict(recurrence="daily", weekdays_mask=0),
            dict(recurrence="weekly", weekdays_mask=weekdays_to_mask([1, 3])),
            dict(recurrence="monthly", month_day=31, weekdays_mask=0),
            dict(recurrence="monthly", month_day=-1, weekdays_mask=0),
        ]
        for kw in cases:
            ev = await self.events.create(1, fields(**kw), max_active=50)
            got = await self.events.get(1, ev.id)
            self.assertEqual((got.recurrence, got.weekdays_mask, got.month_day, got.event_date, got.start_time,
                              got.duration_min, got.remind_before_min, got.timezone, got.status),
                             (kw["recurrence"], kw.get("weekdays_mask", 0), kw.get("month_day"), kw.get("event_date", "2026-11-01"),
                              "16:00", 120, 20, MSK, "active"))
            self.assertGreater(got.effective_from, 0)
        self.assertEqual(len(await self.events.list_for_user(1)), 5)

    async def test_limit_counts_only_active_and_is_atomic(self):
        await self.user(1)
        made = [await self.events.create(1, fields(title=f"E{i}"), max_active=self.LIMIT) for i in range(3)]
        with self.assertRaises(EventLimitError) as cm:
            await self.events.create(1, fields(title="E3"), max_active=self.LIMIT)
        self.assertEqual(cm.exception.limit, 3)
        self.assertEqual(await self.events.count_active(1), 3)
        # отключённое не считается в лимите
        self.assertEqual(await self.events.set_status(1, made[0].id, "disabled"), "ok")
        self.assertEqual(await self.events.count_active(1), 2)
        extra = await self.events.create(1, fields(title="E4"), max_active=self.LIMIT)
        # включить обратно при заполненном лимите нельзя
        self.assertEqual(await self.events.set_status(1, made[0].id, "active", max_active=self.LIMIT), "limit")
        # редактирование при заполненном лимите разрешено
        self.assertTrue(await self.events.update(1, extra.id, fields(title="Переименовано")))
        self.assertEqual((await self.events.get(1, extra.id)).title, "Переименовано")
        # освободили место — включение проходит
        await self.events.delete(1, extra.id)
        self.assertEqual(await self.events.set_status(1, made[0].id, "active", max_active=self.LIMIT), "ok")

    async def test_parallel_creation_never_exceeds_limit(self):
        await self.user(1)
        results = await asyncio.gather(
            *(self.events.create(1, fields(title=f"P{i}"), max_active=self.LIMIT) for i in range(12)), return_exceptions=True)
        self.assertEqual(sum(not isinstance(r, Exception) for r in results), 3)
        self.assertTrue(all(isinstance(r, EventLimitError) for r in results if isinstance(r, Exception)))
        self.assertEqual(await self.events.count_active(1), 3)

    async def test_limit_is_per_user(self):
        await self.user(1); await self.user(2)
        for i in range(3):
            await self.events.create(1, fields(title=f"A{i}"), max_active=3)
        await self.events.create(2, fields(title="B"), max_active=3)          # у второго лимит свой
        self.assertEqual((await self.events.count_active(1), await self.events.count_active(2)), (3, 1))

    async def test_isolation_between_users(self):
        await self.user(1); await self.user(2)
        mine = await self.events.create(1, fields(title="Моё"), max_active=10)
        self.assertIsNone(await self.events.get(2, mine.id))
        self.assertFalse(await self.events.update(2, mine.id, fields(title="Взлом")))
        self.assertFalse(await self.events.delete(2, mine.id))
        self.assertEqual(await self.events.set_status(2, mine.id, "disabled"), "missing")
        self.assertEqual(await self.events.list_for_user(2), [])
        untouched = await self.events.get(1, mine.id)
        self.assertEqual((untouched.title, untouched.status), ("Моё", "active"))

    async def test_delete_cancels_future_notifications(self):
        await self.user(1)
        a = await self.events.create(1, fields(title="A"), max_active=10)
        b = await self.events.create(1, fields(title="B"), max_active=10)
        log = NotificationLogRepository(self.db)
        await log.claim(1, "event", f"{a.id}@16:00", "2026-11-12", now_ts=1)
        await log.claim(1, "event", f"{b.id}@16:00", "2026-11-12", now_ts=1)
        await log.claim(1, "summary", "07:00", "2026-11-12", now_ts=1)
        self.assertTrue(await self.events.delete(1, a.id))
        self.assertFalse(await self.events.delete(1, a.id))                     # повторное удаление безопасно
        self.assertIsNone(await log.get(1, "event", f"{a.id}@16:00", "2026-11-12"))
        self.assertIsNotNone(await log.get(1, "event", f"{b.id}@16:00", "2026-11-12"))     # чужое/другое не тронуто
        self.assertIsNotNone(await log.get(1, "summary", "07:00", "2026-11-12"))

    async def test_validation(self):
        await self.user(1)
        bad_cases = [dict(title="  "), dict(recurrence="yearly"), dict(recurrence="weekly", weekdays_mask=0),
                     dict(recurrence="monthly", month_day=None, weekdays_mask=0), dict(recurrence="monthly", month_day=32),
                     dict(recurrence="none", event_date=None), dict(duration_min=0), dict(duration_min=1441),
                     dict(remind_before_min=0), dict(remind_before_min=1441)]
        for kw in bad_cases:
            with self.assertRaises(ValueError, msg=str(kw)):
                await self.events.create(1, fields(**kw), max_active=10)
        self.assertEqual(await self.events.count_active(1), 0)                   # ничего не записалось

    async def test_update_any_field_in_place(self):
        await self.user(1)
        ev = await self.make(1)
        new = fields(title="Новое", recurrence="monthly", month_day=15, weekdays_mask=0, start_time="09:30",
                     end_time="10:00", duration_min=30, remind_before_min=5)
        self.assertTrue(await self.events.update(1, ev.id, new))
        got = await self.events.get(1, ev.id)
        self.assertEqual((got.id, got.title, got.recurrence, got.month_day, got.start_time, got.duration_min, got.remind_before_min, got.status),
                         (ev.id, "Новое", "monthly", 15, "09:30", 30, 5, "active"))
        self.assertGreater(got.effective_from, 0)                                # правка сдвигает «не раньше»
        self.assertEqual(len(await self.events.list_for_user(1)), 1)             # новое событие не создавалось

    async def test_done_events_hidden_and_not_counted_and_pruned(self):
        await self.user(1)
        ev = await self.events.create(1, fields(recurrence="none", event_date="2026-11-12", weekdays_mask=0), max_active=3)
        self.assertTrue(await self.events.mark_done(ev.id))
        self.assertEqual(await self.events.list_for_user(1), [])
        self.assertEqual(await self.events.count_active(1), 0)
        self.assertEqual(await self.events.set_status(1, ev.id, "active"), "done")   # завершённое не включить
        self.assertFalse(await self.events.update(1, ev.id, fields()))
        recurring = await self.events.create(1, fields(), max_active=3)
        self.assertFalse(await self.events.mark_done(recurring.id))              # повторяющиеся не «завершаются»
        await self.db.execute("UPDATE calendar_events SET created_at = datetime('now', '-90 days') WHERE id = ?", (ev.id,))
        self.assertEqual(await self.events.prune_done(60), 1)

    async def test_timezone_change_follows_user(self):
        await self.user(1)
        ev = await self.make(1, eff=5)
        await self.users.update_timezone(1, "Asia/Yekaterinburg")
        got = await self.events.get(1, ev.id)
        self.assertEqual(got.timezone, "Asia/Yekaterinburg")
        self.assertGreater(got.effective_from, 5)

    async def test_user_event_settings(self):
        await self.user(1)
        u = await self.users.get(1)
        self.assertEqual((u.event_reminders_enabled, u.default_remind_minutes), (True, 15))
        await self.users.set_event_settings(1, reminders_enabled=False, default_remind_minutes=30)
        u = await self.users.get(1)
        self.assertEqual((u.event_reminders_enabled, u.default_remind_minutes), (False, 30))
        for bad in (0, 1441):
            with self.assertRaises(ValueError):
                await self.users.set_event_settings(1, default_remind_minutes=bad)

    async def test_active_with_users_join(self):
        await self.user(1, name="Соф"); await self.user(2, tz="Asia/Vladivostok")
        a = await self.make(1); b = await self.make(2, title="B")
        off = await self.make(1, title="off"); await self.events.set_status(1, off.id, "disabled")
        pairs = await self.events.list_active_with_users()
        self.assertEqual({(e.id, u.telegram_id, u.timezone) for e, u in pairs}, {(a.id, 1, MSK), (b.id, 2, "Asia/Vladivostok")})
        self.assertEqual({u.name for _, u in pairs if u.telegram_id == 1}, {"Соф"})


class MigrationV4Tests(unittest.IsolatedAsyncioTestCase):
    async def test_v3_database_upgrades_keeping_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "db.sqlite"
            conn = sqlite3.connect(path); conn.executescript(SCHEMA); conn.execute("PRAGMA user_version=1")
            conn.execute("INSERT INTO users (telegram_id,name,city,latitude,longitude,timezone) VALUES (7,'Анна','Самара',1,1,'Europe/Samara')")
            conn.commit(); conn.close()
            with mock.patch.object(migrations, "LATEST_VERSION", 3):
                db = Database(path); await db.connect()
                self.assertEqual((await db.fetchone("PRAGMA user_version"))[0], 3)
                # «задел» из v2: строка в calendar_events, как её мог бы создать будущий код
                await db.execute("INSERT INTO calendar_events (user_id, event_type, title, start_time) VALUES (7, 'tutoring', 'Репетитор', '17:00')")
                await db.close()

            db = Database(path); await db.connect()
            try:
                self.assertEqual((await db.fetchone("PRAGMA user_version"))[0], migrations.LATEST_VERSION)
                cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(calendar_events)")}
                self.assertTrue({"recurrence", "weekdays_mask", "month_day", "duration_min", "remind_before_min",
                                 "timezone", "status", "effective_from", "title", "event_date", "start_time"} <= cols)
                old = await db.fetchone("SELECT * FROM calendar_events WHERE title = 'Репетитор'")
                self.assertEqual((old["event_type"], old["recurrence"], old["status"], old["duration_min"], old["remind_before_min"]),
                                 ("tutoring", "none", "active", 60, 15))      # старая строка цела, получила значения по умолчанию
                user = await db.fetchone("SELECT * FROM users WHERE telegram_id = 7")
                self.assertEqual((user["name"], user["event_reminders_enabled"], user["default_remind_minutes"]), ("Анна", 1, 15))
                idx = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertTrue({"idx_calendar_events_status", "idx_calendar_events_user_status"} <= idx)
                # репозиторий «Мои события» не видит и не считает чужие типы событий
                repo = UserEventRepository(db)
                self.assertEqual(await repo.list_for_user(7), [])
                self.assertEqual(await repo.count_active(7), 0)
                self.assertEqual(await repo.list_active_with_users(), [])
            finally:
                await db.close()
            self.assertTrue((Path(d) / "db.sqlite.v3.bak").exists())


async def fast_sleep(_):
    return None


class ReminderCase(EventCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.log = NotificationLogRepository(self.db)
        self.event_log_repo = EventLogRepository(self.db)
        self.event_log = EventLog(self.event_log_repo)

    def dispatcher(self, bot, **kw):
        return EventReminderDispatcher(bot, self.users, self.events, self.log, event_log=self.event_log,
                                       send=functools.partial(send_message_safe, sleep=fast_sleep),
                                       rng=random.Random(5), **kw)

    async def log_rows(self):
        return await self.db.fetchall("SELECT * FROM notification_log WHERE notification_type = 'event'")


class ReminderTimingTests(ReminderCase):
    async def test_sent_at_the_right_time_exactly_once(self):
        await self.user(1, name="Соф")
        await self.make(1)                                           # чт 16:00–18:00, за 20 минут
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        self.assertEqual(sum((await d.run_once(at(MSK, *THU, 15, 39, 59))).values()), 0)      # рано
        self.assertEqual((await d.run_once(at(MSK, *THU, 15, 40, 10)))[Outcome.SENT], 1)
        for minute, sec in ((40, 40), (41, 0), (43, 30), (44, 59)):                            # повторные проходы
            await d.run_once(at(MSK, *THU, 15, minute, sec))
        self.assertEqual(len(bot.sent), 1)
        chat, text = bot.sent[0]
        self.assertEqual(chat, 1)
        self.assertIn("Через 20 минут", text); self.assertIn("<b>Урок английского</b>", text)
        self.assertIn("🕓 16:00–18:00", text); self.assertIn("Соф", text)
        for foreign in ("°", "погод", "сводк", "куртк"):
            self.assertNotIn(foreign, text)                           # никакой утренней сводки
        rows = await self.log_rows()
        self.assertEqual([(r["slot"], r["notification_date"], r["status"]) for r in rows], [("1@16:00", "2026-11-12", "sent")])

    async def test_parallel_runs_do_not_duplicate(self):
        await self.user(1)
        await self.make(1)
        bot = tsch.FakeBot()
        dispatchers = [self.dispatcher(bot) for _ in range(4)]        # несколько «задач планировщика» сразу
        now = at(MSK, *THU, 15, 40, 20)
        await asyncio.gather(*(d.run_once(now) for d in dispatchers for _ in range(3)))
        self.assertEqual(len(bot.sent), 1)

    async def test_restart_does_not_resend_and_stale_reminders_are_dropped(self):
        await self.user(1); await self.user(2)
        await self.make(1); await self.make(2, title="Второе")
        bot = tsch.FakeBot()
        await self.dispatcher(bot).run_once(at(MSK, *THU, 15, 41))
        self.assertEqual(sorted(c for c, _ in bot.sent), [1, 2])
        # «перезапуск»: новое соединение и новый диспетчер поверх той же БД
        await self.db.close()
        self.db = Database(self.settings.database_path); await self.db.connect()
        users, events, log = UserRepository(self.db, self.settings), UserEventRepository(self.db), NotificationLogRepository(self.db)
        bot2 = tsch.FakeBot()
        d2 = EventReminderDispatcher(bot2, users, events, log, send=functools.partial(send_message_safe, sleep=fast_sleep))
        await d2.run_once(at(MSK, *THU, 15, 43))
        self.assertEqual(bot2.sent, [])                                # уже отправленное не дублируется
        # бот был выключен и проснулся позже grace: устаревшее напоминание не шлётся
        await users.get_or_create(3, "U3", None)
        await events.create(3, fields(title="Пропущено"), max_active=5)
        await self.db.execute("UPDATE calendar_events SET effective_from = 0 WHERE user_id = 3")
        await d2.run_once(at(MSK, *THU, 15, 50))
        self.assertEqual(bot2.sent, [])
        await d2.run_once(at(MSK, *THU, 15, 59))                       # ещё до начала, но напоминание безнадёжно устарело
        self.assertEqual(bot2.sent, [])
        self.events, self.users = events, users                         # для asyncTearDown база та же

    async def test_never_reminds_after_start_or_for_past_events(self):
        await self.user(1)
        await self.make(1)
        await self.make(1, title="Прошедшее", recurrence="none", event_date="2026-11-10", weekdays_mask=0)
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        for h, m in ((16, 0), (16, 1), (17, 30), (23, 0)):
            await d.run_once(at(MSK, *THU, h, m, 5))
        self.assertEqual(bot.sent, [])

    async def test_short_reminder_is_not_sent_once_event_started(self):
        await self.user(1)
        await self.make(1, remind_before_min=3)                         # напоминание в 15:57, начало в 16:00
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at(MSK, *THU, 16, 0, 30))                      # бот проснулся уже после начала
        await d.run_once(at(MSK, *THU, 16, 1, 30))
        self.assertEqual(bot.sent, [])
        await d.run_once(at(MSK, *THU, 15, 59, 0))                      # а до начала — напоминание всё ещё уместно
        self.assertEqual(len(bot.sent), 1); self.assertIn("Через 1 минуту", bot.sent[0][1])

    async def test_event_created_after_reminder_moment_gets_no_retroactive_reminder(self):
        await self.user(1)
        eff = int(at(MSK, *THU, 15, 42).timestamp())                    # создали в 15:42 событие на 16:00 «за 20 минут»
        await self.make(1, eff=eff, recurrence="none", event_date="2026-11-12", weekdays_mask=0)
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        for m, s in ((43, 0), (43, 30), (44, 20), (46, 0)):             # 15:40 было «до создания», хоть и в пределах grace
            await d.run_once(at(MSK, *THU, 15, m, s))
        self.assertEqual(bot.sent, [])

    async def test_long_lead_and_midnight_crossing(self):
        await self.user(1)
        await self.make(1, title="Завтра", recurrence="none", event_date="2026-11-13", weekdays_mask=0, remind_before_min=1440,
                        start_time="09:00", end_time="10:00", duration_min=60)
        await self.make(1, title="Ночное", recurrence="none", event_date="2026-11-13", weekdays_mask=0, remind_before_min=30,
                        start_time="00:10", end_time="00:40", duration_min=30)
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at(MSK, *THU, 9, 0, 10))
        await d.run_once(at(MSK, *THU, 23, 40, 10))
        texts = [t for _, t in bot.sent]
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("Через 24 часа" in t and "завтра" in t for t in texts))
        self.assertTrue(any("Ночное" in t and "Через 30 минут" in t and "00:10–00:40" in t for t in texts))

    async def test_monthly_31_fires_in_april_on_the_30th(self):
        await self.user(1)
        await self.make(1, recurrence="monthly", month_day=31, weekdays_mask=0, event_date="2026-01-01")
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at(MSK, 2026, 4, 29, 15, 40, 10)); self.assertEqual(bot.sent, [])
        await d.run_once(at(MSK, 2026, 4, 30, 15, 40, 10)); self.assertEqual(len(bot.sent), 1)
        await d.run_once(at(MSK, 2026, 5, 30, 15, 40, 10)); self.assertEqual(len(bot.sent), 1)
        await d.run_once(at(MSK, 2026, 5, 31, 15, 40, 10)); self.assertEqual(len(bot.sent), 2)


class ReminderLifecycleTests(ReminderCase):
    async def test_deleted_or_disabled_events_are_not_notified(self):
        await self.user(1)
        gone = await self.make(1, title="Удалённое"); off = await self.make(1, title="Отключённое"); keep = await self.make(1, title="Живое")
        await self.events.delete(1, gone.id)
        await self.events.set_status(1, off.id, "disabled")
        await self.db.execute("UPDATE calendar_events SET effective_from = 0")
        bot = tsch.FakeBot()
        await self.dispatcher(bot).run_once(at(MSK, *THU, 15, 41))
        self.assertEqual([t for _, t in bot.sent if "Живое" in t].__len__(), 1)
        self.assertEqual(len(bot.sent), 1)

    async def test_reenabled_event_resumes_but_not_retroactively(self):
        await self.user(1)
        ev = await self.make(1)
        await self.events.set_status(1, ev.id, "disabled")
        await self.events.set_status(1, ev.id, "active")
        await self.db.execute("UPDATE calendar_events SET effective_from = ?", (int(at(MSK, *THU, 15, 42).timestamp()),))
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at(MSK, *THU, 15, 43)); self.assertEqual(bot.sent, [])             # напоминание 15:40 уже «в прошлом»
        await d.run_once(at(MSK, 2026, 11, 19, 15, 40, 10)); self.assertEqual(len(bot.sent), 1)   # следующий четверг — штатно

    async def test_deleted_while_waiting_is_skipped_at_delivery(self):
        await self.user(1)
        ev = await self.make(1)
        now = at(MSK, *THU, 15, 40, 10)
        due = find_due_reminders([ev], now.timestamp())
        self.assertEqual(len(due), 1)
        user = await self.users.get(1)
        d = self.dispatcher(tsch.FakeBot())
        await self.events.delete(1, ev.id)                              # удалили уже после того, как напоминание «выбрали»
        self.assertEqual(await d.deliver(due[0], user, now), Outcome.SKIPPED)
        self.assertEqual(d._bot.sent, [])

    async def test_edit_changes_time_no_stale_or_retro_reminders(self):
        await self.user(1)
        ev = await self.make(1)
        now = at(MSK, *THU, 15, 40, 10)
        stale_due = find_due_reminders([ev], now.timestamp())
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        user = await self.users.get(1)
        # перенесли на 17:00: старое напоминание (15:40) недействительно, новое придёт в 16:40
        await self.events.update(1, ev.id, fields(start_time="17:00", end_time="19:00"))
        await self.db.execute("UPDATE calendar_events SET effective_from = ?", (int(at(MSK, *THU, 15, 35).timestamp()),))
        self.assertEqual(await d.deliver(stale_due[0], user, now), Outcome.SKIPPED)
        await d.run_once(now); self.assertEqual(bot.sent, [])
        await d.run_once(at(MSK, *THU, 16, 40, 10))
        self.assertEqual(len(bot.sent), 1); self.assertIn("17:00–19:00", bot.sent[0][1])
        # перенесли на 15:55 в 15:36: момент напоминания (15:35) уже прошёл (но ещё в пределах grace) -> задним числом не шлём
        await self.events.update(1, ev.id, fields(start_time="15:55", end_time="17:55"))
        await self.db.execute("UPDATE calendar_events SET effective_from = ?", (int(at(MSK, *THU, 15, 36).timestamp()),))
        before = len(bot.sent)
        await d.run_once(at(MSK, *THU, 15, 37)); self.assertEqual(len(bot.sent), before)

    async def test_title_edit_before_send_uses_new_title(self):
        await self.user(1)
        ev = await self.make(1)
        await self.events.update(1, ev.id, fields(title="Новое название"))
        await self.db.execute("UPDATE calendar_events SET effective_from = 0")
        bot = tsch.FakeBot()
        await self.dispatcher(bot).run_once(at(MSK, *THU, 15, 41))
        self.assertIn("Новое название", bot.sent[0][1])

    async def test_finished_one_time_event_is_closed_and_frees_the_limit(self):
        await self.user(1)
        ev = await self.make(1, recurrence="none", event_date="2026-11-12", weekdays_mask=0)
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at(MSK, *THU, 15, 41)); self.assertEqual(len(bot.sent), 1)
        await d.run_once(at(MSK, *THU, 18, 30))                          # ещё в пределах часа после окончания
        self.assertEqual((await self.events.get(1, ev.id)).status, "active")
        await d.run_once(at(MSK, *THU, 19, 5))
        self.assertEqual((await self.events.get(1, ev.id)).status, "done")
        self.assertEqual(await self.events.count_active(1), 0)
        self.assertEqual(await self.events.list_for_user(1), [])

    async def test_user_switches_and_blocked_users(self):
        await self.user(1); await self.user(2); await self.user(3)
        for uid in (1, 2, 3):
            await self.make(uid)
        await self.users.set_event_settings(1, reminders_enabled=False)    # выключил напоминания о событиях
        await self.users.set_bot_blocked(2, True)
        bot = tsch.FakeBot()
        await self.dispatcher(bot).run_once(at(MSK, *THU, 15, 41))
        self.assertEqual([c for c, _ in bot.sent], [3])

    async def test_summary_switch_does_not_affect_event_reminders(self):
        await self.user(1)
        await self.make(1)
        await self.users.set_notifications_enabled(1, False)               # автосводки выключены — напоминания живут отдельно
        bot = tsch.FakeBot()
        await self.dispatcher(bot).run_once(at(MSK, *THU, 15, 41))
        self.assertEqual(len(bot.sent), 1)


class ReminderMultiUserTests(ReminderCase):
    async def test_time_zones_each_user_at_own_local_time(self):
        await self.user(1, tz="Europe/Moscow"); await self.user(2, tz="Asia/Vladivostok"); await self.user(3, tz="America/New_York")
        await self.make(1, title="МСК", recurrence="daily"); await self.make(2, title="ВЛАД", recurrence="daily")
        await self.make(3, title="НЬЮ-ЙОРК", recurrence="daily")
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        await d.run_once(at("Asia/Vladivostok", *THU, 15, 40, 10))         # у владивостокца 15:40
        self.assertEqual([(c, "ВЛАД" in t) for c, t in bot.sent], [(2, True)])
        await d.run_once(at("Europe/Moscow", *THU, 15, 40, 10))
        await d.run_once(at("America/New_York", *THU, 15, 40, 10))
        self.assertEqual(sorted(c for c, _ in bot.sent), [1, 2, 3])
        for chat, text in bot.sent:                                        # каждому — только своё
            own = {1: "МСК", 2: "ВЛАД", 3: "НЬЮ-ЙОРК"}[chat]
            self.assertIn(own, text)
            self.assertFalse([o for o in ("МСК", "ВЛАД", "НЬЮ-ЙОРК") if o != own and o in text])

    async def test_same_time_many_events_and_users_in_parallel(self):
        n = 30
        for uid in range(1, n + 1):
            await self.user(uid, name=f"Имя{uid:02d}")
            await self.make(uid, title=f"Событие{uid:02d}А")
            await self.make(uid, title=f"Событие{uid:02d}Б")               # два события в одно время у одного человека
        bot = tsch.FakeBot()
        res = await self.dispatcher(bot, concurrency=7).run_once(at(MSK, *THU, 15, 40, 10))
        self.assertEqual(res[Outcome.SENT], 2 * n)
        self.assertEqual(len(bot.sent), 2 * n)
        per_user: dict[int, list[str]] = {}
        for chat, text in bot.sent:
            per_user.setdefault(chat, []).append(text)
        self.assertEqual(len(per_user), n)
        for uid, texts in per_user.items():
            self.assertEqual(len(texts), 2)
            joined = " ".join(texts)
            self.assertIn(f"Событие{uid:02d}А", joined); self.assertIn(f"Событие{uid:02d}Б", joined)
            for other in range(1, n + 1):
                if other != uid:
                    self.assertNotIn(f"Событие{other:02d}", joined)
                    self.assertNotIn(f"Имя{other:02d}", joined)
        self.assertEqual(len(await self.log_rows()), 2 * n)

    async def test_name_variants_rotate_between_reminders(self):
        await self.user(1, name="Софья", variants=["Софья", "Софа", "Соф"])
        await self.make(1, recurrence="daily")
        bot = tsch.FakeBot(); d = self.dispatcher(bot)
        for day in range(10, 20):
            await d.run_once(at(MSK, 2026, 11, day, 15, 40, 10))
        used = []
        for _, text in bot.sent:
            used.append(next(v for v in ("Софья", "Софа", "Соф") if v in text.split("\n")[0].replace("Софья", "#1").replace("Софа", "#2") or v in text.split("\n")[0]))
        names = [("Софья" if "Софья" in t.split("\n")[0] else "Софа" if "Софа" in t.split("\n")[0] else "Соф") for _, t in bot.sent]
        self.assertEqual(len(names), 10)
        self.assertTrue(all(a != b for a, b in zip(names, names[1:])))      # подряд одно и то же обращение не повторяется
        self.assertEqual(len(set(names)), 3)


class ReminderErrorTests(ReminderCase):
    async def test_blocked_user_marked_and_logged(self):
        await self.user(1)
        await self.make(1)
        bot = tsch.FakeBot({1: TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")})
        d = self.dispatcher(bot)
        res = await d.run_once(at(MSK, *THU, 15, 40, 10))
        self.assertEqual(res[Outcome.BLOCKED], 1)
        self.assertTrue((await self.users.get(1)).bot_blocked)
        self.assertEqual((await self.log_rows())[0]["status"], "blocked")
        events = await self.event_log_repo.list(categories=("user_blocked",))
        self.assertEqual([e.user_id for e in events], [1])
        await d.run_once(at(MSK, *THU, 15, 41))
        self.assertEqual(len(bot.calls if hasattr(bot, "calls") else bot.sent), 0)

    async def test_transient_failure_retried_within_window_then_sent_once(self):
        await self.user(1)
        await self.make(1)
        bot = tsch.FakeBot({1: TelegramNetworkError(method=None, message="timeout")})
        d = self.dispatcher(bot)
        self.assertEqual((await d.run_once(at(MSK, *THU, 15, 40, 10)))[Outcome.FAILED], 1)
        self.assertEqual((await self.log_rows())[0]["status"], "failed")
        await d.run_once(at(MSK, *THU, 15, 40, 30))                          # 20 с — рано для повтора
        self.assertEqual(bot.sent, [])
        bot.behaviours.clear()                                               # сеть вернулась
        self.assertEqual((await d.run_once(at(MSK, *THU, 15, 41, 10)))[Outcome.SENT], 1)
        await d.run_once(at(MSK, *THU, 15, 42)); self.assertEqual(len(bot.sent), 1)
        self.assertEqual((await self.log_rows())[0]["attempts"], 2)

    async def test_retries_are_capped_and_never_outlive_the_window(self):
        await self.user(1)
        await self.make(1)
        bot = tsch.FakeBot({1: TelegramNetworkError(method=None, message="down")})
        d = self.dispatcher(bot)
        for minute in (40, 41, 42, 43, 44):
            await d.run_once(at(MSK, *THU, 15, minute, 10))
        self.assertEqual((await self.log_rows())[0]["attempts"], 3)         # не больше 3 попыток
        bot.behaviours.clear()
        await d.run_once(at(MSK, *THU, 15, 59))                              # окно grace давно закрыто
        self.assertEqual(bot.sent, [])

    async def test_failure_of_one_user_does_not_block_others(self):
        for uid in (1, 2, 3):
            await self.user(uid); await self.make(uid)
        bot = tsch.FakeBot({2: TelegramNetworkError(method=None, message="down")})
        res = await self.dispatcher(bot).run_once(at(MSK, *THU, 15, 40, 10))
        self.assertEqual((res[Outcome.SENT], res[Outcome.FAILED]), (2, 1))
        self.assertEqual(sorted(c for c, _ in bot.sent), [1, 3])

    async def test_unexpected_exception_in_delivery_is_contained(self):
        await self.user(1); await self.user(2)
        await self.make(1); await self.make(2)

        async def exploding_send(bot, chat_id, text, **kw):
            if chat_id == 1:
                raise RuntimeError("неожиданно")
            return await send_message_safe(bot, chat_id, text, sleep=fast_sleep)

        bot = tsch.FakeBot()
        d = EventReminderDispatcher(bot, self.users, self.events, self.log, send=exploding_send)
        res = await d.run_once(at(MSK, *THU, 15, 40, 10))
        self.assertEqual((res[Outcome.SENT], res[Outcome.FAILED]), (1, 1))


class SchedulerWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_runs_both_runners_and_isolates_failures(self):
        # подменяем apscheduler (в тестовом окружении его нет): проверяем только логику _tick
        fake = {name: types.ModuleType(name) for name in
                ("apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.asyncio", "apscheduler.triggers", "apscheduler.triggers.interval")}
        fake["apscheduler.schedulers.asyncio"].AsyncIOScheduler = object
        fake["apscheduler.triggers.interval"].IntervalTrigger = object
        with mock.patch.dict(sys.modules, fake):
            sys.modules.pop("services.scheduler", None)
            from services.scheduler import SummaryScheduler
        calls = []

        class Summaries:
            async def run_once(self):
                calls.append("summaries"); raise RuntimeError("сводки упали")

        async def reminders():
            calls.append("reminders")

        sched = SummaryScheduler(Summaries(), extra_runners=[reminders])
        await sched._tick()
        self.assertEqual(calls, ["summaries", "reminders"])               # сбой сводок не мешает напоминаниям
        sys.modules.pop("services.scheduler", None)


if __name__ == "__main__":
    unittest.main()
