"""Знакомство: имя и варианты обращения, затем тур по возможностям. Для новых пользователей и для старых без
подтверждённого имени. Тот же диалог открывается из «⚙️ Настройки → Имя и обращения» (edit=True).
Весь диалог живёт в одном сообщении: ввод пользователя удаляется, ошибки показываются прямо в экране."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.filters import TEXT_INPUT, NeedsOnboarding
from bot.helpers import reply
from bot.keyboards.common import button, markup
from bot.keyboards.nav import tour_keyboard
from bot.states.settings_states import Onboarding
from bot.texts import ASK_NAME, ASK_NAME_EDIT, ASK_VARIANTS, TOUR_PAGES
from database.models import User
from database.repositories import UserRepository
from services.names import InvalidNameError, parse_single_name, parse_variants
from utils.formatting import esc, join_ru

router = Router(name="onboarding")
router.message.filter(F.chat.type == ChatType.PRIVATE)


def _with_error(text: str, error: str | None) -> str:
    return f"⚠️ {esc(error)}\n\n{text}" if error else text


async def _name_screen(event: Message | CallbackQuery, user_name: str, edit: bool, *, error: str | None = None,
                       fresh: bool = False) -> None:
    rows = [[button(f"Оставить «{user_name[:30]}»", "onb:keep")]]
    if edit:
        rows.append([button("❌ Отмена", "st:home")])
    await reply(event, _with_error(ASK_NAME_EDIT if edit else ASK_NAME, error), reply_markup=markup(*rows), fresh=fresh)


async def _variants_screen(event: Message | CallbackQuery, main_name: str, edit: bool, *, error: str | None = None) -> None:
    rows = [[button(f"Только «{main_name}»", "onb:only")]]
    if edit:
        rows.append([button("❌ Отмена", "st:home")])
    await reply(event, _with_error(ASK_VARIANTS, error), reply_markup=markup(*rows))


async def begin_onboarding(event: Message | CallbackQuery, user: User, state: FSMContext, *, edit: bool = False) -> None:
    await state.clear()
    await state.set_state(Onboarding.name)
    await state.update_data(edit=edit)
    keep_name = user.name if edit else (user.first_name or user.name)
    await _name_screen(event, keep_name, edit, fresh=isinstance(event, Message))


async def _ask_variants(event: Message | CallbackQuery, state: FSMContext, main_name: str, *, error: str | None = None) -> None:
    await state.update_data(main_name=main_name)
    await state.set_state(Onboarding.variants)
    edit = bool((await state.get_data()).get("edit"))
    await _variants_screen(event, main_name, edit, error=error)


async def _finish(event: Message | CallbackQuery, state: FSMContext, users: UserRepository,
                  user_id: int, main_name: str, variants: list[str]) -> None:
    edit = bool((await state.get_data()).get("edit"))
    await users.set_names(user_id, main_name, variants)
    await state.clear()
    addressing = f"Буду обращаться к тебе так: <b>{esc(join_ru(variants))}</b>."
    if edit:
        await reply(
            event, f"✅ Готово, {esc(main_name)}! {addressing}",
            reply_markup=markup([button("‹ К настройкам", "st:home"), button("🏠 В меню", "nav:home")]),
        )
        return
    # Новый пользователь: после знакомства — тур по возможностям (в этом же сообщении), в конце — предупреждение
    # о том, что Deylo в разработке, и главное меню.
    intro = f"Приятно познакомиться, <b>{esc(main_name)}</b>! 👋\n{addressing}\n\n"
    await reply(event, intro + TOUR_PAGES[0], reply_markup=tour_keyboard(0))


# Любое сообщение от пользователя без подтверждённого имени (в т.ч. /start) -> знакомство.
@router.message(NeedsOnboarding())
async def start_onboarding(message: Message, user: User, state: FSMContext) -> None:
    await begin_onboarding(message, user, state)


@router.message(StateFilter(Onboarding.name), TEXT_INPUT)
async def got_name(message: Message, user: User, state: FSMContext) -> None:
    try:
        name = parse_single_name(message.text or "")
    except InvalidNameError as exc:
        edit = bool((await state.get_data()).get("edit"))
        await _name_screen(message, user.name if edit else (user.first_name or user.name), edit, error=f"{exc} Попробуй ещё раз.")
        return
    await _ask_variants(message, state, name)


@router.callback_query(StateFilter(Onboarding.name), F.data == "onb:keep")
async def keep_name(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    data = await state.get_data()
    candidate = user.name if data.get("edit") else (user.first_name or user.name)
    try:
        name = parse_single_name(candidate)
    except InvalidNameError:
        await callback.answer("Это имя не подходит, напиши своё сообщением", show_alert=True)
        return
    await callback.answer()
    await _ask_variants(callback, state, name)


@router.message(StateFilter(Onboarding.variants), TEXT_INPUT)
async def got_variants(message: Message, user: User, state: FSMContext, users: UserRepository) -> None:
    data = await state.get_data()
    main_name = data.get("main_name")
    if not main_name:  # состояние неполное — начинаем знакомство сначала
        await begin_onboarding(message, user, state, edit=bool(data.get("edit")))
        return
    try:
        variants = parse_variants(message.text or "", main_name)
    except InvalidNameError as exc:
        await _variants_screen(message, main_name, bool(data.get("edit")), error=f"{exc} Попробуй ещё раз.")
        return
    await _finish(message, state, users, message.from_user.id, main_name, variants)


@router.callback_query(StateFilter(Onboarding.variants), F.data == "onb:only")
async def only_main(callback: CallbackQuery, user: User, state: FSMContext, users: UserRepository) -> None:
    data = await state.get_data()
    main_name = data.get("main_name")
    await callback.answer()
    if not main_name:
        await begin_onboarding(callback, user, state, edit=bool(data.get("edit")))
        return
    await _finish(callback, state, users, callback.from_user.id, main_name, [main_name])
