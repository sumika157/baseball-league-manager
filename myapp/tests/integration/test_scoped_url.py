"""`{% scoped_url %}`（共有テンプレートの URL の引き方）の検査。"""

import re
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse

from myapp.application.dto import WorldContext
from myapp.domain.pennant.season import SeasonPhase

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"

WORLD = WorldContext(
    world_id=7,
    name="目印の世界",
    phase=SeasonPhase.IN_SEASON,
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
                self.assertEqual(render(f"{{% scoped_url '{name}'{arguments} %}}"), reverse(name, args=args))

    def test_with_a_world_it_puts_the_world_id_first(self):
        self.assertEqual(render("{% scoped_url 'player_list' 3 %}", world=WORLD), "/pennant/7/team/3/")
        self.assertEqual(render("{% scoped_url 'player_detail' 3 5 %}", world=WORLD), "/pennant/7/team/3/player/5/")
        self.assertEqual(render("{% scoped_url 'standings' %}", world=WORLD), "/pennant/7/standings/")

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
                self.assertEqual(
                    reverse(f"pennant_{name}", args=[1] + [1] * _arity(name)).startswith("/pennant/1/"), True
                )


def _arity(name: str) -> int:
    """実データ側の URL の引数の数。"""
    for arity in range(4):
        try:
            reverse(name, args=[1] * arity)
        except NoReverseMatch:
            continue
        return arity
    raise AssertionError(f"{name} を引けません")
