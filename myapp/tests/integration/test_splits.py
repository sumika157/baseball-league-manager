"""対戦成績と月別成績。試合から集計する横断的な表。"""

from datetime import date

from django.urls import reverse

from myapp.domain.entities import Game
from myapp.domain.value_objects import (
    BattingLine,
    Season,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
)

from ..helpers import (
    play_game,
)
from .base import BaseCase


class MatchupViewTest(BaseCase):
    """対戦成績（フェーズ4）。リーグ画面に、順位表と同じ並びで並べる。"""

    def setUp(self):
        super().setUp()
        self.third = orm_models.Team.objects.create(league=self.league, name="第三チーム")
        # テストチームは相手チームに2勝0敗、第三チームに0勝1敗
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        play_game(self.team, self.rival, home_score=2, away_score=1, day=2)
        play_game(self.third, self.team, home_score=6, away_score=0, day=3)
        self.url = reverse("league_detail", args=[self.league.id])

    def _table(self):
        return self.service.get_league_detail(self.league.id).matchups

    def test_rows_and_columns_share_the_order(self):
        table = self._table()

        self.assertEqual([c.team_id for c in table.columns], [r.team_id for r in table.rows])

    def test_record_against_each_opponent(self):
        table = self._table()
        row = next(r for r in table.rows if r.team_id == self.team.id)
        cells = {c.opponent_id: c.label for c in row.cells}

        self.assertEqual(cells[self.rival.id], "2-0-0")
        self.assertEqual(cells[self.third.id], "0-1-0")
        self.assertEqual(row.total_label, "2-1-0")

    def test_own_column_is_blank(self):
        table = self._table()
        row = next(r for r in table.rows if r.team_id == self.team.id)
        own = next(c for c in row.cells if c.is_self)

        self.assertIsNone(own.opponent_id)
        self.assertEqual(own.label, "—")

    def test_winning_and_losing_are_marked(self):
        table = self._table()
        row = next(r for r in table.rows if r.team_id == self.team.id)
        cells = {c.opponent_id: c for c in row.cells}

        self.assertTrue(cells[self.rival.id].is_winning)
        self.assertTrue(cells[self.third.id].is_losing)

    def test_page_shows_the_table(self):
        response = self.client.get(self.url)

        self.assertContains(response, "対戦成績")
        self.assertContains(response, "2-0-0")

    def test_table_follows_the_selected_season(self):
        play_game(self.team, self.rival, year=2025, home_score=0, away_score=9, day=1)

        table = self.service.get_league_detail(self.league.id, 2025).matchups
        row = next(r for r in table.rows if r.team_id == self.team.id)

        self.assertEqual(row.total_label, "0-1-0")

    def test_league_without_games_has_no_table(self):
        empty = orm_models.League.objects.create(name="無試合リーグ")
        orm_models.Team.objects.create(league=empty, name="新チーム")

        self.assertIsNone(self.service.get_league_detail(empty.id).matchups)


