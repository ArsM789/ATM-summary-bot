"""Иерархия исключений как в aiogram 3 (те же базовые классы и сигнатуры с method/message/retry_after)."""


class TelegramAPIError(Exception):
    def __init__(self, method=None, message=""):
        super().__init__(message)
        self.method = method
        self.message = message


class TelegramNetworkError(TelegramAPIError):
    pass


class TelegramRetryAfter(TelegramAPIError):
    def __init__(self, method=None, message="", retry_after=0):
        super().__init__(method, message)
        self.retry_after = retry_after


class TelegramBadRequest(TelegramAPIError):
    pass


class TelegramForbiddenError(TelegramAPIError):
    pass


class TelegramServerError(TelegramAPIError):
    pass


class TelegramUnauthorizedError(TelegramAPIError):
    pass
