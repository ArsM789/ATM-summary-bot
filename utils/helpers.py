"""Мелкие вспомогательные вещи: логирование и защита от повторной обработки."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, level, logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(numeric_level)
    root.addHandler(console)
    root.addHandler(file_handler)
    # Не засоряем лог подробностями HTTP-клиентов и планировщика.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(max(numeric_level, logging.INFO))


class InFlightGuard:
    """Не даёт обрабатывать одно и то же действие одного пользователя параллельно.

    Пока сводка для пользователя формируется, повторное нажатие кнопки игнорируется —
    так двойной тап не породит два сообщения и два запроса к API.
    """

    def __init__(self) -> None:
        self._active: set[tuple[int, str]] = set()

    def try_acquire(self, user_id: int, action: str) -> bool:
        key = (user_id, action)
        if key in self._active:
            return False
        self._active.add(key)
        return True

    def release(self, user_id: int, action: str) -> None:
        self._active.discard((user_id, action))
