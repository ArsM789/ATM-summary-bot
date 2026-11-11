"""Имя пользователя и варианты обращения: разбор ввода и выбор варианта без подряд-повторов."""
from __future__ import annotations

import random
import re
from typing import Sequence

from database.models import User

MAX_NAME_LEN = 30
MAX_VARIANTS = 6
_SPLIT_RE = re.compile(r"[,;\n]+")
_ALLOWED_EXTRA = set(" -'’ʼ.")


class InvalidNameError(ValueError):
    """Ввод имени не подошёл; текст ошибки можно показывать пользователю."""


def _normalize(part: str) -> str:
    name = " ".join(part.split())
    if not name:
        raise InvalidNameError("Пустое имя — напиши хотя бы одно слово.")
    if len(name) > MAX_NAME_LEN:
        raise InvalidNameError(f"«{name[:15]}…» слишком длинное — максимум {MAX_NAME_LEN} символов.")
    if not name[0].isalpha() or any(not (ch.isalpha() or ch in _ALLOWED_EXTRA) for ch in name):
        raise InvalidNameError(f"«{name}» не похоже на имя: используй буквы, без цифр и символов.")
    return name[0].upper() + name[1:]


def parse_single_name(text: str) -> str:
    """Основное имя: одно имя, без запятых."""
    if _SPLIT_RE.search(text or ""):
        raise InvalidNameError("Сначала только одно имя — варианты обращения спрошу следующим шагом.")
    return _normalize(text or "")


def parse_variants(text: str, main_name: str) -> list[str]:
    """«Арсений, Сеня, Арсен» -> ['Арсений', 'Сеня', 'Арсен']. Основное имя всегда в списке (первым,
    если пользователь его не указал). Дубликаты без учёта регистра убираются, порядок сохраняется."""
    parts = [p for p in _SPLIT_RE.split(text or "") if p.strip()]
    if not parts:
        raise InvalidNameError("Напиши хотя бы один вариант (можно просто повторить имя).")
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = _normalize(part)
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)
    if main_name.casefold() not in seen:
        result.insert(0, main_name)
    if len(result) > MAX_VARIANTS:
        raise InvalidNameError(f"Многовато вариантов — оставь не больше {MAX_VARIANTS}.")
    return result


def choose_name(variants: Sequence[str], last_used: str | None, rng: random.Random, fallback: str = "друг") -> str:
    """Случайный вариант, исключая последний использованный (если есть из чего выбирать)."""
    pool = [v for v in variants if v]
    if not pool:
        return fallback
    if last_used and len(pool) > 1:
        others = [v for v in pool if v.casefold() != last_used.casefold()]
        pool = others or pool
    return rng.choice(pool)


def pick_address(user: User, rng: random.Random) -> str:
    return choose_name(user.name_variants or (user.name,), user.last_used_name, rng, fallback=user.name)
