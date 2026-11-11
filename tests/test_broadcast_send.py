"""Рассылка и безопасная отправка: скорость, RetryAfter, блокировки, защита от повторов, права администратора."""
from __future__ import annotations

import asyncio
import functools
import tempfile
import unittest
from pathlib import Path

import _shims  # noqa: F401

from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter,
)

from config import is_admin
from database.database import Database
from database.repositories import BroadcastRepository, UserRepository
from services.broadcast import AsyncRateLimiter, BroadcastService
from services.telegram_send import SendResult, SendStatus, send_message_safe
from test_database import make_settings


class AdminAccessTests(unittest.TestCase):
    def test_only_configured_admin(self):
        self.assertTrue(is_admin(123, 123))
        self.assertFalse(is_admin(124, 123))
        self.assertFalse(is_admin(None, 123))
        self.assertFalse(is_admin(123, None))   # ADMIN_ID не задан — админа нет
        self.assertFalse(is_admin(None, None))


class FakeBot:
    def __init__(self, script=None):
        self.script = script or {}   # chat_id -> список исключений/None на последовательные попытки
        self.calls: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text, kwargs))
        queue = self.script.get(chat_id)
        if queue:
            item = queue.pop(0)
            if item is not None:
                raise item


class SendSafeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sleeps: list[float] = []

        async def sleep(x):
            self.sleeps.append(x)

        self.send = functools.partial(send_message_safe, sleep=sleep)

    async def test_ok_and_parse_mode_passing(self):
        bot = FakeBot()
        self.assertEqual((await self.send(bot, 1, "hi")).status, SendStatus.SENT)
        self.assertNotIn("parse_mode", bot.calls[0][2])                 # по умолчанию — режим бота (HTML)
        await self.send(bot, 1, "hi", parse_mode=None)
        self.assertIn("parse_mode", bot.calls[1][2])
        self.assertIsNone(bot.calls[1][2]["parse_mode"])                # обычный текст

    async def test_retry_after_waits_and_retries(self):
        bot = FakeBot({1: [TelegramRetryAfter(method=None, message="Flood", retry_after=3), None]})
        res = await self.send(bot, 1, "x")
        self.assertEqual(res.status, SendStatus.SENT)
        self.assertEqual(self.sleeps, [3.5])                              # ждём ровно сколько просит Telegram (+запас)
        self.assertEqual(len(bot.calls), 2)

    async def test_retry_after_forever_gives_up(self):
        bot = FakeBot({1: [TelegramRetryAfter(method=None, message="Flood", retry_after=1)] * 10})
        res = await self.send(bot, 1, "x")
        self.assertEqual(res.status, SendStatus.FAILED)
        self.assertEqual(len(bot.calls), 4)                               # max_attempts

    async def test_forbidden_and_chat_not_found_are_blocked_without_retry(self):
        bot = FakeBot({1: [TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")],
                       2: [TelegramBadRequest(method=None, message="Bad Request: chat not found")]})
        self.assertEqual((await self.send(bot, 1, "x")).status, SendStatus.BLOCKED)
        self.assertEqual((await self.send(bot, 2, "x")).status, SendStatus.BLOCKED)
        self.assertEqual(len(bot.calls), 2)

    async def test_other_bad_request_is_failed_no_retry(self):
        bot = FakeBot({1: [TelegramBadRequest(method=None, message="Bad Request: can't parse entities")]})
        res = await self.send(bot, 1, "x")
        self.assertEqual(res.status, SendStatus.FAILED)
        self.assertEqual(len(bot.calls), 1)

    async def test_network_errors_backoff_then_success(self):
        err = TelegramNetworkError(method=None, message="timeout")
        bot = FakeBot({1: [err, err, None]})
        res = await self.send(bot, 1, "x")
        self.assertEqual(res.status, SendStatus.SENT)
        self.assertEqual(len(self.sleeps), 2)
        self.assertLess(self.sleeps[0], self.sleeps[1])                   # пауза растёт


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_is_respected(self):
        clock = {"t": 0.0}

        async def sleep(x):
            clock["t"] += x

        limiter = AsyncRateLimiter(rate=5, clock=lambda: clock["t"], sleep=sleep)
        for _ in range(11):
            await limiter.wait()
        self.assertAlmostEqual(clock["t"], 10 / 5, places=6)             # 11 отправок при 5/с — 2 секунды ожидания


class BroadcastCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self._tmp.name))
        self.db = Database(self.settings.database_path)
        await self.db.connect()
        self.users = UserRepository(self.db, self.settings)
        self.repo = BroadcastRepository(self.db)
        self.admin = 999
        for uid in [self.admin] + list(range(1, 11)):
            await self.users.get_or_create(uid, f"U{uid}", None)
        self.clock = {"t": 0.0}

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def _sleep(self, x):
        self.clock["t"] += x

    def service(self, bot, rate=10.0):
        send = functools.partial(send_message_safe, sleep=self._sleep)
        return BroadcastService(bot, self.users, self.repo, rate, send=send, sleep=self._sleep, clock=lambda: self.clock["t"])


