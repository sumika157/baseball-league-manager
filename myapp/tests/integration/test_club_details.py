"""編成画面の選手の詳細（能力・年齢・今季の成績）と、球団の画面の「2軍」の印（#102）。

編成画面は選ぶ材料を出し、球団の画面は「1軍に使われるか」を編成の1軍（`ClubManagementService`）から引く。
実データの球団の画面には印を出さない（1軍・2軍はペナントの世界の概念）。
"""

from datetime import date
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.exceptions import TeamNotFound
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.infrastructure import orm_models
from myapp.presentation.views import (
    build_club_details_service,
    build_club_service,
    build_pennant_offseason_service,
    build_pennant_season_service,
)

from .test_club_screen import ClubScreenCase
from .test_pennant_advance import YEAR
from .test_pennant_offseason_screens import DECIDE_RETIREMENTS


class ClubDetailsTest(ClubScreenCase):
    def setUp(self):
        super().setUp()
        orm_models.Player.objects.filter(stints__team_id=self.home_a.id).update(birth_date=date(2001, 1, 1))

    def details(self):
        view = self.view()
        return build_club_details_service(self.world_a).details(self.home_a.id, view.year, view.players)

    def test_the_active_tab_shows_ratings_age_and_the_stats_column(self):
        content = self.client.get(self.club_url("active")).content.decode()

        for label in ("年齢", "能力", "今季の成績", "総合", "ミート", "球威"):
            self.assertIn(label, content)

    def test_each_player_has_an_overall_the_items_and_an_age(self):
        details = self.details()

        self.assertEqual([d.player for d in details], list(self.view().players), "並びは編成の表と同じ")
        for detail in details:
            with self.subTest(player=detail.player.name):
                assert detail.overall is not None
                self.assertEqual(detail.overall.label, "総合")
                self.assertEqual(len(detail.cells), 4 if detail.player.position.is_pitcher else 5)
                self.assertIsNotNone(detail.age)

    def test_the_stats_appear_after_games_are_played(self):
        self.assertTrue(all(d.batting is None and d.pitching is None for d in self.details()), "開幕前は成績が無い")

        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)

        details = self.details()
        self.assertTrue(any(d.batting is not None for d in details))
        self.assertTrue(any(d.pitching is not None for d in details))
        content = self.client.get(self.club_url("active")).content.decode()
        self.assertIn("OPS", content)

    def test_the_ratings_are_read_in_a_fixed_number_of_queries(self):
        view = self.view()
        details = build_club_details_service(self.world_a)
        with CaptureQueriesContext(connection) as queries:
            details.details(self.home_a.id, view.year, view.players)

        ratings_queries = [q for q in queries if "pennantplayerratings" in q["sql"].replace("_", "").lower()]
        self.assertEqual(len(ratings_queries), 1, "選手ごとに能力を引かない")

    def test_the_select_options_carry_the_overall_and_the_age(self):
        self.client.force_login(self.owner)
        self.post("lineup", "manual")

        content = self.client.get(self.club_url("lineup")).content.decode()

        self.assertRegex(content, r"<option value=\"\d+\"[^>]*>[^<]*総合[A-G] \d+・\d+歳）")

    def test_the_screen_is_unchanged_for_other_users_and_other_worlds(self):
        for user in (None, self.other):
            with self.subTest(user=user):
                self.client.logout()
                if user is not None:
                    self.client.force_login(user)
                self.assertEqual(self.client.get(self.club_url("active")).status_code, 200)
        self.assertEqual(self.client.get(self.club_url("active", self.world_b)).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.post(self.club_url(), {"section": "active", "action": "manual"}).status_code, 302)


