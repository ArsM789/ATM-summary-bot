"""Разбор и оформление расписания уроков.

Что понимает парсер (по одному предмету в строке или несколько через запятую / «;»):
    Геометрия              -> 1 урок (число не нужно)
    Геометрия - 2          -> 2 урока подряд (тире любое: - – — −, а также «:»)
    Геометрия: 2, ×2, x2   -> то же
    Геометрия 2            -> то же (число в конце)
    Геометрия - два        -> то же (числа словами до двенадцати)
    Геометрия - 2 урока    -> то же
    Физ-ра                 -> 1 урок (дефис внутри слова — часть названия)
Числа — только целые от 1 до 12. Ошибки собираются по всем строкам сразу, с номером строки и причиной.

Целую неделю можно вставить одним сообщением с заголовками дней (parse_week).
Урок с количеством N — это N отдельных уроков: в расписании они нумеруются по очереди (5. и 6.), а не
«один пункт × N»."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from database.models import LessonItem, compress_lessons, expand_lessons
from utils.formatting import esc

MAX_SUBJECTS_PER_DAY = 15
MAX_SUBJECT_LEN = 40
MAX_LESSONS_PER_DAY = 12  # синхронно с database.repositories.MAX_LESSONS_PER_DAY
DASHES = "-–—−‒―"  # дефис, en dash, em dash, математический минус, figure dash, horizontal bar

_BULLET_RE = re.compile(r"^\s*(?:\d+[.)]|[•*·▪‣\u2022])\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPLIT_RE = re.compile(r"(?<!\d),|,(?!\d)|;")          # запятая, но не десятичная («2,5»)
_UNIT_RE = re.compile(r"\s+(?:урок(?:а|ов)?|ур\.?|шт\.?)$", re.IGNORECASE)
_SEP_COUNT_RE = re.compile(rf"(?P<name>.*\S)(?P<pre>\s*)(?P<sep>[{DASHES}:])(?P<post>\s*)(?P<tail>\S+)")
_TIMES_RE = re.compile(r"(?P<name>.*\S)\s*[×*]\s*(?P<n>\d+)|(?P<name2>.*\S)\s+[xXхХ]\s*(?P<n2>\d+)")
_TRAILING_NUMBER_RE = re.compile(r"(?P<name>.*\S)\s+(?P<n>\d+)")

NUMBER_WORDS = {
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
}

DAY_WORDS = {
    "понедельник": 0, "пн": 0, "вторник": 1, "вт": 1, "среда": 2, "среду": 2, "ср": 2,
    "четверг": 3, "чт": 3, "пятница": 4, "пятницу": 4, "пт": 4,
    "суббота": 5, "субботу": 5, "сб": 5, "воскресенье": 6, "вс": 6,
}
_HEADING_RE = re.compile(r"^[\W_]*(?P<word>[A-Za-zА-Яа-яЁё]+)\s*(?:[:.\-–—]\s*(?P<rest>.*))?$")
_LEADING_JUNK_RE = re.compile(r"^[\s*#_>•▪·\-–—]+")


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


def _heading(line: str) -> tuple[int, str] | None:
    """«Понедельник», «Вт:», «**Среда**», «Четверг: Математика, Физика» -> (день, остаток строки)."""
    cleaned = _LEADING_JUNK_RE.sub("", line).strip().rstrip("*_").strip()
    m = _HEADING_RE.match(cleaned)
    if not m:
        return None
    word = m.group("word").lower()
    day = DAY_WORDS.get(word)
    if day is None:
        return None
    rest = (m.group("rest") or "").strip()
    return day, rest


def _parse_item(raw: str) -> tuple[LessonItem | None, str | None]:
    """Один предмет («Геометрия - 2»). Возвращает (урок, None) или (None, причина ошибки)."""
    item = _UNIT_RE.sub("", raw.strip())
    if not item:
        return None, None

    name: str | None = None
    count = 1

    m = _SEP_COUNT_RE.fullmatch(item)
    if m:
        tail = m.group("tail")
        tail_low = tail.lower()
        spaced = bool(m.group("pre") or m.group("post"))
        base = m.group("name")
        if tail.isascii() and tail.isdigit():
            if base[-1] in DASHES:
                return None, "количество должно быть положительным целым числом"
            name, count = base, int(tail)
        elif tail_low in NUMBER_WORDS and spaced:
            name, count = base, NUMBER_WORDS[tail_low]
        elif spaced and m.group("sep") != ":" and tail_low not in NUMBER_WORDS:
            # «Математика - дваа»: тире с пробелами — признак счётчика, но числа нет
            if tail.lstrip(DASHES).isdigit():
                return None, "количество должно быть положительным целым числом"
            return None, "после тире должно быть число уроков, например «Физика — 2» (или просто «Физика»)"
        # иначе дефис без пробелов — часть названия («Физ-ра»), счётчика нет

    if name is None:
        t = _TIMES_RE.fullmatch(item)
        if t:
            name = t.group("name") or t.group("name2")
            count = int(t.group("n") or t.group("n2"))
        else:
            tn = _TRAILING_NUMBER_RE.fullmatch(item)
            if tn:
                name, count = tn.group("name"), int(tn.group("n"))
            else:
                name = item  # просто предмет — один урок

    name = " ".join(name.split()).strip(" .,;:" + DASHES)  # «Математика —» -> «Математика»
    if not name:
        return None, "пустое название предмета"
    if not any(ch.isalpha() for ch in name):
        return None, "в названии предмета должна быть хотя бы одна буква"
    if len(name) > MAX_SUBJECT_LEN:
        return None, f"название длиннее {MAX_SUBJECT_LEN} символов"
    if count < 1:
        return None, "количество уроков должно быть больше нуля"
    if count > MAX_LESSONS_PER_DAY:
        return None, f"слишком много уроков — максимум {MAX_LESSONS_PER_DAY} в день"
    return LessonItem(name[0].upper() + name[1:], count), None


def _parse_lines(lines: list[tuple[int, str]], result: ParseResult, *, allow_headings: bool) -> None:
    for line_no, raw in lines:
        line = _CONTROL_RE.sub(" ", raw).strip()
        if not line:
            continue
        line = _BULLET_RE.sub("", line)
        if allow_headings is False and _heading(line) is not None and not _split_has_content(line):
            result.errors.append(LineError(
                line_no, raw.strip(),
                "это название дня; сейчас я записываю один день — пришли только предметы "
                "(или нажми «Вставить неделю»)"))
            continue
        for part in _SPLIT_RE.split(line):
            item, error = _parse_item(part)
            if error:
                result.errors.append(LineError(line_no, part.strip() or raw.strip(), error))
            elif item:
                result.items.append(item)


def _split_has_content(line: str) -> bool:
    heading = _heading(line)
    return bool(heading and heading[1])


def parse_lessons(text: str) -> ParseResult:
    """Один день: «Математика - 2» и «Математика — 2» эквивалентны, без числа — один урок.
    Пустые строки пропускаются; корректные строки остаются в result.items, даже если рядом есть ошибки."""
    result = ParseResult()
    _parse_lines(list(enumerate((text or "").splitlines(), start=1)), result, allow_headings=False)
    if len(result.items) > MAX_SUBJECTS_PER_DAY:
        result.errors.append(LineError(0, "", f"слишком много предметов — максимум {MAX_SUBJECTS_PER_DAY} в день"))
    return result


# ----------------------------------------------------------------- вся неделя одним сообщением

@dataclass(slots=True)
class WeekParseResult:
    days: dict[int, ParseResult] = field(default_factory=dict)   # только учебные дни 0..4
    errors: list[LineError] = field(default_factory=list)
    empty_days: list[int] = field(default_factory=list)          # заголовок есть, предметов нет — не трогаем
    weekend_skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and any(r.items for r in self.days.values())


def parse_week(text: str) -> WeekParseResult:
    """Текст вида:  «Понедельник\\nМатематика - 2\\nФизика\\n\\nВторник: Алгебра, Геометрия, Биология - 2».
    Заголовок дня — полное название или сокращение (Пн, Вт…); после него — предметы на следующих строках или
    сразу через «:». Суббота и воскресенье пропускаются (выходные)."""
    week = WeekParseResult()
    current: int | None = None
    seen: set[int] = set()
    buffers: dict[int, list[tuple[int, str]]] = {}

    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = _CONTROL_RE.sub(" ", raw).strip()
        if not line:
            continue
        head = _heading(line)
        if head is not None:
            day, rest = head
            if day in seen:
                week.errors.append(LineError(line_no, raw.strip(), "этот день указан дважды"))
                current = None
                continue
            seen.add(day)
            current = day
            if day >= 5:
                week.weekend_skipped = True
                continue
            buffers.setdefault(day, [])
            if rest:
                buffers[day].append((line_no, rest))
            continue
        if current is None:
            week.errors.append(LineError(line_no, raw.strip(), "строка вне дня недели — сначала напиши день («Понедельник»)"))
        elif current < 5:
            buffers[current].append((line_no, raw))

    for day, lines in sorted(buffers.items()):
        result = ParseResult()
        _parse_lines(lines, result, allow_headings=True)
        if not result.items and not result.errors:
            week.empty_days.append(day)
            continue
        total = sum(i.count for i in result.items)
        if total > MAX_LESSONS_PER_DAY:
            result.errors.append(LineError(0, "", f"в сумме {total} уроков — максимум {MAX_LESSONS_PER_DAY} в день"))
        if len(result.items) > MAX_SUBJECTS_PER_DAY:
            result.errors.append(LineError(0, "", f"слишком много предметов — максимум {MAX_SUBJECTS_PER_DAY} в день"))
        week.days[day] = result
    return week


# ----------------------------------------------------------------- номера уроков для удаления

def parse_numbers(text: str, max_number: int) -> tuple[list[int], str | None]:
    """«3», «2, 4», «2 4», «2-4» -> ([...], None); ошибка -> ([], причина)."""
    numbers: set[int] = set()
    cleaned = (text or "").replace(";", ",")
    tokens = [t for t in re.split(r"[,\s]+", cleaned.strip()) if t]
    if not tokens:
        return [], "Напиши номера уроков, например: 3 или 2, 4"
    for token in tokens:
        m = re.fullmatch(rf"(\d+)\s*[{DASHES}]\s*(\d+)", token)
        if m:
            low, high = int(m.group(1)), int(m.group(2))
            if low > high:
                low, high = high, low
            numbers.update(range(low, high + 1))
        elif token.isascii() and token.isdigit():
            numbers.add(int(token))
        else:
            return [], f"«{token}» — не номер урока"
    bad = [n for n in numbers if not 1 <= n <= max_number]
    if bad:
        return [], f"Уроков с номером {', '.join(map(str, sorted(bad)))} нет — в этом дне их {max_number}"
    return sorted(numbers), None


def remove_lessons(items: list[LessonItem], numbers: list[int]) -> list[LessonItem]:
    """Убирает уроки по номерам (нумерация — по каждому уроку дня). Остальные сохраняют порядок."""
    lessons = expand_lessons(items)
    drop = set(numbers)
    return compress_lessons([s for i, s in enumerate(lessons, start=1) if i not in drop])


# ----------------------------------------------------------------- итоги и оформление

def total_lessons(items: list[LessonItem]) -> int:
    return sum(i.count for i in items)


def day_total_error(items: list[LessonItem]) -> str | None:
    total = total_lessons(items)
    if total > MAX_LESSONS_PER_DAY:
        return f"в сумме получается {total} уроков — максимум {MAX_LESSONS_PER_DAY} в день"
    return None


def format_day_editable(items: list[LessonItem]) -> str:
    """Текст в том же формате, что принимает парсер (можно скопировать, поправить и отправить)."""
    return "\n".join(f"{i.subject} — {i.count}" if i.count > 1 else i.subject for i in items)


def format_day_numbered(items: list[LessonItem]) -> str:
    """По строке на каждый урок: «1. Математика / 2. Математика / 3. Физика» (HTML-экранировано)."""
    return "\n".join(f"{n}. {esc(subject)}" for n, subject in enumerate(expand_lessons(items), start=1))


def format_day_quote(items: list[LessonItem]) -> str:
    """Нумерованный список внутри цитаты Telegram — компактнее и аккуратнее обычного текста."""
    return f"<blockquote>{format_day_numbered(items)}</blockquote>"