class BroadcastTests(BroadcastCase):
    async def test_full_run_stats_blocked_and_no_admin(self):
        bot = FakeBot({
            3: [TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")],
            5: [TelegramBadRequest(method=None, message="Bad Request: chat not found")],
            7: [TelegramBadRequest(method=None, message="Bad Request: message is too long")],   # -> failed
        })
        await self.users.set_bot_blocked(9, True)                         # известно заранее
        svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "Привет, <b>всем</b>")
        final = await svc.run(bid)
        recipients = [c[0] for c in bot.calls]
        self.assertNotIn(self.admin, recipients)                         # админу не шлём
        self.assertEqual(len(recipients), len(set(recipients)))          # одному — не больше раза
        self.assertNotIn(9, recipients)                                  # заведомо заблокированному не пытаемся
        self.assertEqual(set(recipients), {1, 2, 3, 4, 5, 6, 7, 8, 10})
        self.assertTrue(all(c[1] == "Привет, <b>всем</b>" and c[2].get("parse_mode", "x") is None for c in bot.calls))
        self.assertEqual((final.total_count, final.success_count, final.failed_count, final.blocked_count), (10, 6, 1, 3))
        self.assertEqual(final.status, "done")
        self.assertTrue((await self.users.get(3)).bot_blocked)
        self.assertTrue((await self.users.get(5)).bot_blocked)
        self.assertFalse((await self.users.get(7)).bot_blocked)          # failed != blocked

    async def test_progress_reported_on_big_broadcast(self):
        for uid in range(100, 145):
            await self.users.get_or_create(uid, f"U{uid}", None)
        bot = FakeBot()
        svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "x")
        progress = []

        async def on_progress(counts):
            progress.append(dict(counts))

        final = await svc.run(bid, on_progress)
        self.assertEqual(final.success_count, 55)
        self.assertGreaterEqual(len(progress), 2)
        done = [p["sent"] + p["failed"] + p["blocked"] for p in progress]
        self.assertEqual(done, sorted(done))                              # прогресс только растёт
        self.assertTrue(all(p["sent"] + p["failed"] + p["blocked"] + p["pending"] == 55 for p in progress))

    async def test_rate_limit_applied(self):
        bot = FakeBot()
        svc = self.service(bot, rate=5.0)
        bid = await svc.create_draft(self.admin, "x")
        await svc.run(bid)
        self.assertEqual(len(bot.calls), 10)
        self.assertAlmostEqual(self.clock["t"], 9 / 5.0, places=6)       # 10 сообщений при 5/с

    async def test_retry_after_does_not_lose_users(self):
        bot = FakeBot({4: [TelegramRetryAfter(method=None, message="Flood", retry_after=7), None]})
        svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "x")
        final = await svc.run(bid)
        self.assertEqual((final.success_count, final.failed_count), (10, 0))
        self.assertEqual([c[0] for c in bot.calls].count(4), 2)          # повтор именно тому, кого притормозили
        self.assertGreaterEqual(self.clock["t"], 7.5)

    async def test_double_press_and_concurrent_runs_send_once(self):
        bot = FakeBot()
        svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "x")
        results = await asyncio.gather(*(svc.run(bid) for _ in range(5)))   # пять «одновременных нажатий»
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(bot.calls), 10)
        self.assertIsNone(await svc.run(bid))                               # и повторное нажатие позже
        self.assertEqual(len(bot.calls), 10)

    async def test_cancel_before_start_sends_nothing(self):
        bot = FakeBot()
        svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "x")
        self.assertTrue(await svc.cancel(bid))
        self.assertIsNone(await svc.run(bid))
        self.assertEqual(bot.calls, [])
        self.assertEqual((await self.repo.get(bid)).status, "cancelled")
        self.assertFalse(await svc.cancel(bid))                             # повторная отмена безопасна

    async def test_unexpected_send_exception_marks_failed_and_continues(self):
        class Boom(FakeBot):
            async def send_message(self, chat_id, text, **kw):
                if chat_id == 2:
                    raise RuntimeError("неожиданно")
                await super().send_message(chat_id, text, **kw)

        bid = await self.repo.create_draft(self.admin, "x")

        async def raw_send(bot, uid, text, **kw):
            await bot.send_message(uid, text)
            return SendResult(SendStatus.SENT)

        svc = BroadcastService(Boom(), self.users, self.repo, 10.0, send=raw_send, sleep=self._sleep, clock=lambda: self.clock["t"])
        final = await svc.run(bid)
        self.assertEqual((final.success_count, final.failed_count), (9, 1))
        self.assertEqual(final.status, "done")

    async def test_second_broadcast_cannot_start_while_first_running(self):
        first = await self.repo.create_draft(self.admin, "a")
        second = await self.repo.create_draft(self.admin, "b")
        self.assertTrue(await self.repo.claim_start(first, [(1, False)]))
        bot = FakeBot()
        self.assertIsNone(await self.service(bot).run(second))
        self.assertEqual(bot.calls, [])
        self.assertEqual(await self.repo.mark_interrupted(), 1)             # после «падения» процесса первая помечается прерванной
        self.assertEqual((await self.repo.get(first)).status, "interrupted")


if __name__ == "__main__":
    unittest.main()