class ReserveMarkTest(ClubScreenCase):
    def team_url(self, team_id=None, **query) -> str:
        url = reverse("pennant_player_list", args=[self.world_a, team_id or self.home_a.id])
        return url + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")

    def names_of(self, player_ids) -> list[str]:
        return list(orm_models.Player.objects.filter(id__in=player_ids).values_list("name", flat=True))

    def test_only_the_players_off_the_first_team_are_marked(self):
        view = self.view()
        dropped = next(pid for pid in view.active_ids if pid not in view.rotation_ids and pid != view.closer_id)
        self.service.set_active_roster(self.home_a.id, [pid for pid in view.active_ids if pid != dropped])
        reserve = [row for row in self.view().players if not row.is_active]
        self.assertTrue(reserve, "テストの前提: 2軍の選手がいる")

        content = self.client.get(self.team_url(pos="batter")).content.decode()
        content += self.client.get(self.team_url(pos="pitcher")).content.decode()

        self.assertEqual(content.count(">2軍<"), len(reserve))

    def test_the_mark_follows_the_club_plan(self):
        view = self.view()
        dropped = next(pid for pid in view.active_ids if pid not in view.rotation_ids and pid != view.closer_id)
        self.service.set_active_roster(self.home_a.id, [pid for pid in view.active_ids if pid != dropped])
        row = next(r for r in self.view().players if r.player_id == dropped)
        self.assertFalse(row.is_active)

        pos = "pitcher" if row.position.is_pitcher else "batter"
        for extra in ({}, {"view": "ratings"}):
            with self.subTest(extra=extra):
                content = self.client.get(self.team_url(pos=pos, **extra)).content.decode()
                self.assertRegex(content, rf"(?s){row.name}</a>(?:(?!</td>).)*?>2軍<", "落とした選手に印が出る")

    def test_the_other_team_is_marked_by_its_own_first_team(self):
        rival_id = orm_models.Team.objects.get(name="ライバル球団", league__world_id=self.world_a).id
        ids = self.service.view(rival_id).active_ids
        self.service.set_active_roster(rival_id, ids[:-1])
        reserve = build_club_service(self.world_a).reserve_player_ids(rival_id)
        on_the_team = set(orm_models.PlayerStint.objects.filter(team_id=rival_id).values_list("player_id", flat=True))
        self.assertTrue(reserve and reserve < on_the_team, "テストの前提: 1軍と2軍がいる")

        content = self.client.get(self.team_url(rival_id, pos="batter")).content.decode()
        content += self.client.get(self.team_url(rival_id, pos="pitcher")).content.decode()

        self.assertEqual(content.count(">2軍<"), len(reserve))

    def test_a_player_who_left_the_team_is_not_marked(self):
        """退団・引退した選手は、通算の表に残っていても「2軍」ではない（もうチームにいない）。"""
        build_pennant_season_service(self.world_a).advance(AdvanceTarget.SEASON_END)
        retired = orm_models.PlayerStint.objects.filter(team_id=self.home_a.id, to_year=None).order_by("number")[0]
        with mock.patch(DECIDE_RETIREMENTS, return_value=(retired.player_id,)):
            build_pennant_offseason_service(self.world_a).close_season(expected_year=YEAR)
        name = retired.player.name
        reserve = build_club_service(self.world_a).reserve_player_ids(self.home_a.id)
        self.assertNotIn(retired.player_id, reserve)

        content = ""
        for mode in ("batter", "pitcher"):
            content += self.client.get(self.team_url(pos=mode, period="career")).content.decode()
        self.assertIn(name, content, "テストの前提: 通算の表には退団した選手が残る")
        self.assertNotRegex(content, rf"(?s){name}</a>(?:(?!</td>).)*?>2軍<")

    def test_an_unreadable_club_shows_no_mark_instead_of_failing(self):
        with mock.patch(
            "myapp.application.club_management.ClubManagementService.reserve_player_ids",
            side_effect=TeamNotFound("球団が見つかりません。"),
        ):
            response = self.client.get(self.team_url(pos="batter"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(">2軍<", response.content.decode())

    def test_the_real_data_has_no_reserve_mark(self):
        real_team = orm_models.Team.objects.filter(league__world__isnull=True).first()
        assert real_team is not None

        content = self.client.get(reverse("player_list", args=[real_team.id])).content.decode()

        self.assertNotIn(">2軍<", content)
