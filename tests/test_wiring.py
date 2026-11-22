"""Проводка проекта (статически, без запуска aiogram): импорты, обработчики кнопок, аргументы хендлеров, порядок роутеров.

Хендлеры aiogram нельзя исполнить без самого aiogram и Telegram, зато именно на стыках модулей чаще всего прячутся
ошибки (опечатка в импорте, кнопка без обработчика, перекрытие имени). Эти тесты ловят такой класс ошибок."""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_FILES = sorted(p for p in ROOT.rglob("*.py") if "tests" not in p.parts and ".venv" not in p.parts and "shims" not in p.parts)
HANDLERS = sorted((ROOT / "bot" / "handlers").glob("*.py"))
PROJECT_PACKAGES = {"bot", "services", "database", "utils", "config"}


def tree_of(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def module_path(dotted: str) -> Path | None:
    base = ROOT.joinpath(*dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def top_level_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in tree_of(path).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}   # в т.ч. «A, B = …»
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.If, ast.Try)):      # условные определения/импорты
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    names |= {(a.asname or a.name).split(".")[0] for a in sub.names}
    return names


class ImportTests(unittest.TestCase):
    def test_all_files_compile(self):
        for path in PY_FILES:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_every_project_import_resolves(self):
        problems = []
        for path in PY_FILES:
            for node in ast.walk(tree_of(path)):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in PROJECT_PACKAGES:
                    target = module_path(node.module)
                    if target is None:
                        problems.append(f"{path.relative_to(ROOT)}: нет модуля {node.module}")
                        continue
                    defined = top_level_names(target)
                    for alias in node.names:
                        sub_module = module_path(f"{node.module}.{alias.name}")   # from bot.handlers import start
                        if alias.name not in defined and sub_module is None:
                            problems.append(f"{path.relative_to(ROOT)}: {node.module} не содержит {alias.name}")
        self.assertEqual(problems, [])

    def test_no_dead_modules_left(self):
        self.assertFalse(list((ROOT / "services").glob("tomorrow_brief_*.py")))

    def test_imported_names_are_not_shadowed_inside_functions(self):
        bad = []
        for path in PY_FILES:
            tree = tree_of(path)
            imported = set()
            for n in tree.body:
                if isinstance(n, ast.ImportFrom):
                    imported |= {(a.asname or a.name) for a in n.names}
                elif isinstance(n, ast.Import):
                    imported |= {(a.asname or a.name).split(".")[0] for a in n.names}
            imported.discard("annotations")
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = {a.arg for a in fn.args.args}
                    assigned = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
                    bad += [f"{path.relative_to(ROOT)}:{fn.name}:{name}" for name in (params | assigned) & imported]
        self.assertEqual(bad, [])


