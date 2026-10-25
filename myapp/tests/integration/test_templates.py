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


# JavaScript として読まれる場所。イベント属性の値・script 要素の中身・javascript: の URL
_EVENT_ATTRIBUTE = re.compile(r"""\son[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_SCRIPT_BODY = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_JAVASCRIPT_URL = re.compile(r"""=\s*(?:"\s*(javascript:[^"]*)"|'\s*(javascript:[^']*)')""", re.IGNORECASE)
_VARIABLE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_ENDS_WITH_ESCAPEJS = re.compile(r"\|\s*escapejs\s*$")


def javascript_context_variables(text: str) -> list[tuple[int, str, bool]]:
    """JavaScript の文脈に埋めた変数を (行, 式, escapejs で終わるか) の並びで返す。

    Django の自動エスケープは HTML 用で、イベント属性の値はブラウザが HTML として戻してから
    JavaScript に渡すため `'` で文字列を閉じられる（#320）。JavaScript の文脈では、
    最後のフィルタを escapejs にする（後ろに別のフィルタを足すとそこで素の文字列が混ざる）。
    """
    found = []
    for pattern in (_EVENT_ATTRIBUTE, _SCRIPT_BODY, _JAVASCRIPT_URL):
        for region in pattern.finditer(text):
            group = 1 if region.group(1) is not None else 2
            start = region.start(group)
            for variable in _VARIABLE.finditer(region.group(group)):
                line = text[: start + variable.start()].count("\n") + 1
                expression = variable.group(1).strip()
                found.append((line, expression, bool(_ENDS_WITH_ESCAPEJS.search(expression))))
    return found


class TemplateJavaScriptContextTest(TestCase):
    """JavaScript の文脈に埋めた変数は escapejs を通すこと（#320）。

    値を JavaScript に渡すなら escapejs か json_script を使う。json_script は script 要素ごと
    描くのでテンプレートの文字列には script が現れず、この検査に掛からない。
    """

    def test_variables_in_javascript_are_escaped(self):
        offenders = []
        scanned = 0
        for path in sorted(TEMPLATE_ROOT.rglob("*.html")):
            for line, expression, escaped in javascript_context_variables(path.read_text(encoding="utf-8")):
                scanned += 1
                if not escaped:
                    offenders.append(f"{path.relative_to(TEMPLATE_ROOT)}:{line} {{{{ {expression} }}}}")

        # 検査の正規表現が腐って何も拾わなくなっていないこと（退団の確認文が拾えるはず）
        self.assertGreater(scanned, 0)
        self.assertEqual(
            offenders,
            [],
            "JavaScript の文脈に escapejs を通さない変数があります。"
            " 末尾に |escapejs を付けるか json_script で渡してください: " + ", ".join(offenders),
        )

    def test_scanner_finds_each_javascript_context(self):
        """検査そのものが、3つの文脈すべてで素の変数を拾い、escapejs 済みは通すこと。"""
        unsafe = [
            """<button onclick="return confirm('{{ name }}')">""",
            """<a onmouseover='go("{{ name }}")'>""",
            "<script>\nconst name = '{{ name }}';\n</script>",
            """<a href="javascript:go('{{ name }}')">""",
            """<button onclick="f('{{ name|escapejs|upper }}')">""",
        ]
        for text in unsafe:
            with self.subTest(text=text):
                self.assertEqual([escaped for _, _, escaped in javascript_context_variables(text)], [False])

        safe = [
            """<button onclick="return confirm('{{ name|escapejs }}')">""",
            "<script>\nconst name = '{{ name | escapejs }}';\n</script>",
            """<a href="{{ url }}" title="{{ name }}">""",
            """<script src="{% static 'x.js' %}"></script>""",
        ]
        for text in safe:
            with self.subTest(text=text):
                self.assertTrue(all(escaped for _, _, escaped in javascript_context_variables(text)))
