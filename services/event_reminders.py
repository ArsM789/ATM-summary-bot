"""Напоминания о событиях пользователей («Через 20 минут у тебя начнётся: …»).

Работает в том же проходе планировщика, что и утренние сводки (один APScheduler-job), но полностью независимо
от генератора сводок: никакой погоды и расписания уроков — только короткое напоминание.

Надёжность:
- источник истины — SQLite (calendar_events + notification_log): после перезапуска ничего «не восстанавливается»,
  каждый проход заново вычисляет, что пора напомнить;
- напоминание шлётся, только если его момент наступил не более grace секунд назад (по умолчанию 5 минут) и
  событие ещё не началось — «устаревшие» после простоя бота не отправляются;
- дедупликация: UNIQUE (пользователь, 'event', «id@время», дата) + атомарный claim — даже при параллельных
  проходах одно вхождение не уйдёт дважды;
- перед отправкой событие и пользователь перечитываются из БД: удалённое/отключённое/изменённое — пропускается."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from database.models import User
from database.repositories import NotificationLogRepository, UserEventRepository, UserRepository
from services.delivery import Outcome
from services.event_schedule import DueReminder, end_datetime, find_due_reminders, is_finished_one_time, rule_from_event
from services.event_text import reminder_text
from services.events import EventLog, NullEventLog
from services.names import pick_address
from services.telegram_send import SendStatus, send_message_safe

logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "event"
GRACE_SECONDS = 300
MAX_ATTEMPTS = 3


class EventReminderDispatcher:
    def __init__(
        self, bot: Any, users: UserRepository, events: UserEventRepository, log: NotificationLogRepository,
        *, concurrency: int = 5, send: Callable[..., Awaitable[Any]] = send_message_safe,
        event_log: EventLog | None = None, rng: random.Random | None = None, grace_seconds: int = GRACE_SECONDS,
    ) -> None:
        self._bot = bot
        self._users = users
        self._events = events
        self._log = log
        self._semaphore = asyncio.Semaphore(concurrency)
        self._send = send
        self._event_log = event_log or NullEventLog()
        self._rng = rng or random.Random()
        self._grace = grace_seconds
        self._last_prune_hour = -1

    async def run_once(self, now_utc: datetime | None = None) -> Counter:
        now = now_utc or datetime.now(timezone.utc)
        now_ts = now.timestamp()
        pairs = await self._events.list_active_with_users()

        # Разовые события, которые закончились, больше не активны и не занимают лимит.
        for event, _ in pairs:
            if is_finished_one_time(event, now_ts):
                await self._events.mark_done(event.id)
        hour = int(now_ts // 3600)
        if hour != self._last_prune_hour:
            self._last_prune_hour = hour
            await self._events.prune_done()

        eligible = [(e, u) for e, u in pairs
                    if u.event_reminders_enabled and not u.bot_blocked and e.status == "active"
                    and not is_finished_one_time(e, now_ts)]
        due = find_due_reminders([e for e, _ in eligible], now_ts, self._grace)
        results: Counter = Counter()
        if not due:
            return results

        owners = {e.id: u for e, u in eligible}

        async def guarded(item: DueReminder) -> Outcome:
            async with self._semaphore:
                try:
                    return await self.deliver(item, owners[item.event.id], now)
                except Exception:  # noqa: BLE001 — сбой одного пользователя не задевает остальных
                    logger.exception("Сбой напоминания о событии %s", item.event.id)
                    return Outcome.FAILED

        outcomes = await asyncio.gather(*(guarded(d) for d in due))
        for outcome in outcomes:
            results[outcome] += 1
        logger.info("Напоминания: к отправке %d, итог %s", len(due), dict(results))
        await self._report(due, outcomes)
        return results

    async def deliver(self, due: DueReminder, snapshot_user: User, now_utc: datetime) -> Outcome:
        user_id = due.event.user_id
        now_ts = now_utc.timestamp()

        # 1. Актуальность: пользователь и событие — заново из БД, строго по user_id.
        user = await self._users.get(user_id)
        if user is None or user.bot_blocked or not user.event_reminders_enabled:
            return Outcome.SKIPPED
        event = await self._events.get(user_id, due.event.id)
        if event is None or event.status != "active":
            return Outcome.SKIPPED            # удалено или отключено, пока ждали
        again = [d for d in find_due_reminders([event], now_ts, self._grace)
                 if d.local_date == due.local_date and d.slot == due.slot]
        if not again:
            return Outcome.SKIPPED            # событие изменили: время/правило/момент напоминания уже другие
        current = again[0]

        # 2. Дедупликация: занять запись (пользователь, 'event', «id@время», дата начала).
        claim = await self._log.claim(
            user_id, NOTIFICATION_TYPE, current.slot, current.local_date,
            now_ts=int(now_ts), max_attempts=MAX_ATTEMPTS, retry_gap=45, stale_after=120,
        )
        if claim is None:
            return Outcome.SKIPPED

        # 3. Текст: имя (чередуем варианты обращения), реальное время до начала.
        rule = rule_from_event(event)
        end = end_datetime(current.start, rule.duration_min)
        minutes_left = round((current.start.timestamp() - now_ts) / 60)
        address = pick_address(user, self._rng)
        if address != user.last_used_name:
            await self._users.set_last_used_name(user_id, address)
        text = reminder_text(address, event.title, minutes_left, current.start, end,
                             now_utc.astimezone(current.start.tzinfo).date(), self._rng)

        # 4. Отправка и запись результата.
        sent = await self._send(self._bot, user_id, text)
        if sent.status is SendStatus.SENT:
            await self._log.finish(claim.log_id, "sent", now_ts=int(now_ts))
            return Outcome.SENT
        if sent.status is SendStatus.BLOCKED:
            await self._log.finish(claim.log_id, "blocked", sent.error, now_ts=int(now_ts))
            await self._users.set_bot_blocked(user_id, True)
            return Outcome.BLOCKED
        await self._log.finish(claim.log_id, "failed", sent.error, now_ts=int(now_ts))
        logger.error("Напоминание %s не отправлено (попытка %d): %s", event.id, claim.attempt, sent.error)
        return Outcome.FAILED

    async def _report(self, due: list[DueReminder], outcomes: list[Outcome]) -> None:
        for item, outcome in zip(due, outcomes):
            uid = item.event.user_id
            if outcome is Outcome.BLOCKED:
                await self._event_log.log(
                    "user_blocked", "Пользователь заблокировал бота (напоминание не доставлено)", user_id=uid,
                    throttle_key=f"blocked-{uid}", throttle_seconds=86400)
            elif outcome is Outcome.FAILED:
                await self._event_log.log(
                    "delivery", f"Не удалось отправить напоминание о событии #{item.event.id}", level="error",
                    user_id=uid, throttle_key=f"event-fail-{item.event.id}", throttle_seconds=900)
