"""Рассылка администратора: форматированный текст (HTML Telegram) и необязательная картинка;
последовательная отправка с ограничением скорости.

Состояние живёт в SQLite (broadcasts + broadcast_deliveries):
- draft -> running переходит атомарно один раз, поэтому повторный callback ничего не запускает;
- пара (рассылка, пользователь) уникальна — одному человеку сообщение уйдёт не больше раза;
- отправляет один «рабочий» с лимитом скорости, а не сотни параллельных задач."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from database.models import Broadcast
from database.repositories import BroadcastRepository, UserRepository
from services.events import EventLog, NullEventLog
from services.telegram_send import SendStatus, send_message_safe, send_photo_safe

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, int]], Awaitable[None]]

DUPLICATE_WINDOW_SECONDS = 30 * 60  # одинаковую рассылку в течение 30 минут повторно запускать только осознанно


def content_hash(kind: str, text: str, photo_unique_id: str | None) -> str:
    """Отпечаток содержимого рассылки: тип + текст + стабильный id картинки."""
    raw = f"{kind}\x1f{text.strip()}\x1f{photo_unique_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


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
        send_photo: Callable[..., Awaitable[Any]] = send_photo_safe,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        events: EventLog | None = None,
    ) -> None:
        self._events = events or NullEventLog()
        self._bot = bot
        self._users = users
        self._repo = repo
        self._rate = rate
        self._send = send
        self._send_photo = send_photo
        self._sleep = sleep
        self._clock = clock

    async def create_draft(self, admin_id: int, text: str, *, photo_file_id: str | None = None,
                           photo_unique_id: str | None = None, kind: str = "custom") -> int:
        """Черновик: text — уже HTML-разметка Telegram; картинка необязательна (photo_file_id)."""
        return await self._repo.create_draft(
            admin_id, text, photo_file_id=photo_file_id, photo_unique_id=photo_unique_id, kind=kind,
            content_hash=content_hash(kind, text, photo_unique_id),
        )

    async def edit_draft(self, broadcast_id: int, *, text: str | None = None, photo_file_id: str | None = None,
                         photo_unique_id: str | None = None, remove_photo: bool = False) -> Broadcast | None:
        """Меняет текст и/или картинку черновика и пересчитывает отпечаток. None — черновика нет или он уже запущен."""
        current = await self._repo.get(broadcast_id)
        if current is None or current.status != "draft":
            return None
        new_text = current.message if text is None else text
        if remove_photo:
            new_unique = None
        elif photo_file_id is not None:
            new_unique = photo_unique_id
        else:
            new_unique = current.photo_unique_id
        ok = await self._repo.update_draft(
            broadcast_id, message=text, photo_file_id=photo_file_id, photo_unique_id=photo_unique_id,
            clear_photo=remove_photo, content_hash=content_hash(current.kind, new_text, new_unique),
        )
        return await self._repo.get(broadcast_id) if ok else None

    async def get(self, broadcast_id: int) -> Broadcast | None:
        return await self._repo.get(broadcast_id)

    async def recent_duplicate(self, broadcast: Broadcast, within_seconds: int = DUPLICATE_WINDOW_SECONDS) -> Broadcast | None:
        """Такая же рассылка (тот же текст и картинка), запущенная недавно, — чтобы не отправить дважды случайно."""
        if not broadcast.content_hash:
            return None
        return await self._repo.find_recent_duplicate(broadcast.content_hash, within_seconds, exclude_id=broadcast.id)

    async def recipients_count(self, admin_id: int) -> int:
        return len(await self._users.list_broadcast_targets(exclude_id=admin_id))

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

        await self._events.log("broadcast", f"Рассылка #{broadcast_id} запущена: получателей {len(targets)}")
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
                        # Текст — HTML-разметка, полученная из форматирования самого админа (безопасна);
                        # с картинкой текст уходит подписью.
                        if broadcast.photo_file_id:
                            result = await self._send_photo(self._bot, user_id, broadcast.photo_file_id,
                                                            caption=broadcast.message or None)
                        else:
                            result = await self._send(self._bot, user_id, broadcast.message)
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
            await self._events.log("broadcast", f"Рассылка #{broadcast_id} прервана ошибкой", level="error")
            raise
        final = await self._repo.finish(broadcast_id, "done")
        await self._events.log(
            "broadcast",
            f"Рассылка #{broadcast_id} завершена: отправлено {final.success_count} из {final.total_count}, "
            f"ошибок {final.failed_count}, недоступны {final.blocked_count}",
            level="warning" if final.failed_count else "info",
        )
        logger.info(
            "Рассылка %s завершена: всего %d, успешно %d, ошибок %d, недоступны %d",
            broadcast_id, final.total_count, final.success_count, final.failed_count, final.blocked_count,
        )
        return final
