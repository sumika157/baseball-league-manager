"""テンプレートの書き方に関する検査。

コメントが画面に漏れ出す不具合を出したことがあるため、機械的に防ぐ。
"""

import pathlib
import re

from django.conf import settings
from django.test import TestCase

TEMPLATE_ROOT = pathlib.Path(settings.BASE_DIR) / "myapp" / "templates"


class TemplateCommentTest(TestCase):
    def test_no_multiline_hash_comments(self):
        """ハッシュ記法のコメントが複数行にまたがっていないこと。

        Django のハッシュ記法は単一行専用で、複数行にまたがると
        コメントとして解釈されず画面にそのまま出力される。
        複数行にしたい場合は comment タグを使う。
        """
        offenders = []
        for path in sorted(TEMPLATE_ROOT.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\{#", text):
                end = text.find("#}", match.start())
                body = text[match.start() : end + 2] if end != -1 else text[match.start() :]
                if "\n" in body:
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(TEMPLATE_ROOT)}:{line}")

        self.assertEqual(
            offenders,
            [],
            "複数行にまたがるハッシュ記法のコメントがあります。"
            " comment タグに置き換えてください: " + ", ".join(offenders),
        )

    def test_comment_tag_comes_after_extends(self):
        """comment タグが extends より前に置かれていないこと。

        comment は実タグなので、extends より前にあると
        「extends must be the first tag」でテンプレートが壊れる。
        """
        offenders = []
        for path in sorted(TEMPLATE_ROOT.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            extends = re.search(r"\{%\s*extends", text)
            comment = re.search(r"\{%\s*comment", text)
            if extends and comment and comment.start() < extends.start():
                offenders.append(str(path.relative_to(TEMPLATE_ROOT)))

        self.assertEqual(
            offenders,
            [],
            "comment タグが extends より前にあります: " + ", ".join(offenders),
        )


class TemplateRenderTest(TestCase):
    """主要な画面にコメント由来の文字列が出ていないこと。"""

    LEAKED_MARKERS = ["#}", "{% comment", "endcomment", "INSTALLED_APPS"]

    def setUp(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def test_pages_do_not_leak_comment_text(self):
        from django.urls import reverse

        for url in [reverse("dashboard"), reverse("team_list"), "/admin/", "/accounts/login/"]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                for marker in self.LEAKED_MARKERS:
                    self.assertNotIn(marker, body)
