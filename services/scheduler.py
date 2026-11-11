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

logger = logging.getLogger(__name__)


class SummaryScheduler:
    def __init__(self, dispatcher: SummaryDispatcher, interval_seconds: int = 30) -> None:
        self._dispatcher = dispatcher
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
        try:
            await self._dispatcher.run_once()
        except Exception:  # noqa: BLE001
            logger.exception("Сбой прохода планировщика")

    def shutdown(self) -> None:
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
