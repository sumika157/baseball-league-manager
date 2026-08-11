"""Makefile がどの作業ツリーを対象にするかの検査。

並行作業のために git worktree（`.claude/worktrees/<名前>`）を使うが、コンテナへの
バインドマウントは main の作業ツリー全体（`.:/app`）なので、`-w` で対象を指さない
コマンドは **worktree で作業していても main のコードを検査・テストしてしまう**。
これはエラーにならず「通ったのに直っていない」形で出るため、機械的に防ぐ。
"""

import pathlib
import re

from django.conf import settings
from django.test import SimpleTestCase

MAKEFILE = pathlib.Path(settings.BASE_DIR) / "Makefile"

# 作業ツリーの中身を触らないため -w が不要なターゲット（コンテナ自体の操作）。
WORKTREE_AGNOSTIC_TARGETS = frozenset({"up", "down", "ps", "logs", "build"})


class MakefileWorktreeTargetingTest(SimpleTestCase):
    """`make` が対象の作業ツリーを取り違えないことを検査する。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = MAKEFILE.read_text(encoding="utf-8")

    def _recipe_lines(self):
        """(ターゲット名, レシピ行) を列挙する。"""
        target = None
        for raw in self.text.splitlines():
            match = re.match(r"^([a-zA-Z0-9_-]+):", raw)
            if match:
                target = match.group(1)
            elif raw.startswith("\t") and target is not None:
                yield target, raw.strip()

    def test_container_commands_target_the_selected_worktree(self):
        """作業ツリーを触る docker コマンドが $(WORKDIR) を指していること。

        `-w` を省くと worktree で作業していても main のコードが対象になる。
        """
        offenders = []
        for target, line in self._recipe_lines():
            if target in WORKTREE_AGNOSTIC_TARGETS:
                continue
            if not re.search(r"docker compose (exec|run)", line):
                continue
            if "$(WORKDIR)" not in line:
                offenders.append(f"{target}: {line}")

        self.assertEqual(
            offenders,
            [],
            "docker compose のコマンドが $(WORKDIR) を指していません。"
            " -w を省くと worktree ではなく main のコードを対象にしてしまいます: " + "; ".join(offenders),
        )

    def test_workdir_defaults_to_main_and_follows_wt(self):
        """WORKDIR が WT の有無で main / worktree に切り替わること。"""
        self.assertIn(
            "WORKDIR := $(if $(WT),/app/.claude/worktrees/$(WT),/app)",
            self.text,
            "WORKDIR の定義が変わっています。WT 未指定なら /app、指定時は worktree を指すこと。",
        )

    def test_guard_against_running_inside_a_worktree_without_wt(self):
        """worktree の中から WT 無しで呼ばれたら止まること。

        止めずに通すと main のコードを黙って検査・テストしてしまう。
        """
        self.assertIn("/.claude/worktrees/", self.text)
        self.assertRegex(
            self.text,
            r"\$\(error [^)]*WT=",
            "worktree の中から WT 無しで実行されたときに $(error) で止める記述がありません。",
        )

    def test_generated_files_are_kept_out_of_the_worktree(self):
        """コンテナ（root）が作る生成物をコンテナの /tmp に逃がしていること。

        worktree の中に root 所有の生成物が残ると git worktree remove が
        「Directory not empty」で失敗し、削除に sudo が必要になる。
        """
        for variable in ("PYTHONPYCACHEPREFIX", "RUFF_CACHE_DIR", "MYPY_CACHE_DIR"):
            self.assertRegex(
                self.text,
                rf"-e {variable}=/tmp/",
                f"{variable} をコンテナの /tmp に向けていません。"
                " worktree に root 所有の生成物が残り、削除できなくなります。",
            )
