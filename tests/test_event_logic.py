"""События: правила повторения, календарь/DST/полночь, момент напоминаний, разбор ввода, оформление."""
from __future__ import annotations

import random
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import _shims  # noqa: F401

from database.models import UserEvent, weekdays_to_mask
from services.event_input import (
    parse_date_input, parse_duration, parse_month_day, parse_remind_minutes, parse_time_input, parse_title,
)
from services.event_schedule import (
    EventRule, crosses_midnight, effective_month_day, end_datetime, end_time_text, ends_next_day, find_due_reminders,
    is_finished_one_time, next_occurrence, occurrences_between, occurs_on, rule_from_event, start_datetime,
)
from services.event_text import (
    card_text, fields_from_event, format_amount, format_duration, format_remind, format_weekdays, preview_text,
    reminder_text, short_label, when_line,
)

MSK = "Europe/Moscow"


def ts(tz: str, y, mo, d, h=0, mi=0, s=0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(tz)).timestamp()


def event(**kw) -> UserEvent:
    base = dict(id=1, user_id=1, title="Урок английского", recurrence="weekly", weekdays_mask=weekdays_to_mask([1, 3]),
                month_day=None, event_date="2026-11-01", start_time="16:00", duration_min=120, remind_before_min=20,
                timezone=MSK, status="active", effective_from=0, created_at="")
    base.update(kw)
    return UserEvent(**base)


class RecurrenceTests(unittest.TestCase):
    def test_one_time(self):
        rule = EventRule("none", "16:00", 60, MSK, date(2026, 11, 12))
        self.assertTrue(occurs_on(rule, date(2026, 11, 12)))
        self.assertFalse(occurs_on(rule, date(2026, 11, 13)))
        self.assertFalse(occurs_on(rule, date(2026, 11, 11)))

    def test_daily_respects_anchor(self):
        rule = EventRule("daily", "07:00", 30, MSK, date(2026, 11, 10))
        self.assertFalse(occurs_on(rule, date(2026, 11, 9)))
        self.assertTrue(all(occurs_on(rule, date(2026, 11, 10) + timedelta(days=i)) for i in range(400)))

    def test_weekly_multiple_days(self):
        rule = EventRule("weekly", "16:00", 120, MSK, date(2026, 11, 1), weekdays=(1, 3))   # вт, чт
        days = [date(2026, 11, 3) + timedelta(days=i) for i in range(14)]
        self.assertEqual([d.weekday() for d in days if occurs_on(rule, d)], [1, 3, 1, 3])
        self.assertFalse(occurs_on(rule, date(2026, 10, 27)))     # до начала правила

    def test_monthly_clamps_to_last_day(self):
        rule = EventRule("monthly", "10:00", 60, MSK, date(2026, 1, 1), month_day=31)
        hits = [d for d in (date(2026, m, 1) + timedelta(days=i) for m in range(1, 13) for i in range(31)) if d.month and occurs_on(rule, d) ]
        by_month = {d.month: d.day for d in hits if d.year == 2026}
        self.assertEqual(by_month, {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31})

    def test_monthly_exactly_once_per_month_incl_leap_year(self):
        for md in (1, 15, 29, 30, 31, -1):
            rule = EventRule("monthly", "10:00", 60, MSK, date(2027, 1, 1), month_day=md)
            for year, month in ((2027, 2), (2028, 2), (2028, 3), (2027, 12)):
                days = [d for d in (date(year, month, 1) + timedelta(days=i) for i in range(31)) if d.month == month and occurs_on(rule, d)]
                self.assertEqual(len(days), 1, (md, year, month))
        self.assertEqual(effective_month_day(30, 2027, 2), 28)
        self.assertEqual(effective_month_day(30, 2028, 2), 29)           # високосный
        self.assertEqual(effective_month_day(31, 2026, 4), 30)
        self.assertEqual(effective_month_day(-1, 2026, 12), 31)

    def test_next_occurrence_crosses_month_and_year(self):
        rule = EventRule("monthly", "10:00", 60, MSK, date(2026, 12, 1), month_day=31)
        nxt = next_occurrence(rule, ts(MSK, 2026, 12, 31, 10, 1))        # декабрьское уже было
        self.assertEqual((nxt.year, nxt.month, nxt.day), (2027, 1, 31))
        nxt = next_occurrence(rule, ts(MSK, 2027, 1, 31, 10, 1))
        self.assertEqual((nxt.year, nxt.month, nxt.day), (2027, 2, 28))   # в феврале нет 31-го
        weekly = EventRule("weekly", "16:00", 60, MSK, date(2026, 12, 20), weekdays=(1, 3))
        nxt = next_occurrence(weekly, ts(MSK, 2026, 12, 31, 17, 0))       # чт 31.12 уже прошёл -> вт 5.01.2027
        self.assertEqual((nxt.year, nxt.month, nxt.day), (2027, 1, 5))

    def test_next_occurrence_one_time(self):
        rule = EventRule("none", "16:00", 60, MSK, date(2026, 11, 12))
        self.assertIsNotNone(next_occurrence(rule, ts(MSK, 2026, 11, 12, 15, 0)))
        self.assertIsNone(next_occurrence(rule, ts(MSK, 2026, 11, 12, 16, 1)))

    def test_occurrences_between(self):
        rule = EventRule("daily", "16:00", 60, MSK, date(2026, 11, 1))
        got = occurrences_between(rule, ts(MSK, 2026, 11, 10, 0), ts(MSK, 2026, 11, 12, 23, 59))
        self.assertEqual([s.day for s in got], [10, 11, 12])


