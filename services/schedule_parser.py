"""Разбор расписания уроков: «Название — количество», по одной строке на предмет."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from database.models import LessonItem
from utils.formatting import esc

MAX_SUBJECTS_PER_DAY = 15
MAX_SUBJECT_LEN = 40
MAX_LESSONS_PER_DAY = 12  # синхронно с database.repositories.MAX_LESSONS_PER_DAY
DASHES = "-–—−‒―"  # дефис, en dash, em dash, математический минус, figure dash, horizontal bar
_BULLET_RE = re.compile(r"^\s*(?:\d+[.)]|[•*·▪])\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class LineError:
    line_no: int       # номер строки во вводе (с 1); 0 — ошибка всего ввода
    text: str
    reason: str


@dataclass(slots=True)
class ParseResult:
    items: list[LessonItem] = field(default_factory=list)
    errors: list[LineError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.items)


def _last_dash(line: str) -> int:
    return max((line.rfind(d) for d in DASHES), default=-1)


def parse_lessons(text: str) -> ParseResult:
    """`Математика - 2` и `Математика — 2` дают одно и то же. Пустые строки пропускаются.
    Ошибки собираются по всем строкам сразу, корректные строки остаются в result.items."""
    result = ParseResult()
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = _CONTROL_RE.sub(" ", raw).strip()
        if not line:
            continue
        line = _BULLET_RE.sub("", line)

        idx = _last_dash(line)
        if idx < 0:
            result.errors.append(LineError(line_no, raw.strip(), "нет тире — пиши так: «Математика — 2»"))
            continue
        name = " ".join(line[:idx].split())
        count_raw = line[idx + 1:].strip()

        if not name:
            result.errors.append(LineError(line_no, raw.strip(), "пустое название предмета"))
            continue
        if name[-1] in DASHES or count_raw.startswith(tuple(DASHES)):
            result.errors.append(LineError(line_no, raw.strip(), "количество должно быть положительным целым числом"))
            continue
        if not re.fullmatch(r"[0-9]+", count_raw):
            result.errors.append(
                LineError(line_no, raw.strip(), "после тире должно быть число уроков, например «Физика — 1»")
            )
            continue
        count = int(count_raw)
        if count < 1:
            result.errors.append(LineError(line_no, raw.strip(), "количество уроков должно быть больше нуля"))
            continue
        if count > MAX_LESSONS_PER_DAY:
            result.errors.append(
                LineError(line_no, raw.strip(), f"слишком много уроков — максимум {MAX_LESSONS_PER_DAY} в день")
            )
            continue
        if len(name) > MAX_SUBJECT_LEN:
            result.errors.append(
                LineError(line_no, raw.strip(), f"название длиннее {MAX_SUBJECT_LEN} символов")
            )
            continue
        result.items.append(LessonItem(name[0].upper() + name[1:], count))

    if len(result.items) > MAX_SUBJECTS_PER_DAY:
        result.errors.append(LineError(0, "", f"слишком много предметов — максимум {MAX_SUBJECTS_PER_DAY} в день"))
    return result


def total_lessons(items: list[LessonItem]) -> int:
    return sum(i.count for i in items)


def day_total_error(items: list[LessonItem]) -> str | None:
    total = total_lessons(items)
    if total > MAX_LESSONS_PER_DAY:
        return f"в сумме получается {total} уроков — максимум {MAX_LESSONS_PER_DAY} в день"
    return None


def format_day_editable(items: list[LessonItem]) -> str:
    """Текст в том же формате, что принимает парсер (можно скопировать, поправить и отправить)."""
    return "\n".join(f"{i.subject} — {i.count}" for i in items)


def format_day_numbered(items: list[LessonItem]) -> str:
    """1. Математика × 2 — HTML-экранированный нумерованный список (порядок как ввёл пользователь)."""
    lines = []
    for n, item in enumerate(items, start=1):
        suffix = f" × {item.count}" if item.count > 1 else ""
        lines.append(f"{n}. {esc(item.subject)}{suffix}")
    return "\n".join(lines)
