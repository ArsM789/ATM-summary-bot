"""Единый принцип интерфейса: у пользователя один «живой» экран.

- нажатие inline-кнопки -> редактируем то же сообщение (а не присылаем новое);
- ввод текста в сценарии -> удаляем сообщение пользователя и обновляем экран-«якорь» (ошибки — тоже в экране);
- вход из reply-меню / команды (fresh=True) -> прошлый экран убираем (устаревшие кнопки не остаются), новый
  присылаем внизу чата;
- если редактировать нельзя (сообщение удалено/старое) — аккуратно присылаем новое и запоминаем его.

Модуль намеренно не импортирует aiogram.types: тип события определяется по атрибутам, поэтому логика
проверяется тестами с простыми фейками. Якоря хранятся в памяти процесса: после перезапуска бот просто
пришлёт новый экран."""
from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from utils.formatting import clip

logger = logging.getLogger(__name__)


def _is_callback(event: Any) -> bool:
    """CallbackQuery имеет .data и .message, у Message есть .chat."""
    return hasattr(event, "data") and not hasattr(event, "chat")


def _not_modified(exc: Exception) -> bool:
    return "message is not modified" in str(exc).lower()


class UI:
    def __init__(self) -> None:
        self._anchors: dict[int, int] = {}   # user_id -> id живого экрана

    # ----------------------------- якоря -----------------------------

    def anchor_of(self, user_id: int) -> int | None:
        return self._anchors.get(user_id)

    def remember(self, user_id: int, message_id: int) -> None:
        self._anchors[user_id] = message_id

    def forget(self, user_id: int) -> None:
        self._anchors.pop(user_id, None)

    # ----------------------------- безопасные операции -----------------------------

    @staticmethod
    async def delete(bot: Any, chat_id: int, message_id: int | None) -> bool:
        """Удаляет сообщение, если получится (бот может удалять свои и входящие сообщения личного чата)."""
        if not message_id:
            return False
        try:
            await bot.delete_message(chat_id, message_id)
            return True
        except TelegramAPIError as exc:
            logger.debug("Не удалось удалить сообщение %s: %s", message_id, exc)
            return False

    async def retire(self, bot: Any, chat_id: int, message_id: int | None) -> None:
        """Убирает устаревший экран: удаляет; если нельзя — хотя бы снимает кнопки, чтобы не нажимались."""
        if not message_id:
            return
        if await self.delete(bot, chat_id, message_id):
            return
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except TelegramAPIError:
            pass

    # ----------------------------- показ экрана -----------------------------

    async def show(self, event: Any, text: str, kb: Any = None, *, fresh: bool = False) -> int | None:
        """Показывает экран. Возвращает id сообщения-экрана."""
        text = clip(text)
        if _is_callback(event):
            return await self._show_callback(event, text, kb)
        return await self._show_message(event, text, kb, fresh)

    async def _show_callback(self, event: Any, text: str, kb: Any) -> int | None:
        user_id = event.from_user.id
        message = getattr(event, "message", None)
        if message is not None:
            try:
                await event.bot.edit_message_text(
                    text, chat_id=message.chat.id, message_id=message.message_id, reply_markup=kb)
                self.remember(user_id, message.message_id)
                return message.message_id
            except TelegramBadRequest as exc:
                if _not_modified(exc):
                    self.remember(user_id, message.message_id)
                    return message.message_id
                logger.debug("edit_message_text не удался (%s) — присылаю новое сообщение", exc)
            except TelegramAPIError as exc:
                logger.debug("edit_message_text не удался (%s) — присылаю новое сообщение", exc)
        sent = await event.bot.send_message(user_id, text, reply_markup=kb)
        self.remember(user_id, sent.message_id)
        return sent.message_id

    async def _show_message(self, event: Any, text: str, kb: Any, fresh: bool) -> int | None:
        user_id, chat_id, bot = event.from_user.id, event.chat.id, event.bot
        await self.delete(bot, chat_id, getattr(event, "message_id", None))   # входящее сообщение пользователя больше не нужно

        if fresh:
            await self.retire(bot, chat_id, self._anchors.pop(user_id, None))
        else:
            anchor = self._anchors.get(user_id)
            if anchor:
                try:
                    await bot.edit_message_text(text, chat_id=chat_id, message_id=anchor, reply_markup=kb)
                    return anchor
                except TelegramBadRequest as exc:
                    if _not_modified(exc):
                        return anchor
                    logger.debug("Якорь %s не отредактировать (%s) — присылаю новое сообщение", anchor, exc)
                except TelegramAPIError as exc:
                    logger.debug("Якорь %s не отредактировать (%s) — присылаю новое сообщение", anchor, exc)
        sent = await bot.send_message(chat_id, text, reply_markup=kb)
        self.remember(user_id, sent.message_id)
        return sent.message_id

    # ----------------------------- устаревшие кнопки -----------------------------

    async def strip_keyboard(self, event: Any) -> None:
        """Снимает inline-кнопки с сообщения, по которому нажали устаревшую кнопку."""
        message = getattr(event, "message", None)
        if message is None:
            return
        try:
            await event.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=message.message_id, reply_markup=None)
        except TelegramAPIError:
            pass


ui = UI()  # общий «менеджер экранов» процесса: якоря живут в памяти (после перезапуска бот пришлёт новый экран)
