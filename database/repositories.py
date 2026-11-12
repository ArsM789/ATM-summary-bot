"""Репозитории: весь SQL здесь. Каждый метод привязан к user_id — доступ только к своим данным."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from config import Settings
from database.database import Database
from database.models import (
    ALL_WEEKDAYS_MASK, Broadcast, EventRecord, FeedbackRecord, LessonItem, ScheduleEntry, User,
)

logger = logging.getLogger(__name__)

MAX_SCHEDULES_PER_USER = 10
MAX_LESSONS_PER_DAY = 12  # «разумный» предел суммарного числа уроков в день


class DuplicateTimeError(Exception):
    """У пользователя уже есть уведомление на это время."""


class ScheduleLimitError(Exception):
    """Достигнут лимит уведомлений на пользователя."""


class LessonsLimitError(Exception):
    """Суммарно больше MAX_LESSONS_PER_DAY уроков в день."""


def _clean_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())[:50]
    return name or "друг"


def _variants(raw: str | None) -> tuple[str, ...]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    return tuple(str(v) for v in data) if isinstance(data, list) else ()


def _user(row: aiosqlite.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        name=row["name"],
        city=row["city"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        timezone=row["timezone"],
        notifications_enabled=bool(row["notifications_enabled"]),
        username=row["username"],
        first_name=row["first_name"],
        name_variants=_variants(row["name_variants"]),
        last_used_name=row["last_used_name"],
        name_confirmed=bool(row["name_confirmed"]),
        bot_blocked=bool(row["bot_blocked"]),
    )


def _schedule(row: aiosqlite.Row, prefix: str = "") -> ScheduleEntry:
    return ScheduleEntry(
        id=row[f"{prefix}id"],
        user_id=row["user_id"],
        send_time=row["send_time"],
        enabled=bool(row["enabled"]),
        weekdays_mask=row["weekdays_mask"],
        effective_from=row["effective_from"],
    )


# ============================================================ users

class UserRepository:
    def __init__(self, db: Database, settings: Settings,
                 on_created: "Callable[[User], Awaitable[None]] | None" = None) -> None:
        self._db = db
        self._settings = settings
        self._on_created = on_created  # вызывается один раз для нового пользователя (журнал событий)

    async def get(self, user_id: int) -> User | None:
        row = await self._db.fetchone("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
        return _user(row) if row else None

    async def get_or_create(self, user_id: int, tg_first_name: str | None, username: str | None = None) -> User:
        """Находит пользователя или создаёт. Каждое обращение пользователя к боту обновляет его
        username/first_name и снимает отметку «заблокировал бота» (он явно снова пишет боту)."""
        existing = await self.get(user_id)
        if existing:
            changed = (
                (username or None) != existing.username
                or (tg_first_name and tg_first_name != existing.first_name)
                or existing.bot_blocked
            )
            if changed:
                await self._db.execute(
                    "UPDATE users SET username = ?, first_name = COALESCE(?, first_name), bot_blocked = 0 "
                    "WHERE telegram_id = ?",
                    (username or None, tg_first_name or None, user_id),
                )
                existing = await self.get(user_id)
                assert existing is not None
            return existing

        s = self._settings
        is_new = False
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO users (telegram_id, name, city, latitude, longitude, timezone, "
                "username, first_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, _clean_name(tg_first_name), s.default_city, s.default_latitude,
                 s.default_longitude, s.default_timezone, username or None, tg_first_name or None),
            )
            if cursor.rowcount == 1:  # новый пользователь (а не гонка двух запросов)
                is_new = True
                await conn.execute(
                    "INSERT OR IGNORE INTO schedules (user_id, send_time, enabled, weekdays_mask, effective_from) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (user_id, s.default_send_time, ALL_WEEKDAYS_MASK, int(time.time())),
                )
                logger.info("Новый пользователь: %s", user_id)
        created = await self.get(user_id)
        assert created is not None
        if is_new and self._on_created is not None:
            try:
                await self._on_created(created)
            except Exception:  # noqa: BLE001 — журнал не должен ломать регистрацию
                logger.exception("Сбой обработчика создания пользователя")
        return created

    async def set_names(self, user_id: int, main_name: str, variants: list[str]) -> None:
        """Сохраняет основное имя и варианты обращения; помечает имя подтверждённым."""
        await self._db.execute(
            "UPDATE users SET name = ?, name_variants = ?, last_used_name = NULL, name_confirmed = 1, "
            "updated_at = datetime('now') WHERE telegram_id = ?",
            (_clean_name(main_name), json.dumps(variants, ensure_ascii=False), user_id),
        )

    async def set_last_used_name(self, user_id: int, name: str) -> None:
        await self._db.execute("UPDATE users SET last_used_name = ? WHERE telegram_id = ?", (name, user_id))

    async def update_city(self, user_id: int, city: str, latitude: float, longitude: float) -> None:
        await self._db.execute(
            "UPDATE users SET city = ?, latitude = ?, longitude = ?, updated_at = datetime('now') "
            "WHERE telegram_id = ?",
            (city, latitude, longitude, user_id),
        )

    async def update_timezone(self, user_id: int, timezone: str) -> None:
        """Смена пояса: расписания «пересчитываются» с текущего момента, чтобы бот не отправил
        сводку задним числом из-за сдвига часов."""
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE users SET timezone = ?, updated_at = datetime('now') WHERE telegram_id = ?",
                (timezone, user_id),
            )
            await conn.execute(
                "UPDATE schedules SET effective_from = ? WHERE user_id = ?", (int(time.time()), user_id)
            )

    async def set_notifications_enabled(self, user_id: int, enabled: bool) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE users SET notifications_enabled = ?, updated_at = datetime('now') WHERE telegram_id = ?",
                (int(enabled), user_id),
            )
            if enabled:
                await conn.execute(
                    "UPDATE schedules SET effective_from = ? WHERE user_id = ?", (int(time.time()), user_id)
                )

    async def set_bot_blocked(self, user_id: int, blocked: bool) -> None:
        await self._db.execute("UPDATE users SET bot_blocked = ? WHERE telegram_id = ?", (int(blocked), user_id))

    # --- для админ-панели ---

    async def stats(self) -> dict[str, int]:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(bot_blocked), 0) AS blocked, "
            "COALESCE(SUM(notifications_enabled), 0) AS notif, "
            "COALESCE(SUM(name_confirmed), 0) AS named FROM users"
        )
        return {"total": row["total"], "blocked": row["blocked"], "notifications": row["notif"], "named": row["named"]}

    async def list_recent(self, limit: int = 10) -> list[User]:
        rows = await self._db.fetchall("SELECT * FROM users ORDER BY created_at DESC, telegram_id DESC LIMIT ?", (limit,))
        return [_user(r) for r in rows]

    async def list_broadcast_targets(self, exclude_id: int | None) -> list[tuple[int, bool]]:
        """(user_id, bot_blocked) всех пользователей, кроме exclude_id."""
        rows = await self._db.fetchall(
            "SELECT telegram_id, bot_blocked FROM users WHERE telegram_id <> ? ORDER BY telegram_id",
            (exclude_id if exclude_id is not None else -1,),
        )
        return [(r["telegram_id"], bool(r["bot_blocked"])) for r in rows]


# ============================================================ расписание уроков

class WeeklyScheduleRepository:
    """Уроки по предметам и дням недели (таблица weekly_schedule). Порядок — столбец position."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _items(rows: list[aiosqlite.Row]) -> list[LessonItem]:
        return [LessonItem(r["subject_name"], r["lesson_count"]) for r in rows]

    async def get_day(self, user_id: int, weekday: int) -> list[LessonItem]:
        rows = await self._db.fetchall(
            "SELECT subject_name, lesson_count FROM weekly_schedule "
            "WHERE user_id = ? AND weekday = ? ORDER BY position",
            (user_id, weekday),
        )
        return self._items(rows)

    async def get_week(self, user_id: int) -> dict[int, list[LessonItem]]:
        rows = await self._db.fetchall(
            "SELECT weekday, subject_name, lesson_count FROM weekly_schedule "
            "WHERE user_id = ? ORDER BY weekday, position",
            (user_id,),
        )
        week: dict[int, list[LessonItem]] = {}
        for r in rows:
            week.setdefault(r["weekday"], []).append(LessonItem(r["subject_name"], r["lesson_count"]))
        return week

    async def replace_day(self, user_id: int, weekday: int, items: list[LessonItem]) -> None:
        """Полная замена одного дня. Остальные дни и пользователи не затрагиваются."""
        self._validate(items)
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM weekly_schedule WHERE user_id = ? AND weekday = ?", (user_id, weekday))
            await conn.executemany(
                "INSERT INTO weekly_schedule (user_id, weekday, position, subject_name, lesson_count) "
                "VALUES (?, ?, ?, ?, ?)",
                [(user_id, weekday, i, it.subject, it.count) for i, it in enumerate(items, start=1)],
            )

    async def append_to_day(self, user_id: int, weekday: int, items: list[LessonItem]) -> list[LessonItem]:
        """Добавляет предметы в конец дня. Возвращает итоговый список дня."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT subject_name, lesson_count FROM weekly_schedule "
                "WHERE user_id = ? AND weekday = ? ORDER BY position",
                (user_id, weekday),
            )
            current = self._items(list(await cursor.fetchall()))
            combined = current + list(items)
            self._validate(combined)
            await conn.executemany(
                "INSERT INTO weekly_schedule (user_id, weekday, position, subject_name, lesson_count) "
                "VALUES (?, ?, ?, ?, ?)",
                [(user_id, weekday, len(current) + i, it.subject, it.count) for i, it in enumerate(items, start=1)],
            )
        return combined

    async def clear_day(self, user_id: int, weekday: int) -> int:
        return await self._db.execute(
            "DELETE FROM weekly_schedule WHERE user_id = ? AND weekday = ?", (user_id, weekday)
        )

    @staticmethod
    def _validate(items: list[LessonItem]) -> None:
        if sum(i.count for i in items) > MAX_LESSONS_PER_DAY:
            raise LessonsLimitError
        if any(not i.subject.strip() or not 1 <= i.count <= MAX_LESSONS_PER_DAY for i in items):
            raise ValueError("Некорректный предмет или количество уроков")


# ============================================================ время автосводок

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
                    "INSERT INTO schedules (user_id, send_time, enabled, weekdays_mask, effective_from) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (user_id, send_time, weekdays_mask, int(time.time())),
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
                "UPDATE schedules SET send_time = ?, effective_from = ? WHERE id = ? AND user_id = ?",
                (send_time, int(time.time()), schedule_id, user_id),
            ) > 0
        except aiosqlite.IntegrityError as exc:
            raise DuplicateTimeError from exc

    async def set_enabled(self, user_id: int, schedule_id: int, enabled: bool) -> bool:
        return await self._db.execute(
            "UPDATE schedules SET enabled = ?, effective_from = CASE WHEN ? = 1 THEN ? ELSE effective_from END "
            "WHERE id = ? AND user_id = ?",
            (int(enabled), int(enabled), int(time.time()), schedule_id, user_id),
        ) > 0

    async def list_all_active(self) -> list[tuple[ScheduleEntry, User]]:
        """Включённые уведомления пользователей с включёнными уведомлениями, кроме заблокировавших бота."""
        rows = await self._db.fetchall(
            "SELECT s.id AS s_id, s.user_id, s.send_time, s.enabled, s.weekdays_mask, s.effective_from, u.* "
            "FROM schedules s JOIN users u ON u.telegram_id = s.user_id "
            "WHERE s.enabled = 1 AND u.notifications_enabled = 1 AND u.bot_blocked = 0"
        )
        return [(_schedule(r, prefix="s_"), _user(r)) for r in rows]


# ============================================================ журнал уведомлений (дедупликация)

@dataclass(frozen=True, slots=True)
class Claim:
    log_id: int
    attempt: int  # номер попытки: 1, 2, 3…


class NotificationLogRepository:
    """Одна запись на (пользователь, тип, слот-время, локальная дата). Гарантия «не больше одной
    успешной отправки» держится на UNIQUE-ограничении и атомарных UPDATE."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def claim(
        self, user_id: int, ntype: str, slot: str, local_date: str, *,
        now_ts: int | None = None, max_attempts: int = 3, retry_gap: int = 180, stale_after: int = 600,
    ) -> Claim | None:
        """Пытается «занять» отправку. None — слать не нужно (уже отправлено/заблокировано,
        идёт сейчас, исчерпаны попытки или рано для повтора)."""
        now = int(time.time()) if now_ts is None else now_ts
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO notification_log "
                "(user_id, notification_type, slot, notification_date, status, attempts, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, 'sending', 1, ?, ?)",
                (user_id, ntype, slot, local_date, now, now),
            )
            if cursor.rowcount == 1:
                return Claim(cursor.lastrowid, 1)

            cursor = await conn.execute(
                "SELECT id, status, attempts, updated_ts FROM notification_log "
                "WHERE user_id = ? AND notification_type = ? AND slot = ? AND notification_date = ?",
                (user_id, ntype, slot, local_date),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            status, attempts, updated = row["status"], row["attempts"], row["updated_ts"]
            retryable = (
                (status == "failed" and now - updated >= retry_gap)
                or (status == "sending" and now - updated >= stale_after)  # процесс упал посреди отправки
            )
            if not retryable or attempts >= max_attempts:
                return None
            await conn.execute(
                "UPDATE notification_log SET status = 'sending', attempts = attempts + 1, updated_ts = ? "
                "WHERE id = ?",
                (now, row["id"]),
            )
            return Claim(row["id"], attempts + 1)

    async def finish(self, log_id: int, status: str, error: str | None = None, *, now_ts: int | None = None) -> None:
        if status not in ("sent", "failed", "blocked"):
            raise ValueError(status)
        now = int(time.time()) if now_ts is None else now_ts
        await self._db.execute(
            "UPDATE notification_log SET status = ?, error = ?, updated_ts = ?, "
            "sent_at = CASE WHEN ? = 'sent' THEN datetime('now') ELSE sent_at END WHERE id = ?",
            (status, error, now, status, log_id),
        )

    async def get(self, user_id: int, ntype: str, slot: str, local_date: str) -> aiosqlite.Row | None:
        return await self._db.fetchone(
            "SELECT * FROM notification_log WHERE user_id = ? AND notification_type = ? AND slot = ? "
            "AND notification_date = ?",
            (user_id, ntype, slot, local_date),
        )


# ============================================================ обратная связь

class FeedbackRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, user_id: int, message: str) -> int:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO feedback (user_id, message, created_ts) VALUES (?, ?, ?)",
                (user_id, message, int(time.time())),
            )
            return cursor.lastrowid

    async def set_status(self, feedback_id: int, status: str) -> None:
        await self._db.execute("UPDATE feedback SET status = ? WHERE id = ?", (status, feedback_id))

    async def count_since(self, user_id: int, since_ts: int) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM feedback WHERE user_id = ? AND created_ts >= ?", (user_id, since_ts)
        )
        return row["n"]

    async def list_recent(self, limit: int = 10) -> list[FeedbackRecord]:
        rows = await self._db.fetchall(
            "SELECT f.id, f.user_id, f.message, f.status, f.created_at, u.name, u.username "
            "FROM feedback f LEFT JOIN users u ON u.telegram_id = f.user_id ORDER BY f.id DESC LIMIT ?",
            (limit,),
        )
        return [
            FeedbackRecord(r["id"], r["user_id"], r["message"], r["status"], r["created_at"], r["name"], r["username"])
            for r in rows
        ]


