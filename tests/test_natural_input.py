"""Естественный ввод: намерения из обычного текста и быстрое создание события одной фразой."""
from __future__ import annotations

import unittest
from datetime import date

import _shims  # noqa: F401

from database.models import mask_to_weekdays
from services.intents import Intent, detect_intent, is_cancel, normalize
from services.quick_event import apply_quick_to_draft, parse_quick_event

TODAY = date(2026, 11, 12)  # четверг


class IntentTests(unittest.TestCase):
    def test_sections(self):
        cases = {
            Intent.SUMMARY: ["сводка", "Что надеть?", "какая погода", "погода на завтра", "прогноз", "во что одеться"],
            Intent.LESSONS: ["расписание", "Покажи расписание", "какие уроки", "уроки на завтра"],
            Intent.EVENTS: ["мои события", "события", "задачи", "мои задачи", "напоминания"],
            Intent.SETTINGS: ["настройки", "часовой пояс", "поменять город"],
            Intent.FEEDBACK: ["у меня ошибка", "хочу оставить отзыв", "баг", "бот не работает"],
            Intent.HELP: ["помощь", "как пользоваться", "что ты умеешь", "help"],
            Intent.HOME: ["меню", "в главное меню", "домой"],
            Intent.CANCEL: ["отмена", "Отмена!", "cancel", "стоп", "не надо"],
            Intent.ADD_EVENT: ["добавь событие урок в 16:00", "хочу добавить событие", "напомни про врача завтра в 9", "создай задачу"],
        }
        for intent, phrases in cases.items():
            for phrase in phrases:
                self.assertEqual(detect_intent(phrase), intent, phrase)

    def test_unknown_and_edge(self):
        for phrase in ("привет", "как дела", "", "   ", "!!!", "я " * 20):
            self.assertIsNone(detect_intent(phrase), phrase)
        self.assertEqual(normalize("  Ёлка,  ПРИВЕТ!! "), "елка привет")
        self.assertTrue(is_cancel("  ОТМЕНА "))
        self.assertFalse(is_cancel("отмена урока"))                      # только отдельное слово, а не фраза


