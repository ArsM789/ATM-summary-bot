"""Автоматическая отправка вечерних сводок."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, time, timezone
from enum import Enum
from typing import Any

from database.repositories import (
    NotificationLogRepository,
    UserRepository,
)
from services.tomorrow_brief_service import TommorowBriefService
from services.telegram_send import SendStatus, send_message_safe
from services.time_service import get_zone

logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "tomorrow_briefing"
SEND_TIME = "18:00"

CATCHUP_WINDOW_SECONDS = 60 * 60
MAX_ATTEMPTS = 3


class Outcome(str, Enum):
    SENT = "sent"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
    BLOCKED = "blocked"
    FAILED = "failed"


class TomorrowBriefDispatcher:
    def __init__(
        self,
        bot: Any,
        users: UserRepository,
        log: NotificationLogRepository,
        briefs: TommorowBriefService,
        *,
        concurrency: int = 5,
        send=send_message_safe,
    ) -> None:
        self._bot = bot
        self._users = users
        self._log = log
        self._briefs = briefs
        self._semaphore = asyncio.Semaphore(concurrency)
        self._send = send

    async def run_once(
        self,
        now_utc: datetime | None = None,
    ) -> Counter:

        now = now_utc or datetime.now(timezone.utc)

        users = await self._users.list_notification_targets()

        results: Counter = Counter()

        async def guarded(user) -> Outcome:
            async with self._semaphore:
                try:
                    return await self.deliver(user, now)
                except Exception:
                    logger.exception(
                        "Ошибка вечерней сводки для пользователя %s",
                        user.telegram_id,
                    )
                    return Outcome.FAILED

        outcomes = await asyncio.gather(
            *(guarded(user) for user in users)
        )

        for outcome in outcomes:
            results[outcome] += 1

        if results:
            logger.info(
                "Вечерние сводки: %s",
                dict(results),
            )

        return results

    async def deliver(self, user_snapshot, now_utc: datetime) -> Outcome:
        user_id = user_snapshot.telegram_id

        # 1. Получаем актуальные данные пользователя.
        user = await self._users.get(user_id)

        if (
            user is None
            or not user.notifications_enabled
            or user.bot_blocked
        ):
            return Outcome.SKIPPED

        if user.timezone != user_snapshot.timezone:
            return Outcome.SKIPPED

        # 2. Переводим время в часовой пояс пользователя.
        local_now = now_utc.astimezone(get_zone(user.timezone))

        scheduled = datetime.combine(
            local_now.date(),
            time(18, 0),
            tzinfo=local_now.tzinfo,
        )

        # 3. Проверяем, наступило ли время отправки.
        delay = now_utc.timestamp() - scheduled.timestamp()

        if not 0 <= delay <= CATCHUP_WINDOW_SECONDS:
            return Outcome.SKIPPED

        # 4. Занимаем запись в журнале.
        now_ts = int(now_utc.timestamp())

        claim = await self._log.claim(
            user_id,
            NOTIFICATION_TYPE,
            SEND_TIME,
            local_now.date().isoformat(),
            now_ts=now_ts,
            max_attempts=MAX_ATTEMPTS,
        )

        if claim is None:
            return Outcome.SKIPPED

        # 5. Формируем сообщение.
        try:
            result = await self._briefs.build(
                user,
                now=local_now,
            )
        except Exception as exc:
            logger.exception(
                "Не удалось собрать вечернюю сводку пользователя %s",
                user_id,
            )
            await self._log.finish(
                claim.log_id,
                "failed",
                f"build: {exc!r}",
                now_ts=now_ts,
            )
            return Outcome.FAILED

        # Если погода недоступна, пробуем ещё раз через 3 минуты.
        if not result.weather_ok and claim.attempt < MAX_ATTEMPTS:
            await self._log.finish(
                claim.log_id,
                "failed",
                "weather_unavailable",
                now_ts=now_ts,
            )
            return Outcome.DEFERRED

        # 6. Отправляем сообщение.
        sent = await self._send(
            self._bot,
            user_id,
            result.text,
            reply_markup=None,
        )

        # 7. Записываем результат.
        if sent.status is SendStatus.SENT:
            await self._log.finish(
                claim.log_id,
                "sent",
                now_ts=now_ts,
            )
            return Outcome.SENT

        if sent.status is SendStatus.BLOCKED:
            await self._log.finish(
                claim.log_id,
                "blocked",
                sent.error,
                now_ts=now_ts,
            )
            await self._users.set_bot_blocked(user_id, True)
            return Outcome.BLOCKED

        await self._log.finish(
            claim.log_id,
            "failed",
            sent.error,
            now_ts=now_ts,
        )

        return Outcome.FAILED