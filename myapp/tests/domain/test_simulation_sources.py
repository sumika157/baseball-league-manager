"""シミュレーションのソースが守るべき約束を、ソースそのものから検査する。

- Django にも numpy にも依存しない（domain 層の約束。numpy は開発用にしか入っておらず、本番で動くペナントは頼れない）
- 乱数は `random()` だけを使う（`gauss` や `choice` などは版をまたいで同じ列になる保証がない）
- 試合ごとのシードに組み込みの `hash()` を使わない（`PYTHONHASHSEED` で実行ごとに変わる）

違反しても例外にはならず、「環境が変わると結果が変わる」という静かな不具合になるので、機械で見張る。
"""

import ast
from pathlib import Path
from unittest import TestCase

import myapp.domain.simulation as simulation_package

SOURCES = sorted(Path(simulation_package.__file__).parent.glob("*.py"))
FORBIDDEN_MODULES = ("django", "numpy", "scipy")
# 版をまたいで同じ列になる保証があるのは `random()` だけ
UNSTABLE_RANDOM_METHODS = {
    "gauss",
    "normalvariate",
    "lognormvariate",
    "choice",
    "choices",
    "randint",
    "randrange",
    "shuffle",
    "sample",
    "uniform",
    "triangular",
    "betavariate",
    "expovariate",
    "gammavariate",
    "getrandbits",
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class SimulationSourcesTest(TestCase):
    def test_the_sources_are_found(self):
        names = {path.name for path in SOURCES}
        self.assertTrue({"engine.py", "odds.py", "manager.py", "levels.py", "baseline.py"} <= names)

    def test_no_module_imports_django_or_numpy(self):
        for path in SOURCES:
            for node in ast.walk(_tree(path)):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    modules = [node.module]
                for module in modules:
                    self.assertNotIn(
                        module.split(".")[0], FORBIDDEN_MODULES, f"{path.name} が {module} を import している"
                    )

    def test_only_random_is_called_on_the_random_source(self):
        for path in SOURCES:
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, UNSTABLE_RANDOM_METHODS, f"{path.name}:{node.lineno}")

    def test_the_builtin_hash_is_not_used(self):
        for path in SOURCES:
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotEqual(node.func.id, "hash", f"{path.name}:{node.lineno}")

    def test_the_random_module_is_used_only_to_build_the_source(self):
        """`random` を import してよいのは、乱数源を作る `randomness.py` だけ。"""
        for path in SOURCES:
            if path.name == "randomness.py":
                continue
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Import):
                    self.assertNotIn("random", [alias.name for alias in node.names], path.name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "random", path.name)
