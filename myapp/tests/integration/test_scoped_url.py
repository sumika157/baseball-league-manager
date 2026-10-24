"""`{% scoped_url %}`・`{% workspace_url %}`（ワークスペースの中の画面への URL の引き方）の検査。"""

import re
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase
from django.urls import NoReverseMatch

from myapp.application.dto import WorldContext
from myapp.domain.pennant.season import SeasonPhase

from ..helpers import ws_url

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


def render(source: str, **context) -> str:
    return Template("{% load scoped %}" + source).render(Context(context))


class ScopedUrlTest(SimpleTestCase):
    def test_without_a_world_it_is_the_same_as_the_url_tag(self):
        """世界の無い画面（実データ）では、`{% url %}` と同じ文字列になる。"""
        cases = [
            ("player_list", [3], "{% url 'player_list' 3 %}"),
            ("player_detail", [3, 5], "{% url 'player_detail' 3 5 %}"),
            ("standings", [], "{% url 'standings' %}"),
            ("league_titles_by_year", [2, 2026], "{% url 'league_titles_by_year' 2 2026 %}"),
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
            ("game_create", [], "{% url 'game_create' %}"),
            ("player_edit", [3, 5], "{% url 'player_edit' 3 5 %}"),
            ("pennant_club", [7], "{% url 'pennant_club' 7 %}"),
            ("pennant_offseason", [7, 2026], "{% url 'pennant_offseason' 7 2026 %}"),
        ]
        for name, args, url_tag in cases:
            with self.subTest(name=name):
                arguments = "".join(f" {arg}" for arg in args)
                self.assertEqual(render(f"{{% workspace_url '{name}'{arguments} %}}"), render(url_tag))
                self.assertEqual(render(f"{{% workspace_url '{name}'{arguments} %}}"), ws_url(name, *args))

    def test_a_world_does_not_switch_the_name(self):
        self.assertEqual(render("{% workspace_url 'pennant_club' 7 %}", world=WORLD), ws_url("pennant_club", 7))
        self.assertEqual(render("{% workspace_url 'game_list' %}", world=WORLD), ws_url("game_list"))


def _arity(name: str) -> int:
    """実データ側の URL の引数の数。"""
    for arity in range(4):
        try:
            ws_url(name, *[1] * arity)
        except NoReverseMatch:
            continue
        return arity
    raise AssertionError(f"{name} を引けません")
