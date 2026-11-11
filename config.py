"""Конфигурация приложения: читается из переменных окружения (.env)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(RuntimeError):
    """Ошибка конфигурации — выводится пользователю при запуске."""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    bot_name: str
    default_city: str
    default_latitude: float
    default_longitude: float
    default_timezone: str
    default_send_time: str
    admin_id: int | None
    broadcast_rate: float
    database_path: Path
    weather_cache_ttl: int
    weather_timeout: float
    weather_retries: int
    log_level: str
    custom_emoji: dict[str, str]


def is_admin(user_id: int | None, admin_id: int | None) -> bool:
    """Единая серверная проверка прав администратора. Без ADMIN_ID администратора нет ни у кого."""
    return admin_id is not None and user_id is not None and user_id == admin_id


def _get_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом, получено: {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} должно быть в диапазоне {lo}..{hi}, получено: {value}")
    return value


def _get_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} должно быть в диапазоне {lo}..{hi}, получено: {value}")
    return value


def _parse_admin_id(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    if not raw.lstrip("-").isdigit() or int(raw) <= 0:
        raise ConfigError("ADMIN_ID должен быть положительным числом — ваш Telegram ID (узнать: @userinfobot)")
    return int(raw)


def _parse_custom_emoji(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"CUSTOM_EMOJI_IDS должно быть корректным JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("CUSTOM_EMOJI_IDS должно быть JSON-объектом {\"ключ\": \"id\"}")
    result: dict[str, str] = {}
    for key, value in data.items():
        value = str(value)
        if not value.isdigit():
            raise ConfigError(f"CUSTOM_EMOJI_IDS[{key!r}]: id должен состоять из цифр")
        result[str(key)] = value
    return result


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "Не задан BOT_TOKEN. Создайте файл .env (cp .env.example .env) и впишите токен от @BotFather."
        )
    if not _TOKEN_RE.match(token):
        raise ConfigError("BOT_TOKEN выглядит некорректно. Проверьте, что скопировали токен целиком.")

    timezone = os.getenv("DEFAULT_TIMEZONE", "Europe/Moscow").strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"DEFAULT_TIMEZONE: неизвестный часовой пояс {timezone!r}") from exc

    send_time = os.getenv("DEFAULT_SEND_TIME", "07:00").strip()
    if not _TIME_RE.match(send_time):
        raise ConfigError("DEFAULT_SEND_TIME должно быть в формате ЧЧ:ММ, например 07:00")

    db_path = Path(os.getenv("DATABASE_PATH", "data/atm_morning.db").strip())
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return Settings(
        bot_token=token,
        bot_name=os.getenv("BOT_NAME", "ATM Morning Assistant").strip() or "ATM Morning Assistant",
        default_city=os.getenv("DEFAULT_CITY", "Казань").strip() or "Казань",
        default_latitude=_get_float("DEFAULT_LATITUDE", 55.7887, -90, 90),
        default_longitude=_get_float("DEFAULT_LONGITUDE", 49.1221, -180, 180),
        default_timezone=timezone,
        default_send_time=send_time,
        admin_id=_parse_admin_id(os.getenv("ADMIN_ID", "")),
        broadcast_rate=_get_float("BROADCAST_RATE", 15, 1, 28),
        database_path=db_path,
        weather_cache_ttl=_get_int("WEATHER_CACHE_TTL", 600, 0, 3600),
        weather_timeout=_get_float("WEATHER_TIMEOUT", 10, 2, 60),
        weather_retries=_get_int("WEATHER_RETRIES", 2, 0, 5),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        custom_emoji=_parse_custom_emoji(os.getenv("CUSTOM_EMOJI_IDS", "")),
    )
