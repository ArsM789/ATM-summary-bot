"""Минимальный шим aiosqlite поверх stdlib sqlite3 — ТОЛЬКО для тестов в окружении без aiosqlite.
Настоящий aiosqlite выполняет тот же sqlite3 в отдельном потоке; SQL-семантика идентична."""
import sqlite3

Row = sqlite3.Row
IntegrityError = sqlite3.IntegrityError
Error = sqlite3.Error


class Cursor:
    def __init__(self, cursor):
        self._c = cursor

    @property
    def rowcount(self):
        return self._c.rowcount

    @property
    def lastrowid(self):
        return self._c.lastrowid

    async def fetchone(self):
        return self._c.fetchone()

    async def fetchall(self):
        return self._c.fetchall()


class Connection:
    def __init__(self, path):
        self._conn = sqlite3.connect(str(path))

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    async def execute(self, sql, params=()):
        return Cursor(self._conn.execute(sql, params))

    async def executemany(self, sql, seq):
        return Cursor(self._conn.executemany(sql, seq))

    async def executescript(self, script):
        return Cursor(self._conn.executescript(script))

    async def commit(self):
        self._conn.commit()

    async def rollback(self):
        self._conn.rollback()

    async def close(self):
        self._conn.close()


async def connect(path, **kwargs):
    return Connection(path)