class TimeEdgeCaseTests(unittest.TestCase):
    def test_end_time_and_midnight(self):
        self.assertEqual(end_time_text("16:00", 120), "18:00")
        self.assertEqual(end_time_text("23:30", 90), "01:00")
        self.assertTrue(ends_next_day("23:30", 90))
        self.assertFalse(ends_next_day("16:00", 120))
        self.assertFalse(ends_next_day("22:00", 119))
        rule = EventRule("none", "23:30", 90, MSK, date(2026, 11, 12))
        self.assertTrue(crosses_midnight(rule, date(2026, 11, 12)))
        start = start_datetime(rule, date(2026, 11, 12))
        end = end_datetime(start, 90)
        self.assertEqual((end.day, end.hour, end.minute), (13, 1, 0))
        self.assertEqual(start.date(), date(2026, 11, 12))               # вхождение относится к дате НАЧАЛА

    def test_midnight_event_reminder_is_on_previous_day(self):
        ev = event(recurrence="none", event_date="2026-11-13", start_time="00:10", remind_before_min=30, weekdays_mask=0)
        due = find_due_reminders([ev], ts(MSK, 2026, 11, 12, 23, 40, 5))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].local_date, "2026-11-13")                # ключ — дата начала

    def test_dst_spring_forward_duration_is_real_minutes(self):
        # Берлин 29.03.2026: 02:00 -> 03:00. 01:30 + 120 мин = 04:30 по местному
        berlin = ZoneInfo("Europe/Berlin")
        start = datetime(2026, 3, 29, 1, 30, tzinfo=berlin)
        end = end_datetime(start, 120)
        self.assertEqual((end.hour, end.minute), (4, 30))

    def test_dst_event_fires_once_per_day_around_transitions(self):
        rule = EventRule("daily", "16:00", 60, "Europe/Berlin", date(2026, 3, 20))
        for day in (date(2026, 3, 28), date(2026, 3, 29), date(2026, 3, 30), date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26)):
            lo = ts("Europe/Berlin", day.year, day.month, day.day, 0)
            hi = ts("Europe/Berlin", day.year, day.month, day.day, 23, 59)
            self.assertEqual(len(occurrences_between(rule, lo, hi)), 1, day)
        # интервал между соседними вхождениями в день перехода — 23 или 25 часов, но всегда по «16:00 на часах»
        a = start_datetime(rule, date(2026, 3, 28)); b = start_datetime(rule, date(2026, 3, 29))
        self.assertEqual(round((b.timestamp() - a.timestamp()) / 3600), 23)
        self.assertEqual((b.hour, b.minute), (16, 0))

    def test_nonexistent_and_ambiguous_local_time_do_not_crash_or_duplicate(self):
        gap = EventRule("daily", "02:30", 30, "Europe/Berlin", date(2026, 3, 20))      # 29.03 такого времени нет
        amb = EventRule("daily", "02:30", 30, "Europe/Berlin", date(2026, 10, 20))     # 25.10 бывает дважды
        for rule, day in ((gap, date(2026, 3, 29)), (amb, date(2026, 10, 25))):
            lo = ts("Europe/Berlin", day.year, day.month, day.day, 0)
            hi = lo + 26 * 3600
            self.assertEqual(len([s for s in occurrences_between(rule, lo, hi) if s.date() == day]), 1)

    def test_timezones_shift_the_instant(self):
        ev_msk = event(timezone="Europe/Moscow", recurrence="daily", event_date="2026-11-01")
        ev_vlad = event(id=2, timezone="Asia/Vladivostok", recurrence="daily", event_date="2026-11-01")
        now = ts("Europe/Moscow", 2026, 11, 12, 15, 40, 5)            # у москвича 15:40, у владивостокца 22:40
        due = find_due_reminders([ev_msk, ev_vlad], now)
        self.assertEqual([d.event.id for d in due], [1])
        now2 = ts("Asia/Vladivostok", 2026, 11, 12, 15, 40, 5)
        self.assertEqual([d.event.id for d in find_due_reminders([ev_msk, ev_vlad], now2)], [2])


