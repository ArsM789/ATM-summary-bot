"""FSM-состояния. Хранятся по ключу (бот, чат, пользователь) — у каждого пользователя свои."""
from aiogram.fsm.state import State, StatesGroup


class Onboarding(StatesGroup):
    name = State()
    variants = State()


class LessonsInput(StatesGroup):
    text = State()


class CityInput(StatesGroup):
    query = State()


class TimezoneInput(StatesGroup):
    value = State()


class SendTimeInput(StatesGroup):
    value = State()


class FeedbackInput(StatesGroup):
    text = State()


class AdminBroadcast(StatesGroup):
    text = State()      # ждём сообщение рассылки (текст или фото с подписью)
    edit = State()      # ждём новый текст для существующего черновика
    photo = State()     # ждём картинку для черновика
    preview = State()   # черновик показан, ждём решения администратора


class EventForm(StatesGroup):
    """Диалог создания/изменения события («📅 Мои события»)."""

    title = State()
    repeat = State()
    weekdays = State()
    date = State()
    month_day = State()
    time = State()
    duration = State()
    remind = State()
    remind_custom = State()
    preview = State()
