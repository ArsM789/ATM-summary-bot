"""Планировщик: кому пора слать (разные пояса) и многопользовательская доставка без перемешивания данных."""
from __future__ import annotations

import asyncio
import functools
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import _shims  # noqa: F401

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError

from database.database import Database
from database.models import LessonItem, ScheduleEntry, User
from database.repositories import (
    NotificationLogRepository, ScheduleRepository, UserRepository, WeeklyScheduleRepository,
)
from services.clothing_advisor import CORE  # noqa: F401  (проверка импортов)
from services.delivery import Outcome, SummaryDispatcher
from services.scheduling_logic import find_due
from services.summary_generator import SummaryGenerator
from services.summary_service import SummaryService
from services.telegram_send import send_message_safe
from services.weather_models import (
    CurrentConditions, DayForecast, HourPoint, WeatherReport, WeatherUnavailableError,
)
from test_database import make_settings

UTC = timezone.utc


def utc(y, mo, d, h, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def entry(send_time="07:00", mask=127, effective_from=0, eid=1, uid=1, enabled=True) -> ScheduleEntry:
    return ScheduleEntry(eid, uid, send_time, enabled, mask, effective_from)


def user(uid=1, tz="Europe/Moscow", name="U") -> User:
    return User(uid, name, "Город", 1.0, 1.0, tz, True)


class FindDueTests(unittest.TestCase):
    def test_moscow_seven_am(self):
        e, u = entry("07:00"), user(tz="Europe/Moscow")
        self.assertEqual(find_due([(e, u)], utc(2026, 11, 10, 3, 59, 59)), [])          # 06:59:59 по Москве
        due = find_due([(e, u)], utc(2026, 11, 10, 4, 0, 30))                           # 07:00:30
        self.assertEqual([(d.slot, d.local_date) for d in due], [("07:00", "2026-11-10")])
        self.assertEqual(len(find_due([(e, u)], utc(2026, 11, 10, 4, 59))), 1)          # ещё в окне догонки
        self.assertEqual(find_due([(e, u)], utc(2026, 11, 10, 5, 1)), [])               # окно (60 мин) закрылось

    def test_each_user_uses_own_timezone(self):
        moscow, vlad, ny = user(1, "Europe/Moscow"), user(2, "Asia/Vladivostok"), user(3, "America/New_York")
        items = [(entry(uid=u.telegram_id, eid=u.telegram_id), u) for u in (moscow, vlad, ny)]
        # 21:00 UTC 9 ноября: во Владивостоке уже 07:00 ВТОРНИКА 10 ноября
        due = find_due(items, utc(2026, 11, 9, 21, 0, 10))
        self.assertEqual([(d.user.telegram_id, d.local_date) for d in due], [(2, "2026-11-10")])
        # 04:00 UTC: Москва 07:00
        self.assertEqual([d.user.telegram_id for d in find_due(items, utc(2026, 11, 10, 4, 0, 10))], [1])
        # 12:00 UTC: Нью-Йорк 07:00 (EST, UTC−5)
        self.assertEqual([d.user.telegram_id for d in find_due(items, utc(2026, 11, 10, 12, 0, 10))], [3])

    def test_weekday_mask_uses_local_date(self):
        monday_only = entry(mask=0b0000001)
        vlad = user(tz="Asia/Vladivostok")
        # 21:00 UTC воскресенья 8 ноября = 07:00 понедельника 9-го во Владивостоке -> пора
        self.assertEqual(len(find_due([(monday_only, vlad)], utc(2026, 11, 8, 21, 0, 5))), 1)
        # а в Москве в тот же момент ещё воскресенье 00:00 — не пора
        self.assertEqual(find_due([(monday_only, user(tz="Europe/Moscow"))], utc(2026, 11, 8, 21, 0, 5)), [])
        # вторник по местному времени — маска «только пн» не срабатывает
        self.assertEqual(find_due([(monday_only, user(tz="Europe/Moscow"))], utc(2026, 11, 10, 4, 0, 5)), [])

    def test_effective_from_prevents_backdated_send(self):
        added_at = utc(2026, 11, 10, 5, 20).timestamp()          # добавили «07:00» в 08:20 по Москве
        e, u = entry("07:00", effective_from=int(added_at)), user()
        self.assertEqual(find_due([(e, u)], utc(2026, 11, 10, 5, 25)), [])
        self.assertEqual(len(find_due([(e, u)], utc(2026, 11, 11, 4, 0, 5))), 1)  # а завтра — уже штатно

    def test_midnight_crossing(self):
        e, u = entry("23:50"), user(tz="Europe/Moscow")
        due = find_due([(e, u)], utc(2026, 11, 10, 21, 10))     # 00:10 11 ноября по Москве
        self.assertEqual([d.local_date for d in due], ["2026-11-10"])  # слот относится к вчерашней дате

    def test_bad_data_does_not_break_others(self):
        bad = (entry("xx:yy", eid=5), user(5))
        weird_tz = (entry(eid=6), user(6, tz="Not/AZone"))
        good = (entry(eid=7), user(7))
        due = find_due([bad, weird_tz, good], utc(2026, 11, 10, 4, 0, 5))
        self.assertEqual([d.user.telegram_id for d in due], [7])

    def test_dst_zone(self):
        # Берлин зимой UTC+1: 07:00 = 06:00 UTC
        self.assertEqual(len(find_due([(entry(), user(tz="Europe/Berlin"))], utc(2026, 11, 10, 6, 0, 5))), 1)
        # летом UTC+2: 07:00 = 05:00 UTC
        self.assertEqual(len(find_due([(entry(), user(tz="Europe/Berlin"))], utc(2026, 7, 10, 5, 0, 5))), 1)


def synthetic_report(city: str, tz_name: str, now_local: datetime, temp: float) -> WeatherReport:
    tz = ZoneInfo(tz_name)
    start = datetime(now_local.year, now_local.month, now_local.day, tzinfo=tz) - timedelta(days=1)
    hours = tuple(
        HourPoint(start + timedelta(hours=i), temp, temp, 10, 0.0, 0.0, 3.0, 5.0, 3) for i in range(24 * 4)
    )
    days = tuple(
        DayForecast((start + timedelta(days=d)).date(), 3, temp + 2, temp - 2, temp + 1, temp - 3, 10, 0.0, 0.0, 4.0, 6.0)
        for d in range(4)
    )
    cur = CurrentConditions(now_local, temp, temp, 3.0, 5.0, 0.0, 3)
    return WeatherReport(city, tz_name, cur, days, hours, datetime.now(UTC))


class FakeWeather:
    """Возвращает синтетический прогноз; температура зависит от города — чтобы заметить подмену данных."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def get_report(self, city, lat, lon, tz_name):
        self.calls += 1
        if self.fail:
            raise WeatherUnavailableError("down")
        temp = float(sum(map(ord, city)) % 30)
        return synthetic_report(city, tz_name, datetime.now(ZoneInfo(tz_name)), temp)


class FakeBot:
    def __init__(self, behaviours=None):
        self.sent: list[tuple[int, str]] = []
        self.behaviours = behaviours or {}

    async def send_message(self, chat_id, text, **kwargs):
        behaviour = self.behaviours.get(chat_id)
        if behaviour:
            raise behaviour
        await asyncio.sleep(0)  # отдаём управление: проверяем параллельность
        self.sent.append((chat_id, text))


async def fast_sleep(_):
    return None


class DeliveryCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self._tmp.name))
        self.db = Database(self.settings.database_path)
        await self.db.connect()
        self.users = UserRepository(self.db, self.settings)
        self.weekly = WeeklyScheduleRepository(self.db)
        self.schedules = ScheduleRepository(self.db)
        self.log = NotificationLogRepository(self.db)
        self.weather = FakeWeather()
        self.summaries = SummaryService(self.weekly, self.users, self.weather, SummaryGenerator())

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    def dispatcher(self, bot, **kw):
        return SummaryDispatcher(
            bot, self.users, self.schedules, self.log, self.summaries,
            send=functools.partial(send_message_safe, sleep=fast_sleep), **kw,
        )

    async def make_user(self, uid, name, city, tz, send_time="07:00", lessons=None):
        await self.users.get_or_create(uid, name, None)
        await self.users.set_names(uid, name, [name])
        await self.users.update_city(uid, city, 1.0, 1.0)
        await self.users.update_timezone(uid, tz)
        e = (await self.schedules.list_for_user(uid))[0]
        await self.db.execute("DELETE FROM schedules WHERE id = ?", (e.id,))
        await self.db.execute(
            "INSERT INTO schedules (user_id, send_time, enabled, weekdays_mask, effective_from) VALUES (?,?,1,127,0)",
            (uid, send_time),
        )
        for wd, items in (lessons or {}).items():
            await self.weekly.replace_day(uid, wd, items)


class MultiUserDeliveryTests(DeliveryCase):
    async def test_different_users_get_only_their_own_summary(self):
        # Момент: 2026-11-09 (пн) 21:00:30 UTC. Во Владивостоке уже ВТ 10 ноября 07:00:30.
        await self.make_user(1, "Арсений", "Владивосток", "Asia/Vladivostok",
                             lessons={1: [LessonItem("Алгебра_V", 1), LessonItem("Биология_V", 2)], 0: [LessonItem("Понедельничный_V", 1)]})
        await self.make_user(2, "Анна", "Москва", "Europe/Moscow",                       # в Москве 00:00 вторника
                             lessons={1: [LessonItem("Химия_M", 1)]})
        await self.make_user(3, "Борис", "Нью-Йорк", "America/New_York", send_time="16:00",  # в Нью-Йорке 16:00 понедельника
                             lessons={0: [LessonItem("История_NY", 3)], 1: [LessonItem("Вторничный_NY", 1)]})
        bot = FakeBot()
        now = utc(2026, 11, 9, 21, 0, 30)
        res = await self.dispatcher(bot).run_once(now)
        self.assertEqual(res[Outcome.SENT], 2)
        sent = dict(bot.sent)
        self.assertEqual(set(sent), {1, 3})                     # Анне (Москва) в этот момент слать рано

        v, ny = sent[1], sent[3]
        # Владивосток: вторник, СВОИ уроки вторника, СВОЁ имя, СВОЙ город
        self.assertIn("Владивосток", v); self.assertIn("Вторник, 10 ноября", v)
        self.assertIn("Алгебра_V", v); self.assertIn("Биология_V × 2", v)
        self.assertNotIn("Понедельничный_V", v)
        # Нью-Йорк: 16:00 понедельника — «день» (вечер начинается с 17:00): СВОИ уроки понедельника
        self.assertIn("Нью-Йорк", ny); self.assertIn("Понедельник, 9 ноября", ny)
        self.assertIn("История_NY × 3", ny)
        self.assertNotIn("Вторничный_NY", ny)
        # ничего чужого
        for other in ("Химия_M", "Анна", "Москва", "Алгебра_V", "Арсений"):
            self.assertNotIn(other, ny)
        for other in ("Химия_M", "Анна", "Москва", "История_NY", "Борис"):
            self.assertNotIn(other, v)

        # повторный проход (тот же момент и позже в окне) — дублей нет
        res2 = await self.dispatcher(bot).run_once(now + timedelta(seconds=30))
        self.assertEqual(res2[Outcome.SENT], 0)
        self.assertEqual(len(bot.sent), 2)

    async def test_many_users_in_parallel_no_cross_contamination(self):
        n = 40
        zones = ["Europe/Moscow", "Asia/Yekaterinburg", "Asia/Novosibirsk", "Europe/Samara"]
        for uid in range(1, n + 1):
            await self.make_user(uid, f"Имя{uid:02d}", f"Город{uid:02d}", zones[0], send_time="07:00",
                                 lessons={wd: [LessonItem(f"Предмет{uid:02d}д{wd}", 1 + uid % 3)] for wd in range(5)})
        bot = FakeBot()
        res = await self.dispatcher(bot, concurrency=7).run_once(utc(2026, 11, 10, 4, 0, 30))   # 07:00 МСК, вторник
        self.assertEqual(res[Outcome.SENT], n)
        self.assertEqual(len({uid for uid, _ in bot.sent}), n)          # каждому — ровно одно сообщение
        self.assertEqual(len(bot.sent), n)
        for uid, text in bot.sent:
            self.assertIn(f"Город{uid:02d}", text)
            self.assertIn(f"Предмет{uid:02d}д1", text)                  # вторник = индекс 1
            for other in range(1, n + 1):
                if other != uid:
                    self.assertNotIn(f"Имя{other:02d}", text, (uid, other))
                    self.assertNotIn(f"Предмет{other:02d}", text, (uid, other))
        # журнал: n записей sent
        rows = await self.db.fetchall("SELECT status FROM notification_log")
        self.assertEqual([r["status"] for r in rows], ["sent"] * n)

    async def test_restart_does_not_resend(self):
        await self.make_user(1, "А", "Г", "Europe/Moscow")
        bot = FakeBot()
        now = utc(2026, 11, 10, 4, 0, 30)
        await self.dispatcher(bot).run_once(now)
        self.assertEqual(len(bot.sent), 1)
        # «перезапуск»: новые объекты поверх той же БД
        await self.db.close()
        db2 = Database(self.settings.database_path)
        await db2.connect()
        users2, sched2, log2 = UserRepository(db2, self.settings), ScheduleRepository(db2), NotificationLogRepository(db2)
        summaries2 = SummaryService(WeeklyScheduleRepository(db2), users2, FakeWeather(), SummaryGenerator())
        bot2 = FakeBot()
        res = await SummaryDispatcher(bot2, users2, sched2, log2, summaries2).run_once(now + timedelta(minutes=10))
        self.assertEqual(bot2.sent, [])
        self.assertEqual(res[Outcome.SENT], 0)
        # а на следующий день — снова штатно
        await SummaryDispatcher(bot2, users2, sched2, log2, summaries2,
                                send=functools.partial(send_message_safe, sleep=fast_sleep)).run_once(now + timedelta(days=1))
        self.assertEqual(len(bot2.sent), 1)
        await db2.close()
        self.db = Database(self.settings.database_path)  # для asyncTearDown
        await self.db.connect()

    async def test_missed_while_down_is_caught_up_within_window_only(self):
        await self.make_user(1, "А", "Г", "Europe/Moscow")
        bot = FakeBot()
        res = await self.dispatcher(bot).run_once(utc(2026, 11, 10, 4, 40))   # бот проснулся в 07:40 — догоняем
        self.assertEqual(res[Outcome.SENT], 1)
        bot2 = FakeBot()
        await self.make_user(2, "Б", "Г", "Europe/Moscow")
        res = await self.dispatcher(bot2).run_once(utc(2026, 11, 10, 6, 30))  # 09:30 — слишком поздно для 07:00
        self.assertEqual(bot2.sent, [])

    async def test_blocked_user_marked_and_others_unaffected(self):
        await self.make_user(1, "Блок", "Г", "Europe/Moscow")
        await self.make_user(2, "Норм", "Г", "Europe/Moscow")
        bot = FakeBot({1: TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")})
        res = await self.dispatcher(bot).run_once(utc(2026, 11, 10, 4, 0, 30))
        self.assertEqual((res[Outcome.BLOCKED], res[Outcome.SENT]), (1, 1))
        self.assertTrue((await self.users.get(1)).bot_blocked)
        self.assertFalse((await self.users.get(2)).bot_blocked)
        row = await self.log.get(1, "summary", "07:00", "2026-11-10")
        self.assertEqual(row["status"], "blocked")
        res2 = await self.dispatcher(bot).run_once(utc(2026, 11, 10, 4, 5))   # заблокировавшему больше не пытаемся слать
        self.assertEqual((res2[Outcome.BLOCKED], res2[Outcome.SENT], res2[Outcome.FAILED]), (0, 0, 0))
        self.assertEqual([uid for uid, _ in bot.sent], [2])                  # заблокировавшему повторно не слали

    async def test_transient_failure_retried_later_then_sent_once(self):
        await self.make_user(1, "А", "Г", "Europe/Moscow")
        bot = FakeBot({1: TelegramNetworkError(method=None, message="timeout")})
        d = self.dispatcher(bot)
        t0 = utc(2026, 11, 10, 4, 0, 30)
        self.assertEqual((await d.run_once(t0))[Outcome.FAILED], 1)
        self.assertEqual((await self.log.get(1, "summary", "07:00", "2026-11-10"))["status"], "failed")
        self.assertEqual(sum((await d.run_once(t0 + timedelta(seconds=60))).values()), 1)  # рано для повтора -> SKIPPED
        self.assertEqual(len(bot.sent), 0)
        bot.behaviours.clear()                                                  # сеть вернулась
        res = await d.run_once(t0 + timedelta(minutes=5))
        self.assertEqual(res[Outcome.SENT], 1)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(sum((await d.run_once(t0 + timedelta(minutes=10))).values()), 1)  # SKIPPED, не SENT
        self.assertEqual(len(bot.sent), 1)

    async def test_weather_outage_defers_then_sends_partial_on_last_attempt(self):
        await self.make_user(1, "А", "Г", "Europe/Moscow", lessons={1: [LessonItem("Алгебра", 1)]})
        self.weather.fail = True
        bot = FakeBot()
        d = self.dispatcher(bot)
        t0 = utc(2026, 11, 10, 4, 0, 30)
        self.assertEqual((await d.run_once(t0))[Outcome.DEFERRED], 1)
        self.assertEqual((await d.run_once(t0 + timedelta(minutes=4)))[Outcome.DEFERRED], 1)
        self.assertEqual(bot.sent, [])
        self.assertEqual((await d.run_once(t0 + timedelta(minutes=8)))[Outcome.SENT], 1)   # 3-я попытка: без погоды
        text = bot.sent[0][1]
        self.assertIn("Погоду сейчас получить не удалось", text)
        self.assertIn("Алгебра", text)                                           # расписание — настоящее, погода не выдумана
        self.assertNotIn("°", text)

    async def test_stale_snapshot_is_rechecked(self):
        await self.make_user(1, "А", "Г", "Europe/Moscow")
        d = self.dispatcher(FakeBot())
        now = utc(2026, 11, 10, 4, 0, 30)
        items = await self.schedules.list_all_active()
        due = find_due(items, now)
        self.assertEqual(len(due), 1)
        await self.users.set_notifications_enabled(1, False)        # пользователь выключил автосводки уже после поиска
        self.assertEqual(await d.deliver(due[0], now), Outcome.SKIPPED)
        await self.users.set_notifications_enabled(1, True)
        await self.db.execute("UPDATE schedules SET effective_from = ?", (int(now.timestamp()) + 1000,))
        self.assertEqual(await d.deliver(due[0], now), Outcome.SKIPPED)    # время изменили/пересоздали
        await self.db.execute("DELETE FROM schedules")
        self.assertEqual(await d.deliver(due[0], now), Outcome.SKIPPED)    # запись удалили


if __name__ == "__main__":
    unittest.main()