# ============================================================ рассылки

def _broadcast(row: aiosqlite.Row) -> Broadcast:
    return Broadcast(
        id=row["id"], admin_id=row["admin_id"], message=row["message"], status=row["status"],
        total_count=row["total_count"], success_count=row["success_count"],
        failed_count=row["failed_count"], blocked_count=row["blocked_count"],
    )


class BroadcastRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_draft(self, admin_id: int, message: str) -> int:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO broadcasts (admin_id, message) VALUES (?, ?)", (admin_id, message)
            )
            return cursor.lastrowid

    async def get(self, broadcast_id: int) -> Broadcast | None:
        row = await self._db.fetchone("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,))
        return _broadcast(row) if row else None

    async def cancel(self, broadcast_id: int) -> bool:
        """Отмена возможна только до запуска (draft). Идемпотентна."""
        return await self._db.execute(
            "UPDATE broadcasts SET status = 'cancelled' WHERE id = ? AND status = 'draft'", (broadcast_id,)
        ) > 0

    async def claim_start(self, broadcast_id: int, targets: list[tuple[int, bool]]) -> bool:
        """Атомарно: draft -> running и запись получателей. False, если рассылка уже запущена/отменена
        (повторный callback) или другая рассылка ещё идёт."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE broadcasts SET status = 'running', total_count = ? WHERE id = ? AND status = 'draft' "
                "AND NOT EXISTS (SELECT 1 FROM broadcasts WHERE status = 'running')",
                (len(targets), broadcast_id),
            )
            if cursor.rowcount != 1:
                return False
            await conn.executemany(
                "INSERT OR IGNORE INTO broadcast_deliveries (broadcast_id, user_id, status, error) VALUES (?, ?, ?, ?)",
                [
                    (broadcast_id, uid, "blocked" if blocked else "pending",
                     "bot_blocked (известно заранее)" if blocked else None)
                    for uid, blocked in targets
                ],
            )
            return True

    async def next_pending(self, broadcast_id: int, limit: int = 50) -> list[int]:
        rows = await self._db.fetchall(
            "SELECT user_id FROM broadcast_deliveries WHERE broadcast_id = ? AND status = 'pending' "
            "ORDER BY user_id LIMIT ?",
            (broadcast_id, limit),
        )
        return [r["user_id"] for r in rows]

    async def mark_delivery(self, broadcast_id: int, user_id: int, status: str, error: str | None = None) -> None:
        if status not in ("sent", "failed", "blocked"):
            raise ValueError(status)
        await self._db.execute(
            "UPDATE broadcast_deliveries SET status = ?, error = ?, "
            "sent_at = CASE WHEN ? = 'sent' THEN datetime('now') ELSE NULL END "
            "WHERE broadcast_id = ? AND user_id = ? AND status = 'pending'",
            (status, error, status, broadcast_id, user_id),
        )

    async def counts(self, broadcast_id: int) -> dict[str, int]:
        rows = await self._db.fetchall(
            "SELECT status, COUNT(*) AS n FROM broadcast_deliveries WHERE broadcast_id = ? GROUP BY status",
            (broadcast_id,),
        )
        result = {"pending": 0, "sent": 0, "failed": 0, "blocked": 0}
        result.update({r["status"]: r["n"] for r in rows})
        return result

    async def finish(self, broadcast_id: int, status: str = "done") -> Broadcast:
        counts = await self.counts(broadcast_id)
        await self._db.execute(
            "UPDATE broadcasts SET status = ?, success_count = ?, failed_count = ?, blocked_count = ? WHERE id = ?",
            (status, counts["sent"], counts["failed"], counts["blocked"], broadcast_id),
        )
        result = await self.get(broadcast_id)
        assert result is not None
        return result

    async def mark_interrupted(self) -> int:
        """При старте процесса: рассылки, оборванные перезапуском, больше не «идут»."""
        return await self._db.execute("UPDATE broadcasts SET status = 'interrupted' WHERE status = 'running'")


# ============================================================ журнал событий (для админа)

class EventLogRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, level: str, category: str, message: str, user_id: int | None = None,
                  ts: int | None = None) -> int:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO event_log (ts, level, category, message, user_id) VALUES (?, ?, ?, ?, ?)",
                (int(time.time()) if ts is None else ts, level, category, message[:500], user_id),
            )
            return cursor.lastrowid

    @staticmethod
    def _where(categories: tuple[str, ...] | None, levels: tuple[str, ...] | None) -> tuple[str, list]:
        clauses, params = [], []
        if categories:
            clauses.append(f"category IN ({','.join('?' * len(categories))})")
            params.extend(categories)
        if levels:
            clauses.append(f"level IN ({','.join('?' * len(levels))})")
            params.extend(levels)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    async def list(self, categories: tuple[str, ...] | None = None, levels: tuple[str, ...] | None = None,
                   limit: int = 10, offset: int = 0) -> list[EventRecord]:
        where, params = self._where(categories, levels)
        rows = await self._db.fetchall(
            f"SELECT * FROM event_log{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", (*params, limit, offset)
        )
        return [EventRecord(r["id"], r["ts"], r["level"], r["category"], r["message"], r["user_id"]) for r in rows]

    async def count(self, categories: tuple[str, ...] | None = None, levels: tuple[str, ...] | None = None) -> int:
        where, params = self._where(categories, levels)
        row = await self._db.fetchone(f"SELECT COUNT(*) AS n FROM event_log{where}", params)
        return row["n"]

    async def counts_since(self, since_ts: int) -> dict[str, int]:
        """Сводка за период для шапки админ-панели: новые пользователи, ошибки, задержки."""
        row = await self._db.fetchone(
            "SELECT "
            "COALESCE(SUM(category = 'user_new'), 0) AS new_users, "
            "COALESCE(SUM(level = 'error'), 0) AS errors, "
            "COALESCE(SUM(category IN ('delay', 'weather')), 0) AS delays "
            "FROM event_log WHERE ts >= ?",
            (since_ts,),
        )
        return {"new_users": row["new_users"], "errors": row["errors"], "delays": row["delays"]}

    async def prune(self, keep_last: int = 2000, older_than_ts: int | None = None) -> int:
        """Оставляет не больше keep_last последних записей (и/или удаляет старше older_than_ts)."""
        removed = await self._db.execute(
            "DELETE FROM event_log WHERE id <= (SELECT COALESCE(MAX(id), 0) FROM event_log) - ?", (keep_last,)
        )
        if older_than_ts is not None:
            removed += await self._db.execute("DELETE FROM event_log WHERE ts < ?", (older_than_ts,))
        return removed
