"""Рассылка администратора: последовательная отправка с ограничением скорости.

Состояние живёт в SQLite (broadcasts + broadcast_deliveries):
- draft -> running переходит атомарно один раз, поэтому повторный callback ничего не запускает;
- пара (рассылка, пользователь) уникальна — одному человеку сообщение уйдёт не больше раза;
- отправляет один «рабочий» с лимитом скорости, а не сотни параллельных задач."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from database.models import Broadcast
from database.repositories import BroadcastRepository, UserRepository
from services.telegram_send import SendStatus, send_message_safe

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, int]], Awaitable[None]]


class AsyncRateLimiter:
    """Не чаще `rate` событий в секунду (равномерно, без всплесков)."""

    def __init__(
        self, rate: float, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0

    async def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            await self._sleep(self._next - now)
            now = self._next
        self._next = max(now, self._next) + self._interval


class BroadcastService:
    def __init__(
        self, bot: Any, users: UserRepository, repo: BroadcastRepository, rate: float,
        *, send: Callable[..., Awaitable[Any]] = send_message_safe,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._users = users
        self._repo = repo
        self._rate = rate
        self._send = send
        self._sleep = sleep
        self._clock = clock

    async def create_draft(self, admin_id: int, text: str) -> int:
        return await self._repo.create_draft(admin_id, text)

    async def cancel(self, broadcast_id: int) -> bool:
        return await self._repo.cancel(broadcast_id)

    async def run(self, broadcast_id: int, on_progress: ProgressCallback | None = None) -> Broadcast | None:
        """Запускает рассылку. None — запускать нельзя (уже запущена/отменена/идёт другая)."""
        broadcast = await self._repo.get(broadcast_id)
        if broadcast is None:
            return None
        targets = await self._users.list_broadcast_targets(exclude_id=broadcast.admin_id)  # админу не шлём
        if not await self._repo.claim_start(broadcast_id, targets):
            return None

        limiter = AsyncRateLimiter(self._rate, self._clock, self._sleep)
        last_report = self._clock()
        done_since_report = 0
        try:
            while True:
                batch = await self._repo.next_pending(broadcast_id, 50)
                if not batch:
                    break
                for user_id in batch:
                    await limiter.wait()
                    try:
                        # Текст админа уходит как обычный текст (parse_mode=None): без риска сломать разметку.
                        result = await self._send(self._bot, user_id, broadcast.message, parse_mode=None)
                        status, error = result.status, result.error
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Неожиданная ошибка рассылки для %s", user_id)
                        status, error = SendStatus.FAILED, repr(exc)
                    await self._repo.mark_delivery(broadcast_id, user_id, status.value, error)
                    if status is SendStatus.BLOCKED:
                        await self._users.set_bot_blocked(user_id, True)
                    done_since_report += 1
                    if on_progress and (done_since_report >= 20 or self._clock() - last_report >= 3):
                        await on_progress(await self._repo.counts(broadcast_id))
                        done_since_report, last_report = 0, self._clock()
        except BaseException:
            await self._repo.finish(broadcast_id, "interrupted")
            raise
        final = await self._repo.finish(broadcast_id, "done")
        logger.info(
            "Рассылка %s завершена: всего %d, успешно %d, ошибок %d, недоступны %d",
            broadcast_id, final.total_count, final.success_count, final.failed_count, final.blocked_count,
        )
        return final
