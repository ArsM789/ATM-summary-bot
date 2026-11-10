"""Репозитории: весь SQL здесь. Каждый метод привязан к user_id — доступ только к своим данным."""
from __future__ import annotations

import logging

import aiosqlite

from config import Settings
from database.database import Database
from database.models import ALL_WEEKDAYS_MASK, ScheduleEntry, User

logger = logging.getLogger(__name__)

MAX_SCHEDULES_PER_USER = 10


class DuplicateTimeError(Exception):
    """У пользователя уже есть уведомление на это время."""


class ScheduleLimitError(Exception):
    """Достигнут лимит уведомлений на пользователя."""


def _clean_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())[:50]
    return name or "друг"


def _user(row: aiosqlite.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        name=row["name"],
        city=row["city"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        timezone=row["timezone"],
        notifications_enabled=bool(row["notifications_enabled"]),
    )


def _schedule(row: aiosqlite.Row) -> ScheduleEntry:
    return ScheduleEntry(
        id=row["id"],
        user_id=row["user_id"],
        send_time=row["send_time"],
        enabled=bool(row["enabled"]),
        weekdays_mask=row["weekdays_mask"],
        last_sent_on=row["last_sent_on"],
    )


class UserRepository:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def get(self, user_id: int) -> User | None:
        row = await self._db.fetchone("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
        return _user(row) if row else None

    async def get_or_create(self, user_id: int, display_name: str | None) -> User:
        existing = await self.get(user_id)
        if existing:
            return existing
        s = self._settings
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO users (telegram_id, name, city, latitude, longitude, timezone) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, _clean_name(display_name), s.default_city, s.default_latitude,
                 s.default_longitude, s.default_timezone),
            )
            if cursor.rowcount == 1:  # новый пользователь (а не гонка двух запросов) — заводим стартовые данные
                await conn.executemany(
                    "INSERT OR IGNORE INTO lessons (user_id, weekday, count) VALUES (?, ?, ?)",
                    [(user_id, wd, n) for wd, n in s.default_lessons.items()],
                )
                await conn.execute(
                    "INSERT OR IGNORE INTO schedules (user_id, send_time, enabled, weekdays_mask) "
                    "VALUES (?, ?, 1, ?)",
                    (user_id, s.default_send_time, ALL_WEEKDAYS_MASK),
                )
                logger.info("Новый пользователь: %s", user_id)
        created = await self.get(user_id)
        assert created is not None
        return created

    async def update_name(self, user_id: int, name: str) -> None:
        await self._db.execute(
            "UPDATE users SET name = ?, updated_at = datetime('now') WHERE telegram_id = ?",
            (_clean_name(name), user_id),
        )

    async def update_city(self, user_id: int, city: str, latitude: float, longitude: float) -> None:
        await self._db.execute(
            "UPDATE users SET city = ?, latitude = ?, longitude = ?, updated_at = datetime('now') "
            "WHERE telegram_id = ?",
            (city, latitude, longitude, user_id),
        )

    async def update_timezone(self, user_id: int, timezone: str) -> None:
        await self._db.execute(
            "UPDATE users SET timezone = ?, updated_at = datetime('now') WHERE telegram_id = ?",
            (timezone, user_id),
        )

    async def set_notifications_enabled(self, user_id: int, enabled: bool) -> None:
        await self._db.execute(
            "UPDATE users SET notifications_enabled = ?, updated_at = datetime('now') WHERE telegram_id = ?",
            (int(enabled), user_id),
        )


class LessonsRepository:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._defaults = settings.default_lessons

    async def get_week(self, user_id: int) -> dict[int, int]:
        rows = await self._db.fetchall("SELECT weekday, count FROM lessons WHERE user_id = ?", (user_id,))
        week = dict(self._defaults)
        week.update({row["weekday"]: row["count"] for row in rows})
        return week

    async def set_day(self, user_id: int, weekday: int, count: int) -> None:
        if not 0 <= weekday <= 6 or not 0 <= count <= 15:
            raise ValueError("Некорректный день недели или количество уроков")
        await self._db.execute(
            "INSERT INTO lessons (user_id, weekday, count) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, weekday) DO UPDATE SET count = excluded.count",
            (user_id, weekday, count),
        )

    async def reset_to_defaults(self, user_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO lessons (user_id, weekday, count) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, weekday) DO UPDATE SET count = excluded.count",
                [(user_id, wd, n) for wd, n in self._defaults.items()],
            )


class ScheduleRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_for_user(self, user_id: int) -> list[ScheduleEntry]:
        rows = await self._db.fetchall(
            "SELECT * FROM schedules WHERE user_id = ? ORDER BY send_time", (user_id,)
        )
        return [_schedule(r) for r in rows]

    async def get(self, user_id: int, schedule_id: int) -> ScheduleEntry | None:
        row = await self._db.fetchone(
            "SELECT * FROM schedules WHERE id = ? AND user_id = ?", (schedule_id, user_id)
        )
        return _schedule(row) if row else None

    async def add(self, user_id: int, send_time: str, weekdays_mask: int = ALL_WEEKDAYS_MASK) -> ScheduleEntry:
        try:
            async with self._db.transaction() as conn:
                cursor = await conn.execute("SELECT COUNT(*) FROM schedules WHERE user_id = ?", (user_id,))
                (count,) = await cursor.fetchone()
                if count >= MAX_SCHEDULES_PER_USER:
                    raise ScheduleLimitError
                cursor = await conn.execute(
                    "INSERT INTO schedules (user_id, send_time, enabled, weekdays_mask) VALUES (?, ?, 1, ?)",
                    (user_id, send_time, weekdays_mask),
                )
                new_id = cursor.lastrowid
        except aiosqlite.IntegrityError as exc:
            raise DuplicateTimeError from exc
        entry = await self.get(user_id, new_id)
        assert entry is not None
        return entry

    async def delete(self, user_id: int, schedule_id: int) -> bool:
        return await self._db.execute(
            "DELETE FROM schedules WHERE id = ? AND user_id = ?", (schedule_id, user_id)
        ) > 0

    async def update_time(self, user_id: int, schedule_id: int, send_time: str) -> bool:
        try:
            return await self._db.execute(
                "UPDATE schedules SET send_time = ?, last_sent_on = NULL WHERE id = ? AND user_id = ?",
                (send_time, schedule_id, user_id),
            ) > 0
        except aiosqlite.IntegrityError as exc:
            raise DuplicateTimeError from exc

    async def set_enabled(self, user_id: int, schedule_id: int, enabled: bool) -> bool:
        return await self._db.execute(
            "UPDATE schedules SET enabled = ? WHERE id = ? AND user_id = ?",
            (int(enabled), schedule_id, user_id),
        ) > 0

    async def set_weekdays_mask(self, user_id: int, schedule_id: int, mask: int) -> bool:
        if not 1 <= mask <= ALL_WEEKDAYS_MASK:
            raise ValueError("Нужно выбрать хотя бы один день недели")
        return await self._db.execute(
            "UPDATE schedules SET weekdays_mask = ? WHERE id = ? AND user_id = ?",
            (mask, schedule_id, user_id),
        ) > 0

    async def list_all_active(self) -> list[tuple[ScheduleEntry, User]]:
        """Все включённые уведомления пользователей с включёнными уведомлениями — для планировщика."""
        rows = await self._db.fetchall(
            "SELECT s.id AS s_id, s.user_id, s.send_time, s.enabled, s.weekdays_mask, s.last_sent_on, "
            "u.telegram_id, u.name, u.city, u.latitude, u.longitude, u.timezone, u.notifications_enabled "
            "FROM schedules s JOIN users u ON u.telegram_id = s.user_id "
            "WHERE s.enabled = 1 AND u.notifications_enabled = 1"
        )
        result: list[tuple[ScheduleEntry, User]] = []
        for r in rows:
            entry = ScheduleEntry(
                id=r["s_id"], user_id=r["user_id"], send_time=r["send_time"], enabled=bool(r["enabled"]),
                weekdays_mask=r["weekdays_mask"], last_sent_on=r["last_sent_on"],
            )
            result.append((entry, _user(r)))
        return result

    async def claim_send(self, schedule_id: int, local_date: str) -> bool:
        """Атомарно «занимает» отправку за дату: True — можно слать, False — уже отправлено.
        Защищает от дублей при перезапусках и гонках планировщика."""
        return await self._db.execute(
            "UPDATE schedules SET last_sent_on = ? WHERE id = ? "
            "AND (last_sent_on IS NULL OR last_sent_on <> ?)",
            (local_date, schedule_id, local_date),
        ) > 0
