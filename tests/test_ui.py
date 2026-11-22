"""Единый принцип интерфейса (bot/ui.py): редактирование вместо новых сообщений, чистка временных и устаревших."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import _shims  # noqa: F401

from aiogram.exceptions import TelegramBadRequest
from bot.ui import UI


class FakeBot:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}
        self._next_id = 1000

    def _maybe_fail(self, name):
        exc = self.fail.get(name)
        if exc:
            raise exc

    async def edit_message_text(self, text, chat_id, message_id, reply_markup=None):
        self.calls.append(("edit", chat_id, message_id, text, reply_markup)); self._maybe_fail("edit")

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        self.calls.append(("send", chat_id, text, reply_markup)); self._maybe_fail("send")
        return SimpleNamespace(message_id=self._next_id)

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", chat_id, message_id)); self._maybe_fail("delete")

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.calls.append(("strip", chat_id, message_id, reply_markup)); self._maybe_fail("strip")

    def kinds(self):
        return [c[0] for c in self.calls]


def bad(text):
    return TelegramBadRequest(method=None, message=text)


def msg(bot, uid=1, mid=50, chat=None):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), chat=SimpleNamespace(id=chat or uid), message_id=mid, bot=bot)


def cb(bot, uid=1, mid=60, data="x"):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), data=data, bot=bot,
                           message=SimpleNamespace(chat=SimpleNamespace(id=uid), message_id=mid))


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot, self.ui = FakeBot(), UI()

    async def test_button_press_edits_the_same_message(self):
        mid = await self.ui.show(cb(self.bot, mid=60), "Новый экран", "KB")
        self.assertEqual(mid, 60)
        self.assertEqual(self.bot.calls, [("edit", 1, 60, "Новый экран", "KB")])         # ни одного нового сообщения
        self.assertEqual(self.ui.anchor_of(1), 60)

    async def test_button_press_when_text_unchanged_is_silent(self):
        self.bot.fail["edit"] = bad("Bad Request: message is not modified")
        await self.ui.show(cb(self.bot, mid=60), "То же", None)
        self.assertEqual(self.bot.kinds(), ["edit"])                                    # повторное нажатие не плодит сообщений
        self.assertEqual(self.ui.anchor_of(1), 60)

    async def test_button_on_deleted_message_falls_back_to_new_message(self):
        self.bot.fail["edit"] = bad("Bad Request: message to edit not found")
        mid = await self.ui.show(cb(self.bot, mid=60), "Экран", "KB")
        self.assertEqual(self.bot.kinds(), ["edit", "send"])
        self.assertEqual(self.ui.anchor_of(1), mid)
        self.assertNotEqual(mid, 60)

    async def test_text_input_deletes_user_message_and_edits_the_screen(self):
        self.ui.remember(1, 60)
        mid = await self.ui.show(msg(self.bot, mid=77), "Шаг 2", "KB")
        self.assertEqual(mid, 60)
        self.assertEqual(self.bot.calls, [("delete", 1, 77), ("edit", 1, 60, "Шаг 2", "KB")])   # без новых сообщений

    async def test_text_input_without_known_screen_sends_one_and_remembers_it(self):
        mid = await self.ui.show(msg(self.bot, mid=77), "Шаг", None)
        self.assertEqual(self.bot.kinds(), ["delete", "send"])
        self.assertEqual(self.ui.anchor_of(1), mid)
        mid2 = await self.ui.show(msg(self.bot, mid=78), "Шаг 2", None)                 # следующий ввод — уже правка
        self.assertEqual(mid2, mid)
        self.assertEqual(self.bot.kinds()[-2:], ["delete", "edit"])

    async def test_anchor_lost_is_replaced(self):
        self.ui.remember(1, 60)
        self.bot.fail["edit"] = bad("Bad Request: message to edit not found")
        mid = await self.ui.show(msg(self.bot, mid=77), "Шаг", "KB")
        self.assertEqual(self.bot.kinds(), ["delete", "edit", "send"])
        self.assertEqual(self.ui.anchor_of(1), mid)

    async def test_fresh_entry_retires_old_screen_and_sends_new(self):
        self.ui.remember(1, 60)
        mid = await self.ui.show(msg(self.bot, mid=77), "Раздел", "KB", fresh=True)
        self.assertEqual(self.bot.calls[:2], [("delete", 1, 77), ("delete", 1, 60)])    # нажатие меню и старый экран убраны
        self.assertEqual(self.bot.kinds()[2], "send")
        self.assertEqual(self.ui.anchor_of(1), mid)

    async def test_fresh_entry_strips_buttons_if_old_screen_cannot_be_deleted(self):
        self.ui.remember(1, 60)
        self.bot.fail["delete"] = bad("Bad Request: message can't be deleted")
        await self.ui.show(msg(self.bot, mid=77), "Раздел", None, fresh=True)
        self.assertIn(("strip", 1, 60, None), self.bot.calls)                           # старые кнопки не остаются
        self.assertEqual(self.bot.kinds()[-1], "send")

    async def test_failure_to_delete_user_message_is_harmless(self):
        self.bot.fail["delete"] = bad("Bad Request: message can't be deleted")
        mid = await self.ui.show(msg(self.bot, mid=77), "Экран", None)
        self.assertIsNotNone(mid)

    async def test_users_do_not_share_screens(self):
        await self.ui.show(cb(self.bot, uid=1, mid=60), "A", None)
        await self.ui.show(cb(self.bot, uid=2, mid=70), "B", None)
        self.assertEqual((self.ui.anchor_of(1), self.ui.anchor_of(2)), (60, 70))
        self.ui.forget(1)
        self.assertEqual((self.ui.anchor_of(1), self.ui.anchor_of(2)), (None, 70))
        await self.ui.show(msg(self.bot, uid=2, mid=71), "Ввод B", None)                # правится экран B, а не A
        self.assertIn(("edit", 2, 70, "Ввод B", None), self.bot.calls)

    async def test_long_text_is_clipped(self):
        await self.ui.show(cb(self.bot), "я" * 6000, None)
        self.assertLessEqual(len(self.bot.calls[0][3]), 4096)

    async def test_strip_keyboard(self):
        await self.ui.strip_keyboard(cb(self.bot, mid=60))
        self.assertEqual(self.bot.calls, [("strip", 1, 60, None)])
        self.bot.fail["strip"] = bad("Bad Request: message is not modified")
        await self.ui.strip_keyboard(cb(self.bot, mid=61))                              # ошибка не пробрасывается


if __name__ == "__main__":
    unittest.main()
