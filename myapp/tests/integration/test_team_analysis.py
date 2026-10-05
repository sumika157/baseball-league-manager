"""戦力分析ページ（球団×年度のデプス表・年齢構成）。"""

from datetime import date

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.entities import Game
from myapp.domain.value_objects import BattingLine, FieldingPosition, PitchingLine, Season
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository

from ..helpers import build_team_analysis_service
from .base import BaseCase


class AnalysisCase(BaseCase):
    """選手の登録と、守備位置つきの試合を作る下ごしらえ。"""

    def player(
        self, team, name, number, position="内野手", *, throws="", bats="", birth=None, from_year=2020, to_year=None
    ):
        created = self.service.register_player(team.id, name, number, position)
        orm_models.Player.objects.filter(id=created.id).update(throws=throws, bats=bats, birth_date=birth)
        orm_models.PlayerStint.objects.filter(player_id=created.id).update(from_year=from_year, to_year=to_year)
        return created.id

    def game(self, home, away, *, year=2026, day=1, batting=(), pitching=()):
        """batting は (選手id, チーム, 守備位置の記号, 交代の順)、pitching は (選手id, 登板順)。"""
        game = Game(
            season=Season(year),
            played_on=date(year, 4, day),
            home_team_id=home.id,
            away_team_id=away.id,
            home_score=1,
            away_score=0,
        )
        for player_id, team, label, slot in batting:
            game.record_batting(
                player_id,
                BattingLine(),
                team_id=team.id,
                batting_order=1,
                slot_sequence=slot,
                fielding_position=FieldingPosition.from_label(label),
            )
        for player_id, order in pitching:
            game.record_pitching(player_id, PitchingLine(), appearance_order=order)
        return DjangoGameRepository().save(game)

    @staticmethod
    def names(table):
        return [p.name for row in table.rows for cell in row.cells for p in cell.players]

    @staticmethod
    def find(table, name):
        return next(p for row in table.rows for cell in row.cells for p in cell.players if p.name == name)

    @staticmethod
    def cells(table, label):
        """表の1行に並ぶ選手を {列の見出し: [名前]} で返す。"""
        row = next(r for r in table.rows if r.label == label)
        return {cell.hand_label: [p.name for p in cell.players] for cell in row.cells}