class DueReminderTests(unittest.TestCase):
    def setUp(self):
        self.ev = event(recurrence="weekly", weekdays_mask=weekdays_to_mask([3]))   # четверг 16:00, за 20 мин; 12.11.2026 — четверг

    def due_at(self, h, mi, s=0, ev=None, **kw):
        return find_due_reminders([ev or self.ev], ts(MSK, 2026, 11, 12, h, mi, s), **kw)

    def test_window_boundaries(self):
        self.assertEqual(self.due_at(15, 39, 59), [])                       # ещё рано
        self.assertEqual(len(self.due_at(15, 40, 0)), 1)                    # ровно в момент
        self.assertEqual(len(self.due_at(15, 44, 59)), 1)                   # в пределах grace (5 мин)
        self.assertEqual(self.due_at(15, 45, 1), [])                        # устарело
        self.assertEqual(self.due_at(16, 0, 0), [])                         # событие началось
        self.assertEqual(self.due_at(17, 0, 0), [])                         # событие идёт / прошло

    def test_short_lead_never_reminds_after_the_event_has_started(self):
        # «за 1–3 минуты»: момент напоминания может оказаться внутри grace, когда событие уже началось
        for lead in (1, 2, 3):
            ev = event(weekdays_mask=weekdays_to_mask([3]), remind_before_min=lead)
            self.assertEqual(len(self.due_at(15, 59 - lead + 1, 30, ev=ev)), 1, lead)    # до начала — напоминаем
            self.assertEqual(self.due_at(16, 0, 0, ev=ev), [], lead)                    # в момент начала — уже нет
            self.assertEqual(self.due_at(16, 0, 30, ev=ev), [], lead)                   # после начала — нет
            self.assertEqual(self.due_at(16, 1, 30, ev=ev), [], lead)

    def test_other_weekday_no_reminder(self):
        self.assertEqual(find_due_reminders([self.ev], ts(MSK, 2026, 11, 13, 15, 40, 5)), [])   # пятница

    def test_inactive_and_effective_from(self):
        self.assertEqual(self.due_at(15, 40, 5, ev=event(weekdays_mask=weekdays_to_mask([3]), status="disabled")), [])
        # создано в 15:42: момент напоминания (15:40) ещё в пределах grace, но был ДО создания -> задним числом не шлём
        late = event(weekdays_mask=weekdays_to_mask([3]), effective_from=int(ts(MSK, 2026, 11, 12, 15, 42)))
        self.assertEqual(self.due_at(15, 43, ev=late), [])
        self.assertEqual(len(self.due_at(15, 43, ev=event(weekdays_mask=weekdays_to_mask([3]), effective_from=int(ts(MSK, 2026, 11, 12, 15, 39))))), 1)
        fresh = event(weekdays_mask=weekdays_to_mask([3]), effective_from=int(ts(MSK, 2026, 11, 12, 15, 30)))
        self.assertEqual(len(self.due_at(15, 41, ev=fresh)), 1)

    def test_long_lead_reaches_previous_day(self):
        ev = event(recurrence="none", event_date="2026-11-13", start_time="09:00", remind_before_min=1440, weekdays_mask=0)
        due = find_due_reminders([ev], ts(MSK, 2026, 11, 12, 9, 0, 10))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].start.day, 13)

    def test_several_events_same_time_and_slot_keys(self):
        a = event(id=1, recurrence="daily", event_date="2026-11-01")
        b = event(id=2, recurrence="daily", event_date="2026-11-01", title="Другое")
        due = find_due_reminders([a, b], ts(MSK, 2026, 11, 12, 15, 40, 5))
        self.assertEqual([d.event.id for d in due], [1, 2])
        self.assertEqual({d.slot for d in due}, {"1@16:00", "2@16:00"})

    def test_monthly_31_in_april(self):
        ev = event(recurrence="monthly", month_day=31, event_date="2026-01-01", weekdays_mask=0)
        self.assertEqual(len(find_due_reminders([ev], ts(MSK, 2026, 4, 30, 15, 40, 5))), 1)    # апрель: 30-го
        self.assertEqual(find_due_reminders([ev], ts(MSK, 2026, 4, 29, 15, 40, 5)), [])

    def test_is_finished_one_time(self):
        ev = event(recurrence="none", event_date="2026-11-12", weekdays_mask=0)     # 16:00–18:00
        self.assertFalse(is_finished_one_time(ev, ts(MSK, 2026, 11, 12, 18, 30)))
        self.assertTrue(is_finished_one_time(ev, ts(MSK, 2026, 11, 12, 19, 1)))
        self.assertFalse(is_finished_one_time(event(), ts(MSK, 2030, 1, 1)))        # повторяющиеся не «заканчиваются»


