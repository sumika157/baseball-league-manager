"""`{% scoped_url %}`・`{% workspace_url %}`（ワークスペースの中の画面への URL の引き方）の検査。

どちらも context の `workspace`（今の要求のワークスペース）の key を先頭に足す。`workspace` が無ければ例外で、
key の無い URL を黙って作らない（W2 T4。#78）。
"""

import re
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase
from django.urls import NoReverseMatch

from myapp.application.dto import WorkspaceContext, WorldContext
from myapp.domain.pennant.season import SeasonPhase
from myapp.domain.workspace import Visibility, WorkspaceAccess, WorkspaceRole
from myapp.templatetags.scoped import WorkspaceMissing

from ..helpers import DEFAULT_WORKSPACE_KEY, ws_url

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"

WORLD = WorldContext(
    world_id=7,
    name="目印の世界",
    phase=SeasonPhase.IN_SEASON,
    season_year=2026,
    today=None,
    managed_team_id=None,
    managed_team_name="",
    default_league_id=None,
)


def _workspace(key: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=1,
        key=key,
        name="目印のワークスペース",
        access=WorkspaceAccess(role=WorkspaceRole.VIEWER, visibility=Visibility.PUBLIC),
    )


# テストの既定のワークスペース（`ws_url` が workspace を省いたときの key）
WORKSPACE = _workspace(DEFAULT_WORKSPACE_KEY)
KEY = DEFAULT_WORKSPACE_KEY


def render(source: str, **context) -> str:
    """context の `workspace` を省くと、既定のワークスペースの中で描く（`workspace=None` で外）。"""
    context.setdefault("workspace", WORKSPACE)
    return Template("{% load scoped %}" + source).render(Context(context))


class ScopedUrlTest(SimpleTestCase):
    def test_without_a_world_it_is_the_same_as_the_url_tag(self):
        """世界の無い画面（実データ）では、`{% url %}` と同じ文字列になる。"""
        cases = [
            ("player_list", [3], f"{{% url 'player_list' '{KEY}' 3 %}}"),
            ("player_detail", [3, 5], f"{{% url 'player_detail' '{KEY}' 3 5 %}}"),
            ("standings", [], f"{{% url 'standings' '{KEY}' %}}"),
            ("league_titles_by_year", [2, 2026], f"{{% url 'league_titles_by_year' '{KEY}' 2 2026 %}}"),
        ]
        for name, args, url_tag in cases:
            with self.subTest(name=name):
                arguments = "".join(f" {arg}" for arg in args)
                self.assertEqual(render(f"{{% scoped_url '{name}'{arguments} %}}", world=None), render(url_tag))
                self.assertEqual(render(f"{{% scoped_url '{name}'{arguments} %}}"), ws_url(name, *args))

    def test_with_a_world_it_puts_the_world_id_first(self):
        self.assertEqual(render("{% scoped_url 'player_list' 3 %}", world=WORLD), ws_url("pennant_player_list", 7, 3))
        self.assertEqual(
            render("{% scoped_url 'player_detail' 3 5 %}", world=WORLD), ws_url("pennant_player_detail", 7, 3, 5)
        )
        self.assertEqual(render("{% scoped_url 'standings' %}", world=WORLD), ws_url("pennant_standings", 7))

    def test_a_screen_without_a_world_version_fails_loudly(self):
        """世界の範囲に無い画面（編集など）を世界の中で引くと、黙って実データへ向けず例外にする。"""
        with self.assertRaises(NoReverseMatch):
            render("{% scoped_url 'player_edit' 3 5 %}", world=WORLD)

    def test_every_name_used_by_the_templates_exists_in_the_world(self):
        """共有テンプレートで `scoped_url` に渡している名前は、すべて世界の範囲にも URL がある。"""
        used = set()
        for path in TEMPLATES.rglob("*.html"):
            used.update(re.findall(r"\{% scoped_url '([a-z_]+)'", path.read_text(encoding="utf-8")))
        self.assertGreaterEqual(len(used), 8, "テンプレートから名前を拾えていません")
        for name in sorted(used):
            with self.subTest(name=name):
                self.assertTrue(
                    ws_url(f"pennant_{name}", 1, *[1] * _arity(name)).startswith(ws_url("pennant_world", 1))
                )


class WorkspaceUrlTest(SimpleTestCase):
    """`{% workspace_url %}` は名前を切り替えない（世界の中でも、渡した名前の URL を引く）。"""

    def test_it_is_the_same_as_the_url_tag(self):
        cases = [
            ("game_create", [], f"{{% url 'game_create' '{KEY}' %}}"),
            ("player_edit", [3, 5], f"{{% url 'player_edit' '{KEY}' 3 5 %}}"),
            ("pennant_club", [7], f"{{% url 'pennant_club' '{KEY}' 7 %}}"),
            ("pennant_offseason", [7, 2026], f"{{% url 'pennant_offseason' '{KEY}' 7 2026 %}}"),
        ]
        for name, args, url_tag in cases:
            with self.subTest(name=name):
                arguments = "".join(f" {arg}" for arg in args)
                self.assertEqual(render(f"{{% workspace_url '{name}'{arguments} %}}"), render(url_tag))
                self.assertEqual(render(f"{{% workspace_url '{name}'{arguments} %}}"), ws_url(name, *args))

    def test_a_world_does_not_switch_the_name(self):
        self.assertEqual(render("{% workspace_url 'pennant_club' 7 %}", world=WORLD), ws_url("pennant_club", 7))
        self.assertEqual(render("{% workspace_url 'game_list' %}", world=WORLD), ws_url("game_list"))


class WorkspaceKeyTest(SimpleTestCase):
    """どちらのタグも、context の `workspace` の key の下の URL を引く。外の描画では例外にする。"""

    def test_the_key_of_the_context_is_put_first(self):
        other = _workspace("otherkey01")
        self.assertEqual(render("{% workspace_url 'team_list' %}", workspace=other), "/w/otherkey01/teams/")
        self.assertEqual(
            render("{% scoped_url 'player_list' 3 %}", workspace=other, world=WORLD), "/w/otherkey01/pennant/7/team/3/"
        )

    def test_outside_a_workspace_both_tags_fail_loudly(self):
        """key の無い URL を黙って作らない（外の画面のテンプレートは `{% if workspace %}` で囲む）。"""
        for source in ("{% workspace_url 'team_list' %}", "{% scoped_url 'standings' %}"):
            with self.subTest(source=source), self.assertRaises(WorkspaceMissing):
                render(source, workspace=None)

    def test_the_entry_url_takes_the_key_it_is_given(self):
        """`/` の一覧から、行ごとのワークスペースの入口（ダッシュボード）へ。今の要求のワークスペースは見ない。"""
        self.assertEqual(render("{% workspace_entry_url 'otherkey01' %}", workspace=None), "/w/otherkey01/")


def _arity(name: str) -> int:
    """実データ側の URL の引数の数。"""
    for arity in range(4):
        try:
            ws_url(name, *[1] * arity)
        except NoReverseMatch:
            continue
        return arity
    raise AssertionError(f"{name} を引けません")
