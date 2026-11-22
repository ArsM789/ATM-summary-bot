"""Рассылки v2: форматирование, картинка, правка черновика, отпечаток содержимого, защита от повторной отправки."""
from __future__ import annotations

import asyncio
import functools
import unittest
from unittest import mock

import _shims  # noqa: F401

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from database.repositories import BroadcastRepository
from services.broadcast import BroadcastService, content_hash
from services.telegram_send import SendStatus, send_message_safe, send_photo_safe
from test_database import DbCase


class MediaBot:
    def __init__(self, script=None):
        self.script = script or {}
        self.messages: list[tuple[int, str, dict]] = []
        self.photos: list[tuple[int, str, dict]] = []

    def _maybe_fail(self, chat_id):
        queue = self.script.get(chat_id)
        if queue:
            item = queue.pop(0)
            if item is not None:
                raise item

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text, kw)); self._maybe_fail(chat_id)

    async def send_photo(self, chat_id, photo, **kw):
        self.photos.append((chat_id, photo, kw)); self._maybe_fail(chat_id)


async def fast_sleep(_):
    return None


class HashTests(unittest.TestCase):
    def test_hash(self):
        base = content_hash("custom", "Обновление", "u1")
        self.assertEqual(base, content_hash("custom", "  Обновление  ", "u1"))        # пробелы по краям не важны
        self.assertNotEqual(base, content_hash("custom", "Обновление!", "u1"))
        self.assertNotEqual(base, content_hash("custom", "Обновление", "u2"))          # другая картинка
        self.assertNotEqual(base, content_hash("custom", "Обновление", None))
        self.assertNotEqual(base, content_hash("downtime", "Обновление", "u1"))
        self.assertEqual(len(base), 32)


class SendPhotoSafeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ok_and_caption_and_default_parse_mode(self):
        bot = MediaBot()
        res = await send_photo_safe(bot, 1, "FILE", caption="<b>Привет</b>", sleep=fast_sleep)
        self.assertEqual(res.status, SendStatus.SENT)
        self.assertEqual(bot.photos, [(1, "FILE", {"caption": "<b>Привет</b>"})])      # parse_mode не передаём: HTML по умолчанию
        await send_photo_safe(bot, 2, "FILE", sleep=fast_sleep)
        self.assertEqual(bot.photos[1][2], {})                                           # без подписи — без caption

    async def test_error_classification_matches_text_sender(self):
        sleeps = []

        async def sleep(x):
            sleeps.append(x)

        bot = MediaBot({1: [TelegramRetryAfter(method=None, message="Flood", retry_after=4), None],
                        2: [TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")],
                        3: [TelegramBadRequest(method=None, message="Bad Request: wrong file identifier")],
                        4: [TelegramNetworkError(method=None, message="timeout")] * 9})
        self.assertEqual((await send_photo_safe(bot, 1, "F", sleep=sleep)).status, SendStatus.SENT)
        self.assertEqual(sleeps, [4.5])
        self.assertEqual((await send_photo_safe(bot, 2, "F", sleep=sleep)).status, SendStatus.BLOCKED)
        self.assertEqual((await send_photo_safe(bot, 3, "F", sleep=sleep)).status, SendStatus.FAILED)
        self.assertEqual((await send_photo_safe(bot, 4, "F", sleep=sleep, max_attempts=3)).status, SendStatus.FAILED)
        self.assertEqual(len([p for p in bot.photos if p[0] == 4]), 3)

    async def test_text_sender_unchanged_after_refactor(self):
        bot = MediaBot({1: [TelegramNetworkError(method=None, message="t"), None]})
        self.assertEqual((await send_message_safe(bot, 1, "x", sleep=fast_sleep)).status, SendStatus.SENT)
        self.assertEqual(len(bot.messages), 2)
        await send_message_safe(bot, 2, "x", parse_mode=None, sleep=fast_sleep)
        self.assertIn("parse_mode", bot.messages[-1][2])


class BroadcastV2Tests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.admin = 999
        for uid in [self.admin, 1, 2, 3, 4]:
            await self.users.get_or_create(uid, f"U{uid}", None)
        self.repo = BroadcastRepository(self.db)

    def service(self, bot, **kw):
        return BroadcastService(
            bot, self.users, self.repo, 1000.0,
            send=functools.partial(send_message_safe, sleep=fast_sleep),
            send_photo=functools.partial(send_photo_safe, sleep=fast_sleep), sleep=fast_sleep, **kw)

    async def test_formatted_text_goes_as_html_to_everyone_but_admin(self):
        bot = MediaBot(); svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "<b>Обновление</b>\n• новая вечерняя сводка\n<a href=\"https://t.me/x\">канал</a>")
        final = await svc.run(bid)
        self.assertEqual((final.total_count, final.success_count), (4, 4))
        self.assertEqual(bot.photos, [])
        self.assertEqual(sorted(c for c, _, _ in bot.messages), [1, 2, 3, 4])
        self.assertTrue(all(t.startswith("<b>Обновление</b>") and "parse_mode" not in kw for _, t, kw in bot.messages))

    async def test_photo_with_caption_sent_as_photo_only(self):
        bot = MediaBot(); svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "<i>Подпись</i>", photo_file_id="FILE123", photo_unique_id="UNIQ")
        await svc.run(bid)
        self.assertEqual(bot.messages, [])                                         # текст не дублируется отдельным сообщением
        self.assertEqual(sorted(c for c, _, _ in bot.photos), [1, 2, 3, 4])
        self.assertTrue(all(p == "FILE123" and kw == {"caption": "<i>Подпись</i>"} for _, p, kw in bot.photos))
        got = await self.repo.get(bid)
        self.assertEqual((got.photo_file_id, got.photo_unique_id, got.status), ("FILE123", "UNIQ", "done"))

    async def test_photo_without_caption(self):
        bot = MediaBot(); svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "", photo_file_id="F", photo_unique_id="U")
        await svc.run(bid)
        self.assertTrue(all(kw == {} for _, _, kw in bot.photos)); self.assertEqual(len(bot.photos), 4)

    async def test_photo_errors_are_counted_like_text_errors(self):
        bot = MediaBot({2: [TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")],
                        3: [TelegramBadRequest(method=None, message="Bad Request: wrong file identifier")]})
        svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "x", photo_file_id="F", photo_unique_id="U")
        final = await svc.run(bid)
        self.assertEqual((final.success_count, final.failed_count, final.blocked_count), (2, 1, 1))
        self.assertTrue((await self.users.get(2)).bot_blocked)

    async def test_edit_draft_changes_text_photo_and_hash(self):
        svc = self.service(MediaBot())
        bid = await svc.create_draft(self.admin, "Старый текст")
        first = await svc.get(bid)
        edited = await svc.edit_draft(bid, text="Новый текст")
        self.assertEqual(edited.message, "Новый текст"); self.assertNotEqual(edited.content_hash, first.content_hash)
        with_photo = await svc.edit_draft(bid, photo_file_id="F1", photo_unique_id="U1")
        self.assertEqual((with_photo.message, with_photo.photo_file_id), ("Новый текст", "F1"))   # текст сохранился
        self.assertNotEqual(with_photo.content_hash, edited.content_hash)
        edited_again = await svc.edit_draft(bid, text="Ещё текст")
        self.assertEqual((edited_again.photo_file_id, edited_again.photo_unique_id), ("F1", "U1"))   # картинка сохранилась
        no_photo = await svc.edit_draft(bid, remove_photo=True)
        self.assertEqual((no_photo.photo_file_id, no_photo.photo_unique_id), (None, None))
        self.assertEqual(no_photo.content_hash, content_hash("custom", "Ещё текст", None))
        self.assertIsNone(await svc.edit_draft(bid + 100, text="x"))               # несуществующий

    async def test_started_broadcast_cannot_be_edited(self):
        svc = self.service(MediaBot())
        bid = await svc.create_draft(self.admin, "Текст")
        await svc.run(bid)
        self.assertIsNone(await svc.edit_draft(bid, text="Подмена"))
        self.assertEqual((await svc.get(bid)).message, "Текст")

    async def test_recent_duplicate_detection(self):
        svc = self.service(MediaBot())
        first = await svc.create_draft(self.admin, "Обновление 1.5", photo_file_id="F", photo_unique_id="U")
        self.assertIsNone(await svc.recent_duplicate(await svc.get(first)))          # ещё ничего не отправлялось
        await svc.run(first)
        same = await svc.create_draft(self.admin, "Обновление 1.5", photo_file_id="F2", photo_unique_id="U")   # та же картинка (другой file_id)
        self.assertEqual((await svc.recent_duplicate(await svc.get(same))).id, first)
        other = await svc.create_draft(self.admin, "Другое обновление")
        self.assertIsNone(await svc.recent_duplicate(await svc.get(other)))
        downtime = await svc.create_draft(self.admin, "Обновление 1.5", photo_file_id="F", photo_unique_id="U", kind="downtime")
        self.assertIsNone(await svc.recent_duplicate(await svc.get(downtime)))       # другой тип — не дубль
        # через 31 минуту прежняя рассылка уже не считается «недавней»
        with mock.patch("database.repositories.time.time", return_value=__import__("time").time() + 31 * 60):
            self.assertIsNone(await svc.recent_duplicate(await svc.get(same)))

    async def test_cancelled_or_draft_broadcasts_are_not_duplicates(self):
        svc = self.service(MediaBot())
        a = await svc.create_draft(self.admin, "Текст"); await svc.cancel(a)
        b = await svc.create_draft(self.admin, "Текст")
        self.assertIsNone(await svc.recent_duplicate(await svc.get(b)))

    async def test_double_start_still_sends_once(self):
        bot = MediaBot(); svc = self.service(bot)
        bid = await svc.create_draft(self.admin, "Текст", photo_file_id="F", photo_unique_id="U")
        results = await asyncio.gather(*(svc.run(bid) for _ in range(6)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(bot.photos), 4)

    async def test_downtime_draft_kind_and_recipients_count(self):
        svc = self.service(MediaBot())
        bid = await svc.create_draft(self.admin, "Временно недоступен", kind="downtime")
        self.assertEqual((await svc.get(bid)).kind, "downtime")
        self.assertEqual(await svc.recipients_count(self.admin), 4)                    # без админа
        await self.users.set_bot_blocked(1, True)
        self.assertEqual(await svc.recipients_count(self.admin), 4)                    # заблокировавшие входят в счёт (учитываются в статистике)


if __name__ == "__main__":
    unittest.main()
