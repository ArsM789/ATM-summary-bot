"""Журнал важных событий для администратора (новые пользователи, ошибки, задержки, доставка…).

Запись — «best effort»: сбой журнала никогда не ломает основную работу бота. Повторяющиеся события можно
подавлять по throttle_key (например, «Open-Meteo недоступен» — не чаще раза в 5 минут). Тексты сообщений
пользователей сюда не попадают — только факты и Telegram ID."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from database.repositories import EventLogRepository

logger = logging.getLogger(__name__)

LEVELS = ("info", "warning", "error")


class EventLog:
    def __init__(
        self, repo: EventLogRepository, *, clock: Callable[[], float] = time.time,
        default_throttle: int = 300, keep_last: int = 2000, retention_days: int = 30,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._default_throttle = default_throttle
        self._keep_last = keep_last
        self._retention = retention_days * 86400
        self._last: dict[str, float] = {}
        self._writes = 0

    async def log(
        self, category: str, message: str, *, level: str = "info", user_id: int | None = None,
        throttle_key: str | None = None, throttle_seconds: int | None = None,
    ) -> None:
        """Записывает событие. Никогда не бросает исключений."""
        try:
            now = self._clock()
            if throttle_key is not None:
                window = self._default_throttle if throttle_seconds is None else throttle_seconds
                if now - self._last.get(throttle_key, -1e18) < window:
                    return
                self._last[throttle_key] = now
                if len(self._last) > 500:  # не даём словарю расти бесконечно
                    self._last = {k: v for k, v in self._last.items() if now - v < 3600}
            await self._repo.add(level if level in LEVELS else "info", category, message, user_id, int(now))
            self._writes += 1
            if self._writes % 100 == 0:
                await self._repo.prune(self._keep_last, int(now) - self._retention)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось записать событие в журнал: %s", message)


class NullEventLog:
    """Заглушка с тем же интерфейсом (когда журнал не подключён, например в тестах других модулей)."""

    async def log(self, *args, **kwargs) -> None:
        return None
