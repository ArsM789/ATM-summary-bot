"""Модели погодных данных, коды WMO и разбор ответа Open-Meteo. Без сетевых зависимостей."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo


class WeatherError(Exception):
    """Базовая ошибка погодного сервиса."""


class WeatherUnavailableError(WeatherError):
    """Погодный сервис недоступен (сеть, тайм-аут, 5xx, неразборчивый ответ)."""


class CityNotFoundError(WeatherError):
    """Город не найден в геокодере."""


class Condition(NamedTuple):
    text: str
    emoji: str
    kind: str  # clear | cloud | fog | drizzle | rain | freezing | snow | thunder | unknown


# Коды WMO, которые возвращает Open-Meteo (weather_code).
_WMO: dict[int, Condition] = {
    0: Condition("ясно", "☀️", "clear"),
    1: Condition("преимущественно ясно", "🌤", "clear"),
    2: Condition("переменная облачность", "⛅", "cloud"),
    3: Condition("пасмурно", "☁️", "cloud"),
    45: Condition("туман", "🌫", "fog"),
    48: Condition("туман с изморозью", "🌫", "fog"),
    51: Condition("лёгкая морось", "🌦", "drizzle"),
    53: Condition("морось", "🌦", "drizzle"),
    55: Condition("сильная морось", "🌧", "drizzle"),
    56: Condition("ледяная морось", "🌧", "freezing"),
    57: Condition("сильная ледяная морось", "🌧", "freezing"),
    61: Condition("небольшой дождь", "🌦", "rain"),
    63: Condition("дождь", "🌧", "rain"),
    65: Condition("сильный дождь", "🌧", "rain"),
    66: Condition("ледяной дождь", "🌧", "freezing"),
    67: Condition("сильный ледяной дождь", "🌧", "freezing"),
    71: Condition("небольшой снег", "🌨", "snow"),
    73: Condition("снег", "❄️", "snow"),
    75: Condition("сильный снег", "❄️", "snow"),
    77: Condition("снежная крупа", "❄️", "snow"),
    80: Condition("небольшие ливни", "🌦", "rain"),
    81: Condition("ливни", "🌧", "rain"),
    82: Condition("сильные ливни", "⛈", "rain"),
    85: Condition("небольшой снегопад", "🌨", "snow"),
    86: Condition("сильный снегопад", "❄️", "snow"),
    95: Condition("гроза", "⛈", "thunder"),
    96: Condition("гроза с градом", "⛈", "thunder"),
    99: Condition("сильная гроза с градом", "⛈", "thunder"),
}
_UNKNOWN = Condition("переменные условия", "🌡", "unknown")


def describe_code(code: int | None) -> Condition:
    if code is None:
        return _UNKNOWN
    return _WMO.get(int(code), _UNKNOWN)


PRECIP_KINDS = frozenset({"drizzle", "rain", "freezing", "snow", "thunder"})


@dataclass(frozen=True, slots=True)
class HourPoint:
    time: datetime
    temp: float
    apparent: float
    precip_prob: float | None  # %, None если модель не отдала
    precip: float              # мм
    snowfall: float            # см
    wind: float                # м/с
    gusts: float               # м/с
    code: int | None


@dataclass(frozen=True, slots=True)
class DayForecast:
    date: date
    code: int | None
    temp_max: float
    temp_min: float
    apparent_max: float | None
    apparent_min: float | None
    precip_prob_max: float | None
    precip_sum: float
    snowfall_sum: float
    wind_max: float | None
    gusts_max: float | None


@dataclass(frozen=True, slots=True)
class CurrentConditions:
    time: datetime
    temp: float
    apparent: float
    wind: float
    gusts: float
    precip: float
    code: int | None


@dataclass(frozen=True, slots=True)
class WeatherReport:
    city: str
    timezone: str
    current: CurrentConditions
    days: tuple[DayForecast, ...]
    hours: tuple[HourPoint, ...]
    fetched_at: datetime  # UTC

    def day(self, d: date) -> DayForecast | None:
        return next((item for item in self.days if item.date == d), None)

    def hours_between(self, start: datetime, end: datetime) -> list[HourPoint]:
        """Часы в полуинтервале [start, end)."""
        return [h for h in self.hours if start <= h.time < end]


class WeatherParseError(WeatherUnavailableError):
    """Ответ API получен, но не соответствует ожидаемому формату."""


# ----------------------------- разбор ответа Open-Meteo -----------------------------

CURRENT_PARAMS = (
    "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_gusts_10m"
)
HOURLY_PARAMS = (
    "temperature_2m,apparent_temperature,precipitation_probability,precipitation,snowfall,"
    "weather_code,wind_speed_10m,wind_gusts_10m"
)
DAILY_PARAMS = (
    "weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
    "apparent_temperature_min,precipitation_probability_max,precipitation_sum,snowfall_sum,"
    "wind_speed_10m_max,wind_gusts_10m_max"
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _code(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _column(block: dict[str, Any], name: str, size: int) -> list[Any]:
    values = block.get(name)
    if not isinstance(values, list):
        return [None] * size
    if len(values) < size:
        return values + [None] * (size - len(values))
    return values


def _parse_dt(value: str, tz: ZoneInfo) -> datetime:
    # С параметром timezone=<IANA> Open-Meteo отдаёт локальное время без смещения.
    return datetime.fromisoformat(value).replace(tzinfo=tz)


def parse_report(payload: dict[str, Any], city: str, tz_name: str) -> WeatherReport:
    """Превращает JSON Open-Meteo (/v1/forecast) в WeatherReport."""
    tz = ZoneInfo(tz_name)
    try:
        cur = payload["current"]
        current = CurrentConditions(
            time=_parse_dt(cur["time"], tz),
            temp=float(cur["temperature_2m"]),
            apparent=float(cur.get("apparent_temperature", cur["temperature_2m"])),
            wind=float(cur.get("wind_speed_10m") or 0.0),
            gusts=float(cur.get("wind_gusts_10m") or cur.get("wind_speed_10m") or 0.0),
            precip=float(cur.get("precipitation") or 0.0),
            code=_code(cur.get("weather_code")),
        )

        hourly = payload["hourly"]
        times = hourly["time"]
        n_hours = len(times)
        cols = {
            name: _column(hourly, name, n_hours)
            for name in (
                "temperature_2m", "apparent_temperature", "precipitation_probability",
                "precipitation", "snowfall", "weather_code", "wind_speed_10m", "wind_gusts_10m",
            )
        }
        hours: list[HourPoint] = []
        for i, stamp in enumerate(times):
            temp = _num(cols["temperature_2m"][i])
            if temp is None:
                continue  # час без температуры бесполезен
            apparent = _num(cols["apparent_temperature"][i])
            wind = _num(cols["wind_speed_10m"][i]) or 0.0
            hours.append(
                HourPoint(
                    time=_parse_dt(stamp, tz),
                    temp=temp,
                    apparent=apparent if apparent is not None else temp,
                    precip_prob=_num(cols["precipitation_probability"][i]),
                    precip=_num(cols["precipitation"][i]) or 0.0,
                    snowfall=_num(cols["snowfall"][i]) or 0.0,
                    wind=wind,
                    gusts=_num(cols["wind_gusts_10m"][i]) or wind,
                    code=_code(cols["weather_code"][i]),
                )
            )

        daily = payload["daily"]
        dates = daily["time"]
        n_days = len(dates)
        dcols = {
            name: _column(daily, name, n_days)
            for name in (
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "apparent_temperature_max", "apparent_temperature_min",
                "precipitation_probability_max", "precipitation_sum", "snowfall_sum",
                "wind_speed_10m_max", "wind_gusts_10m_max",
            )
        }
        days: list[DayForecast] = []
        for i, stamp in enumerate(dates):
            t_max = _num(dcols["temperature_2m_max"][i])
            t_min = _num(dcols["temperature_2m_min"][i])
            if t_max is None or t_min is None:
                continue
            days.append(
                DayForecast(
                    date=date.fromisoformat(stamp),
                    code=_code(dcols["weather_code"][i]),
                    temp_max=t_max,
                    temp_min=t_min,
                    apparent_max=_num(dcols["apparent_temperature_max"][i]),
                    apparent_min=_num(dcols["apparent_temperature_min"][i]),
                    precip_prob_max=_num(dcols["precipitation_probability_max"][i]),
                    precip_sum=_num(dcols["precipitation_sum"][i]) or 0.0,
                    snowfall_sum=_num(dcols["snowfall_sum"][i]) or 0.0,
                    wind_max=_num(dcols["wind_speed_10m_max"][i]),
                    gusts_max=_num(dcols["wind_gusts_10m_max"][i]),
                )
            )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise WeatherParseError(f"Неожиданный формат ответа Open-Meteo: {exc!r}") from exc

    if not hours or not days:
        raise WeatherParseError("В ответе Open-Meteo нет почасовых или дневных данных")

    return WeatherReport(
        city=city,
        timezone=tz_name,
        current=current,
        days=tuple(days),
        hours=tuple(hours),
        fetched_at=datetime.now(timezone.utc),
    )


# ----------------------------- геокодинг (поиск города) -----------------------------

@dataclass(frozen=True, slots=True)
class GeoPlace:
    name: str
    latitude: float
    longitude: float
    country: str | None
    region: str | None
    timezone: str | None
    country_code: str | None = None

    @property
    def label(self) -> str:
        """«Казань, Татарстан, Россия» — чтобы одноимённые города различались в списке выбора."""
        parts = [self.name]
        if self.region and self.region.casefold() != self.name.casefold():
            parts.append(self.region)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


# Сокращения и разговорные названия, которых нет в геокодере.
CITY_ALIASES = {
    "спб": "Санкт-Петербург", "питер": "Санкт-Петербург", "санкт петербург": "Санкт-Петербург",
    "мск": "Москва", "екб": "Екатеринбург", "ека": "Екатеринбург", "нск": "Новосибирск",
    "нижний": "Нижний Новгород", "н новгород": "Нижний Новгород", "нн": "Нижний Новгород",
    "ростов": "Ростов-на-Дону", "владик": "Владивосток", "челны": "Набережные Челны",
    "самара": "Самара", "ёбург": "Екатеринбург",
}
_PLACE_PREFIX_RE = re.compile(r"^(?:г\.?|город|гор\.?|пгт\.?|пос\.?|поселок|посёлок|с\.|село|д\.|деревня)\s+", re.IGNORECASE)


def search_queries(text: str) -> tuple[list[str], str | None]:
    """Превращает ввод пользователя в список запросов к геокодеру (пробуются по очереди) и «уточнение».

    «г. Казань» -> «Казань»;  «Казань, Россия» -> сначала «Казань, Россия» (API понимает страну/регион
    после запятой), затем просто «Казань» с последующей фильтрацией; «СПб» -> «Санкт-Петербург»;
    «Ростов на Дону» -> ещё и «Ростов-на-Дону». Пустой список — ввод непригоден."""
    cleaned = " ".join((text or "").replace("\u00a0", " ").split()).strip(" .;")
    cleaned = _PLACE_PREFIX_RE.sub("", cleaned)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if not parts:
        return [], None
    name, qualifier = parts[0], (parts[1] if len(parts) > 1 else None)

    alias_key = re.sub(r"[.\-]", " ", name).casefold()
    alias_key = " ".join(alias_key.split())
    name = CITY_ALIASES.get(alias_key, name)
    if len(name) < 2:
        return [], None

    queries = [f"{name}, {qualifier}" if qualifier else name]
    if qualifier:
        queries.append(name)
    if " " in name and "-" not in name:
        queries.append(name.replace(" ", "-"))
    unique: list[str] = []
    for q in queries:
        if q not in unique:
            unique.append(q)
    return unique, qualifier


def parse_geocoding(payload: dict, qualifier: str | None = None, limit: int = 5) -> list[GeoPlace]:
    """Разбор ответа /v1/search: пропускает битые записи, убирает дубли, при наличии уточнения
    («Россия», «Татарстан», «RU») оставляет подходящие. Порядок — как у геокодера (по релевантности)."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    places: list[GeoPlace] = []
    seen: set[tuple] = set()
    for item in results:
        try:
            name = str(item["name"]).strip()
            lat, lon = float(item["latitude"]), float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not name or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        key_names = (name.casefold(), str(item.get("admin1") or "").casefold(), str(item.get("country_code") or "").casefold())
        key_coords = (round(lat, 1), round(lon, 1), name.casefold())
        if key_names in seen or key_coords in seen:
            continue
        seen.add(key_names)
        seen.add(key_coords)
        tz = item.get("timezone")
        places.append(GeoPlace(
            name=name, latitude=lat, longitude=lon,
            country=item.get("country") or None, region=item.get("admin1") or None,
            timezone=tz if isinstance(tz, str) and tz else None,
            country_code=item.get("country_code") or None,
        ))
    if qualifier:
        q = qualifier.casefold()
        matching = [
            p for p in places
            if any(q in (value or "").casefold() for value in (p.country, p.region, p.country_code))
        ]
        if matching:
            places = matching
    return places[:limit]