class HandlerTests(unittest.TestCase):
    @staticmethod
    def injectable() -> set[str]:
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        data = re.search(r"dp\.workflow_data\.update\((.*?)\n    \)", main_src, re.S).group(1)
        return set(re.findall(r"(\w+)=", data)) | {"message", "callback", "query", "state", "bot", "user", "raw_state", "event"}

    @staticmethod
    def handler_functions():
        for path in HANDLERS:
            for node in ast.walk(tree_of(path)):
                if isinstance(node, ast.AsyncFunctionDef) and any(ast.unparse(d).startswith("router.") for d in node.decorator_list):
                    yield path, node

    def test_handler_arguments_are_injectable(self):
        allowed = self.injectable()
        bad = [f"{p.name}:{fn.name}:{a.arg}" for p, fn in self.handler_functions() for a in fn.args.args if a.arg not in allowed]
        self.assertEqual(bad, [])

    def test_workflow_data_matches_what_main_builds(self):
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        for name in ("settings", "users", "weekly", "schedules", "feedback", "weather", "summaries", "broadcast",
                     "guard", "events", "event_repo", "user_events"):
            self.assertIn(f"{name}=", re.search(r"dp\.workflow_data\.update\((.*?)\n    \)", main_src, re.S).group(1), name)

    def test_every_button_has_a_handler(self):
        produced: set[str] = {"nav:home"}      # HOME — константа в keyboards/nav.py
        for path in list((ROOT / "bot" / "keyboards").glob("*.py")) + HANDLERS:
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r'button\((?:f?"[^"]*"|[^,]+),\s*(f?)"([^"]+)"', src):
                produced.add(re.sub(r"\{[^}]*\}", "1", m.group(2)))
        src = "\n".join(p.read_text(encoding="utf-8") for p in HANDLERS)
        exact = set(re.findall(r'F\.data == "([^"]+)"', src))
        for group in re.findall(r"F\.data\.in_\(\{([^}]+)\}\)", src):
            exact |= set(re.findall(r'"([^"]+)"', group))
        prefixes = set(re.findall(r'F\.data\.startswith\("([^"]+)"\)', src))
        regexps = [re.compile(r) for r in re.findall(r'F\.data\.regexp\(r"([^"]+)"\)', src)]
        missing = []
        for data in sorted(produced):
            data = re.sub(r"^adm:lg:1:", "adm:lg:all:", data)     # {код фильтра} в шаблоне заменён заглушкой
            data = re.sub(r"^ct:1$", "ct:1", data)
            if not (data in exact or any(data.startswith(p) for p in prefixes) or any(r.match(data) for r in regexps)):
                missing.append(data)
        self.assertEqual(missing, [])
        self.assertLessEqual(max(len(d.encode()) for d in produced), 64)     # лимит Telegram на callback_data

    def test_router_order(self):
        order = re.findall(r"dp\.include_router\((\w+)\.router\)", (ROOT / "main.py").read_text(encoding="utf-8"))
        pos = {name: i for i, name in enumerate(order)}
        self.assertEqual(order[0], "errors")
        # знакомство и навигация раньше сценариев; события раньше обратной связи (чтобы диалоги не «съедали» меню); fallback — последний
        self.assertLess(pos["onboarding"], pos["start"]); self.assertLess(pos["start"], pos["nav"])
        self.assertLess(pos["nav"], pos["lessons"]); self.assertLess(pos["nav"], pos["events_handlers"])
        self.assertLess(pos["events_handlers"], pos["feedback"]); self.assertLess(pos["admin"], pos["fallback"])
        self.assertEqual(order[-1], "fallback")

    def test_screens_never_lose_their_buttons_by_accident(self):
        """reply(...) без reply_markup стирает кнопки экрана. Допустимо только там, где экран намеренно завершается."""
        allowed = {("onboarding.py", "_name_screen"), ("onboarding.py", "_variants_screen")}   # у них клавиатура собирается выше
        offenders = []
        for path in HANDLERS:
            for fn in ast.walk(tree_of(path)):
                if isinstance(fn, ast.AsyncFunctionDef):
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "reply":
                            if "reply_markup" not in {k.arg for k in node.keywords} and (path.name, fn.name) not in allowed:
                                offenders.append(f"{path.name}:{node.lineno}:{fn.name}")
        self.assertEqual(offenders, [])

    def test_sections_do_not_send_stray_messages(self):
        """Разделы показывают экраны через ui/reply; прямые send_message / message.answer допустимы только в списке исключений."""
        allowed_files = {"admin.py", "nav.py", "settings.py", "inline_mode.py", "errors.py", "events.py"}
        offenders = []
        for path in HANDLERS:
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r"await message\.answer\(|\.bot\.send_message\(", src):
                if path.name not in allowed_files:
                    offenders.append(f"{path.name}: {m.group(0)}")
        self.assertEqual(offenders, [])


class AdminAccessTests(unittest.TestCase):
    """Права администратора: серверный фильтр на всём админ-роутере и ни одной админской кнопки вне него."""

    ADMIN_PREFIXES = ("adm:", "bc:")

    def test_admin_router_is_filtered_for_messages_and_callbacks(self):
        src = (ROOT / "bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"router\.message\.filter\([^)]*IsAdmin\(\)")
        self.assertRegex(src, r"router\.callback_query\.filter\(IsAdmin\(\)\)")

    def test_admin_callbacks_are_handled_only_inside_admin_router(self):
        for path in HANDLERS:
            if path.name == "admin.py":
                continue
            src = path.read_text(encoding="utf-8")
            for prefix in self.ADMIN_PREFIXES:
                self.assertNotIn(f'"{prefix}', src.replace("# ", ""), f"{path.name} обрабатывает админский callback {prefix}")

    def test_dangerous_admin_actions_recheck_rights_in_the_handler(self):
        src = (ROOT / "bot" / "handlers" / "admin.py").read_text(encoding="utf-8")
        for name in ("ask_confirmation", "cancel_broadcast", "send_broadcast"):
            body = src[src.index(f"async def {name}("):]
            body = body[:body.index("\n@router") if "\n@router" in body else len(body)]
            self.assertIn("is_admin(", body, name)
            self.assertIn("_owned_draft(", body, name)      # и рассылка принадлежит этому администратору

    def test_start_of_broadcast_requires_two_steps(self):
        """«Отправить» в предпросмотре ведёт на подтверждение (bc:ask), а запуск (bc:send) — только с экрана подтверждения."""
        kb = (ROOT / "bot" / "keyboards" / "admin_menu.py").read_text(encoding="utf-8")
        preview = kb[kb.index("def broadcast_preview_keyboard"):kb.index("def broadcast_confirm_keyboard")]
        self.assertIn("bc:ask:", preview)
        self.assertNotIn("bc:send:", preview)
        self.assertIn("bc:send:", kb[kb.index("def broadcast_confirm_keyboard"):])


if __name__ == "__main__":
    unittest.main()
