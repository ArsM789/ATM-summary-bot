"""Знакомство: имя и варианты обращения. Для новых пользователей и для старых без подтверждённого имени.
Тот же диалог открывается из «⚙️ Настройки → Имя и обращения» (edit=True)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.filters import TEXT_INPUT, NeedsOnboarding
from bot.helpers import edit_or_reply, reply
from bot.keyboards.common import button, markup
from bot.keyboards.main_menu import main_menu
from bot.states.settings_states import Onboarding
from database.models import User
from database.repositories import UserRepository
from services.names import InvalidNameError, parse_single_name, parse_variants
from utils.formatting import esc, join_ru
from bot.handlers.start import DEVELOPMENT_NOTICE

router = Router(name="onboarding")
router.message.filter(F.chat.type == ChatType.PRIVATE)

ASK_NAME = "Привет! Давай познакомимся. Как тебя зовут?"
ASK_VARIANTS = (
    "А как мне тебя называть?\n\n"
    "Например, если тебя зовут Артём, можно указать:\n<i>Артём, Тёма</i>.\n\n"
    "Если Софья:\n<i>Софья, Софа, Соф</i>.\n\n"
    "Напиши варианты, которыми тебе комфортно пользоваться, через запятую. Я буду иногда менять их, "
    "чтобы общение не выглядело однообразным.\n\n"
    "Если хочешь только один вариант — просто напиши его."
)


async def begin_onboarding(event: Message | CallbackQuery, user: User, state: FSMContext, *, edit: bool = False) -> None:
    await state.clear()
    await state.set_state(Onboarding.name)
    await state.update_data(edit=edit)
    keep_name = user.name if edit else (user.first_name or user.name)
    text = ASK_NAME if not edit else "Как тебя зовут? Напиши основное имя."
    await reply(
        event, text,
        reply_markup=markup([button(f"Оставить «{keep_name[:30]}»", "onb:keep")]),
    )


async def _ask_variants(event: Message | CallbackQuery, state: FSMContext, main_name: str) -> None:
    await state.update_data(main_name=main_name)
    await state.set_state(Onboarding.variants)
    await reply(
        event, ASK_VARIANTS,
        reply_markup=markup([button(f"Только «{main_name}»", "onb:only")]),
    )


async def _finish(event: Message | CallbackQuery, state: FSMContext, users: UserRepository,
                  user_id: int, main_name: str, variants: list[str]) -> None:
    await users.set_names(user_id, main_name, variants)
    await state.clear()
    await reply(event, DEVELOPMENT_NOTICE)
    await reply(
        event,
        f"Приятно познакомиться, {esc(main_name)}! 👋\n"
        f"Буду обращаться к тебе так: <b>{esc(join_ru(variants))}</b>.\n\n"
        f"Изменить это можно в «⚙️ Настройки». Что дальше:\n"
        f"📅 <b>Моё расписание</b> — внеси уроки, чтобы они попадали в сводку\n"
        f"📅 <b>Мои события</b> — твои занятия / кружки и любые другие задачи\n"
        f"🌤 <b>Сводка</b> — Твоя личная сводка с погодой, рекомендациями по одежде, а также твоими уроками и занятиями (если ты их добавил) в этот день",
        reply_markup=main_menu(),
    )


# Любое сообщение от пользователя без подтверждённого имени (в т.ч. /start) -> знакомство.
@router.message(NeedsOnboarding())
async def start_onboarding(message: Message, user: User, state: FSMContext) -> None:
    await begin_onboarding(message, user, state)


@router.message(StateFilter(Onboarding.name), TEXT_INPUT)
async def got_name(message: Message, state: FSMContext) -> None:
    try:
        name = parse_single_name(message.text or "")
    except InvalidNameError as exc:
        await message.answer(f"{esc(exc)}\nПопробуй ещё раз.")
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
async def got_variants(message: Message, state: FSMContext, users: UserRepository) -> None:
    data = await state.get_data()
    main_name = data.get("main_name")
    if not main_name:  # состояние неполное — начинаем сначала
        await state.clear()
        await message.answer("Давай начнём сначала — напиши /start")
        return
    try:
        variants = parse_variants(message.text or "", main_name)
    except InvalidNameError as exc:
        await message.answer(f"{esc(exc)}\nПопробуй ещё раз.")
        return
    await _finish(message, state, users, message.from_user.id, main_name, variants)


@router.callback_query(StateFilter(Onboarding.variants), F.data == "onb:only")
async def only_main(callback: CallbackQuery, state: FSMContext, users: UserRepository) -> None:
    data = await state.get_data()
    main_name = data.get("main_name")
    await callback.answer()
    if not main_name:
        await state.clear()
        await edit_or_reply(callback, "Давай начнём сначала — напиши /start")
        return
    await _finish(callback, state, users, callback.from_user.id, main_name, [main_name])
