"""Шим python-dotenv для тестов: .env в тестах не читаем, переменные задаёт сам тест."""


def load_dotenv(*args, **kwargs):
    return False
