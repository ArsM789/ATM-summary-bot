"""Асинхронная обёртка над SQLite (aiosqlite): одно соединение, сериализация через Lock."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from database.migrations import run_migrations
from database.schema import SCHEMA, SCHEMA_VERSION

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        had_data = self._path.exists() and self._path.stat().st_size > 0
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)  # базовая схема v1: при первом запуске создаёт таблицы
        cursor = await self._conn.execute("PRAGMA user_version")
        if (await cursor.fetchone())[0] == 0:  # свежая база (или база без версии из v1)
            await self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._conn.commit()
        await run_migrations(self._conn, self._path, had_data)
        logger.info("SQLite готова: %s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не подключена")
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Несколько операций как одно целое: либо всё применится, либо ничего."""
        async with self._lock:
            try:
                yield self._db
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Одиночная запись; возвращает число затронутых строк."""
        async with self.transaction() as conn:
            cursor = await conn.execute(sql, params)
            return cursor.rowcount

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self._db.execute(sql, params)
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self._db.execute(sql, params)
            return list(await cursor.fetchall())
