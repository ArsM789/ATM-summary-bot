"""Админ-панель (/admin): рассылка, пользователи, обращения.

Доступ — только ADMIN_ID из .env. Проверка выполняется на сервере в фильтре IsAdmin, навешанном
на ВСЕ обработчики роутера (и сообщения, и callback-кнопки); критичные действия дополнительно
перепроверяют права внутри обработчика. Остальные пользователи эти хендлеры не видят вовсе."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.filters import TEXT_INPUT, IsAdmin
from bot.helpers import clip, edit_or_reply
from bot.keyboards.admin_menu import (
    admin_keyboard, back_keyboard, broadcast_confirm_keyboard, cancel_keyboard, logs_keyboard,
)
from bot.states.settings_states import AdminBroadcast
from config import Settings, is_admin
from database.repositories import EventLogRepository, FeedbackRepository, UserRepository
from services.broadcast import BroadcastService
from services.events_format import FILTERS, PAGE_SIZE, format_events_page
from services.time_service import get_zone
from utils.formatting import esc

logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.chat.type == ChatType.PRIVATE, IsAdmin())
router.callback_query.filter(IsAdmin())

MAX_BROADCAST_LEN = 3500
_background_tasks: set[asyncio.Task] = set()  # сильные ссылки, чтобы фоновые рассылки не собирались GC


def _bc_id(data: str | None) -> int | None:
    try:
        return int((data or "").rsplit(":", 1)[-1])
    except ValueError:
        return None


async def _admin_text(event_repo: EventLogRepository) -> str:
    counts = await event_repo.counts_since(int(time.time()) - 86400)
    return (
        "🛠 <b>Админ-панель</b>\n\n"
        f"За 24 часа: 🆕 новых — <b>{counts['new_users']}</b> · 🔴 ошибок — <b>{counts['errors']}</b> · "
        f"⏱ задержек — <b>{counts['delays']}</b>"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, event_repo: EventLogRepository) -> None:
    await state.clear()
    await message.answer(await _admin_text(event_repo), reply_markup=admin_keyboard())


@router.callback_query(F.data.in_({"adm:home", "adm:cancel"}))
async def admin_home(callback: CallbackQuery, state: FSMContext, event_repo: EventLogRepository) -> None:
    await state.clear()
    await callback.answer()
    await edit_or_reply(callback, await _admin_text(event_repo), reply_markup=admin_keyboard())


# ----------------------------------------------------------------- журнал событий

async def _logs_view(code: str, page: int, event_repo: EventLogRepository, users: UserRepository,
                     settings: Settings) -> tuple[str, "InlineKeyboardMarkup"]:
    categories, levels, title = FILTERS.get(code, FILTERS["all"])
    code = code if code in FILTERS else "all"
    total = await event_repo.count(categories, levels)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    records = await event_repo.list(categories, levels, PAGE_SIZE, page * PAGE_SIZE)
    admin = await users.get(settings.admin_id) if settings.admin_id else None
    try:
        tz = get_zone(admin.timezone if admin else settings.default_timezone)
    except ValueError:
        tz = get_zone("UTC")
    return clip(format_events_page(records, tz, title, total, page)), logs_keyboard(code, page, pages)


@router.message(Command("logs"))
async def cmd_logs(message: Message, state: FSMContext, event_repo: EventLogRepository,
                   users: UserRepository, settings: Settings) -> None:
    await state.clear()
    text, keyboard = await _logs_view("all", 0, event_repo, users, settings)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.regexp(r"^adm:lg:[a-z]{3}:\d{1,4}$"))
async def show_logs(callback: CallbackQuery, event_repo: EventLogRepository, users: UserRepository,
                    settings: Settings) -> None:
    _, _, code, page = (callback.data or "").split(":")
    text, keyboard = await _logs_view(code, int(page), event_repo, users, settings)
    await callback.answer()
    await edit_or_reply(callback, text, reply_markup=keyboard)


# ----------------------------------------------------------------- рассылка

@router.callback_query(F.data == "adm:bc")
async def ask_broadcast_text(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AdminBroadcast.text)
    await callback.answer()
    await edit_or_reply(
        callback,
        "📢 Напиши текст рассылки одним сообщением. Он уйдёт как обычный текст всем пользователям в личные чаты.",
        reply_markup=cancel_keyboard(),
    )


@router.message(StateFilter(AdminBroadcast.text), TEXT_INPUT)
async def got_broadcast_text(message: Message, state: FSMContext, broadcast: BroadcastService,
                             users: UserRepository, settings: Settings) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пустой. Напиши сообщение для рассылки.")
        return
    if len(text) > MAX_BROADCAST_LEN:
        await message.answer(f"Слишком длинный текст ({len(text)}). Максимум {MAX_BROADCAST_LEN} символов.")
        return
    admin_id = message.from_user.id
    recipients = len(await users.list_broadcast_targets(exclude_id=admin_id))
    broadcast_id = await broadcast.create_draft(admin_id, text)  # черновик живёт в БД, а не в памяти
    await state.clear()
    # Предпросмотр — ровно то, что получат пользователи (обычный текст).
    await message.answer("👀 <b>Предпросмотр</b> — так это увидят пользователи:")
    await message.answer(text, parse_mode=None)
    await message.answer(
        f"Получателей: <b>{recipients}</b> (без вас). Отправить?",
        reply_markup=broadcast_confirm_keyboard(broadcast_id, recipients),
    )


@router.callback_query(F.data.startswith("bc:cancel:"))
async def cancel_broadcast(callback: CallbackQuery, broadcast: BroadcastService, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings.admin_id):  # повторная серверная проверка
        await callback.answer()
        return
    broadcast_id = _bc_id(callback.data)
    cancelled = broadcast_id is not None and await broadcast.cancel(broadcast_id)
    await callback.answer("Отменено" if cancelled else "Эту рассылку уже нельзя отменить")
    await edit_or_reply(
        callback,
        "❌ Рассылка отменена. Ничего не отправлено." if cancelled else "Эта рассылка уже запущена или завершена.",
    )


@router.callback_query(F.data.startswith("bc:send:"))
async def send_broadcast(callback: CallbackQuery, broadcast: BroadcastService, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings.admin_id):  # повторная серверная проверка
        await callback.answer()
        return
    broadcast_id = _bc_id(callback.data)
    if broadcast_id is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    await callback.answer("Запускаю…")
    await edit_or_reply(callback, "📤 Рассылка запускается…")

    async def on_progress(counts: dict[str, int]) -> None:
        done = counts["sent"] + counts["failed"] + counts["blocked"]
        total = done + counts["pending"]
        await edit_or_reply(callback, f"📤 Идёт рассылка: {done} из {total}…")

    async def job() -> None:
        try:
            final = await broadcast.run(broadcast_id, on_progress)
        except Exception:  # noqa: BLE001
            logger.exception("Рассылка %s завершилась ошибкой", broadcast_id)
            await callback.bot.send_message(settings.admin_id, "⚠️ Рассылка прервалась из-за ошибки. Подробности в логе.")
            return
        if final is None:
            await edit_or_reply(callback, "Эта рассылка уже запущена, отменена или сейчас идёт другая — повторно не запускаю.")
            return
        await edit_or_reply(
            callback,
            "✅ <b>Рассылка завершена</b>\n\n"
            f"Всего получателей: <b>{final.total_count}</b>\n"
            f"Успешно отправлено: <b>{final.success_count}</b>\n"
            f"Не удалось отправить: <b>{final.failed_count}</b>\n"
            f"Заблокировали бота / недоступны: <b>{final.blocked_count}</b>",
        )

    task = asyncio.create_task(job())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ----------------------------------------------------------------- пользователи и обращения

@router.callback_query(F.data == "adm:users")
async def show_users(callback: CallbackQuery, users: UserRepository) -> None:
    stats = await users.stats()
    recent = await users.list_recent(10)
    lines = [
        "👥 <b>Пользователи</b>\n",
        f"Всего: <b>{stats['total']}</b>",
        f"С подтверждённым именем: <b>{stats['named']}</b>",
        f"С включёнными автосводками: <b>{stats['notifications']}</b>",
        f"Заблокировали бота: <b>{stats['blocked']}</b>\n",
        "<b>Последние регистрации:</b>",
    ]
    for u in recent:
        handle = f"@{esc(u.username)}" if u.username else "без username"
        lines.append(f"• {esc(u.name)} ({handle}) · <code>{u.telegram_id}</code> · {esc(u.city)}, {esc(u.timezone)}")
    await callback.answer()
    await edit_or_reply(callback, "\n".join(lines), reply_markup=back_keyboard())


@router.callback_query(F.data == "adm:fb")
async def show_feedback(callback: CallbackQuery, feedback: FeedbackRepository) -> None:
    items = await feedback.list_recent(10)
    lines = ["✉️ <b>Последние обращения</b>\n"]
    if not items:
        lines.append("Пока нет обращений.")
    status_icon = {"delivered": "✅", "new": "🆕", "delivery_failed": "⚠️"}
    for f in items:
        who = esc(f.user_name or "—") + (f" (@{esc(f.username)})" if f.username else "")
        preview = esc(f.message[:150] + ("…" if len(f.message) > 150 else ""))
        lines.append(f"{status_icon.get(f.status, '•')} <b>#{f.id}</b> {who}, <code>{f.user_id}</code>\n«{preview}»\n")
    await callback.answer()
    await edit_or_reply(callback, clip("\n".join(lines)), reply_markup=back_keyboard())