class InputTests(unittest.TestCase):
    def test_time(self):
        for text, expected in {"16:00": "16:00", "16.30": "16:30", "16 30": "16:30", "в 9": "09:00", "9:05": "09:05",
                               "16": "16:00", "9 ч": "09:00", "0:00": "00:00"}.items():
            self.assertEqual(parse_time_input(text), expected, text)
        for bad in ("24:00", "16:60", "abc", "", "16:5", "25"):
            self.assertIsNone(parse_time_input(bad), bad)

    def test_duration(self):
        good = {"2 часа": 120, "2ч": 120, "1.5 часа": 90, "1,5 часа": 90, "90 мин": 90, "90": 90, "2": 120, "45": 45,
                "10": 10, "1 час 30 минут": 90, "1 час и 30 минут": 90, "1:30": 90, "полчаса": 30, "полтора часа": 90,
                "два часа": 120, "час": 60, "1,5": 90, "24 часа": 1440}
        for text, minutes in good.items():
            self.assertEqual(parse_duration(text), (minutes, None), text)
        for bad in ("", "0", "25 часов", "abc", "3 дня", "-5", "1441"):
            minutes, error = parse_duration(bad)
            self.assertIsNone(minutes, bad)
            self.assertTrue(error, bad)

    def test_custom_remind_is_minutes(self):
        self.assertEqual(parse_remind_minutes("45"), (45, None))
        self.assertEqual(parse_remind_minutes("5"), (5, None))               # «5» — это 5 минут, а не 5 часов
        self.assertEqual(parse_remind_minutes("1 час"), (60, None))
        self.assertEqual(parse_remind_minutes("90 мин"), (90, None))
        for bad in ("0", "2000", "abc", ""):
            self.assertIsNone(parse_remind_minutes(bad)[0], bad)

    def test_date(self):
        today = date(2026, 11, 12)  # четверг
        cases = {"сегодня": today, "завтра": date(2026, 11, 13), "послезавтра": date(2026, 11, 14),
                 "в пятницу": date(2026, 11, 13), "четверг": today, "понедельник": date(2026, 11, 16),
                 "15.11": date(2026, 11, 15), "15.11.2026": date(2026, 11, 15), "15.11.26": date(2026, 11, 15),
                 "15 ноября": date(2026, 11, 15), "15 ноя": date(2026, 11, 15), "10.11": date(2027, 11, 10),
                 "5 января 2027": date(2027, 1, 5), "1 мая": date(2027, 5, 1)}
        for text, expected in cases.items():
            self.assertEqual(parse_date_input(text, today), (expected, None), text)
        for bad in ("", "абв", "31.02", "10.11.2026", "32.01", "0.5", "13.13"):
            value, error = parse_date_input(bad, today)
            self.assertIsNone(value, bad)
            self.assertTrue(error, bad)
        self.assertEqual(parse_date_input("29.02", date(2027, 11, 12))[0], date(2028, 2, 29))   # следующий високосный в пределах года

    def test_month_day_and_title(self):
        self.assertEqual(parse_month_day("15"), (15, None))
        self.assertEqual(parse_month_day("15-го"), (15, None))
        self.assertEqual(parse_month_day("последний день"), (-1, None))
        for bad in ("0", "32", "abc", ""):
            self.assertIsNone(parse_month_day(bad)[0], bad)
        self.assertEqual(parse_title("  урок   английского "), ("Урок английского", None))
        for bad in ("", "   ", "!!!", "я" * 61):
            self.assertIsNone(parse_title(bad)[0], bad)


class TextTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual([format_remind(m) for m in (1, 5, 20, 60, 90, 120, 300)],
                         ["За 1 минуту", "За 5 минут", "За 20 минут", "За 1 час", "За 1 час 30 минут", "За 2 часа", "За 5 часов"])
        self.assertEqual([format_duration(m) for m in (30, 60, 120, 90, 121)],
                         ["30 минут", "1 час", "2 часа", "1 час 30 минут", "2 часа 1 минута"])
        self.assertEqual(format_amount(21), "21 минуту")

    def test_weekdays(self):
        self.assertEqual(format_weekdays((1, 3)), "Вторник, четверг")
        self.assertEqual(format_weekdays((0, 1, 2, 3, 4)), "Будни")
        self.assertEqual(format_weekdays(tuple(range(7))), "Каждый день")
        self.assertEqual(format_weekdays((5, 6)), "Выходные")

    def test_preview_matches_spec(self):
        ev = event()
        text = preview_text(fields_from_event(ev), date(2026, 11, 12))
        for line in ("Название: <b>Урок английского</b>", "Дни: <b>Вторник, четверг</b>", "Время: <b>16:00–18:00</b>",
                     "Повторение: <b>Каждую неделю</b>", "Уведомление: <b>За 20 минут</b>"):
            self.assertIn(line, text)

    def test_when_lines(self):
        today = date(2026, 11, 12)
        self.assertEqual(when_line(fields_from_event(event(recurrence="none", event_date="2026-11-15", weekdays_mask=0)), today),
                         ("Дата", "15 ноября, воскресенье"))
        self.assertEqual(when_line(fields_from_event(event(recurrence="none", event_date="2027-01-05", weekdays_mask=0)), today)[1],
                         "5 января 2027, вторник")
        self.assertEqual(when_line(fields_from_event(event(recurrence="daily")), today), ("Дни", "Каждый день"))
        self.assertEqual(when_line(fields_from_event(event(recurrence="monthly", month_day=-1, weekdays_mask=0)), today),
                         ("Число", "Последний день месяца"))

    def test_midnight_crossing_marked(self):
        text = preview_text(fields_from_event(event(start_time="23:30", duration_min=90)))
        self.assertIn("23:30–01:00 (до следующего дня)", text)

    def test_html_escaped(self):
        ev = event(title="<b>Хак</b> & Ко")
        self.assertNotIn("<b>Хак</b>", preview_text(fields_from_event(ev)))
        self.assertIn("&lt;b&gt;Хак&lt;/b&gt;", preview_text(fields_from_event(ev)))
        self.assertLessEqual(len(short_label(fields_from_event(event(title="я" * 60)))), 40)

    def test_reminder_is_short_and_has_no_summary_content(self):
        tz = ZoneInfo(MSK)
        start = datetime(2026, 11, 12, 16, 0, tzinfo=tz); end = datetime(2026, 11, 12, 18, 0, tzinfo=tz)
        for seed in range(30):
            text = reminder_text("Соф", "Урок английского", 20, start, end, date(2026, 11, 12), random.Random(seed))
            self.assertIn("Через 20 минут", text)
            self.assertIn("<b>Урок английского</b>", text)
            self.assertTrue(text.rstrip().endswith("🕓 16:00–18:00"))
            self.assertIn("Соф", text)
            for foreign in ("°", "погод", "куртк", "урок" + "ов", "Доброе утро", "сводк"):
                self.assertNotIn(foreign, text)
            self.assertLess(len(text), 200)
        neutral = reminder_text("друг", "Тренировка", 5, start, end, date(2026, 11, 12), random.Random(1))
        self.assertNotIn("друг", neutral)
        self.assertIn("Привет", neutral) if "Привет" in neutral else self.assertIn("Напоминание", neutral)
        tomorrow = reminder_text("Соф", "Х", 1440, start, end, date(2026, 11, 11), random.Random(1))
        self.assertIn("Через 24 часа", tomorrow); self.assertIn("завтра", tomorrow)
        self.assertIn("&lt;", reminder_text("А", "<i>", 5, start, end, date(2026, 11, 12)))


if __name__ == "__main__":
    unittest.main()
