"""Безопасная отправка сообщений: RetryAfter, повторы при временных сбоях, классификация ошибок."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter, TelegramServerError,
)

logger = logging.getLogger(__name__)

_USE_DEFAULT = object()
_UNREACHABLE_MARKERS = ("chat not found", "user is deactivated", "peer_id_invalid", "bot was blocked")


class SendStatus(str, Enum):
    SENT = "sent"
    BLOCKED = "blocked"   # пользователь заблокировал бота / чат недоступен — повторять бессмысленно
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SendResult:
    status: SendStatus
    error: str | None = None


async def send_message_safe(
    bot: Any,
    chat_id: int,
    text: str,
    *,
    parse_mode: Any = _USE_DEFAULT,
    reply_markup: Any = None,
    max_attempts: int = 4,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SendResult:
    """Отправляет сообщение и никогда не бросает Telegram-исключения наружу.

    - TelegramRetryAfter: ждём указанное Telegram время и повторяем;
    - сетевые сбои/5xx: повтор с нарастающей паузой;
    - Forbidden / «chat not found»: BLOCKED (без повторов);
    - остальное (в т.ч. ошибка разметки): FAILED без повторов.
    parse_mode не передан -> используется режим бота по умолчанию (HTML); None -> обычный текст.
    """
    kwargs: dict[str, Any] = {}
    if parse_mode is not _USE_DEFAULT:
        kwargs["parse_mode"] = parse_mode
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup

    last_error = "unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return SendResult(SendStatus.SENT)
        except TelegramRetryAfter as exc:
            last_error = f"RetryAfter {exc.retry_after}s"
            logger.warning("Telegram просит подождать %ss (chat %s), попытка %d/%d", exc.retry_after, chat_id, attempt, max_attempts)
            if attempt == max_attempts:
                break
            await sleep(float(exc.retry_after) + 0.5)
        except TelegramForbiddenError as exc:
            return SendResult(SendStatus.BLOCKED, f"Forbidden: {exc}")
        except TelegramBadRequest as exc:
            message = str(exc).lower()
            if any(marker in message for marker in _UNREACHABLE_MARKERS):
                return SendResult(SendStatus.BLOCKED, f"BadRequest: {exc}")
            logger.error("Telegram отклонил сообщение для %s: %s", chat_id, exc)
            return SendResult(SendStatus.FAILED, f"BadRequest: {exc}")
        except (TelegramNetworkError, TelegramServerError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Временный сбой отправки (chat %s), попытка %d/%d: %s", chat_id, attempt, max_attempts, exc)
            if attempt == max_attempts:
                break
            await sleep(1.5 ** attempt)
        except TelegramAPIError as exc:
            logger.error("Ошибка Telegram API при отправке %s: %s", chat_id, exc)
            return SendResult(SendStatus.FAILED, f"{type(exc).__name__}: {exc}")
    return SendResult(SendStatus.FAILED, last_error)