class QuickEventTests(unittest.TestCase):
    def parse(self, text):
        return parse_quick_event(text, TODAY)

    def test_spec_example(self):
        q = self.parse("урок английского по вторникам и четвергам в 16:00 на 2 часа")
        self.assertEqual((q.title, q.recurrence, q.weekdays, q.start_time, q.duration_min),
                         ("Урок английского", "weekly", (1, 3), "16:00", 120))

    def test_one_time_with_dates(self):
        q = self.parse("Созвон завтра в 15:30")
        self.assertEqual((q.title, q.recurrence, q.event_date, q.start_time), ("Созвон", "none", date(2026, 11, 13), "15:30"))
        q = self.parse("поход в кино в субботу в 20:00")
        self.assertEqual((q.title, q.event_date, q.start_time), ("Поход в кино", date(2026, 11, 14), "20:00"))
        q = self.parse("день рождения мамы 15.12 в 18:00")
        self.assertEqual((q.title, q.event_date, q.start_time), ("День рождения мамы", date(2026, 12, 15), "18:00"))
        q = self.parse("запиши врач 20 ноября в 9:00")
        self.assertEqual((q.title, q.event_date, q.start_time), ("Врач", date(2026, 11, 20), "09:00"))

    def test_recurring_kinds(self):
        q = self.parse("тренировка каждый день в 7 утра")
        self.assertEqual((q.title, q.recurrence, q.start_time), ("Тренировка", "daily", "07:00"))
        q = self.parse("йога каждую среду и пятницу в 19:30 на 1 час 15 минут")
        self.assertEqual((q.recurrence, q.weekdays, q.duration_min), ("weekly", (2, 4), 75))
        q = self.parse("оплатить кружок 15 числа каждый месяц в 10:00")
        self.assertEqual((q.title, q.recurrence, q.month_day), ("Оплатить кружок", "monthly", 15))
        q = self.parse("отчёт в последний день месяца в 12:00")
        self.assertEqual((q.recurrence, q.month_day, q.start_time), ("monthly", -1, "12:00"))
        q = self.parse("репетитор по пн, ср и пт в 17:00")
        self.assertEqual((q.recurrence, q.weekdays), ("weekly", (0, 2, 4)))

    def test_reminder_and_duration(self):
        q = self.parse("напомни про врача 20 ноября в 9:00 за 30 минут")
        self.assertEqual((q.title, q.remind_min, q.event_date), ("Про врача", 30, date(2026, 11, 20)))   # формулировка пользователя
        q = self.parse("футбол в пятницу в 18:00 на полтора часа")
        self.assertEqual((q.duration_min, q.start_time), (90, "18:00"))
        q = self.parse("лекция завтра в 10:00 на 90 минут за 1 час")
        self.assertEqual((q.duration_min, q.remind_min), (90, 60))

    def test_time_forms_and_no_false_matches(self):
        self.assertEqual(self.parse("встреча в 12").start_time, "12:00")
        self.assertEqual(self.parse("встреча в 16 часов").start_time, "16:00")
        self.assertEqual(self.parse("ужин в 7 вечера").start_time, "19:00")
        self.assertEqual(self.parse("встреча в 16.30").start_time, "16:30")
        q = self.parse("встреча 15 ноября")                                 # «15» — дата, а не время
        self.assertEqual((q.start_time, q.event_date), (None, date(2026, 11, 15)))
        q = self.parse("концерт на 15 ноября в 19:00")
        self.assertEqual((q.event_date, q.start_time, q.duration_min), (date(2026, 11, 15), "19:00", None))   # «на 15 ноября» — не длительность
        self.assertIsNone(self.parse("встреча в 25:00").start_time)

    def test_nothing_found_means_plain_title(self):
        q = self.parse("математика репетитор")
        self.assertEqual(q.title, "Математика репетитор")
        self.assertFalse(q.found_anything)
        self.assertFalse(self.parse("").found_anything)
        self.assertIsNone(self.parse("в 16:00").title)

    def test_original_case_preserved(self):
        self.assertEqual(self.parse("Урок IELTS по вторникам в 16:00").title, "Урок IELTS")

    def test_apply_to_draft_complete_phrase_goes_straight_to_preview(self):
        base = {"title": "", "recurrence": "none", "weekdays_mask": 0, "month_day": None, "event_date": None,
                "start_time": "", "duration_min": 60, "remind_before_min": 15}
        draft, chain = apply_quick_to_draft(base, self.parse("урок английского по вторникам и четвергам в 16:00 на 2 часа"), TODAY)
        self.assertEqual(chain, [])                                           # ничего спрашивать не нужно
        self.assertEqual((draft["title"], draft["recurrence"], mask_to_weekdays(draft["weekdays_mask"]), draft["start_time"],
                          draft["duration_min"], draft["remind_before_min"], draft["event_date"]),
                         ("Урок английского", "weekly", (1, 3), "16:00", 120, 15, "2026-11-12"))   # напоминание — по умолчанию пользователя

    def test_apply_to_draft_asks_only_for_missing_parts(self):
        base = {"title": "", "recurrence": "none", "weekdays_mask": 0, "month_day": None, "event_date": None,
                "start_time": "", "duration_min": 60, "remind_before_min": 20}
        cases = {
            "математика репетитор": ["repeat", "when", "time"],
            "встреча в 12": ["repeat", "when"],                                  # название «Встреча» найдено, время тоже
            "в 16:00 по вторникам": ["title"],
            "созвон завтра": ["time"],
            "тренировка каждый день": ["time"],
            "йога каждую неделю в 19:00": ["when"],                              # когда именно — неизвестно, спросим только это
            "оплата каждый месяц в 10:00": ["when"],
        }
        for text, expected in cases.items():
            _, chain = apply_quick_to_draft(base, self.parse(text), TODAY)
            self.assertEqual(chain, expected, text)

    def test_apply_does_not_overwrite_existing_defaults_with_nothing(self):
        base = {"title": "Старое", "recurrence": "weekly", "weekdays_mask": 4, "month_day": None, "event_date": "2026-11-01",
                "start_time": "09:00", "duration_min": 45, "remind_before_min": 10}
        draft, chain = apply_quick_to_draft(base, self.parse("мусор"), TODAY)
        self.assertEqual((draft["duration_min"], draft["remind_before_min"], draft["start_time"]), (45, 10, "09:00"))


if __name__ == "__main__":
    unittest.main()
