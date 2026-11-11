"""Подключает шимы ТОЛЬКО если настоящих пакетов нет (в окружении с установленными aiogram/aiosqlite
используются они). Импортировать первым в тестах, которым нужны БД или исключения Telegram."""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

USING_SHIMS = []
for _name in ("aiosqlite", "dotenv", "aiogram"):
    try:
        importlib.import_module(_name)
    except ImportError:
        USING_SHIMS.append(_name)
if USING_SHIMS:
    sys.path.append(str(Path(__file__).resolve().parent / "shims"))
