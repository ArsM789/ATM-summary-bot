"""Быстрое создание события одной фразой (естественный ввод).

    «урок английского по вторникам и четвергам в 16:00 на 2 часа»
    «созвон завтра в 15:30»          «тренировка каждый день в 7 утра»
    «оплатить кружок 15 числа каждый месяц в 10:00»      «напомни про врача 20 ноября в 9:00 за 30 минут»

Парсер ничего не придумывает: находит то, что сказано явно, остальное оставляет пустым — недостающее бот
спросит обычным диалогом, а всё найденное пользователь увидит в предпросмотре перед сохранением."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from database.models import weekdays_to_mask
from services.event_input import parse_date_input, parse_duration, parse_time_input
from services.intents import normalize

_WEEKDAYS = {
    "понедельник": 0, "понедельникам": 0, "пн": 0, "вторник": 1, "вторникам": 1, "вт": 1,
    "среда": 2, "среду": 2, "средам": 2, "ср": 2, "четверг": 3, "четвергам": 3, "чт": 3,
    "пятница": 4, "пятницу": 4, "пятницам": 4, "пт": 4, "суббота": 5, "субботу": 5, "субботам": 5, "сб": 5,
    "воскресенье": 6, "воскресеньям": 6, "вс": 6,
}
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_MONTHS = "январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр"
_UNIT = r"(?:часа|часов|час|ч|минуты|минут|минуту|мин)"
_NUM = r"(?:\d+(?:[.,]\d+)?|полтора|одну|один|два|две|три|четыре|пять)"

_LEAD_RE = re.compile(
    r"^\s*(?:пожалуйста[, ]*)?(?:хочу\s+)?(?:добавь|добавить|создай|создать|запиши|записать|поставь|поставить|"
    r"напомни(?:ть)?(?:\s+мне)?)\s*(?:мне\s+)?(?:новое\s+|новую\s+)?(?:событие|задачу|напоминание|встречу)?\s*[:\-—]?\s+")
_REMIND_RE = re.compile(rf"\bза\s+({_NUM}\s*{_UNIT}|полчаса|полтора\s+часа|час)\b")
_DURATION_RE = re.compile(rf"\b(?:на|длительностью|длится)\s+({_NUM}\s*{_UNIT}(?:\s*(?:и\s*)?\d+\s*(?:минут|мин)\w*)?|полчаса|полтора\s+часа)")
_DURATION_BARE_RE = re.compile(rf"\b(?<!в )(\d+(?:[.,]\d+)?\s*{_UNIT}(?:\s*(?:и\s*)?\d+\s*(?:минут|мин)\w*)?)\b")
_TIME_COLON_RE = re.compile(r"\b(?:в|к|с)?\s*(\d{1,2}:\d{2})\b")
_TIME_V_DOT_RE = re.compile(r"\bв\s+(\d{1,2}[.]\d{2})\b(?![./]\d)")
_TIME_V_HOUR_RE = re.compile(rf"\bв\s+(\d{{1,2}})(?:\s*(?:часов|часа|час|ч\b))?(?!\d|[:.]\d|\s*(?:числа|{_MONTHS}|[./-]\d))(\s*(?:утра|вечера|дня|ночи))?")
_DATE_WORD_RE = re.compile(r"\b(сегодня|завтра|послезавтра)\b")
_DATE_NUM_RE = re.compile(r"\b(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)\b")
_DATE_TEXT_RE = re.compile(rf"\b(\d{{1,2}}\s+(?:{_MONTHS})\w*(?:\s+\d{{4}})?)")
_DATE_WD_RE = re.compile(rf"\b(?:в|во|на)\s+({_WD})\b")
_DAILY_RE = re.compile(r"\b(?:каждый\s+день|ежедневно|каждое\s+утро|каждый\s+вечер|по\s+утрам|по\s+вечерам)\b")
_WEEKLY_RE = re.compile(r"\b(?:каждую\s+неделю|еженедельно)\b")
_MONTHLY_RE = re.compile(r"\b(?:каждый\s+месяц|ежемесячно)\b")
_EACH_WD_RE = re.compile(rf"\b(?:каждый|каждую|каждое|по)\s+((?:{_WD})(?:\s*(?:,|и|и\s+по)\s*(?:{_WD}))*)\b")
_MONTH_DAY_RE = re.compile(r"\b(\d{1,2})[-\s]?(?:го|е)?\s*числа\b")
_LAST_DAY_RE = re.compile(r"\bпоследн\w+\s+день\s+месяца\b")
_STOP_EDGES = {"в", "на", "по", "и", "к", "с", "за", "каждый", "каждую", "каждое", "мне", "пожалуйста", "утра", "вечера", "дня", "ночи"}
_PERIOD_HOURS = {"вечера": 12, "дня": 12}


@dataclass
class QuickEvent:
    title: str | None = None
    recurrence: str | None = None            # none | daily | weekly | monthly
    weekdays: tuple[int, ...] = ()
    month_day: int | None = None
    event_date: date | None = None
    start_time: str | None = None
    duration_min: int | None = None
    remind_min: int | None = None

    @property
    def found_anything(self) -> bool:
        """Найдено ли что-то кроме названия (тогда фраза воспринимается как «быстрое событие»)."""
        return any((self.recurrence, self.weekdays, self.month_day, self.event_date, self.start_time,
                    self.duration_min, self.remind_min))


def _cut(work: str, match: re.Match) -> str:
    return (work[:match.start()] + " " + work[match.end():])


def parse_quick_event(text: str, today: date) -> QuickEvent:
    """Разбирает свободную фразу. Всё найденное убирается из текста, остаток — название."""
    original = " ".join((text or "").split())
    work = original.lower().replace("ё", "е")
    q = QuickEvent()

    lead = _LEAD_RE.match(work + " ")
    if lead:
        work = work[lead.end():]

    if (m := _REMIND_RE.search(work)):
        minutes, _ = parse_duration(m.group(1))
        if minutes:
            q.remind_min = minutes
            work = _cut(work, m)

    m = _DURATION_RE.search(work) or _DURATION_BARE_RE.search(work)
    if m:
        minutes, _ = parse_duration(m.group(1))
        if minutes:
            q.duration_min = minutes
            work = _cut(work, m)

    # время начала
    hour_shift = 0
    if (m := _TIME_COLON_RE.search(work)):
        q.start_time = parse_time_input(m.group(1)); work = _cut(work, m)
    elif (m := _TIME_V_DOT_RE.search(work)):
        q.start_time = parse_time_input(m.group(1).replace(".", ":")); work = _cut(work, m)
    elif (m := _TIME_V_HOUR_RE.search(work)):
        hour = int(m.group(1))
        period = (m.group(2) or "").strip()
        if period in _PERIOD_HOURS and hour < 12:
            hour += _PERIOD_HOURS[period]
        q.start_time = parse_time_input(f"{hour}:00"); work = _cut(work, m)

    # периодичность и дни недели
    if (m := _MONTHLY_RE.search(work)):
        q.recurrence = "monthly"; work = _cut(work, m)
    if (m := _DAILY_RE.search(work)):
        q.recurrence = "daily"; work = _cut(work, m)
    if (m := _WEEKLY_RE.search(work)):
        q.recurrence = q.recurrence or "weekly"; work = _cut(work, m)
    if (m := _EACH_WD_RE.search(work)):
        days = {_WEEKDAYS[w] for w in re.findall(_WD, m.group(1))}
        if days:
            q.weekdays = tuple(sorted(days))
            q.recurrence = q.recurrence if q.recurrence in ("weekly", "monthly") else "weekly"
            work = _cut(work, m)
    if (m := _LAST_DAY_RE.search(work)):
        q.month_day = -1; q.recurrence = "monthly"; work = _cut(work, m)
    elif (m := _MONTH_DAY_RE.search(work)):
        day = int(m.group(1))
        if 1 <= day <= 31:
            q.month_day = day; q.recurrence = q.recurrence or "monthly"; work = _cut(work, m)

    # дата (для разового события)
    if not q.weekdays and q.month_day is None and q.recurrence in (None, "none"):
        for pattern in (_DATE_TEXT_RE, _DATE_NUM_RE, _DATE_WORD_RE, _DATE_WD_RE):
            m = pattern.search(work)
            if not m:
                continue
            value, error = parse_date_input(m.group(1), today)
            if value is not None and not error:
                q.event_date, q.recurrence = value, "none"
                work = _cut(work, m)
                break

    # название — остаток
    tokens = [t for t in re.split(r"\s+", re.sub(r"[,;:!?()\"«»]", " ", work)) if t]
    while tokens and tokens[0].strip(".-—") in _STOP_EDGES:
        tokens.pop(0)
    while tokens and tokens[-1].strip(".-—") in _STOP_EDGES:
        tokens.pop()
    title = " ".join(tokens).strip(" .-—")
    # название берём из оригинала, чтобы сохранить регистр (по позициям слов)
    if title:
        q.title = _restore_case(title, original)
    return q


def _restore_case(title: str, original: str) -> str:
    """Возвращает написание названия как у пользователя (слова ищем в исходной фразе без учёта регистра/ё)."""
    originals = {w.lower().replace("ё", "е"): w for w in re.findall(r"[\w\-]+", original)}
    words = [originals.get(w, w) for w in title.split()]
    result = " ".join(words)
    return result[:1].upper() + result[1:] if result else result


def apply_quick_to_draft(draft: dict, q: QuickEvent, today: date) -> tuple[dict, list[str]]:
    """Подставляет найденное в черновик события и возвращает (черновик, шаги диалога, которые ещё нужно пройти).

    Нужные шаги: название; «повторение/когда»; время. Длительность и напоминание остаются по умолчанию
    (их видно в предпросмотре и можно изменить)."""
    d = dict(draft)
    if q.title:
        d["title"] = q.title
    if q.start_time:
        d["start_time"] = q.start_time
    if q.duration_min:
        d["duration_min"] = q.duration_min
    if q.remind_min:
        d["remind_before_min"] = q.remind_min

    chain: list[str] = []
    if not d.get("title"):
        chain.append("title")

    rec = q.recurrence
    when_missing = False
    if rec is None:
        chain += ["repeat", "when"]
    else:
        d["recurrence"] = rec
        if rec == "none":
            if q.event_date:
                d["event_date"] = q.event_date.isoformat()
            else:
                d["event_date"] = None; when_missing = True
        else:
            d["event_date"] = today.isoformat()      # повторяющееся начинается с сегодняшнего дня
            if rec == "weekly":
                d["weekdays_mask"] = weekdays_to_mask(q.weekdays) if q.weekdays else 0
                when_missing = not q.weekdays
            elif rec == "monthly":
                d["month_day"] = q.month_day
                when_missing = q.month_day is None
        if rec != "weekly":
            d["weekdays_mask"] = 0
        if rec != "monthly":
            d["month_day"] = None
        if when_missing:
            chain.append("when")
    if not d.get("start_time"):
        chain.append("time")
    return d, chain
