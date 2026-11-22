"""Админ-панель (/admin): рассылка, пользователи, обращения.

Доступ — только ADMIN_ID из .env. Проверка выполняется на сервере в фильтре IsAdmin, навешанном
на ВСЕ обработчики роутера (и сообщения, и callback-кнопки); критичные действия дополнительно
перепроверяют права внутри обработчика. Остальные пользователи эти хендлеры не видят вовсе."""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


from bot.filters import TEXT_INPUT, IsAdmin
from bot.helpers import clip, edit_or_reply
from bot.keyboards.admin_menu import (
    admin_keyboard, back_keyboard, broadcast_confirm_keyboard, broadcast_input_keyboard, broadcast_preview_keyboard,
    cancel_keyboard, logs_keyboard,
)
from bot.texts import DOWNTIME_NOTICE
from bot.ui import ui
from bot.states.settings_states import AdminBroadcast
from config import Settings, is_admin
from database.repositories import EventLogRepository, FeedbackRepository, UserRepository
from services.broadcast import BroadcastService
from database.models import Broadcast
from services.events_format import FILTERS, PAGE_SIZE, format_events_page
from services.time_service import get_zone
from utils.formatting import esc

logger = logging.getLogger(__name__)

router = Router(name="admin")


router.message.filter(F.chat.type == ChatType.PRIVATE, IsAdmin())
router.callback_query.filter(IsAdmin())

MAX_BROADCAST_LEN = 3500
_background_tasks: set[asyncio.Task] = set()  # сильные ссылки, чтобы фоновые рассылки не собирались GC



@router.message(Command("ban"))
async def cmd_ban(
    message: Message,
    command: CommandObject,
    users: UserRepository,
    settings: Settings,
) -> None:
    args = (command.args or "").strip().split(maxsplit=1)

    if not args or not args[0].isdigit():
        await message.answer(
            "Использование: <code>/ban ID [причина]</code>\n"
            "Пример: <code>/ban 123456789 Спам</code>"
        )
        return

    user_id = int(args[0])
    reason = args[1].strip() if len(args) > 1 else None

    if user_id <= 0:
        await message.answer("Telegram ID должен быть положительным числом.")
        return

    if user_id == settings.admin_id:
        await message.answer("Нельзя заблокировать администратора.")
        return

    await users.ban(user_id, message.from_user.id, reason)

    text = (
        f"Пользователь <code>{user_id}</code> заблокирован."
        + (f"\nПричина: {esc(reason)}" if reason else "")
    )
    await message.answer(text)


@router.message(Command("unban"))
async def cmd_unban(
    message: Message,
    command: CommandObject,
    users: UserRepository,
) -> None:
    args = (command.args or "").strip().split()

    if not args or not args[0].isdigit():
        await message.answer(
            "Использование: <code>/unban ID</code>\n"
            "Пример: <code>/unban 123456789</code>"
        )
        return

    user_id = int(args[0])

    if user_id <= 0:
        await message.answer("Telegram ID должен быть положительным числом.")
        return

    removed = await users.unban(user_id)

    if removed:
        await message.answer(f"Пользователь <code>{user_id}</code> разблокирован.")
    else:
        await message.answer(f"Пользователь <code>{user_id}</code> не был заблокирован.")


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
    await _drop_preview(message.bot, message.chat.id, state)
    await state.clear()
    await ui.show(message, await _admin_text(event_repo), admin_keyboard(), fresh=True)


@router.callback_query(F.data.in_({"adm:home", "adm:cancel"}))
async def admin_home(callback: CallbackQuery, state: FSMContext, event_repo: EventLogRepository) -> None:
    await _drop_preview(callback.bot, callback.from_user.id, state)   # временный предпросмотр рассылки больше не нужен
    await state.clear()
    await callback.answer()
    await ui.show(callback, await _admin_text(event_repo), admin_keyboard())


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
    await ui.show(message, text, keyboard, fresh=True)


@router.callback_query(F.data.regexp(r"^adm:lg:[a-z]{3}:\d{1,4}$"))
async def show_logs(callback: CallbackQuery, event_repo: EventLogRepository, users: UserRepository,
                    settings: Settings) -> None:
    _, _, code, page = (callback.data or "").split(":")
    text, keyboard = await _logs_view(code, int(page), event_repo, users, settings)
    await callback.answer()
    await edit_or_reply(callback, text, reply_markup=keyboard)


# ----------------------------------------------------------------- рассылка

MAX_TEXT_LEN = 3500       # текст сообщения (лимит Telegram 4096, запас на разметку)
MAX_CAPTION_LEN = 1000    # подпись к картинке (лимит Telegram 1024)

