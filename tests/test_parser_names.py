"""Парсер расписания уроков и система имён."""
from __future__ import annotations

import random
import unittest

import _shims  # noqa: F401  (путь проекта и шимы)

from database.models import LessonItem, User, compress_lessons, expand_lessons
from services.names import InvalidNameError, choose_name, parse_single_name, parse_variants, pick_address
from services.schedule_parser import (
    day_total_error, format_day_editable, format_day_numbered, format_day_quote, parse_lessons, parse_numbers,
    parse_week, remove_lessons, total_lessons,
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
            "Математика — дваа": "число уроков",
            "Математика — 2.5": "число уроков",
            "Математика — 13": "максимум",
            "— 2": "пустое название",
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
        # каждый урок — своя строка (Математика занимает уроки 1 и 2)
        self.assertEqual(format_day_numbered(items), "1. Математика\n2. Математика\n3. Физика\n4. &lt;b&gt;")
        self.assertEqual(format_day_quote(items), "<blockquote>" + format_day_numbered(items) + "</blockquote>")
        # «редактируемый» вид парсится обратно в то же самое
        self.assertEqual(parse_lessons(format_day_editable(items)).items, [LessonItem("Математика", 2), LessonItem("Физика", 1), LessonItem("<b>", 1)])


class NewCountSemanticsTests(unittest.TestCase):
    """Предмет без числа — один урок; «- 2» — два отдельных урока."""

    def items(self, text):
        r = parse_lessons(text)
        self.assertEqual(r.errors, [], text)
        return [(i.subject, i.count) for i in r.items]

    def test_no_count_means_one(self):
        self.assertEqual(self.items("Геометрия"), [("Геометрия", 1)])
        self.assertEqual(self.items("геометрия\nФизика\nИстория"), [("Геометрия", 1), ("Физика", 1), ("История", 1)])

    def test_two_lessons_render_as_two_numbered_lines(self):
        # ровно сценарий пользователя: 4 урока до, потом «Геометрия - 2» -> уроки 5 и 6
        text = "Алгебра\nФизика\nИстория\nЛитература\nгеометрия - 2"
        r = parse_lessons(text)
        self.assertEqual(total_lessons(r.items), 6)
        self.assertEqual(format_day_numbered(r.items).splitlines()[-2:], ["5. Геометрия", "6. Геометрия"])
        self.assertNotIn("×", format_day_numbered(r.items))

    def test_flexible_count_forms(self):
        for form in ("Геометрия - 2", "Геометрия — 2", "Геометрия: 2", "Геометрия x2", "Геометрия х 2", "Геометрия ×2",
                     "Геометрия * 2", "Геометрия 2", "Геометрия - два", "Геометрия — 2 урока", "Геометрия -2"):
            self.assertEqual(self.items(form), [("Геометрия", 2)], form)

    def test_hyphen_inside_name_is_not_a_count(self):
        self.assertEqual(self.items("Физ-ра"), [("Физ-ра", 1)])
        self.assertEqual(self.items("Физ-ра - 2"), [("Физ-ра", 2)])
        self.assertEqual(self.items("Что-то там"), [("Что-то там", 1)])
        self.assertEqual(self.items("Информатика 10 класс"), [("Информатика 10 класс", 1)])

    def test_dangling_dash_is_ignored(self):
        self.assertEqual(self.items("Математика —"), [("Математика", 1)])
        self.assertEqual(self.items("Математика -"), [("Математика", 1)])

    def test_comma_separated(self):
        self.assertEqual(self.items("Физика, История - 2; Химия"), [("Физика", 1), ("История", 2), ("Химия", 1)])
        self.assertEqual(self.items("Математика - 2\nФизика, Химия"), [("Математика", 2), ("Физика", 1), ("Химия", 1)])

    def test_typos_are_reported_not_swallowed(self):
        for line in ("Математика - дваа", "Математика - ?", "Математика - 0", "Математика - -2", "123", "- 2"):
            r = parse_lessons(line)
            self.assertTrue(r.errors and not r.items, line)

    def test_day_heading_in_single_day_mode_is_rejected_with_hint(self):
        r = parse_lessons("Понедельник\nМатематика")
        self.assertEqual([i.subject for i in r.items], ["Математика"])
        self.assertIn("название дня", r.errors[0].reason)

    def test_editable_text_roundtrip(self):
        items = [LessonItem("Математика", 2), LessonItem("Физика", 1)]
        self.assertEqual(format_day_editable(items), "Математика — 2\nФизика")  # единичные без числа
        self.assertEqual(parse_lessons(format_day_editable(items)).items, items)

    def test_expand_and_compress(self):
        items = [LessonItem("Геометрия", 2), LessonItem("Физика", 1), LessonItem("Геометрия", 1)]
        lessons = expand_lessons(items)
        self.assertEqual(lessons, ["Геометрия", "Геометрия", "Физика", "Геометрия"])
        self.assertEqual(compress_lessons(lessons), items)
        self.assertEqual(compress_lessons(["А", "а", "Б"]), [LessonItem("А", 2), LessonItem("Б", 1)])


class WeekParserTests(unittest.TestCase):
    def test_full_week_paste(self):
        text = ("Понедельник\nМатематика - 2\nФизика\n\n"
                "Вторник: Алгебра, Геометрия, Биология - 2\n"
                "**Среда**\nХимия\n"
                "чт\nГеография - 2\n"
                "Пятница:\nФизкультура\n"
                "Суббота\nневажно\n")
        w = parse_week(text)
        self.assertEqual(w.errors, [])
        self.assertTrue(w.ok and w.weekend_skipped)
        self.assertEqual(sorted(w.days), [0, 1, 2, 3, 4])
        self.assertEqual([(i.subject, i.count) for i in w.days[0].items], [("Математика", 2), ("Физика", 1)])
        self.assertEqual([(i.subject, i.count) for i in w.days[1].items], [("Алгебра", 1), ("Геометрия", 1), ("Биология", 2)])
        self.assertEqual(w.days[3].items, [LessonItem("География", 2)])

    def test_partial_week_and_empty_day(self):
        w = parse_week("Вторник\nАлгебра\nСреда\n")
        self.assertEqual(sorted(w.days), [1])
        self.assertEqual(w.empty_days, [2])        # день без предметов не трогаем
        self.assertTrue(w.ok)

    def test_errors_are_located(self):
        w = parse_week("Математика\nПонедельник\nФизика - дваа\nВторник\nАлгебра\nПонедельник")
        self.assertTrue(any("вне дня" in e.reason for e in w.errors))
        self.assertTrue(any("дважды" in e.reason for e in w.errors))
        self.assertTrue(any(e.line_no == 3 for e in w.days[0].errors))
        self.assertFalse(w.ok)

    def test_day_total_limit(self):
        w = parse_week("Понедельник\nМатематика - 12\nФизика")
        self.assertTrue(any("максимум" in e.reason for e in w.days[0].errors))


class RemoveLessonsTests(unittest.TestCase):
    ITEMS = [LessonItem("Математика", 2), LessonItem("Физика", 1), LessonItem("История", 2)]   # 5 уроков

    def test_parse_numbers(self):
        self.assertEqual(parse_numbers("3", 5), ([3], None))
        self.assertEqual(parse_numbers("2, 4", 5), ([2, 4], None))
        self.assertEqual(parse_numbers("2 4;5", 5), ([2, 4, 5], None))
        self.assertEqual(parse_numbers("2-4", 5), ([2, 3, 4], None))
        self.assertEqual(parse_numbers("4-2", 5), ([2, 3, 4], None))
        for bad, part in (("", "номера"), ("abc", "не номер"), ("6", "нет"), ("0", "нет")):
            numbers, error = parse_numbers(bad, 5)
            self.assertEqual(numbers, [], bad)
            self.assertIn(part, error)

    def test_remove_splits_repeated_subject_and_keeps_order(self):
        # убрать 2-й урок: «Математика ×2» превращается в одну Математику
        self.assertEqual(remove_lessons(self.ITEMS, [2]),
                         [LessonItem("Математика", 1), LessonItem("Физика", 1), LessonItem("История", 2)])
        # убрать 3 (Физика): математики не склеиваются с историей
        self.assertEqual(remove_lessons(self.ITEMS, [3]),
                         [LessonItem("Математика", 2), LessonItem("История", 2)])
        self.assertEqual(remove_lessons(self.ITEMS, [1, 2, 3, 4, 5]), [])
        # после удаления «разделителя» одинаковые уроки сливаются в одну запись — отображение то же
        merged = remove_lessons([LessonItem("А", 1), LessonItem("Б", 1), LessonItem("А", 1)], [2])
        self.assertEqual(merged, [LessonItem("А", 2)])
        self.assertEqual(expand_lessons(merged), ["А", "А"])


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
