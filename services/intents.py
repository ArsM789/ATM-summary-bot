"""Намерения из обычного текста: «что надеть», «покажи расписание», «отмена». Чистая функция без aiogram."""
from __future__ import annotations

import re
from enum import Enum


class Intent(str, Enum):
    SUMMARY = "summary"
    LESSONS = "lessons"
    EVENTS = "events"
    SETTINGS = "settings"
    FEEDBACK = "feedback"
    HELP = "help"
    HOME = "home"
    CANCEL = "cancel"
    ADD_EVENT = "add_event"


CANCEL_WORDS = frozenset({"отмена", "отменить", "отмена!", "cancel", "стоп", "хватит", "выход", "выйти", "не надо", "не нужно"})
_HOME_WORDS = frozenset({"меню", "главное меню", "в меню", "домой", "в главное меню", "на главную", "главная"})
_ADD_VERBS = ("добавь", "добавить", "создай", "создать", "запиши", "записать", "поставь", "поставить", "напомни", "напомнить",
              "хочу добавить", "хочу создать", "новое событие", "новая задача", "новая встреча")


def normalize(text: str) -> str:
    """Нижний регистр, ё -> е, без знаков препинания по краям, одиночные пробелы."""
    cleaned = (text or "").lower().replace("ё", "е")
    cleaned = re.sub(r"[^\w\s:.\-/]", " ", cleaned)
    return " ".join(cleaned.split()).strip(" .-")


def is_cancel(text: str) -> bool:
    return normalize(text) in {normalize(w) for w in CANCEL_WORDS}


def detect_intent(text: str) -> Intent | None:
    """Определяет, чего хочет пользователь, по короткой реплике. None — не поняли."""
    t = normalize(text)
    if not t or len(t.split()) > 14:
        return None
    if t in {normalize(w) for w in CANCEL_WORDS}:
        return Intent.CANCEL
    if t in _HOME_WORDS:
        return Intent.HOME
    if t in {"помощь", "help", "справка", "инструкция", "start"} or any(
            k in t for k in ("как пользоваться", "что умеешь", "что ты умеешь", "как это работает", "что ты можешь")):
        return Intent.HELP
    if any(t.startswith(v) for v in _ADD_VERBS) and len(t.split()) >= 2:
        return Intent.ADD_EVENT
    if any(k in t for k in ("сводк", "погод", "что надеть", "во что одеться", "что одеть", "прогноз", "одеваться", "одеться")):
        return Intent.SUMMARY
    if "событи" in t or "задач" in t or "напомин" in t or "кружок" in t or "кружки" in t:
        return Intent.EVENTS
    if "расписан" in t or "уроки" in t or "уроков" in t or "предмет" in t:
        return Intent.LESSONS
    if any(k in t for k in ("настройк", "часовой пояс", "часовой", "сменить город", "поменять город", "мой город")):
        return Intent.SETTINGS
    if any(k in t for k in ("ошибк", "баг", "предложени", "отзыв", "жалоб", "не работает", "сломал")):
        return Intent.FEEDBACK
    return None