BC_PROMPT = (
    "📢 <b>Новая рассылка</b>\n\n"
    "Пришли сообщение рассылки. Форматирование (жирный, курсив, ссылки) сохранится.\n"
    "Картинка — по желанию: пришли фото с подписью (до 1000 символов) или добавишь её на следующем шаге.\n\n"
    "Перед отправкой будет предпросмотр."
)


def _plain_len(html_text: str) -> int:
    """Длина текста без HTML-тегов (для лимита подписи)."""
    return len(html_lib.unescape(re.sub(r"<[^>]+>", "", html_text)))


def _extract(message: Message) -> tuple[str, str | None, str | None]:
    """(HTML-текст, file_id картинки, unique_id картинки) из сообщения администратора."""
    raw = message.text or message.caption or ""
    text = message.html_text.strip() if raw else ""
    if message.photo:
        photo = message.photo[-1]
        return text, photo.file_id, photo.file_unique_id
    return text, None, None


def _bc_id(data: str | None) -> int | None:
    try:
        return int((data or "").rsplit(":", 1)[-1])
    except ValueError:
        return None


async def _owned_draft(broadcast: BroadcastService, bc_id: int | None, admin_id: int) -> Broadcast | None:
    """Рассылка существует и принадлежит этому администратору (проверка на сервере в каждом действии)."""
    if bc_id is None:
        return None
    bc = await broadcast.get(bc_id)
    return bc if bc is not None and bc.admin_id == admin_id else None


async def _drop_preview(bot, chat_id: int, state: FSMContext) -> None:
    """Удаляет сообщение-предпросмотр (временное), если оно есть."""
    data = await state.get_data()
    await ui.delete(bot, chat_id, data.get("preview_id"))
    await state.update_data(preview_id=None)


async def _show_control(event: Message | CallbackQuery, state: FSMContext, broadcast: BroadcastService,
                        bc: Broadcast, notice: str = "") -> None:
    """Предпросмотр (ровно то, что получат пользователи) + экран управления. Прошлый предпросмотр удаляется."""
    admin_id = event.from_user.id
    bot = event.bot
    chat_id = event.chat.id if isinstance(event, Message) else event.from_user.id
    await _drop_preview(bot, chat_id, state)

    preview_id, problem = None, None
    try:
        if bc.photo_file_id:
            sent = await bot.send_photo(chat_id, bc.photo_file_id, caption=bc.message or None)
        else:
            sent = await bot.send_message(chat_id, bc.message or "(пусто)")
        preview_id = sent.message_id
    except TelegramAPIError as exc:   # например, Telegram не принял разметку/картинку — лучше узнать до отправки
        problem = f"Telegram не принял это сообщение: {exc}"
    await state.set_state(AdminBroadcast.preview)
    await state.update_data(bc_id=bc.id, preview_id=preview_id)

    recipients = await broadcast.recipients_count(admin_id)
    title = "🛠 <b>Уведомление о недоступности</b>" if bc.kind == "downtime" else "📢 <b>Рассылка</b>"
    lines = [f"{notice}\n" if notice else "", title + " — предпросмотр выше",
             f"Получателей: <b>{recipients}</b> (без вас) · картинка: <b>{'есть' if bc.photo_file_id else 'нет'}</b>"]
    if problem:
        lines.append(f"\n⚠️ {esc(problem)}")
    await ui.show(event, "\n".join(line for line in lines if line is not None).strip(),
                  broadcast_preview_keyboard(bc.id, recipients, bool(bc.photo_file_id)))


@router.callback_query(F.data == "adm:bc")
async def ask_broadcast_content(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AdminBroadcast.text)
    await callback.answer()
    await ui.show(callback, BC_PROMPT, cancel_keyboard())


@router.message(StateFilter(AdminBroadcast.text), F.photo | TEXT_INPUT)
async def got_broadcast_content(message: Message, state: FSMContext, broadcast: BroadcastService) -> None:
    text, file_id, unique_id = _extract(message)
    if not text and not file_id:
        await ui.show(message, "⚠️ Пустое сообщение. " + BC_PROMPT, cancel_keyboard())
        return
    limit = MAX_CAPTION_LEN if file_id else MAX_TEXT_LEN
    if _plain_len(text) > limit:
        await ui.show(message, f"⚠️ Слишком длинный текст ({_plain_len(text)}). Максимум — {limit} символов"
                      f"{' для подписи к картинке' if file_id else ''}.\n\n" + BC_PROMPT, cancel_keyboard())
        return
    bc_id = await broadcast.create_draft(message.from_user.id, text, photo_file_id=file_id, photo_unique_id=unique_id)
    bc = await broadcast.get(bc_id)
    await _show_control(message, state, broadcast, bc)


@router.message(StateFilter(AdminBroadcast.text, AdminBroadcast.edit, AdminBroadcast.photo), ~F.text, ~F.photo)
async def broadcast_unsupported(message: Message) -> None:
    await ui.show(message, "⚠️ Поддерживается текст и фото. Пришли сообщение ещё раз.", cancel_keyboard())


