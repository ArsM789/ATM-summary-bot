"""Анализ погоды и подбор одежды.

Не одна таблица «температура -> одежда», а набор правил, которые смотрят на несколько
параметров сразу: ощущаемую температуру, ветер и порывы, осадки (и их тип), гололёд,
гроз, перепад температуры за период, солнце. Правила по очереди дополняют «гардероб»
(Outfit), а в конце проходит проверка на противоречия (напр. шарф при +25°).

Все фразы одежды стоят в винительном падеже — их ставят после «надеть» / «взять».
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from statistics import mean
from typing import Sequence

from services.weather_models import PRECIP_KINDS, CurrentConditions, HourPoint, describe_code
from utils.formatting import fmt_temp, round_half_up


class Thermal(IntEnum):
    """Тепловой уровень по ощущаемой температуре (чем больше, тем теплее)."""

    EXTREME = 0      # < −15
    HARD_FROST = 1   # −15 … −8
    FROST = 2        # −8 … −2
    COLD = 3         # −2 … +4
    COOL = 4         # +4 … +10
    FRESH = 5        # +10 … +16
    COMFORT = 6      # +16 … +22
    WARM = 7         # +22 … +28
    HOT = 8          # ≥ +28


_BOUNDS = (
    (-25.0, Thermal.EXTREME),
    (-12.0, Thermal.HARD_FROST),
    (-5.0, Thermal.FROST),
    (4.0, Thermal.COLD),
    (10.0, Thermal.COOL),
    (16.0, Thermal.FRESH),
    (22.0, Thermal.COMFORT),
    (28.0, Thermal.WARM),
)


def thermal_level(ref: float) -> Thermal:
    for upper, level in _BOUNDS:
        if ref < upper:
            return level
    return Thermal.HOT


# Слоты гардероба (порядок = порядок вывода).
CORE, OUTER, LEGS, FEET, HEAD, HANDS, NECK, EXTRAS = (
    "core", "outer", "legs", "feet", "head", "hands", "neck", "extras",
)
SLOTS = (CORE, OUTER, LEGS, FEET, HEAD, HANDS, NECK, EXTRAS)


@dataclass(frozen=True, slots=True)
class Item:
    text: str
    optional: bool = False


@dataclass(slots=True)
class Outfit:
    slots: dict[str, dict[str, Item]] = field(default_factory=lambda: {s: {} for s in SLOTS})
    warnings: list[str] = field(default_factory=list)

    def put(self, slot: str, key: str, text: str, optional: bool = False) -> None:
        self.slots[slot][key] = Item(text, optional)

    def drop(self, slot: str, key: str | None = None) -> None:
        if key is None:
            self.slots[slot].clear()
        else:
            self.slots[slot].pop(key, None)

    def has(self, slot: str, key: str) -> bool:
        return key in self.slots[slot]

    def texts(self, slots: Sequence[str], *, optional: bool) -> list[str]:
        result: list[str] = []
        for slot in slots:
            for item in self.slots[slot].values():
                if item.optional == optional:
                    result.append(item.text)
        return result

    def all_texts(self) -> list[str]:
        return [item.text for slot in SLOTS for item in self.slots[slot].values()]


@dataclass(frozen=True, slots=True)
class WeatherFacts:
    """Сжатые факты о периоде, в который пользователь будет на улице."""

    ref: float                  # опорная ощущаемая температура
    min_feels: float
    max_feels: float
    min_temp: float             # фактическая температура (для гололёда)
    trend: float                # ощущаемая в конце периода − в начале
    max_wind: float
    max_gust: float
    max_precip_prob: float | None
    wet_hours: int
    precip_kind: str | None     # rain | snow | freezing | thunder | mixed
    rain_mm: float
    snow_cm: float
    fog: bool
    sunny: bool
    dominant_code: int | None
    hours_count: int

    @property
    def swing(self) -> float:
        return self.max_feels - self.min_feels


def _is_wet(h: HourPoint) -> bool:
    kind = describe_code(h.code).kind
    by_code = kind in PRECIP_KINDS
    by_amount = h.precip >= 0.2 or h.snowfall >= 0.1
    prob_ok = h.precip_prob is None or h.precip_prob >= 30
    return (by_code or by_amount) and prob_ok


def build_facts(hours: Sequence[HourPoint], current: CurrentConditions | None = None) -> WeatherFacts:
    """Собирает факты по часам периода. Если часов нет — по текущим условиям."""
    points = list(hours)
    if not points:
        if current is None:
            raise ValueError("Нет данных для анализа погоды")
        return WeatherFacts(
            ref=current.apparent, min_feels=current.apparent, max_feels=current.apparent,
            min_temp=current.temp, trend=0.0, max_wind=current.wind, max_gust=current.gusts,
            max_precip_prob=None, wet_hours=0, precip_kind=None, rain_mm=0.0, snow_cm=0.0,
            fog=False, sunny=False, dominant_code=current.code, hours_count=0,
        )

    feels = [p.apparent for p in points]
    avg, low, high = mean(feels), min(feels), max(feels)
    ref = 0.6 * avg + 0.4 * low  # холод важнее: на улице мёрзнут от минимума, а не от среднего

    wet = [p for p in points if _is_wet(p)]
    kinds: set[str] = set()
    rain_mm = snow_cm = 0.0
    for p in wet:
        kind = describe_code(p.code).kind
        if kind == "thunder":
            kinds.add("thunder")
        elif kind == "freezing":
            kinds.add("freezing")
        elif p.snowfall >= 0.1 or kind == "snow":
            kinds.add("snow")
            snow_cm += p.snowfall
        else:
            kinds.add("rain")
            rain_mm += p.precip

    if "thunder" in kinds:
        precip_kind: str | None = "thunder"
    elif "freezing" in kinds:
        precip_kind = "freezing"
    elif {"rain", "snow"} <= kinds:
        precip_kind = "mixed"
    elif kinds:
        precip_kind = next(iter(kinds))
    else:
        precip_kind = None

    probs = [p.precip_prob for p in points if p.precip_prob is not None]
    codes = [p.code for p in points if p.code is not None]
    dominant = max(set(codes), key=codes.count) if codes else (current.code if current else None)
    clear_hours = sum(1 for c in codes if describe_code(c).kind == "clear")

    return WeatherFacts(
        ref=ref,
        min_feels=low,
        max_feels=high,
        min_temp=min(p.temp for p in points),
        trend=points[-1].apparent - points[0].apparent,
        max_wind=max(p.wind for p in points),
        max_gust=max(p.gusts for p in points),
        max_precip_prob=max(probs) if probs else None,
        wet_hours=len(wet),
        precip_kind=precip_kind,
        rain_mm=rain_mm,
        snow_cm=snow_cm,
        fog=any(describe_code(c).kind == "fog" for c in codes),
        sunny=bool(codes) and clear_hours / len(codes) >= 0.6,
        dominant_code=dominant,
        hours_count=len(points),
    )


@dataclass(frozen=True, slots=True)
class ClothingAdvice:
    level: Thermal
    facts: WeatherFacts
    outfit: Outfit
    tags: frozenset[str]


# --------------------------------- правила ---------------------------------

_BASE: dict[Thermal, list[tuple[str, str, str, bool]]] = {
    Thermal.EXTREME: [
        (CORE, "base", "термобельё", False), (CORE, "mid", "тёплый свитер или флиску", False),
        (OUTER, "coat", "пуховик", False), (LEGS, "legs", "тёплые штаны", False),
        (FEET, "shoes", "зимние ботинки с тёплыми носками", False),
    ],
    Thermal.HARD_FROST: [
        (CORE, "base", "термобельё", False), (CORE, "mid", "тёплую кофту или свитер", False),
        (OUTER, "coat", "зимнюю куртку или пуховик", False), (LEGS, "legs", "тёплые штаны", False),
        (FEET, "shoes", "зимние ботинки", False),
    ],
    Thermal.FROST: [
        (CORE, "mid", "тёплую кофту или худи", False), (OUTER, "coat", "тёплую зимнюю куртку", False),
        (LEGS, "legs", "тёплые штаны", False), (FEET, "shoes", "утеплённую обувь", False),
    ],
    Thermal.COLD: [
        (CORE, "mid", "кофту или худи", False), (OUTER, "coat", "тёплую куртку", False),
        (LEGS, "legs", "плотные штаны или джинсы", False), (FEET, "shoes", "закрытую обувь", False),
    ],
    Thermal.COOL: [
        (CORE, "mid", "кофту или худи", False), (OUTER, "coat", "куртку", False),
        (LEGS, "legs", "джинсы или брюки", False), (FEET, "shoes", "закрытую обувь", False),
    ],
    Thermal.FRESH: [
        (CORE, "mid", "футболку и кофту или худи", False),
        (OUTER, "coat", "лёгкую куртку или ветровку", False),
        (LEGS, "legs", "джинсы или брюки", False), (FEET, "shoes", "кроссовки", False),
    ],
    Thermal.COMFORT: [
        (CORE, "mid", "футболку или лонгслив", False),
        (OUTER, "coat", "лёгкую кофту или ветровку на вечер", True),
        (LEGS, "legs", "лёгкие брюки или джинсы", False), (FEET, "shoes", "кроссовки", False),
    ],
    Thermal.WARM: [
        (CORE, "mid", "лёгкую футболку", False), (LEGS, "legs", "шорты или лёгкие брюки", False),
        (FEET, "shoes", "лёгкую обувь", False),
    ],
    Thermal.HOT: [
        (CORE, "mid", "лёгкую свободную футболку из дышащей ткани", False),
        (LEGS, "legs", "шорты или лёгкие свободные брюки", False),
        (FEET, "shoes", "лёгкую открытую обувь", False),
    ],
}


def _rule_base(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    for slot, key, text, optional in _BASE[level]:
        o.put(slot, key, text, optional)


def _rule_accessories(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    ref = f.ref
    # Шапка
    if ref <= 0:
        o.put(HEAD, "hat", "шапку")
    elif ref <= 6:
        o.put(HEAD, "hat", "шапку", optional=True)
    # Перчатки
    if ref <= -12:
        o.put(HANDS, "gloves", "тёплые варежки или перчатки")
    elif ref <= 1:
        o.put(HANDS, "gloves", "перчатки")
    elif ref <= 6:
        o.put(HANDS, "gloves", "перчатки", optional=True)
    # Шарф: холод усиливается порывистым ветром
    if ref <= -8:
        o.put(NECK, "scarf", "шарф")
    elif ref <= -1 or (ref <= 4 and f.max_gust >= 10):
        o.put(NECK, "scarf", "шарф", optional=True)
    # Термобельё на границе: по желанию
    if level in (Thermal.COLD, Thermal.FROST) and ref <= 0.5 and not o.has(CORE, "base"):
        o.put(CORE, "base", "термобельё", optional=True)


def _rule_rain(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    if f.precip_kind not in ("rain", "mixed", "thunder") or not f.wet_hours:
        return
    strong = (f.max_precip_prob is not None and f.max_precip_prob >= 60) or f.rain_mm >= 1.0
    if f.max_gust >= 10:
        # В сильный ветер зонт бесполезен — куртка с капюшоном надёжнее.
        o.put(OUTER, "rain", "непромокаемую куртку с капюшоном", optional=not strong)
    else:
        o.put(EXTRAS, "umbrella", "зонт", optional=not strong)
    if f.rain_mm >= 2.0 or (strong and level <= Thermal.FRESH):
        shoes = "утеплённую непромокаемую обувь" if level <= Thermal.FROST else "непромокаемую обувь"
        o.put(FEET, "shoes", shoes)


def _rule_snow(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    if f.precip_kind not in ("snow", "mixed") or not f.wet_hours:
        return
    if level <= Thermal.COOL:
        shoes = "утеплённую непромокаемую обувь" if level <= Thermal.COLD else "непромокаемую обувь"
        o.put(FEET, "shoes", shoes)
    if not o.has(HEAD, "hat"):
        o.put(HEAD, "hood", "шапку или капюшон", optional=True)
    o.warnings.append("ожидается снег — на дорогах может быть скользко")


def _rule_ice(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    risky = f.precip_kind == "freezing" or (f.precip_kind in ("rain", "mixed") and -2 <= f.min_temp <= 1)
    if risky:
        o.warnings.append("возможна наледь — лучше обувь с нескользящей подошвой, ходи аккуратно")


def _rule_wind(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    gust = round_half_up(f.max_gust)
    if f.max_gust >= 14:
        o.warnings.append(f"порывы ветра до {gust} м/с — нужна куртка с ветрозащитой и капюшоном")
    elif f.max_gust >= 10 and level <= Thermal.FRESH:
        o.warnings.append(f"ветрено, порывы до {gust} м/с")


def _rule_thunder(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    if f.precip_kind == "thunder":
        o.warnings.append("возможна гроза — по возможности не оставайся на открытых местах")


def _rule_fog(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    if f.fog:
        o.warnings.append("туман — на дороге будь внимательнее")


def _rule_layers(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    if f.swing < 8 or level == Thermal.EXTREME:
        return
    swing = round_half_up(f.swing)
    if f.trend >= 4:
        o.warnings.append(f"к концу периода заметно потеплеет (перепад около {swing}°) — лучше одеться слоями")
    elif f.trend <= -4:
        o.warnings.append(f"к концу периода заметно похолодает (перепад около {swing}°) — лучше одеться слоями")
    else:
        o.warnings.append(f"перепад температуры около {swing}° — лучше одеться слоями")


def _rule_sun(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    if f.sunny and f.ref >= 14:
        o.put(EXTRAS, "sunglasses", "солнцезащитные очки", optional=True)
    if level >= Thermal.WARM:
        o.put(HEAD, "cap", "кепку или панаму", optional=level < Thermal.HOT)
    if level == Thermal.HOT:
        o.put(EXTRAS, "water", "воду", optional=False)


_RULES = (
    _rule_base, _rule_accessories, _rule_rain, _rule_snow, _rule_ice,
    _rule_wind, _rule_thunder, _rule_fog, _rule_layers, _rule_sun,
)


def _harmonise(o: Outfit, f: WeatherFacts, level: Thermal) -> None:
    """Убирает противоречия, которые могли возникнуть из-за сочетания правил."""
    if level >= Thermal.FRESH:
        for key in ("hat",):
            o.drop(HEAD, key)
        o.drop(HANDS)
        o.drop(NECK)
    if level >= Thermal.COOL:
        o.drop(CORE, "base")
    if level >= Thermal.WARM:
        o.drop(OUTER, "coat")
        o.drop(HEAD, "hood")
    if level <= Thermal.COLD:
        o.drop(HEAD, "cap")
        o.drop(EXTRAS, "sunglasses")
        o.drop(EXTRAS, "water")
    # Не «кепка» и «шапка» одновременно.
    if o.has(HEAD, "hat"):
        o.drop(HEAD, "cap")


def advise(facts: WeatherFacts) -> ClothingAdvice:
    level = thermal_level(facts.ref)
    outfit = Outfit()
    for rule in _RULES:
        rule(outfit, facts, level)
    _harmonise(outfit, facts, level)

    tags: set[str] = set()
    if facts.precip_kind:
        tags.add(facts.precip_kind)
    if facts.max_gust >= 10:
        tags.add("wind")
    if facts.swing >= 8:
        tags.add("layers")
    if any("наледь" in w for w in outfit.warnings):
        tags.add("ice")
    if facts.sunny and facts.ref >= 14:
        tags.add("sun")
    return ClothingAdvice(level=level, facts=facts, outfit=outfit, tags=frozenset(tags))


def describe_feels(facts: WeatherFacts) -> str:
    """Короткая служебная строка (для логов и тестов)."""
    return f"ref={fmt_temp(facts.ref)} level={thermal_level(facts.ref).name}"
