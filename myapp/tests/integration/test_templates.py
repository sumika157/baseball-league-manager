"""テンプレートの書き方に関する検査。

コメントが画面に漏れ出す不具合を出したことがあるため、機械的に防ぐ。
"""

import pathlib
import re

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from ..helpers import ws_url

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

        from ..helpers import default_workspace

        # 今の URL はワークスペースを1つ解決する（無ければ 404）
        default_workspace()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

    def test_pages_do_not_leak_comment_text(self):

        for url in [ws_url("dashboard"), ws_url("team_list"), "/admin/", "/accounts/login/"]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                for marker in self.LEAKED_MARKERS:
                    self.assertNotIn(marker, body)


def _workspace_url_names() -> set[str]:
    """ワークスペースの中の画面（`myapp/urls.py` の `workspace_urlpatterns`）の URL 名。"""
    from django.urls import URLPattern, URLResolver

    from myapp.urls import workspace_urlpatterns

    names: set[str] = set()
    pending: list = list(workspace_urlpatterns)
    while pending:
        entry = pending.pop()
        if isinstance(entry, URLResolver):
            pending.extend(entry.url_patterns)
        elif isinstance(entry, URLPattern) and entry.name:
            names.add(entry.name)
    return names


class WorkspaceUrlTagTest(SimpleTestCase):
    """ワークスペースの中の画面へのリンクを、素の `{% url %}` で引かない（W2 T3。#78）。

    中の画面の URL はワークスペースの key を先頭に持つ（`/w/<key>/...`。T4）。素の `{% url %}` は key を
    足せないので、`{% workspace_url %}`（名前を切り替えない）か `{% scoped_url %}`（実データとペナントの世界で
    引き分ける）で引く。ほかの epic（`epic/postseason` など）を取り込んだときも、新しいテンプレートの
    素の `{% url %}` をここで拾う。
    """

    URL_TAG = re.compile(r"\{%\s*url\s+['\"]([^'\"]+)['\"]")
    WORKSPACE_TAGS = re.compile(r"\{%\s*(workspace_url|scoped_url)\s+['\"]([^'\"]+)['\"]")

    def _templates(self):
        for path in sorted(TEMPLATE_ROOT.rglob("*.html")):
            yield path.relative_to(TEMPLATE_ROOT).as_posix(), path.read_text(encoding="utf-8")

    def test_workspace_names_are_collected(self):
        names = _workspace_url_names()
        self.assertGreaterEqual(len(names), 40, "workspace_urlpatterns から名前を拾えていません")
        self.assertIn("pennant_club", names)
        self.assertNotIn("signup", names, "新規登録はワークスペースの外")

    def test_no_bare_url_tag_to_a_workspace_page(self):
        names = _workspace_url_names()
        offenders = []
        for relative, text in self._templates():
            for match in self.URL_TAG.finditer(text):
                name = match.group(1)
                if name in names:
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{relative}:{line} {name}")
        self.assertEqual(
            offenders,
            [],
            "ワークスペースの中の画面を素の {% url %} で引いています。{% workspace_url %} か {% scoped_url %} に"
            "替えてください: " + ", ".join(offenders),
        )

    def test_workspace_tags_name_only_workspace_pages(self):
        """`workspace_url`・`scoped_url` はワークスペースの中の名前だけを引く（外の画面は素の `{% url %}`）。"""
        names = _workspace_url_names()
        offenders = []
        for relative, text in self._templates():
            for match in self.WORKSPACE_TAGS.finditer(text):
                tag, name = match.groups()
                if name not in names:
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{relative}:{line} {tag} {name}")
        self.assertEqual(offenders, [], "ワークスペースの外の画面を引いています: " + ", ".join(offenders))


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
