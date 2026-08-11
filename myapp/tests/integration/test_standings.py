"""順位表。年別の切り替えと、リーグ単位の絞り込み。"""

from django.urls import reverse

from myapp.infrastructure import orm_models

from ..helpers import (
    play_game,
)
from .base import BaseCase


class StandingsTest(BaseCase):
    def test_record_is_aggregated_from_games(self):
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        play_game(self.team, self.rival, home_score=2, away_score=2, day=2)
        play_game(self.team, self.rival, home_score=1, away_score=4, day=3)

        rows = {r.team_name: r for r in self.service.get_standings(2026).rows}

        self.assertEqual(rows["テストチーム"].wins, 1)
        self.assertEqual(rows["テストチーム"].losses, 1)
        self.assertEqual(rows["テストチーム"].ties, 1)
        self.assertEqual(rows["テストチーム"].games_played, 3)

    def test_rank_is_derived_from_winning_percentage(self):
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        play_game(self.team, self.rival, home_score=6, away_score=2, day=2)
        play_game(self.team, self.rival, home_score=1, away_score=4, day=3)

        rows = self.service.get_standings(2026).rows

        self.assertEqual([r.team_name for r in rows], ["テストチーム", "相手チーム"])
        self.assertEqual([r.rank for r in rows], [1, 2])
        self.assertEqual(rows[0].games_behind, "—")

    def test_teams_without_games_are_excluded(self):
        other = orm_models.Team.objects.create(league=self.league, name="未実施チーム")
        play_game(self.team, self.rival)

        names = [r.team_name for r in self.service.get_standings(2026).rows]

        self.assertNotIn(other.name, names)

    def test_defaults_to_the_latest_season(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        board = self.service.get_standings()

        self.assertEqual(board.year, 2026)
        self.assertEqual(board.available_years, [2026, 2025])

    def test_page_renders(self):
        play_game(self.team, self.rival, home_score=5, away_score=3)
        response = self.client.get(reverse("standings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テストチーム")

    def test_page_by_year(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        response = self.client.get(reverse("standings_by_year", args=[2025]))
        self.assertContains(response, "2025年")

    def test_page_without_any_game(self):
        response = self.client.get(reverse("standings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "試合がまだ登録されていません")


class LeagueScopedStandingsTest(BaseCase):
    """順位はリーグの中で争われる（フェーズ2）。"""

    def setUp(self):
        super().setUp()
        self.other_league = orm_models.League.objects.create(name="別リーグ")
        self.x = orm_models.Team.objects.create(league=self.other_league, name="Xチーム")
        self.y = orm_models.Team.objects.create(league=self.other_league, name="Yチーム")

        # テストリーグ側は僅差、別リーグ側は圧勝
        play_game(self.team, self.rival, home_score=2, away_score=1, day=1)
        play_game(self.x, self.y, home_score=10, away_score=0, day=1)

    def test_standings_are_split_by_league(self):
        board = self.service.get_standings(2026)

        names = {lg.league_name: [r.team_name for r in lg.rows] for lg in board.leagues}
        self.assertEqual(len(board.leagues), 2)
        self.assertEqual(names["テストリーグ"], ["テストチーム", "相手チーム"])
        self.assertEqual(names["別リーグ"], ["Xチーム", "Yチーム"])

    def test_other_league_teams_do_not_share_the_rank(self):
        """別リーグの1位どうしが同じ表で2位に落ちたりしないこと。"""
        board = self.service.get_standings(2026)

        leaders = [lg.rows[0] for lg in board.leagues]
        self.assertTrue(all(row.rank == 1 for row in leaders))

    def test_leagues_without_games_are_omitted(self):
        orm_models.League.objects.create(name="未実施リーグ")
        board = self.service.get_standings(2026)

        self.assertNotIn("未実施リーグ", [lg.league_name for lg in board.leagues])

    def test_page_shows_each_league_heading(self):
        response = self.client.get(reverse("standings"))

        self.assertContains(response, "テストリーグ")
        self.assertContains(response, "別リーグ")