class MonthlySplitViewTest(BaseCase):
    """月別成績（フェーズ4）。通算値では見えない調子の波を出す。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self._play(month=4, day=1, at_bats=4, singles=1)
        self._play(month=4, day=2, at_bats=4, singles=1)
        self._play(month=5, day=1, at_bats=4, singles=3)
        self.url = reverse("player_detail", args=[self.team.id, self.player.id])

    def _play(self, *, month, day, **line):
        game = Game(
            season=Season(2026),
            played_on=date(2026, month, day),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=1,
            away_score=0,
        )
        game.record_batting(self.player.id, BattingLine(**line))
        DjangoGameRepository().save(game)

    def _months(self, player_id=None):
        profile = self.service.get_player_profile(self.team.id, player_id or self.player.id)
        return profile.months

    def test_grouped_by_month_oldest_first(self):
        self.assertEqual([m.label for m in self._months()], ["2026年4月", "2026年5月"])

    def test_rate_comes_from_the_monthly_total(self):
        april, may = self._months()

        self.assertEqual(april.appearances, 2)
        self.assertAlmostEqual(april.batting_average, 0.25)
        self.assertAlmostEqual(may.batting_average, 0.75)

    def test_months_without_appearance_are_omitted(self):
        play_game(self.team, self.rival, year=2026, day=20)

        self.assertEqual(len(self._months()), 2)

    def test_player_without_games_has_no_months(self):
        self.assertEqual(self._months(self.pitcher.id), [])

    def test_page_shows_the_table(self):
        response = self.client.get(self.url)

        self.assertContains(response, "月別成績")
        self.assertContains(response, "2026年4月")

    def test_page_of_a_player_without_games_omits_the_table(self):
        response = self.client.get(reverse("player_detail", args=[self.team.id, self.pitcher.id]))

        self.assertNotContains(response, "月別成績")


class YearlySplitViewTest(BaseCase):
    """年度別成績。上の帯は率と補正指標だけなので、実数はこの表が出し場所。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.url = reverse("player_detail", args=[self.team.id, self.player.id])

    def _play(self, *, year=2026, month=4, day=1, **line):
        play_game(
            self.team,
            self.rival,
            year=year,
            month=month,
            day=day,
            batting={self.player.id: BattingLine(**line)},
        )

    def _years(self, player_id=None):
        return self.service.get_player_profile(self.team.id, player_id or self.player.id).years

    def test_grouped_by_year_oldest_first(self):
        self._play(year=2026, at_bats=4, singles=3)
        self._play(year=2025, at_bats=4, singles=1)

        self.assertEqual([y.label for y in self._years()], ["2025年", "2026年"])

    def test_months_of_the_same_year_are_merged(self):
        self._play(month=4, at_bats=4, singles=1)
        self._play(month=5, at_bats=4, singles=3)

        years = self._years()

        self.assertEqual(len(years), 1)
        self.assertEqual(years[0].appearances, 2)
        self.assertAlmostEqual(years[0].batting_average, 0.5)  # 4/8

    def test_shows_the_counts_that_the_summary_strip_omits(self):
        """打席・安打・二塁打などの実数は個人ページのどこにも出ていなかった。"""
        self._play(at_bats=4, singles=1, doubles=1, runs_batted_in=2, walks=1)

        year = self._years()[0]

        self.assertEqual((year.at_bats, year.hits, year.doubles), (4, 2, 1))
        self.assertEqual((year.runs_batted_in, year.walks), (2, 1))
        self.assertEqual(year.plate_appearances, 5)  # 打数4 ＋ 四球1

    def test_page_shows_the_table(self):
        self._play(at_bats=4, singles=1)

        response = self.client.get(self.url)

        self.assertContains(response, "年度別成績")
        self.assertContains(response, "2026年")

    def test_career_total_row_is_omitted_for_a_single_year(self):
        """年度も在籍も1つなら、通算行は年度行と同じ値になるので出さない。"""
        self._play(at_bats=4, singles=1)

        self.assertNotContains(self.client.get(self.url), "通算")

    def test_career_total_row_appears_across_years(self):
        self._play(year=2025, at_bats=4, singles=1)
        self._play(year=2026, at_bats=4, singles=3)

        self.assertContains(self.client.get(self.url), "通算")

    def test_career_total_row_holds_the_career_totals(self):
        """通算行は年度行と同じ列を使う。項目名がずれると空欄になって気づけない。"""
        self._play(year=2025, at_bats=4, singles=1)
        self._play(year=2026, at_bats=4, singles=3)

        detail = self.service.get_player_profile(self.team.id, self.player.id).detail

        self.assertEqual((detail.at_bats, detail.hits, detail.plate_appearances), (8, 4, 8))
        self.assertContains(self.client.get(self.url), "0.500")  # 通算打率 4/8

    def test_player_without_games_has_no_years(self):
        self.assertEqual(self._years(), [])
        self.assertNotContains(self.client.get(self.url), "年度別成績")


