"""テストのモジュールどうしの import の検査。

unittest はモジュールの中にあるテストのクラスを、そこで定義されたものか import されたものかを区別せずに集める。
テストのメソッドを持つクラスを別の `test_` モジュールから import すると、import した先でも同じテストが流れる
（エラーにはならず、件数と時間が黙って増える）。土台のクラス（`*Case`）にテストを足したときに起きやすい。
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase

TESTS_DIR = Path(__file__).resolve().parents[1]


def _module_path(importer: Path, node: ast.ImportFrom) -> Path | None:
    """`from .test_x import ...`・`from myapp.tests.domain.test_x import ...` が指すファイル。

    テストのモジュール以外は None。
    """
    if node.module is None:
        return None
    if node.level:
        base = importer.parent
        for _ in range(node.level - 1):
            base = base.parent
        path = base.joinpath(*node.module.split("."))
    elif node.module.startswith("myapp.tests."):
        path = TESTS_DIR.joinpath(*node.module.split(".")[2:])
    else:
        return None
    path = path.with_suffix(".py")
    return path if path.name.startswith("test_") and path.exists() else None


def _classes_with_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(item, ast.FunctionDef) and item.name.startswith("test") for item in node.body)
    }


class TestModuleImportTest(SimpleTestCase):
    def test_no_test_class_is_imported_into_another_test_module(self):
        found = []
        for importer in sorted(TESTS_DIR.rglob("test_*.py")):
            for node in ast.walk(ast.parse(importer.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.ImportFrom):
                    continue
                source = _module_path(importer, node)
                if source is None:
                    continue
                imported = {alias.name for alias in node.names} & _classes_with_tests(source)
                where = importer.relative_to(TESTS_DIR)
                found.extend(f"{where}: {source.name} の {name}" for name in sorted(imported))

        self.assertEqual(found, [], "テストを持つクラスを import すると、import した先でも同じテストが流れる")

    def test_the_check_sees_relative_and_absolute_imports(self):
        """検査が import を拾えていなければ、上のテストは何も確かめていない。"""
        importer = TESTS_DIR / "domain" / "test_x.py"
        relative = ast.parse("from .test_release import member").body[0]
        absolute = ast.parse("from myapp.tests.domain.test_release import member").body[0]
        assert isinstance(relative, ast.ImportFrom) and isinstance(absolute, ast.ImportFrom)

        self.assertEqual(_module_path(importer, relative), TESTS_DIR / "domain" / "test_release.py")
        self.assertEqual(_module_path(importer, absolute), TESTS_DIR / "domain" / "test_release.py")
        self.assertIn("RetirementChanceTest", _classes_with_tests(TESTS_DIR / "domain" / "test_retirement.py"))
