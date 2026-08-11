"""一覧の並べ替えと表示順。不正なソートキーが既定の並びに落ちることも含む。"""

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.value_objects import (
    BattingLine,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoLeagueRepository,
)

from ..helpers import (
    play_game,
)
from .base import BaseCase


class SortingViewTest(BaseCase):
    def setUp(self):
        super().setUp()
        a = self.service.register_player(self.team.id, "少打", 1, "内野手")
        b = self.service.register_player(self.team.id, "多打", 2, "外野手")
        play_game(
            self.team,
            self.rival,
            batting={
                a.id: BattingLine(at_bats=20, singles=4, home_runs=1),
                b.id: BattingLine(at_bats=20, singles=2, home_runs=5),
            },
        )
        self.url = reverse("player_list", args=[self.team.id])

    def _names(self, query=""):
        listing = self.client.get(f"{self.url}{query}").context["listing"]
        return [r.name for r in listing.rows]

    def test_default_order_is_ops(self):
        self.assertEqual(self._names(), ["多打", "少打"])

    def test_sort_by_home_runs_ascending(self):
        self.assertEqual(self._names("?sort=home_runs&dir=asc"), ["少打", "多打"])

    def test_sort_by_home_runs_descending(self):
        self.assertEqual(self._names("?sort=home_runs&dir=desc"), ["多打", "少打"])

    def test_invalid_sort_key_does_not_break_the_page(self):
        response = self.client.get(f"{self.url}?sort=../../etc/passwd")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["listing"].sort, "ops")

    def test_sort_link_keeps_other_query_params(self):
        body = self.client.get(f"{self.url}?pos=pitcher").content.decode()
        self.assertIn("pos=pitcher", body)
        self.assertIn("sort=era", body)

    def test_header_shows_the_active_direction(self):
        body = self.client.get(f"{self.url}?sort=home_runs&dir=desc").content.decode()
        self.assertIn("sort-link is-active", body)

    def test_team_list_can_be_sorted(self):
        response = self.client.get(f"{reverse('team_list')}?sort=name&dir=asc")
        names = [t.name for t in response.context["teams"]]
        self.assertEqual(names, sorted(names))

    def test_team_list_defaults_to_manual_order(self):
        """名前順ではなく、管理画面で設定した表示順が既定になること。"""
        orm_models.Team.objects.update(display_order=5)
        orm_models.Team.objects.create(league=self.league, name="Zチーム", display_order=1)
        response = self.client.get(reverse("team_list"))

        # 名前順なら最後に来るはずの Z が、表示順1なので先頭に出る
        self.assertEqual(response.context["teams"][0].name, "Zチーム")
        self.assertEqual(response.context["current_sort"], "order")

    def test_standings_can_be_sorted(self):
        response = self.client.get(f"{reverse('standings')}?sort=wins&dir=desc")
        self.assertEqual(response.context["standings"].sort, "wins")


