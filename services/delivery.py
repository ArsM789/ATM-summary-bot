"""Автоматическая отправка сводок. Для каждого пользователя всё делается отдельно и независимо:
свежая загрузка его данных -> «занять» запись в журнале -> собрать его сводку -> отправить в его чат
-> записать результат. Общих изменяемых данных между пользователями нет."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from database.repositories import NotificationLogRepository, ScheduleRepository, UserRepository
from services.scheduling_logic import CATCHUP_WINDOW_SECONDS, Due, find_due
from services.summary_service import SummaryService
from services.telegram_send import SendStatus, send_message_safe
from services.time_service import get_zone

logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "summary"
MAX_ATTEMPTS = 3


class Outcome(str, Enum):
    SENT = "sent"
    SKIPPED = "skipped"    # неактуально или уже отправлено
    DEFERRED = "deferred"  # погоду получить не удалось — повторим позже
    BLOCKED = "blocked"
    FAILED = "failed"


class SummaryDispatcher:
    def __init__(
        self, bot: Any, users: UserRepository, schedules: ScheduleRepository,
        log: NotificationLogRepository, summaries: SummaryService,
        *, concurrency: int = 5, reply_markup: Any = None,
        send: Callable[..., Awaitable[Any]] = send_message_safe,
    ) -> None:
        self._bot = bot
        self._users = users
        self._schedules = schedules
        self._log = log
        self._summaries = summaries
        self._semaphore = asyncio.Semaphore(concurrency)
        self._reply_markup = reply_markup
        self._send = send

    async def run_once(self, now_utc: datetime | None = None) -> Counter:
        """Один проход планировщика: найти, кому пора, и отправить (ограниченно параллельно)."""
        now = now_utc or datetime.now(timezone.utc)
        items = await self._schedules.list_all_active()
        due = find_due(items, now, CATCHUP_WINDOW_SECONDS)
        results: Counter = Counter()
        if not due:
            return results

        async def guarded(item: Due) -> Outcome:
            async with self._semaphore:
                try:
                    return await self.deliver(item, now)
                except Exception:  # noqa: BLE001 — сбой одного пользователя не должен задеть остальных
                    logger.exception("Сбой доставки сводки пользователю %s", item.user.telegram_id)
                    return Outcome.FAILED

        for outcome in await asyncio.gather(*(guarded(d) for d in due)):
            results[outcome] += 1
        logger.info("Планировщик: к отправке %d, итог %s", len(due), dict(results))
        return results

    async def deliver(self, due: Due, now_utc: datetime) -> Outcome:
        user_id = due.user.telegram_id

        # 1. Актуальность: данные берём из БД заново, по user_id (а не из снимка, сделанного при поиске).
        user = await self._users.get(user_id)
        if user is None or not user.notifications_enabled or user.bot_blocked:
            return Outcome.SKIPPED
        if user.timezone != due.user.timezone:
            return Outcome.SKIPPED  # пояс сменили между поиском и отправкой — следующий проход пересчитает
        entry = await self._schedules.get(user_id, due.entry.id)
        if entry is None or not entry.enabled or entry.send_time != due.slot or entry.effective_from > due.scheduled_ts:
            return Outcome.SKIPPED

        # 2. Дедупликация: занять запись журнала (user, тип, слот, локальная дата).
        now_ts = int(now_utc.timestamp())
        claim = await self._log.claim(
            user_id, NOTIFICATION_TYPE, due.slot, due.local_date, now_ts=now_ts, max_attempts=MAX_ATTEMPTS
        )
        if claim is None:
            return Outcome.SKIPPED

        # 3. Сводка именно этого пользователя, по его локальному времени.
        local_now = now_utc.astimezone(get_zone(user.timezone))
        try:
            result = await self._summaries.build(user, now=local_now)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось собрать сводку пользователя %s", user_id)
            await self._log.finish(claim.log_id, "failed", f"build: {exc!r}", now_ts=now_ts)
            return Outcome.FAILED

        if not result.weather_ok and claim.attempt < MAX_ATTEMPTS:
            await self._log.finish(claim.log_id, "failed", "weather_unavailable", now_ts=now_ts)
            return Outcome.DEFERRED  # на последней попытке отправим сводку без погоды

        # 4. Отправка в личный чат пользователя.
        sent = await self._send(self._bot, user_id, result.text, reply_markup=self._reply_markup)

        # 5. Результат — в БД.
        if sent.status is SendStatus.SENT:
            await self._log.finish(claim.log_id, "sent", now_ts=now_ts)
            return Outcome.SENT
        if sent.status is SendStatus.BLOCKED:
            await self._log.finish(claim.log_id, "blocked", sent.error, now_ts=now_ts)
            await self._users.set_bot_blocked(user_id, True)
            logger.warning("Пользователь %s недоступен (заблокировал бота?): %s", user_id, sent.error)
            return Outcome.BLOCKED
        await self._log.finish(claim.log_id, "failed", sent.error, now_ts=now_ts)
        logger.error("Сводка пользователю %s не отправлена (попытка %d): %s", user_id, claim.attempt, sent.error)
        return Outcome.FAILED
