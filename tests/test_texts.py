"""Тексты знакомства и уведомлений: полнота, лимиты Telegram, предупреждение о разработке."""
from __future__ import annotations

import re
import unittest

import _shims  # noqa: F401

from bot.texts import ASK_NAME, ASK_VARIANTS, CHANNEL_URL, DEVELOPMENT_NOTICE, DEVELOPMENT_SHORT, DOWNTIME_NOTICE, TOUR_PAGES


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


class TourTests(unittest.TestCase):
    def test_tour_covers_everything_requested(self):
        joined = " ".join(plain(p) for p in TOUR_PAGES)
        for topic in ("Сводка", "Моё расписание", "Мои события", "Настройки", "Сообщить об ошибке",   # назначение разделов
                      "18:00", "Утром",                                                                 # утренняя и вечерняя сводки
                      "Вставить неделю", "Математика — 2",                                              # настройка расписания
                      "напомин", "задач",                                                               # задачи и напоминания
                      "Время сводок", "часовой пояс"):                                                  # уведомления
            self.assertIn(topic, joined, topic)

    def test_last_page_is_the_development_warning(self):
        self.assertEqual(TOUR_PAGES[-1], DEVELOPMENT_NOTICE)
        for phrase in ("находится на этапе разработки", "временно не работать", "не удаляйте бота", CHANNEL_URL):
            self.assertIn(phrase, TOUR_PAGES[-1])
        self.assertIn("в разработке", DEVELOPMENT_SHORT)
        self.assertIn("нестабильно", DEVELOPMENT_SHORT)

    def test_pages_fit_telegram_and_html_is_balanced(self):
        self.assertGreaterEqual(len(TOUR_PAGES), 5)
        for page in TOUR_PAGES:
            self.assertLess(len(page), 3000)
            for tag in ("b", "i", "code"):
                self.assertEqual(page.count(f"<{tag}>"), page.count(f"</{tag}>"), (tag, page[:30]))

    def test_onboarding_prompts_keep_project_wording(self):
        self.assertTrue(ASK_NAME.startswith("Привет! Давай познакомимся."))
        self.assertIn("Софья, Софа, Соф", ASK_VARIANTS)


class DowntimeNoticeTests(unittest.TestCase):
    def test_notice(self):
        text = plain(DOWNTIME_NOTICE)
        for phrase in ("временно недоступен", "не стоит", CHANNEL_URL):
            self.assertIn(phrase, text)
        self.assertEqual(DOWNTIME_NOTICE.count("<b>"), DOWNTIME_NOTICE.count("</b>"))
        self.assertLess(len(DOWNTIME_NOTICE), 1000)               # безопасно и для подписи, и для обычного сообщения


if __name__ == "__main__":
    unittest.main()