@router.callback_query(F.data.regexp(r"^bc:txt:\d+$"))
async def ask_new_text(callback: CallbackQuery, state: FSMContext, broadcast: BroadcastService) -> None:
    bc = await _owned_draft(broadcast, _bc_id(callback.data), callback.from_user.id)
    if bc is None or bc.status != "draft":
        await callback.answer("Эта рассылка уже недоступна", show_alert=True)
        return
    await state.set_state(AdminBroadcast.edit)
    await state.update_data(bc_id=bc.id)
    await callback.answer()
    limit = MAX_CAPTION_LEN if bc.photo_file_id else MAX_TEXT_LEN
    await ui.show(callback, f"✏️ Пришли новый текст рассылки (до {limit} символов). Форматирование сохранится.",
                  broadcast_input_keyboard(bc.id))


@router.message(StateFilter(AdminBroadcast.edit), TEXT_INPUT)
async def got_new_text(message: Message, state: FSMContext, broadcast: BroadcastService) -> None:
    data = await state.get_data()
    bc = await _owned_draft(broadcast, data.get("bc_id"), message.from_user.id)
    if bc is None or bc.status != "draft":
        await state.clear()
        await ui.show(message, "Эта рассылка уже недоступна.", back_keyboard())
        return
    text, _, _ = _extract(message)
    limit = MAX_CAPTION_LEN if bc.photo_file_id else MAX_TEXT_LEN
    if not text or _plain_len(text) > limit:
        await ui.show(message, f"⚠️ Текст пустой или длиннее {limit} символов. Пришли ещё раз.", broadcast_input_keyboard(bc.id))
        return
    updated = await broadcast.edit_draft(bc.id, text=text)
    await _show_control(message, state, broadcast, updated or bc, notice="✅ Текст обновлён.")


@router.callback_query(F.data.regexp(r"^bc:img:\d+$"))
async def ask_photo(callback: CallbackQuery, state: FSMContext, broadcast: BroadcastService) -> None:
    bc = await _owned_draft(broadcast, _bc_id(callback.data), callback.from_user.id)
    if bc is None or bc.status != "draft":
        await callback.answer("Эта рассылка уже недоступна", show_alert=True)
        return
    await state.set_state(AdminBroadcast.photo)
    await state.update_data(bc_id=bc.id)
    await callback.answer()
    await ui.show(callback, "🖼 Пришли картинку (как фото). Текст рассылки станет её подписью.",
                  broadcast_input_keyboard(bc.id, bool(bc.photo_file_id)))


@router.message(StateFilter(AdminBroadcast.photo), F.photo)
async def got_photo(message: Message, state: FSMContext, broadcast: BroadcastService) -> None:
    data = await state.get_data()
    bc = await _owned_draft(broadcast, data.get("bc_id"), message.from_user.id)
    if bc is None or bc.status != "draft":
        await state.clear()
        await ui.show(message, "Эта рассылка уже недоступна.", back_keyboard())
        return
    if _plain_len(bc.message) > MAX_CAPTION_LEN:
        await ui.show(message, f"⚠️ Текст длиннее {MAX_CAPTION_LEN} символов — подпись к картинке не может быть такой длинной. "
                      "Сначала сократи текст (✏️ Текст).", broadcast_input_keyboard(bc.id, bool(bc.photo_file_id)))
        return
    photo = message.photo[-1]
    updated = await broadcast.edit_draft(bc.id, photo_file_id=photo.file_id, photo_unique_id=photo.file_unique_id)
    await _show_control(message, state, broadcast, updated or bc, notice="✅ Картинка добавлена.")


@router.callback_query(F.data.regexp(r"^bc:rmimg:\d+$"))
async def remove_photo(callback: CallbackQuery, state: FSMContext, broadcast: BroadcastService) -> None:
    bc = await _owned_draft(broadcast, _bc_id(callback.data), callback.from_user.id)
    if bc is None or bc.status != "draft":
        await callback.answer("Эта рассылка уже недоступна", show_alert=True)
        return
    updated = await broadcast.edit_draft(bc.id, remove_photo=True)
    await callback.answer("Картинка убрана")
    await _show_control(callback, state, broadcast, updated or bc, notice="🗑 Картинка убрана.")


@router.callback_query(F.data.regexp(r"^bc:back:\d+$"))
async def back_to_preview(callback: CallbackQuery, state: FSMContext, broadcast: BroadcastService) -> None:
    bc = await _owned_draft(broadcast, _bc_id(callback.data), callback.from_user.id)
    if bc is None or bc.status != "draft":
        await callback.answer("Эта рассылка уже недоступна", show_alert=True)
        return
    await callback.answer()
    await _show_control(callback, state, broadcast, bc)


