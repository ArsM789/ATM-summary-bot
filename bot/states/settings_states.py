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
    text = State()
