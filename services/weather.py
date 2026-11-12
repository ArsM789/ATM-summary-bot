"""Клиент Open-Meteo: прогноз и геокодинг. Кэш, тайм-ауты, повторы, ограничение параллелизма."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from services.weather_models import (
    CURRENT_PARAMS,
    DAILY_PARAMS,
    HOURLY_PARAMS,
    CityNotFoundError,
    GeoPlace,
    WeatherParseError,
    WeatherReport,
    WeatherUnavailableError,
    parse_geocoding,
    parse_report,
    search_queries,
)

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class WeatherService:
    def __init__(self, *, timeout: float, retries: int, cache_ttl: int, max_concurrent: int = 4) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retries = retries
        self._cache_ttl = cache_ttl
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[tuple[float, float, str], tuple[float, WeatherReport]] = {}
        self._locks: dict[tuple[float, float, str], asyncio.Lock] = {}

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, headers={"User-Agent": "ATM-Morning-Assistant/1.0"}
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------ прогноз ------------------------------

    async def get_report(self, city: str, latitude: float, longitude: float, tz_name: str) -> WeatherReport:
        """Прогноз для точки. Ответы кэшируются на cache_ttl секунд; параллельные запросы
        к одной точке объединяются (одна сетевая загрузка на всех)."""
        key = (round(latitude, 2), round(longitude, 2), tz_name)
        cached = self._cache_lookup(key)
        if cached:
            return self._with_city(cached, city)

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache_lookup(key)  # пока ждали замок, другой запрос мог всё загрузить
            if cached:
                return self._with_city(cached, city)
            params = {
                "latitude": f"{latitude:.4f}",
                "longitude": f"{longitude:.4f}",
                "current": CURRENT_PARAMS,
                "hourly": HOURLY_PARAMS,
                "daily": DAILY_PARAMS,
                "timezone": tz_name,
                "forecast_days": "3",
                "wind_speed_unit": "ms",
            }
            payload = await self._get_json(FORECAST_URL, params)
            report = parse_report(payload, city, tz_name)
            if self._cache_ttl > 0:
                self._cache[key] = (time.monotonic(), report)
            return report

    def _cache_lookup(self, key: tuple[float, float, str]) -> WeatherReport | None:
        entry = self._cache.get(key)
        if entry and time.monotonic() - entry[0] < self._cache_ttl:
            return entry[1]
        return None

    @staticmethod
    def _with_city(report: WeatherReport, city: str) -> WeatherReport:
        if report.city == city:
            return report
        from dataclasses import replace
        return replace(report, city=city)

    # ------------------------------ геокодинг ------------------------------

    async def search_city(self, text: str, count: int = 5) -> list[GeoPlace]:
        """Ищет город по вводу пользователя. Пробует несколько вариантов запроса (см. search_queries):
        «Казань, Россия» -> «Казань»; «Ростов на Дону» -> «Ростов-на-Дону»; «СПб» -> «Санкт-Петербург»."""
        queries, qualifier = search_queries(text)
        if not queries:
            raise CityNotFoundError(text)
        for query in queries:
            payload = await self._get_json(
                GEOCODING_URL, {"name": query, "count": "10", "language": "ru", "format": "json"}
            )
            places = parse_geocoding(payload, qualifier, limit=count)
            if places:
                return places
        raise CityNotFoundError(text)

    # ------------------------------ HTTP ------------------------------

    async def _get_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None

        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                async with self._semaphore:
                    async with self._session.get(url, params=params) as response:
                        if response.status in _RETRY_STATUSES:
                            raise WeatherUnavailableError(f"HTTP {response.status}")
                        if response.status != 200:
                            body = (await response.text())[:200]
                            # Остальные 4xx повторять бессмысленно.
                            raise WeatherParseError(f"HTTP {response.status}: {body}")
                        data = await response.json(content_type=None)
                if not isinstance(data, dict):
                    raise WeatherParseError("Ответ API не является JSON-объектом")
                return data
            except WeatherParseError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, WeatherUnavailableError) as exc:
                last_error = exc
                logger.warning(
                    "Open-Meteo: попытка %d/%d не удалась (%s)", attempt + 1, self._retries + 1, exc
                )
                if attempt < self._retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
        raise WeatherUnavailableError(f"Open-Meteo недоступен: {last_error!r}")