@router.callback_query(F.data == "adm:down")
async def downtime_draft(callback: CallbackQuery, state: FSMContext, broadcast: BroadcastService) -> None:
    """Отдельная кнопка: заранее подготовленное уведомление о временной недоступности (с тем же предпросмотром и подтверждением)."""
    await state.clear()
    bc_id = await broadcast.create_draft(callback.from_user.id, DOWNTIME_NOTICE, kind="downtime")
    await callback.answer()
    await _show_control(callback, state, broadcast, await broadcast.get(bc_id))


@router.callback_query(F.data.regexp(r"^bc:ask:\d+$"))
async def ask_confirmation(callback: CallbackQuery, broadcast: BroadcastService, settings: Settings) -> None:
    """Первое нажатие «Отправить» — только подтверждение (защита от случайного запуска)."""
    if not is_admin(callback.from_user.id, settings.admin_id):
        await callback.answer()
        return
    bc = await _owned_draft(broadcast, _bc_id(callback.data), callback.from_user.id)
    if bc is None or bc.status != "draft":
        await callback.answer("Эта рассылка уже запущена, отменена или недоступна", show_alert=True)
        return
    recipients = await broadcast.recipients_count(callback.from_user.id)
    duplicate = await broadcast.recent_duplicate(bc)
    text = (f"🚀 Отправить рассылку <b>{recipients}</b> пользователям?\nОтменить после запуска нельзя.")
    if duplicate is not None:
        text += ("\n\n⚠️ <b>Такая же рассылка уже отправлялась недавно</b> "
                 f"(#{duplicate.id}). Повторная отправка — ещё одно такое же сообщение каждому.")
    await callback.answer()
    await ui.show(callback, text, broadcast_confirm_keyboard(bc.id, recipients, duplicate is not None))


@router.callback_query(F.data.regexp(r"^bc:cancel:\d+$"))
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext, broadcast: BroadcastService,
                           settings: Settings, event_repo: EventLogRepository) -> None:
    if not is_admin(callback.from_user.id, settings.admin_id):  # повторная серверная проверка
        await callback.answer()
        return
    bc = await _owned_draft(broadcast, _bc_id(callback.data), callback.from_user.id)
    cancelled = bc is not None and await broadcast.cancel(bc.id)
    await _drop_preview(callback.bot, callback.from_user.id, state)
    await state.clear()
    await callback.answer("Отменено" if cancelled else "Эту рассылку уже нельзя отменить")
    notice = "❌ Рассылка отменена. Ничего не отправлено." if cancelled else "Эта рассылка уже запущена или завершена."
    await ui.show(callback, f"{notice}\n\n" + await _admin_text(event_repo), admin_keyboard())


@router.callback_query(F.data.regexp(r"^bc:send:\d+$"))
async def send_broadcast(callback: CallbackQuery, state: FSMContext, broadcast: BroadcastService,
                         settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings.admin_id):  # повторная серверная проверка
        await callback.answer()
        return
    bc = await _owned_draft(broadcast, _bc_id(callback.data), callback.from_user.id)
    if bc is None:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return
    broadcast_id = bc.id
    await callback.answer("Запускаю…")
    await _drop_preview(callback.bot, callback.from_user.id, state)
    await state.clear()
    await ui.show(callback, "📤 Рассылка запускается…", None)   # кнопки убраны: повторное нажатие невозможно

    async def on_progress(counts: dict[str, int]) -> None:
        done = counts["sent"] + counts["failed"] + counts["blocked"]
        total = done + counts["pending"]
        await ui.show(callback, f"📤 Идёт рассылка: {done} из {total}…", None)

    async def job() -> None:
        try:
            final = await broadcast.run(broadcast_id, on_progress)
        except Exception:  # noqa: BLE001
            logger.exception("Рассылка %s завершилась ошибкой", broadcast_id)
            await callback.bot.send_message(settings.admin_id, "⚠️ Рассылка прервалась из-за ошибки. Подробности в журнале.")
            return
        if final is None:
            await ui.show(callback, "Эта рассылка уже запущена, отменена или сейчас идёт другая — повторно не запускаю.",
                          back_keyboard())
            return
        await ui.show(
            callback,
            "✅ <b>Рассылка завершена</b>\n\n"
            f"Всего получателей: <b>{final.total_count}</b>\n"
            f"Успешно отправлено: <b>{final.success_count}</b>\n"
            f"Не удалось отправить: <b>{final.failed_count}</b>\n"
            f"Заблокировали бота / недоступны: <b>{final.blocked_count}</b>",
            back_keyboard(),
        )

    task = asyncio.create_task(job())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# сообщение администратора, пока открыт предпросмотр: убираем, чтобы не засорять чат
@router.message(StateFilter(AdminBroadcast.preview))
async def preview_stray_message(message: Message) -> None:
    await ui.delete(message.bot, message.chat.id, message.message_id)


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