class PlayerGameMonthTabTest(BaseCase):
    """試合ごとの成績は月で切り替える。1シーズン140試合を1つの表に並べない。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self._play(month=4, day=1)
        self._play(month=4, day=2)
        self._play(month=5, day=1)
        self.url = reverse("player_detail", args=[self.team.id, self.player.id])

    def _play(self, *, month, day):
        play_game(
            self.team,
            self.rival,
            month=month,
            day=day,
            batting={self.player.id: BattingLine(at_bats=4, singles=1)},
        )

    def _profile(self, month=None):
        return self.service.get_player_profile(self.team.id, self.player.id, month=month)

    def test_latest_month_is_selected_by_default(self):
        profile = self._profile()

        self.assertEqual(profile.selected_month, "2026-05")
        self.assertEqual([r.played_on.month for r in profile.games], [5])

    def test_selected_month_shows_only_that_month_newest_first(self):
        profile = self._profile("2026-04")

        self.assertEqual([r.played_on.day for r in profile.games], [2, 1])

    def test_unknown_month_falls_back_to_the_latest(self):
        """不正な指定はエラーにせず既定に落とす（並べ替えのキーと同じ扱い）。"""
        self.assertEqual(self._profile("2026-99").selected_month, "2026-05")
        self.assertEqual(self._profile("なにか").selected_month, "2026-05")

    def test_appearances_counts_every_month(self):
        """出場試合数は選んだ月ではなく全期間の数。"""
        self.assertEqual(self._profile("2026-04").appearances, 3)

    def test_page_has_a_tab_per_month(self):
        body = self.client.get(self.url).content.decode()

        self.assertIn("?month=2026-04", body)
        self.assertIn("?month=2026-05", body)

    def test_page_shows_only_the_selected_month(self):
        response = self.client.get(self.url, {"month": "2026-04"})

        self.assertContains(response, "2026/04/02")
        self.assertNotContains(response, "2026/05/01")


class TeamMonthlySplitViewTest(BaseCase):
    """チームの月別成績（フェーズ4）。個人の月別成績と対になる推移。"""

    def setUp(self):
        super().setUp()
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.url = reverse("player_list", args=[self.team.id])

    def test_grouped_by_month_oldest_first(self):
        play_game(self.team, self.rival, month=5, day=1)
        play_game(self.team, self.rival, month=4, day=1)

        rows = self.service.list_team_monthly_splits(self.team.id)

        self.assertEqual([r.label for r in rows], ["2026年4月", "2026年5月"])

    def test_record_and_rate_per_month(self):
        play_game(
            self.team,
            self.rival,
            month=4,
            day=1,
            home_score=5,
            away_score=3,
            batting={self.batter.id: BattingLine(at_bats=4, singles=1)},
        )
        play_game(
            self.team,
            self.rival,
            month=4,
            day=2,
            home_score=1,
            away_score=2,
            batting={self.batter.id: BattingLine(at_bats=4, singles=1)},
        )

        april = self.service.list_team_monthly_splits(self.team.id)[0]

        self.assertEqual(april.games_played, 2)
        self.assertEqual(april.record_label, "1-1-0")
        self.assertAlmostEqual(april.batting_average, 0.25)  # 2/8

    def test_the_opponents_lines_are_not_counted(self):
        """試合の明細には相手の選手も入る。自チームの分だけを合計する。"""
        theirs = self.service.register_player(self.rival.id, "相手", 1, "内野手")
        play_game(
            self.team,
            self.rival,
            month=4,
            batting={
                self.batter.id: BattingLine(at_bats=4, singles=1),
                theirs.id: BattingLine(at_bats=4, singles=4),
            },
        )

        april = self.service.list_team_monthly_splits(self.team.id)[0]

        self.assertAlmostEqual(april.batting_average, 0.25)

    def test_page_shows_the_table(self):
        play_game(self.team, self.rival, month=4)

        response = self.client.get(self.url)

        self.assertContains(response, "月別成績")
        self.assertContains(response, "2026年4月")

    def test_team_without_games_has_no_months(self):
        self.assertEqual(self.service.list_team_monthly_splits(self.team.id), [])
        self.assertNotContains(self.client.get(self.url), "月別成績")