class TeamAnalysisPageTest(AnalysisCase):
    def setUp(self):
        super().setUp()
        self.ace = self.player(self.team, "左腕エース", 18, "投手", throws="左", birth=date(1998, 4, 1))
        self.setup_man = self.player(self.team, "右の中継ぎ", 20, "投手", throws="右")
        self.shortstop = self.player(self.team, "遊撃手", 6, "内野手", bats="右", birth=date(2000, 4, 2))
        self.rival_pitcher = self.player(self.rival, "相手投手", 11, "投手", throws="左")
        self.game(
            self.team,
            self.rival,
            batting=[(self.shortstop, self.team, "遊", 0)],
            pitching=[(self.ace, 1), (self.setup_man, 2), (self.rival_pitcher, 1)],
        )

    def analysis(self, **kwargs):
        return build_team_analysis_service().get_analysis(self.team.id, **kwargs)

    def url(self, team=None):
        return reverse("team_analysis", args=[(team or self.team).id])

    def test_anonymous_can_view_without_any_write_controls(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'method="post"')
        self.assertNotContains(response, "編集")
        self.assertContains(response, "左腕エース")

    def test_post_is_not_allowed(self):
        self.assertEqual(self.client.post(self.url()).status_code, 405)

    def test_unknown_team_is_404(self):
        self.assertEqual(self.client.get(reverse("team_analysis", args=[99999])).status_code, 404)

    def test_left_handed_starter_lands_in_starter_by_left_cell(self):
        analysis = self.analysis(year=2026)
        self.assertEqual(self.cells(analysis.pitchers, "先発")["左腕"], ["左腕エース"])
        self.assertEqual(self.cells(analysis.pitchers, "救援")["右腕"], ["右の中継ぎ"])

    def test_opposing_pitcher_in_the_same_game_is_not_counted(self):
        analysis = self.analysis(year=2026)
        self.assertNotIn("相手投手", self.names(analysis.pitchers))
        self.assertEqual(analysis.pitchers.count, 2)

    def test_fielder_is_grouped_by_primary_position(self):
        analysis = self.analysis(year=2026)
        self.assertEqual(self.cells(analysis.fielders, "二遊間")["右打"], ["遊撃手"])
        player = self.find(analysis.fielders, "遊撃手")
        self.assertEqual((player.position_label, player.games, player.starts), ("遊", 1, 1))

    def test_other_years_games_and_players_outside_the_year_are_not_counted(self):
        # 2025年に先発した登板は、2026年の表に数えない
        self.game(self.team, self.rival, year=2025, pitching=[(self.setup_man, 1)])
        future = self.player(self.team, "来年入団", 30, "投手", throws="右", from_year=2027)
        retired = self.player(self.team, "引退済み", 31, "投手", throws="右", to_year=2024)
        self.game(self.team, self.rival, day=2, pitching=[(future, 1), (retired, 1)])

        analysis = self.analysis(year=2026)
        names = self.names(analysis.pitchers)
        self.assertNotIn("来年入団", names)
        self.assertNotIn("引退済み", names)
        reliever = self.find(analysis.pitchers, "右の中継ぎ")
        self.assertEqual((reliever.games, reliever.starts), (1, 0))

    def test_invalid_year_falls_back_to_latest(self):
        self.game(self.team, self.rival, year=2025, pitching=[(self.ace, 1)])
        for year in ("1999", "abc", "-1", ""):
            with self.subTest(year=year):
                response = self.client.get(self.url(), {"year": year})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["analysis"].year, 2026)
        self.assertEqual(self.client.get(self.url(), {"year": "2025"}).context["analysis"].year, 2025)

    def test_invalid_tab_falls_back_to_depth(self):
        response = self.client.get(self.url(), {"tab": "unknown"})
        self.assertEqual(response.context["analysis"].tab, "depth")

    def test_team_without_games_still_renders(self):
        lonely = orm_models.Team.objects.create(league=self.league, name="試合なしチーム")
        self.player(lonely, "孤独な投手", 1, "投手", throws="右", from_year=date.today().year)
        response = self.client.get(self.url(lonely))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["analysis"].year, date.today().year)
        self.assertContains(response, "孤独な投手")

    def test_age_is_counted_on_april_first(self):
        analysis = self.analysis(year=2026)
        ace = self.find(analysis.pitchers, "左腕エース")
        shortstop = self.find(analysis.fielders, "遊撃手")
        self.assertEqual((ace.age, shortstop.age), (28, 25))
        bands = {row.band: row for row in analysis.age_rows}
        self.assertEqual(bands["28歳"].pitchers, 1)
        self.assertEqual(bands["25歳"].fielders, 1)
        self.assertEqual(bands["不明"].pitchers, 1)

    def test_query_count_does_not_grow_with_roster(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self.analysis(year=2026)
            return len(captured)

        before = count()
        for number in range(40, 70):
            player_id = self.player(
                self.team, f"控え{number}", number, "投手" if number % 2 else "外野手", throws="右", bats="左"
            )
            self.game(self.team, self.rival, day=number % 28 + 1, pitching=[(player_id, 2)])
        self.assertEqual(count(), before)
        self.assertLessEqual(before, 6)


class AnalysisIndexTest(AnalysisCase):
    def test_redirects_to_first_team(self):
        first = build_team_analysis_service().first_team_id()
        response = self.client.get(reverse("analysis_index"))
        self.assertRedirects(response, reverse("team_analysis", args=[first]), fetch_redirect_response=False)

    def test_team_and_year_choice_is_carried_over(self):
        response = self.client.get(reverse("analysis_index"), {"team": self.rival.id, "year": "2025"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('team_analysis', args=[self.rival.id])}?year=2025")

    def test_unknown_team_choice_falls_back_to_first_team(self):
        first = build_team_analysis_service().first_team_id()
        response = self.client.get(reverse("analysis_index"), {"team": "99999"})
        self.assertEqual(response["Location"], reverse("team_analysis", args=[first]))

    def test_no_teams_shows_a_notice(self):
        orm_models.Team.objects.all().delete()
        response = self.client.get(reverse("analysis_index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "チームがまだ登録されていない")


class AnalysisLinksTest(TestCase):
    def setUp(self):
        league = orm_models.League.objects.create(name="リーグ")
        self.team = orm_models.Team.objects.create(league=league, name="チーム")

    def test_header_links_to_analysis_index(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f'href="{reverse("analysis_index")}"')

    def test_team_page_links_to_its_analysis(self):
        response = self.client.get(reverse("player_list", args=[self.team.id]))
        self.assertContains(response, f'href="{reverse("team_analysis", args=[self.team.id])}"')
