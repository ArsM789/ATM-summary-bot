"""Парсер расписания уроков и система имён."""
from __future__ import annotations

import random
import unittest

import _shims  # noqa: F401  (путь проекта и шимы)

from database.models import LessonItem, User
from services.names import InvalidNameError, choose_name, parse_single_name, parse_variants, pick_address
from services.schedule_parser import (
    day_total_error, format_day_editable, format_day_numbered, parse_lessons, total_lessons,
)


class LessonParserTests(unittest.TestCase):
    def test_example_from_spec(self):
        text = "Математика — 2\nРусский язык — 1\nФизика — 1\nИстория — 1\nАнглийский — 2"
        r = parse_lessons(text)
        self.assertTrue(r.ok)
        self.assertEqual([(i.subject, i.count) for i in r.items],
                         [("Математика", 2), ("Русский язык", 1), ("Физика", 1), ("История", 1), ("Английский", 2)])
        self.assertEqual(total_lessons(r.items), 7)

    def test_all_dash_kinds_equivalent(self):
        for dash in ("-", "–", "—", "−", "‒", "―"):
            r = parse_lessons(f"Математика {dash} 2")
            self.assertEqual(r.items, [LessonItem("Математика", 2)], dash)
        self.assertEqual(parse_lessons("Математика - 2").items, parse_lessons("Математика — 2").items)
        self.assertEqual(parse_lessons("Физика—1").items, [LessonItem("Физика", 1)])  # без пробелов

    def test_extra_spaces_and_blank_lines(self):
        r = parse_lessons("\n   математика    -   3  \n\n  Физ-ра — 2 \n")
        self.assertEqual(r.items, [LessonItem("Математика", 3), LessonItem("Физ-ра", 2)])  # дефис внутри названия
        self.assertFalse(r.errors)

    def test_order_preserved_and_duplicates_kept(self):
        r = parse_lessons("Алгебра — 1\nГеометрия — 1\nАлгебра — 1")
        self.assertEqual([i.subject for i in r.items], ["Алгебра", "Геометрия", "Алгебра"])

    def test_list_markers_stripped(self):
        r = parse_lessons("1. Математика — 2\n• Физика — 1")
        self.assertEqual([i.subject for i in r.items], ["Математика", "Физика"])

    def test_invalid_counts_have_specific_errors(self):
        cases = {
            "Математика — 0": "больше нуля",
            "Математика — -2": "положительным",
            "Математика — два": "число уроков",
            "Математика — 2.5": "число уроков",
            "Математика — 13": "максимум",
            "Математика": "нет тире",
            "— 2": "пустое название",
            "Математика —": "число уроков",
            "М" * 41 + " — 1": "длиннее",
        }
        for line, reason in cases.items():
            r = parse_lessons(line)
            self.assertEqual(len(r.errors), 1, line)
            self.assertIn(reason, r.errors[0].reason, line)
            self.assertEqual(r.errors[0].line_no, 1)
            self.assertFalse(r.items, line)

    def test_good_lines_survive_bad_ones(self):
        r = parse_lessons("Математика — 2\nФизика — ноль\nИстория — 1")
        self.assertEqual([i.subject for i in r.items], ["Математика", "История"])
        self.assertEqual([e.line_no for e in r.errors], [2])
        self.assertFalse(r.ok)

    def test_empty_input(self):
        for text in ("", "   \n \n"):
            r = parse_lessons(text)
            self.assertEqual((r.items, r.errors), ([], []))
            self.assertFalse(r.ok)

    def test_too_many_subjects_and_lessons(self):
        r = parse_lessons("\n".join(f"Предмет{i} — 1" for i in range(16)))
        self.assertTrue(any("слишком много предметов" in e.reason for e in r.errors))
        items = [LessonItem("А", 6), LessonItem("Б", 6)]
        self.assertIsNone(day_total_error(items))
        self.assertIsNotNone(day_total_error(items + [LessonItem("В", 1)]))

    def test_formatting(self):
        items = [LessonItem("Математика", 2), LessonItem("Физика", 1), LessonItem("<b>", 1)]
        self.assertEqual(format_day_numbered(items), "1. Математика × 2\n2. Физика\n3. &lt;b&gt;")
        # «редактируемый» вид парсится обратно в то же самое
        self.assertEqual(parse_lessons(format_day_editable(items)).items, [LessonItem("Математика", 2), LessonItem("Физика", 1), LessonItem("<b>", 1)])


class NameTests(unittest.TestCase):
    def test_single_name(self):
        self.assertEqual(parse_single_name("  арсений "), "Арсений")
        self.assertEqual(parse_single_name("Анна-Мария"), "Анна-Мария")
        for bad in ("", "   ", "A1", "Арсений, Сеня", "🙂", "Имя<script>", "а" * 31):
            with self.assertRaises(InvalidNameError, msg=bad):
                parse_single_name(bad)

    def test_variants_parsing(self):
        self.assertEqual(parse_variants("Арсений, Сеня, Арсен", "Арсений"), ["Арсений", "Сеня", "Арсен"])
        self.assertEqual(parse_variants("сеня; арсен\nсеня", "Арсений"), ["Арсений", "Сеня", "Арсен"])  # основное имя добавлено первым
        self.assertEqual(parse_variants("Арсений", "Арсений"), ["Арсений"])  # «только один вариант»
        self.assertEqual(parse_variants("арсений, АРСЕНИЙ", "Арсений"), ["Арсений"])  # дубликаты без учёта регистра
        for bad in ("", " , ", "Сеня, 123"):
            with self.assertRaises(InvalidNameError, msg=bad):
                parse_variants(bad, "Арсений")
        with self.assertRaises(InvalidNameError):
            parse_variants("а, б, в, г, д, е, ж", "Арсений")

    def test_no_immediate_repeats(self):
        rng = random.Random(1)
        variants = ["Арсений", "Сеня", "Арсен"]
        last, seq = None, []
        for _ in range(500):
            last = choose_name(variants, last, rng)
            seq.append(last)
        self.assertTrue(all(a != b for a, b in zip(seq, seq[1:])))
        self.assertEqual(set(seq), set(variants))  # все варианты реально используются

    def test_single_variant_and_empty(self):
        rng = random.Random(1)
        self.assertEqual(choose_name(["Анна"], "Анна", rng), "Анна")  # повтор неизбежен, но не ломается
        self.assertEqual(choose_name([], None, rng, fallback="друг"), "друг")

    def test_pick_address_for_user_without_variants(self):
        user = User(1, "Анна", "К", 1.0, 1.0, "UTC", True)  # «старый» пользователь: вариантов нет
        self.assertEqual(pick_address(user, random.Random(1)), "Анна")
        user2 = User(1, "Анна", "К", 1.0, 1.0, "UTC", True, name_variants=("Анна", "Аня"), last_used_name="Аня")
        self.assertEqual(pick_address(user2, random.Random(1)), "Анна")


if __name__ == "__main__":
    unittest.main()
