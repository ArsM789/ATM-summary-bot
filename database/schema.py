"""Схема SQLite. SQLite — единственный источник истины для настроек и расписаний."""

# Базовая схема (версия 1). Все последующие изменения — в database/migrations.py.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id           INTEGER PRIMARY KEY,
    name                  TEXT    NOT NULL,
    city                  TEXT    NOT NULL,
    latitude              REAL    NOT NULL,
    longitude             REAL    NOT NULL,
    timezone              TEXT    NOT NULL,
    notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK (notifications_enabled IN (0, 1)),
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lessons (
    user_id  INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    weekday  INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),   -- 0 = понедельник
    count    INTEGER NOT NULL CHECK (count BETWEEN 0 AND 15),
    PRIMARY KEY (user_id, weekday)
);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    send_time     TEXT    NOT NULL,                              -- «ЧЧ:ММ» по времени пользователя
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    weekdays_mask INTEGER NOT NULL DEFAULT 127 CHECK (weekdays_mask BETWEEN 1 AND 127),
    last_sent_on  TEXT,                                          -- УСТАРЕЛО (v1): дедупликация теперь в notification_log
    UNIQUE (user_id, send_time)
);

CREATE INDEX IF NOT EXISTS idx_schedules_user ON schedules(user_id);
"""
