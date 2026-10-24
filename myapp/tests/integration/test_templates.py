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

    中の画面の URL は T4 でワークスペースの key を先頭に持つ（`/w/<key>/...`）。素の `{% url %}` は key を
    足せないので、`{% workspace_url %}`（名前を切り替えない）か `{% scoped_url %}`（実データとペナントの世界で
    引き分ける）で引く。ほかの epic（`epic/postseason` など）を取り込んだときも、新しいテンプレートの
    素の `{% url %}` をここで拾う。
    """

    # ワークスペースの外の画面（ロゴ・404・パスワード変更）から中の画面へのリンク。
    # 外の画面にはワークスペースが無く key を足せないので、T4 で `/`（`home`）へ向け替える
    OUTSIDE_LINKS = {
        ("myapp/base.html", "dashboard"),
        ("404.html", "dashboard"),
        ("registration/password_change_form.html", "team_list"),
        ("registration/password_change_done.html", "team_list"),
    }
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
                if name in names and (relative, name) not in self.OUTSIDE_LINKS:
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{relative}:{line} {name}")
        self.assertEqual(
            offenders,
            [],
            "ワークスペースの中の画面を素の {% url %} で引いています。{% workspace_url %} か {% scoped_url %} に"
            "替えてください: " + ", ".join(offenders),
        )

    def test_outside_links_still_exist(self):
        """例外の表が古くならない（向け替えた・消したものは表からも消す）。"""
        found = set()
        for relative, text in self._templates():
            found.update((relative, name) for name in self.URL_TAG.findall(text))
        self.assertEqual(self.OUTSIDE_LINKS - found, set())

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
