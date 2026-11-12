"""Обёртка над APScheduler 3.x. Единственное задание — периодический проход «кому пора слать».

Источник истины — SQLite: расписания, пояса и журнал отправок лежат в БД. Планировщик ничего
не хранит между перезапусками и не нуждается в восстановлении заданий: после старта первый же
проход находит всё, что пора отправлять (включая недогнанное в пределах окна), а журнал
исключает повторные отправки."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from services.delivery import SummaryDispatcher
from services.events import EventLog, NullEventLog

logger = logging.getLogger(__name__)


class SummaryScheduler:
    def __init__(
            self,
            dispatcher: SummaryDispatcher,
            tomorrow_dispatcher=None,
            interval_seconds: int = 30,
            events: EventLog | None = None,
    ) -> None:
        self._dispatchers = [dispatcher]

        if tomorrow_dispatcher is not None:
            self._dispatchers.append(tomorrow_dispatcher)

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
        for dispatcher in self._dispatchers:
            try:
                await dispatcher.run_once()
            except Exception as exc:
                logger.exception(
                    "Сбой диспетчера %s",
                    type(dispatcher).__name__,
                )

                await self._events.log(
                    "error",
                    (
                        f"Сбой диспетчера {type(dispatcher).__name__}: "
                        f"{type(exc).__name__}: {str(exc)[:150]}"
                    ),
                    level="error",
                    throttle_key=f"dispatcher-{type(dispatcher).__name__}",
                    throttle_seconds=300,
                )

    def shutdown(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