class TeamOrderingTest(BaseCase):
    def setUp(self):
        super().setUp()
        orm_models.Team.objects.filter(id=self.team.id).update(display_order=2, name="Aチーム")
        orm_models.Team.objects.filter(id=self.rival.id).update(display_order=1, name="Bチーム")

    def test_display_order_beats_name(self):
        names = [t.name for t in self.service.list_teams().rows]
        self.assertEqual(names, ["Bチーム", "Aチーム"])

    def test_dashboard_uses_the_same_order(self):
        names = [t.name for t in self.service.get_dashboard().teams]
        self.assertEqual(names, ["Bチーム", "Aチーム"])

    def test_same_order_falls_back_to_name(self):
        orm_models.Team.objects.update(display_order=0)
        names = [t.name for t in self.service.list_teams().rows]
        self.assertEqual(names, ["Aチーム", "Bチーム"])

    def _league_page(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        return self.client.get(f"/admin/myapp/league/{self.league.id}/change/")

    def test_admin_league_page_loads_the_sortable_script(self):
        self.assertContains(self._league_page(), "admin-inline-sortable.js")

    def test_team_changelist_can_be_reordered(self):
        """リーグ編集画面だけでなく、チーム一覧からも並べ替えられること。"""
        self.client.force_login(User.objects.create_superuser(username="t", password="x"))
        response = self.client.get("/admin/myapp/team/")

        self.assertContains(response, "admin-inline-sortable.js")
        self.assertContains(response, 'name="form-0-display_order"')

    def test_changelist_explains_how_to_reorder(self):
        """表示順の列は隠しているので、操作方法を画面で伝える。"""
        self.client.force_login(User.objects.create_superuser(username="t3", password="x"))

        for url in ("/admin/myapp/team/", "/admin/myapp/league/"):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "sortable-hint")

    def test_no_link_on_the_page_carries_a_sort(self):
        """絞り込みのリンクなどに並べ替えが紛れ込まないこと。

        1つでも残っていると、そこから並べ替えられない状態に入ってしまう。
        """
        self.client.force_login(User.objects.create_superuser(username="t6", password="x"))
        url = f"/admin/myapp/team/?league__id__exact={self.league.id}&o=1"

        body = self.client.get(url, follow=True).content.decode()

        self.assertNotIn("?o=", body)
        self.assertIn("admin-inline-sortable.js", body)

    def test_name_cell_is_rendered_as_a_header_cell(self):
        """一覧のリンク列は th で描かれる。

        つまみと折り返しの CSS が td だけを指していると効かないため、
        この前提が変わっていないことを確かめる。
        """
        self.client.force_login(User.objects.create_superuser(username="t5", password="x"))
        body = self.client.get("/admin/myapp/team/").content.decode()

        self.assertIn('<th class="field-name">', body)

    def test_order_input_is_submitted_but_the_column_is_hidden(self):
        """数値は送信するが列としては見せない（インラインと同じ扱い）。"""
        self.client.force_login(User.objects.create_superuser(username="t4", password="x"))
        body = self.client.get("/admin/myapp/team/").content.decode()

        self.assertIn('name="form-0-display_order"', body)
        # 列を隠す指定が読み込まれていること
        self.assertIn("myapp/css/admin-theme.css", body)
        self.assertIn("column-display_order", body)

    def test_team_changelist_is_ordered_by_league_then_order(self):
        self.client.force_login(User.objects.create_superuser(username="t2", password="x"))
        body = self.client.get("/admin/myapp/team/").content.decode()

        # リーグごとに区切られ、リーグ内は表示順に並ぶ
        self.assertIn("group-heading-row", body)
        self.assertLess(body.index("Bチーム"), body.index("Aチーム"))

    def test_order_field_is_submitted_but_not_shown(self):
        body = self._league_page().content.decode()

        self.assertIn('type="hidden" name="teams-0-display_order"', body)
        self.assertIn('class="column-display_order required hidden"', body)
        # インラインの表示順が数値欄として出ていないこと。
        # （リーグ自身の表示順は別の欄なので、ページ全体では数値欄が存在する）
        self.assertNotIn('type="number" name="teams-0-display_order"', body)


class LeagueOrderingTest(BaseCase):
    """リーグの表示順を管理画面から並べ替えられること。"""

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        # 名前順なら A → Z だが、表示順で逆にする
        orm_models.League.objects.filter(id=self.league.id).update(name="Zリーグ", display_order=1)
        self.first = orm_models.League.objects.create(name="Aリーグ", display_order=2)
        orm_models.Team.objects.create(league=self.first, name="Aチーム")

    def test_display_order_beats_name(self):
        names = [lg.name for lg in DjangoLeagueRepository().find_all()]
        self.assertEqual(names, ["Zリーグ", "Aリーグ"])

    def test_same_order_falls_back_to_name(self):
        orm_models.League.objects.update(display_order=0)
        names = [lg.name for lg in DjangoLeagueRepository().find_all()]
        self.assertEqual(names, ["Aリーグ", "Zリーグ"])

    def test_admin_list_is_ordered_and_editable(self):
        body = self.client.get("/admin/myapp/league/").content.decode()

        self.assertLess(body.index("Zリーグ"), body.index("Aリーグ"))
        # 一覧から直接編集できる（ドラッグの結果もここに入る）
        self.assertIn('name="form-0-display_order"', body)

    def test_admin_loads_the_sortable_script(self):
        self.assertContains(self.client.get("/admin/myapp/league/"), "admin-inline-sortable.js")

    def test_order_is_reflected_in_standings(self):
        play_game(self.team, self.rival, day=1)
        a2 = orm_models.Team.objects.create(league=self.first, name="A2チーム")
        play_game(orm_models.Team.objects.get(name="Aチーム"), a2, day=1)

        names = [g.league_name for g in self.service.get_standings(2026).leagues]
        self.assertEqual(names, ["Zリーグ", "Aリーグ"])

    def test_order_is_reflected_in_the_team_list(self):
        names = [g.league_name for g in self.service.list_teams_by_league().rows]
        self.assertEqual(names, ["Zリーグ", "Aリーグ"])

    def test_order_is_reflected_in_dashboard_tabs(self):
        names = [g.league_name for g in self.service.get_dashboard().leagues]
        self.assertEqual(names, ["Zリーグ", "Aリーグ"])
