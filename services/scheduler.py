"""Обёртка над APScheduler 3.x. Единственное задание — периодический проход «кому пора слать».

Источник истины — SQLite: расписания, пояса и журнал отправок лежат в БД. Планировщик ничего
не хранит между перезапусками и не нуждается в восстановлении заданий: после старта первый же
проход находит всё, что пора отправлять (включая недогнанное в пределах окна), а журнал
исключает повторные отправки."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from services.delivery import SummaryDispatcher
from services.events import EventLog, NullEventLog

logger = logging.getLogger(__name__)


class SummaryScheduler:
    def __init__(
        self, dispatcher: SummaryDispatcher, interval_seconds: int = 30, events: EventLog | None = None,
        extra_runners: Sequence[Callable[[], Awaitable[Any]]] = (),
    ) -> None:
        """extra_runners — дополнительные «исполнители» того же прохода (например, напоминания о событиях):
        один APScheduler-job на всё, второй планировщик не нужен."""
        self._dispatcher = dispatcher
        self._extra_runners = tuple(extra_runners)
        self._events = events or NullEventLog()
        self._interval = interval_seconds
        self._scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._scheduler.add_job(
            self._tick,
            IntervalTrigger(seconds=self._interval),
            id="summary-dispatch",
            max_instances=1,        # проходы не накладываются друг на друга
            coalesce=True,          # пропущенные запуски схлопываются в один
            misfire_grace_time=60,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("Планировщик сводок запущен (проход каждые %d с)", self._interval)

    async def _tick(self) -> None:
        # Каждый исполнитель — в своём try: сбой сводок не мешает напоминаниям и наоборот.
        for name, runner in (("summaries", self._dispatcher.run_once), *(
                (getattr(r, "__qualname__", "runner"), r) for r in self._extra_runners)):
            try:
                await runner()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Сбой прохода планировщика (%s)", name)
                await self._events.log("error", f"Сбой прохода планировщика ({name}): {type(exc).__name__}: {str(exc)[:150]}",
                                       level="error", throttle_key=f"scheduler-tick-{name}", throttle_seconds=300)

    def shutdown(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
