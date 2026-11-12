"""Версионные миграции SQLite (PRAGMA user_version).

Версия 1 — исходная схема (database/schema.py, CREATE TABLE IF NOT EXISTS).
Дальше каждая миграция применяется один раз, в транзакции, и только добавляет:
новые таблицы/колонки/индексы. Старые таблицы и данные не удаляются.
Перед миграцией существующей базы делается копия файла (<имя>.v<версия>.bak).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

LATEST_VERSION = 3


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _add_column(conn: aiosqlite.Connection, table: str, column: str, ddl: str) -> None:
    """ALTER TABLE ADD COLUMN, безопасный для повторного запуска."""
    if column not in await _columns(conn, table):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def _migrate_to_2(conn: aiosqlite.Connection) -> None:
    # --- users: имя и обращения, профиль Telegram, признак блокировки бота ---
    await _add_column(conn, "users", "username", "TEXT")
    await _add_column(conn, "users", "first_name", "TEXT")
    # users.name остаётся «основным именем» (preferred_name из ТЗ) — второе поле не заводим.
    await _add_column(conn, "users", "name_variants", "TEXT NOT NULL DEFAULT '[]'")  # JSON-список
    await _add_column(conn, "users", "last_used_name", "TEXT")
    # 0 = имя ещё не подтверждено пользователем (у старых пользователей оно взято из профиля Telegram)
    await _add_column(conn, "users", "name_confirmed", "INTEGER NOT NULL DEFAULT 0")
    await _add_column(conn, "users", "bot_blocked", "INTEGER NOT NULL DEFAULT 0")
    await conn.execute("UPDATE users SET first_name = name WHERE first_name IS NULL")

    # --- schedules: с какого момента запись действует (чтобы не слать «задним числом») ---
    await _add_column(conn, "schedules", "effective_from", "INTEGER NOT NULL DEFAULT 0")

    # --- расписание уроков по предметам ---
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_schedule (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            weekday      INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
            position     INTEGER NOT NULL CHECK (position >= 1),
            subject_name TEXT    NOT NULL CHECK (length(trim(subject_name)) > 0),
            lesson_count INTEGER NOT NULL CHECK (lesson_count BETWEEN 1 AND 12),
            UNIQUE (user_id, weekday, position)
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_weekly_schedule_user_day ON weekly_schedule(user_id, weekday)")

    # --- задел под будущие события (тренировки, репетитор, встречи…). Сейчас НЕ используется кодом. ---
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            event_type TEXT    NOT NULL,                 -- 'tutoring' | 'training' | 'meeting' | ...
            title      TEXT    NOT NULL,
            weekday    INTEGER CHECK (weekday BETWEEN 0 AND 6),   -- для повторяющихся
            event_date TEXT,                                      -- для разовых «ГГГГ-ММ-ДД»
            start_time TEXT,
            end_time   TEXT,
            details    TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_user ON calendar_events(user_id, weekday)")

    # --- журнал уведомлений (дедупликация отправок) ---
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id           INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            notification_type TEXT    NOT NULL,           -- 'summary'
            slot              TEXT    NOT NULL DEFAULT '',-- время отправки «ЧЧ:ММ» из расписания
            notification_date TEXT    NOT NULL,           -- локальная дата пользователя «ГГГГ-ММ-ДД»
            status            TEXT    NOT NULL CHECK (status IN ('sending','sent','failed','blocked')),
            attempts          INTEGER NOT NULL DEFAULT 1,
            error             TEXT,
            created_ts        INTEGER NOT NULL,
            updated_ts        INTEGER NOT NULL,
            sent_at           TEXT,
            UNIQUE (user_id, notification_type, slot, notification_date)
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_log_user_date ON notification_log(user_id, notification_date)")

    # --- обратная связь ---
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            message    TEXT    NOT NULL CHECK (length(trim(message)) > 0),
            status     TEXT    NOT NULL DEFAULT 'new' CHECK (status IN ('new','delivered','delivery_failed')),
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            created_ts INTEGER NOT NULL
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, created_ts)")

    # --- рассылки ---
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broadcasts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id      INTEGER NOT NULL,
            message       TEXT    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft','running','done','cancelled','interrupted')),
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            total_count   INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count  INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broadcast_deliveries (
            broadcast_id INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
            user_id      INTEGER NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed','blocked')),
            error        TEXT,
            sent_at      TEXT,
            PRIMARY KEY (broadcast_id, user_id)     -- один получатель — одна доставка за рассылку
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_status ON broadcast_deliveries(broadcast_id, status)"
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(bot_blocked)")


async def _migrate_to_3(conn: aiosqlite.Connection) -> None:
    # Журнал важных событий для администратора (без текстов сообщений пользователей).
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       INTEGER NOT NULL,                      -- unix-время события
            level    TEXT    NOT NULL CHECK (level IN ('info','warning','error')),
            category TEXT    NOT NULL,                      -- user_new | user_blocked | error | delay | weather | delivery | broadcast | feedback | system
            message  TEXT    NOT NULL,
            user_id  INTEGER                                -- без внешнего ключа: события переживают удаление пользователя
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(ts)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_event_log_category_ts ON event_log(category, ts)")


_MIGRATIONS = {2: _migrate_to_2, 3: _migrate_to_3}


def _backup_sync(path: Path, version: int) -> Path:
    target = path.with_name(f"{path.name}.v{version}.bak")
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


async def run_migrations(conn: aiosqlite.Connection, path: Path, had_data: bool) -> None:
    cursor = await conn.execute("PRAGMA user_version")
    current = (await cursor.fetchone())[0]
    if current >= LATEST_VERSION:
        return
    if had_data and current >= 1:
        backup = await asyncio.to_thread(_backup_sync, path, current)
        logger.info("Копия БД перед миграцией: %s", backup)
    for version in range(max(current, 1) + 1, LATEST_VERSION + 1):
        logger.info("Применяю миграцию БД до версии %d", version)
        await conn.commit()
        await conn.execute("BEGIN")
        try:
            await _MIGRATIONS[version](conn)
            await conn.execute(f"PRAGMA user_version={version}")
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
